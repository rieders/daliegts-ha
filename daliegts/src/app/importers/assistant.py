"""Import-Assistent (v0.29): beliebige Tabellen (CSV/JSON) sowie Part-DB-Dumps und InvenTree-Datenbanken mit frei einstellbarer Zuordnung.

Ablauf: Quelle → flache Tabelle (Spalten + Zeilen) → Zuordnung Spalte→Feld → Wert-Ersetzungen → Regeln (wenn … dann …) → Anlegen/Abgleichen.
Die Konfiguration ist ein JSON-Objekt und lässt sich als Profil speichern:

{"columns": {"<Quellspalte>": "<Zielfeld>"}, "maps": {"category": {"Resistors": "Passive/Widerstände"}},
 "rules": [{"field": "category", "op": "contains", "value": "LED", "action": "set", "target": "footprint", "text": "5mm"}],
 "dup": {"by": "name_or_mpn", "fields": "fill", "stock": "add"}, "options": {"create_locations": true, "location_root": "", "review": false}}
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Item, ItemCode, Location
from ..services import stock as stocksvc
from ..services.codes import add_item_code
from ..services.items import (create_item, ensure_category_path, ensure_footprint, ensure_manufacturer, reindex, set_parameter)
from ..services.locations import create_location, guess_type
from .common import Report

MAX_ROWS = 100_000
SAMPLE_VALUES = 3
TARGETS = [  # (Schlüssel, Beschriftung)
    ("", "– ignorieren –"), ("name", "Name / Bezeichnung"), ("description", "Beschreibung"), ("category", "Kategorie (Pfad)"),
    ("location", "Lagerort (Pfad)"), ("quantity", "Menge"), ("min_stock", "Mindestbestand"), ("manufacturer", "Hersteller"),
    ("mpn", "Typ-/Herstellernummer (MPN)"), ("footprint", "Gehäuse / Bauform"), ("tags", "Schlagwörter"), ("unit", "Einheit"),
    ("notes", "Notizen"), ("code", "Fremdcode (EAN, IPN, alte Nummer)"), ("param:", "Parameter (Spaltenname als Parametername)"),
]
VALUE_FIELDS = ["category", "location", "footprint", "manufacturer", "unit", "tags"]
SCALARS = ["name", "description", "notes", "manufacturer", "mpn", "footprint", "unit", "code", "category", "location", "quantity", "min_stock", "tags"]
OPS = {"contains": "enthält", "equals": "ist gleich", "startswith": "beginnt mit", "endswith": "endet mit", "regex": "passt auf (Regex)",
       "empty": "ist leer", "notempty": "ist nicht leer", "gt": "ist größer als", "lt": "ist kleiner als"}
ACTIONS = {"set": "setze Feld auf", "replace": "ersetze im Feld", "prefix": "stelle voran", "suffix": "hänge an", "tag": "füge Schlagwort hinzu", "skip": "Zeile überspringen"}

_SYN = {
    "name": "name bezeichnung bauteil part teil artikel artikelname partname titel",
    "description": "description beschreibung",
    "category": "category kategorie categorypath kategoriepfad gruppe",
    "location": "storagelocation storelocation lagerort location standort lager defaultlocation storagelocationfullpath",
    "quantity": "amount quantity menge bestand instock stock totalstock anzahl qty stuck",
    "min_stock": "minamount minimumstock mindestbestand mininstock minstock",
    "manufacturer": "manufacturer hersteller",
    "mpn": "mpn manufacturerpartnumber manufacturerpartno herstellernr herstellernummer typ typnummer manufacturerpn",
    "footprint": "footprint gehause gehaeuse package bauform",
    "tags": "tags keywords schlagworter stichworte schlagwoerter",
    "unit": "unit units einheit",
    "notes": "notes comment comments kommentar notizen bemerkung",
    "code": "ipn ean barcode gtin code upc",
}


def norm_key(s: str) -> str:
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


_SYN_INDEX = {}
for _t, _words in _SYN.items():
    for _w in _words.split():
        _SYN_INDEX.setdefault(norm_key(_w), _t)


def guess_mapping(columns: list[str]) -> dict[str, str]:
    out, used = {}, set()
    for c in columns:
        k = norm_key(c)
        if k.startswith("param") and len(k) > 5:
            out[c] = "param:" + re.sub(r"^\s*param(eter)?\s*[:\-]?\s*", "", c, flags=re.I).strip()
            continue
        t = _SYN_INDEX.get(k, "")
        if t and t not in used or t in ("description", "notes", "tags", "code"):
            out[c] = t
            used.add(t)
    return out


# ------------------------------------------------------------------ Quellen lesen
@dataclass
class Table:
    columns: list[str]
    rows: list[list[str]]
    kind: str = "tabelle"
    notes: list[str] = field(default_factory=list)

    def dicts(self):
        for r in self.rows:
            yield dict(zip(self.columns, r, strict=False))


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def read_table(path: Path) -> Table:
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if suffix == ".sql":
        from .sqldump import load_dump

        return flatten_partdb(load_dump(path))
    if suffix in (".sqlite", ".sqlite3", ".db") or raw[:15] == b"SQLite format 3":
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "part_part" in names:
            return flatten_inventree(con)
        if "parts" in names:
            return flatten_partdb(con)
        raise ValueError("Diese Datenbank kenne ich nicht (erwartet: InvenTree oder Part-DB).")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if suffix == ".json" or text.lstrip()[:1] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        rows = [r for r in data if isinstance(r, dict)]
        cols: list[str] = []
        for r in rows[:2000]:
            for k in r:
                if k not in cols:
                    cols.append(str(k))
        return Table(cols, [[_s(r.get(c)) for c in cols] for r in rows[:MAX_ROWS]], "JSON")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    allrows = [r for r in reader if any(c.strip() for c in r)]
    if not allrows:
        raise ValueError("Die Datei ist leer.")
    head = [h.strip() or f"Spalte {i + 1}" for i, h in enumerate(allrows[0])]
    seen: dict[str, int] = {}
    for i, h in enumerate(head):  # doppelte Spaltennamen unterscheiden
        seen[h] = seen.get(h, 0) + 1
        if seen[h] > 1:
            head[i] = f"{h} ({seen[h]})"
    body = [(r + [""] * len(head))[:len(head)] for r in allrows[1:MAX_ROWS + 1]]
    return Table(head, [[c.strip() for c in r] for r in body], "CSV")


def _path(rows: dict, rid, sep=" / ") -> str:
    parts, seen = [], set()
    while rid not in (None, "", 0) and rid in rows and rid not in seen:
        seen.add(rid)
        parts.append(str(rows[rid]["name"]))
        rid = rows[rid]["parent_id"] if "parent_id" in rows[rid].keys() else None
    return sep.join(reversed(parts))


def _tab(con, name):
    try:
        return [dict(r) for r in con.execute(f'SELECT * FROM "{name}"')]
    except sqlite3.Error:
        return []


def flatten_partdb(con: sqlite3.Connection) -> Table:
    con.row_factory = sqlite3.Row
    idx = lambda name: {r["id"]: r for r in con.execute(f'SELECT * FROM "{name}"')} if _tab(con, name) else {}  # noqa: E731
    cats, locs, fps, mans = idx("categories"), idx("storelocations"), idx("footprints"), idx("manufacturers")
    cols = ["Name", "Beschreibung", "Kategorie", "Lagerort", "Menge", "Mindestbestand", "Hersteller", "Gehäuse", "Notizen", "Part-DB-ID"]
    rows = []
    for p in con.execute("SELECT * FROM parts"):
        k = p.keys()
        g = lambda c, p=p, k=k: p[c] if c in k else None  # noqa: E731
        rows.append([_s(g("name")), _s(g("description")), _path(cats, g("id_category")), _path(locs, g("id_storelocation")), _s(g("instock")),
                     _s(g("mininstock")), _s(mans[g("id_manufacturer")]["name"]) if g("id_manufacturer") in mans else "",
                     _path(fps, g("id_footprint")), _s(g("comment")), _s(g("id"))])
    return Table(cols, rows[:MAX_ROWS], "Part-DB 0.x", ["Lagerorte, Kategorien und Gehäuse wurden zu Pfaden zusammengesetzt; Bestand je Bauteil."])


def flatten_inventree(con: sqlite3.Connection) -> Table:
    con.row_factory = sqlite3.Row
    parts = {r["id"]: r for r in con.execute("SELECT * FROM part_part")}
    cats = {r["id"]: r for r in con.execute("SELECT * FROM part_partcategory")} if _tab(con, "part_partcategory") else {}
    locs = {r["id"]: r for r in con.execute("SELECT * FROM stock_stocklocation")} if _tab(con, "stock_stocklocation") else {}
    companies = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM company_company")} if _tab(con, "company_company") else {}
    mfr: dict = {}
    for r in _tab(con, "company_manufacturerpart"):
        mfr.setdefault(r.get("part_id"), r)
    tmpl = {r["id"]: r["name"] for r in _tab(con, "part_partparametertemplate")}
    params: dict = {}
    for r in _tab(con, "part_partparameter"):
        params.setdefault(r.get("part_id"), {})[tmpl.get(r.get("template_id"), f"Parameter {r.get('template_id')}")] = r.get("data")
    pnames = sorted({n for d in params.values() for n in d})
    stock: dict = {}
    for r in _tab(con, "stock_stockitem"):
        stock.setdefault(r.get("part_id"), []).append(r)
    cols = ["Name", "IPN", "Beschreibung", "Kategorie", "Schlüsselwörter", "Einheit", "Mindestbestand", "Notizen", "Hersteller", "MPN",
            "Lagerort", "Menge", "Charge", "Seriennummer", "InvenTree-ID"] + [f"Param: {n}" for n in pnames]
    rows = []
    for pid, p in parts.items():
        k = p.keys()
        g = lambda c, p=p, k=k: p[c] if c in k else None  # noqa: E731
        m = mfr.get(pid)
        base = [_s(g("name")), _s(g("IPN")), _s(g("description")), _path(cats, g("category_id")), _s(g("keywords")), _s(g("units")),
                _s(g("minimum_stock")), _s(g("notes")), _s(companies.get(m.get("manufacturer_id"))) if m else "", _s(m.get("MPN")) if m else ""]
        tail = [_s(params.get(pid, {}).get(n)) for n in pnames]
        items = stock.get(pid) or [None]
        for s in items:
            if s is None:
                rows.append(base + ["", "", "", "", _s(pid)] + tail)
            else:
                rows.append(base + [_path(locs, s.get("location_id")), _s(s.get("quantity")), _s(s.get("batch")), _s(s.get("serial")), _s(pid)] + tail)
    return Table(cols, rows[:MAX_ROWS], "InvenTree", ["Eine Zeile je Lagerartikel; Teile ohne Bestand stehen mit leerer Menge da."])


# ------------------------------------------------------------------ Umwandeln
def split_path(s: str) -> str:
    parts = [p.strip() for p in re.split(r"\s*(?:->|=>|»|›|>|/|\\)\s*", s or "") if p.strip()]
    return " / ".join(parts)


def num(s: str) -> float | None:
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    m = re.match(r"^-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _fmt(text: str, f: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(f.get(m.group(1), "")), text or "")


def _cond(rule: dict, f: dict) -> bool:
    v = str(f.get(rule.get("field", ""), "") or "")
    val = str(rule.get("value", ""))
    op = rule.get("op", "contains")
    lo, vlo = v.lower(), val.lower()
    if op == "contains":
        return vlo in lo
    if op == "equals":
        return lo == vlo
    if op == "startswith":
        return lo.startswith(vlo)
    if op == "endswith":
        return lo.endswith(vlo)
    if op == "regex":
        try:
            return re.search(val, v, re.I) is not None
        except re.error:
            return False
    if op == "empty":
        return not v.strip()
    if op == "notempty":
        return bool(v.strip())
    if op in ("gt", "lt"):
        a, b = num(v), num(val)
        return a is not None and b is not None and (a > b if op == "gt" else a < b)
    return False


def transform(row: dict, cfg: dict) -> tuple[dict, bool]:
    """Wendet Spaltenzuordnung, Wert-Ersetzungen und Regeln an. Gibt (Felder, überspringen?) zurück."""
    f: dict[str, str] = {}
    params: dict[str, str] = {}
    for col, target in (cfg.get("columns") or {}).items():
        val = _s(row.get(col))
        if not target or not val:
            continue
        if target.startswith("param:"):
            params[target[6:].strip() or col] = val
        elif target in ("description", "notes"):
            f[target] = (f.get(target, "") + " " + val).strip() if target == "description" else (f.get(target, "") + "\n" + val).strip()
        elif target == "tags":
            f[target] = f[target] + ", " + val if f.get(target) else val
        elif target == "code":
            f[target] = f[target] + ";" + val if f.get(target) else val
        elif not f.get(target):
            f[target] = val
    for k in ("category", "location"):
        if f.get(k):
            f[k] = split_path(f[k])
    maps = cfg.get("maps") or {}
    for k, m in maps.items():
        if k in f and f[k] in m and m[f[k]] != "":
            f[k] = m[f[k]]
        elif k == "tags" and f.get("tags"):
            f["tags"] = ", ".join(m.get(t.strip(), t.strip()) for t in f["tags"].split(","))
    for k in ("category", "location"):  # nach Ersetzung erneut normalisieren
        if f.get(k):
            f[k] = split_path(f[k])
    skip = False
    for rule in cfg.get("rules") or []:
        if not _cond(rule, f):
            continue
        act, tgt, text = rule.get("action"), rule.get("target") or rule.get("field", ""), _fmt(str(rule.get("text", "")), f)
        if act == "skip":
            skip = True
        elif act == "set":
            if tgt.startswith("param:"):
                params[tgt[6:]] = text
            else:
                f[tgt] = split_path(text) if tgt in ("category", "location") else text
        elif act == "replace":
            find = str(rule.get("find", ""))
            cur = f.get(tgt, "")
            try:
                f[tgt] = re.sub(find, text, cur, flags=re.I) if rule.get("regex") else re.sub(re.escape(find), text, cur, flags=re.I)
            except re.error:
                pass
        elif act == "prefix":
            f[tgt] = text + f.get(tgt, "")
        elif act == "suffix":
            f[tgt] = f.get(tgt, "") + text
        elif act == "tag":
            f["tags"] = (f.get("tags", "") + ", " + text).strip(", ")
    f["_params"] = json.dumps(params, ensure_ascii=False) if params else ""
    return f, skip


def distinct_values(table: Table, cfg: dict, limit: int = 300) -> dict[str, list[tuple[str, int]]]:
    """Werte je Feld VOR den Ersetzungen, damit man sie einzeln umbenennen kann."""
    c2 = dict(cfg, maps={}, rules=[])
    out: dict[str, dict[str, int]] = {k: {} for k in VALUE_FIELDS}
    for row in table.dicts():
        f, _ = transform(row, c2)
        for k in VALUE_FIELDS:
            if k == "tags":
                vals = [t.strip() for t in f.get(k, "").split(",") if t.strip()]
            else:
                vals = [f[k]] if f.get(k) else []
            for v in vals:
                out[k][v] = out[k].get(v, 0) + 1
    return {k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[:limit] for k, v in out.items() if v}


# ------------------------------------------------------------------ Anlegen
@dataclass
class RowResult:
    no: int
    name: str
    action: str
    note: str = ""


def ensure_location_path(db: Session, path: str, create: bool, cache: dict, root: str = "") -> int | None:
    full = split_path((root + " / " if root else "") + path) if (path or root) else ""
    if not full:
        return None
    if full in cache:
        return cache[full]
    parent = None
    for i, name in enumerate(full.split(" / ")):
        key = " / ".join(full.split(" / ")[:i + 1])
        if key in cache and cache[key] is not None:
            parent = cache[key]
            continue
        q = select(Location).where(func.lower(Location.name) == name.lower())
        q = q.where(Location.parent_id.is_(None)) if parent is None else q.where(Location.parent_id == parent)
        loc = db.scalars(q).first()
        if loc is None:
            if not create:
                cache[full] = None
                return None
            loc = create_location(db, name, parent, guess_type(name))
        cache[key] = parent = loc.id
    cache[full] = parent
    return parent


def _find_existing(db: Session, f: dict, by: str) -> Item | None:
    def by_name():
        return db.scalars(select(Item).where(func.lower(Item.name) == f["name"].lower())).first() if f.get("name") else None

    def by_mpn():
        return db.scalars(select(Item).where(func.lower(Item.mpn) == f["mpn"].lower())).first() if f.get("mpn") else None

    def by_code():
        for c in re.split(r"[;\n]+", f.get("code", "")):
            c = c.strip()
            if c:
                row = db.scalar(select(ItemCode).where(func.lower(ItemCode.code) == c.lower()))
                if row:
                    return db.get(Item, row.item_id)
        return None
    order = {"name": [by_name], "mpn": [by_mpn], "code": [by_code], "name_or_mpn": [by_mpn, by_name], "any": [by_code, by_mpn, by_name], "never": []}
    for fn in order.get(by, [by_mpn, by_name]):
        hit = fn()
        if hit:
            return hit
    return None


def run(db: Session, table: Table, cfg: dict, user_id: int | None = None, dry_run: bool = True, preview_limit: int = 300) -> tuple[Report, list[RowResult]]:
    report = Report("Import-Assistent", dry_run)
    results: list[RowResult] = []
    dup = cfg.get("dup") or {}
    by, fields_mode, stock_mode = dup.get("by", "name_or_mpn"), dup.get("fields", "fill"), dup.get("stock", "add")
    opt = cfg.get("options") or {}
    create_locs, loc_root, review = bool(opt.get("create_locations", True)), str(opt.get("location_root", "")), bool(opt.get("review", False))
    loc_cache: dict = {}
    seen_in_file: dict[str, Item] = {}
    touched: set[int] = set()
    for n, row in enumerate(table.dicts(), start=1):
        f, skip = transform(row, cfg)
        label = f.get("name") or f"Zeile {n}"

        def add(action, note="", n=n, label=label):
            report.inc(action)
            if len(results) < preview_limit or action == "Fehler":
                results.append(RowResult(n, label, action, note))
        if skip:
            add("Übersprungen (Regel)")
            continue
        if not f.get("name"):
            add("Fehler", "kein Name – Zeile ausgelassen")
            continue
        qty = num(f.get("quantity", ""))
        if f.get("quantity") and qty is None:
            report.warnings.append(f"Zeile {n} „{label}“: Menge „{f['quantity']}“ nicht lesbar.")
        qty = qty or 0.0
        params = json.loads(f["_params"]) if f.get("_params") else {}
        existing = seen_in_file.get(f["name"].lower()) or _find_existing(db, f, by)
        loc_id = ensure_location_path(db, f.get("location", ""), create_locs, loc_cache, loc_root) if (f.get("location") or loc_root and qty) else None
        if f.get("location") and loc_id is None:
            report.warnings.append(f"Zeile {n} „{label}“: Lagerort „{f['location']}“ gibt es nicht (Neuanlage aus).")
        cat = ensure_category_path(db, f["category"], "/") if f.get("category") else None
        fp, man = ensure_footprint(db, f.get("footprint", "")), ensure_manufacturer(db, f.get("manufacturer", ""))
        mins = num(f.get("min_stock", ""))
        note = []
        if existing is None:
            item = create_item(db, f["name"], user_id, description=f.get("description", ""), notes=f.get("notes", ""), kind="bauteil",
                               category_id=cat.id if cat else None, footprint_id=fp.id if fp else None, manufacturer_id=man.id if man else None,
                               mpn=f.get("mpn", ""), tags=f.get("tags", ""), unit=f.get("unit") or "Stk", min_stock=mins or 0)
            action = "Neu angelegt"
            seen_in_file[f["name"].lower()] = item
        else:
            item = existing
            if fields_mode == "skip":
                action = "Vorhanden – unverändert"
            else:
                changed = []
                for attr, val in (("description", f.get("description")), ("notes", f.get("notes")), ("mpn", f.get("mpn")), ("unit", f.get("unit")),
                                  ("category_id", cat.id if cat else None), ("footprint_id", fp.id if fp else None),
                                  ("manufacturer_id", man.id if man else None), ("min_stock", mins)):
                    if val in (None, ""):
                        continue
                    cur = getattr(item, attr)
                    if (fields_mode == "overwrite" and cur != val) or (fields_mode == "fill" and cur in (None, "", 0)):
                        setattr(item, attr, val)
                        changed.append(attr)
                if f.get("tags"):
                    have = [t.strip() for t in (item.tags or "").split(",") if t.strip()]
                    new = [t.strip() for t in f["tags"].split(",") if t.strip() and t.strip() not in have]
                    if new:
                        item.tags = ", ".join(have + new)
                        changed.append("tags")
                action = "Aktualisiert" if changed else "Vorhanden – unverändert"
                if changed:
                    note.append(", ".join(changed))
        touched.add(item.id)
        for pname, pval in params.items():
            if fields_mode != "skip" or existing is None:
                set_parameter(db, item, pname[:80], pval[:100], "")
        for code in re.split(r"[;\n]+", f.get("code", "")):
            if code.strip():
                try:
                    from ..services.codes import check_free

                    check_free(db, code.strip(), "item", item.id)
                    add_item_code(db, item.id, code.strip(), "altsystem", "import")
                except ValueError as e:
                    report.warnings.append(f"Zeile {n} „{label}“: Code {code.strip()} nicht übernommen ({e}).")
        if qty > 0 and stock_mode != "ignore":
            if existing is not None and stock_mode == "set":
                lot = stocksvc.find_lot(db, item.id, loc_id)
                if lot is None:
                    lot = stocksvc.add_stock(db, item, loc_id, qty, user_id, note="Import")
                elif lot.quantity != qty:
                    stocksvc.count_stock(db, lot, qty, user_id, note="Import (Bestand gesetzt)")
            else:
                lot = stocksvc.add_stock(db, item, loc_id, qty, user_id, note="Import")
            if review:
                lot.needs_review = True
            note.append(f"Bestand {qty:g}" + (f" in {f['location']}" if f.get("location") else ""))
            report.inc("Bestände gebucht")
        add(action, "; ".join(note))
    db.flush()
    if dry_run:
        db.rollback()
    else:
        for iid in touched:
            reindex(db, db.get(Item, iid))
        db.commit()
    return report, results
