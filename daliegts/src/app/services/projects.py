"""Projekte / Baugruppen (v0.10): gespeicherte Stücklisten, z. B. „Lichtschranke“ – auch direkt aus KiCad.

- Import: KiCad-Schaltplan (.kicad_sch), KiCad-Netzliste (.xml/.net) oder Stücklisten-CSV (KiCad, Excel).
- Zuordnung zu Lagerartikeln: Lagerwerk-Code/MPN/LCSC-Nummer im Symbol, gemerkte Zuordnung, sonst Wert + Gehäuse
  („10k“ + „R_0805“ findet „MD-0805 10K“ und „10 kOhm SMD 0805“). Einmal von Hand zugeordnet = gemerkt.
- Bauen: für N Platinen Bedarf, Bestand und Fächer anzeigen und daraus einen Auftrag machen.
"""
from __future__ import annotations

import csv
import io
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, ItemCode, ItemParameter, Project, ProjectLine, Supplier, SupplierPart
from . import locations as locsvc
from .codes import add_item_code, next_code, resolve
from .params import parse_value


# ------------------------------------------------------------------ KiCad lesen
@dataclass
class BomRow:
    refs: list[str]
    value: str = ""
    footprint: str = ""
    mpn: str = ""
    lcsc: str = ""
    code: str = ""  # Lagerwerk-Artikelcode im Symbolfeld „Lagerwerk“
    dnp: bool = False
    qty: float = 0.0

    @property
    def quantity(self) -> float:
        return self.qty or float(len(self.refs)) or 1.0


FIELD_MPN = ("mpn", "manufacturer_part_number", "manufacturer part number", "hersteller-nr.", "hersteller-nr", "herstellernummer", "part number", "partnumber", "mfr. part #")
FIELD_LCSC = ("lcsc", "lcsc part", "lcsc part #", "jlcpcb part #", "lcsc#")
FIELD_CODE = ("lagerwerk", "daliegts", "artikel", "artikelcode", "lager")


def _pick(fields: dict[str, str], names: tuple[str, ...]) -> str:
    for n in names:
        v = fields.get(n, "").strip()
        if v and v not in ("~", "-"):
            return v
    return ""


def _sexp(text: str):
    """Minimaler S-Ausdruck-Parser für KiCad-Dateien."""
    tokens = re.findall(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+', text)
    stack: list[list] = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif t.startswith('"'):
            stack[-1].append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(t)
    return stack[0]


def _kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def parse_kicad_sch(text: str) -> list[BomRow]:
    root = _sexp(text)
    sch = next((n for n in root if isinstance(n, list) and n and n[0] == "kicad_sch"), root)
    by_ref: dict[str, BomRow] = {}
    for sym in _kids(sch, "symbol"):  # platzierte Symbole (Bibliothekssymbole stecken in lib_symbols)
        lib = next((c[1] for c in _kids(sym, "lib_id")), "")
        props = {c[1].lower(): (c[2] if len(c) > 2 else "") for c in _kids(sym, "property") if len(c) > 1}
        ref = props.get("reference", "")
        for inst in _kids(sym, "instances"):  # KiCad 7/8: tatsächliche Referenz je Projekt
            for proj in _kids(inst, "project"):
                for path in _kids(proj, "path"):
                    for r in _kids(path, "reference"):
                        if len(r) > 1 and not str(r[1]).endswith("?"):
                            ref = r[1]
        in_bom = next((c[1] for c in _kids(sym, "in_bom")), "yes")
        dnp = next((c[1] for c in _kids(sym, "dnp")), "no") == "yes"
        if not ref or ref.startswith("#") or lib.startswith("power:") or in_bom == "no":
            continue
        if ref in by_ref:  # mehrteilige Symbole (z. B. OPV Einheit A/B) nur einmal zählen
            continue
        by_ref[ref] = BomRow([ref], props.get("value", ""), props.get("footprint", ""), _pick(props, FIELD_MPN), _pick(props, FIELD_LCSC),
                             _pick(props, FIELD_CODE), dnp)
    return list(by_ref.values())


def parse_kicad_xml(text: str) -> list[BomRow]:
    root = ET.fromstring(text)
    rows = []
    for comp in root.iter("comp"):
        ref = comp.get("ref", "")
        fields = {(f.get("name") or "").lower(): (f.text or "") for f in comp.iter("field")}
        props = {(p.get("name") or "").lower(): (p.get("value") or "") for p in comp.iter("property")}
        fields.update({k: v for k, v in props.items() if k not in fields})
        if not ref or ref.startswith("#") or "exclude_from_bom" in props:
            continue
        rows.append(BomRow([ref], (comp.findtext("value") or "").strip(), (comp.findtext("footprint") or "").strip(),
                           _pick(fields, FIELD_MPN), _pick(fields, FIELD_LCSC), _pick(fields, FIELD_CODE), "dnp" in props))
    return rows


def parse_kicad_net(text: str) -> list[BomRow]:
    """KiCad-Netzliste im S-Ausdruck-Format (.net)."""
    rows = []
    for comps in _find_all(_sexp(text), "components"):
        for comp in _kids(comps, "comp"):
            get = {c[0]: (c[1] if len(c) > 1 else "") for c in comp if isinstance(c, list) and c}
            fields = {}
            for fl in _kids(comp, "fields"):
                for f in _kids(fl, "field"):
                    name = next((x[1] for x in _kids(f, "name")), "")
                    fields[str(name).lower()] = f[-1] if isinstance(f[-1], str) else ""
            props = {str(p[1][1]).lower(): (p[2][1] if len(p) > 2 and isinstance(p[2], list) else "") for p in _kids(comp, "property") if len(p) > 1 and isinstance(p[1], list)}
            ref = str(get.get("ref", ""))
            if not ref or ref.startswith("#") or "exclude_from_bom" in props:
                continue
            rows.append(BomRow([ref], str(get.get("value", "")), str(get.get("footprint", "")), _pick(fields, FIELD_MPN), _pick(fields, FIELD_LCSC),
                               _pick(fields, FIELD_CODE), "dnp" in props))
    return rows


def _find_all(node, name):
    out = []
    if isinstance(node, list):
        if node and node[0] == name:
            out.append(node)
        for c in node:
            out += _find_all(c, name)
    return out


def _split_refs(s: str) -> list[str]:
    return [r for r in re.split(r"[\s,;]+", s or "") if r]


def parse_bom_csv(text: str) -> list[BomRow]:
    text = text.lstrip("\ufeff")
    first = text.splitlines()[0] if text.strip() else ""
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for rec in csv.DictReader(io.StringIO(text), dialect=dialect):
        f = {(k or "").strip().strip('"').lower(): (v or "").strip() for k, v in rec.items() if k}
        refs = _split_refs(_pick(f, ("reference", "references", "referenz", "designator", "designators", "ref", "refs", "bauteil")))
        value = _pick(f, ("value", "wert", "comment", "name", "bezeichnung"))
        if not refs and not value:
            continue
        qraw = _pick(f, ("qty", "quantity", "menge", "anzahl", "stück", "qnty"))
        try:
            qty = float(qraw.replace(",", ".")) if qraw else 0.0
        except ValueError:
            qty = 0.0
        dnp = _pick(f, ("dnp", "do not populate", "nicht bestücken")).lower() in ("1", "yes", "ja", "x", "true", "dnp")
        rows.append(BomRow(refs, value, _pick(f, ("footprint", "package", "gehäuse", "bauform")), _pick(f, FIELD_MPN), _pick(f, FIELD_LCSC),
                           _pick(f, FIELD_CODE), dnp, qty))
    return rows


def parse_file(name: str, data: bytes) -> list[BomRow]:
    text = data.decode("utf-8-sig", errors="replace")
    low = name.lower()
    if low.endswith(".kicad_sch") or text.lstrip().startswith("(kicad_sch"):
        return parse_kicad_sch(text)
    if low.endswith(".xml") or text.lstrip().startswith("<?xml") or "<export" in text[:500]:
        return parse_kicad_xml(text)
    if low.endswith(".net") or text.lstrip().startswith("(export"):
        return parse_kicad_net(text)
    return parse_bom_csv(text)


def group_rows(rows: list[BomRow]) -> list[BomRow]:
    """Gleiche Bauteile (Wert + Gehäuse + MPN) zu einer Zeile mit Referenzliste zusammenfassen."""
    groups: dict[tuple, BomRow] = {}
    for r in rows:
        key = (r.value.strip().lower(), r.footprint.strip().lower(), r.mpn.strip().lower(), r.lcsc.strip().lower(), r.code.strip().lower(), r.dnp)
        g = groups.get(key)
        if g is None:
            groups[key] = BomRow(list(r.refs), r.value, r.footprint, r.mpn, r.lcsc, r.code, r.dnp, r.qty if r.qty and len(r.refs) != r.qty else 0.0)
        else:
            g.refs += r.refs
            if r.qty and len(r.refs) != r.qty:
                g.qty = (g.qty or len(g.refs) - len(r.refs)) + r.qty
    out = list(groups.values())
    for g in out:
        g.refs = sorted(set(g.refs), key=locsvc.natural_key)
    return sorted(out, key=lambda g: locsvc.natural_key(g.refs[0] if g.refs else g.value))


# ------------------------------------------------------------------ Zuordnung zu Lagerartikeln
PACKAGE_RE = re.compile(r"(?<![0-9])(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?![0-9])|"
                        r"\b(SOT-?23(?:-\d)?|SOT-?223|SOT-?89|SOD-?123|SOD-?323|SOIC-?\d+|SO-?\d+|TSSOP-?\d+|MSOP-?\d+|QFN-?\d+|LQFP-?\d+|TQFP-?\d+|"
                        r"DIP-?\d+|TO-?92|TO-?220|TO-?247|TO-?252|TO-?263|DPAK|D2PAK|SMA|SMB|SMC|MELF|MINIMELF|AXIAL|RADIAL)\b", re.I)


def package_of(text: str) -> str:
    """„Resistor_SMD:R_0805_2012Metric“ -> „0805“, „Package_TO_SOT_THT:TO-92_Inline“ -> „TO92“."""
    t = (text or "").split(":")[-1].replace("_", " ")
    if re.search(r"axial|din0\d{3}", t, re.I):
        return "AXIAL"
    m = PACKAGE_RE.search(t)
    if not m:
        return ""
    return (m.group(1) or m.group(2)).upper().replace("-", "")


def compact(s: str) -> str:
    return re.sub(r"[\s\-_./]", "", (s or "").upper())


def kind_of_ref(ref: str) -> str:
    m = re.match(r"^([A-Z]+)", (ref or "").upper())
    p = m.group(1) if m else ""
    return {"R": "R", "RN": "R", "RV": "R", "C": "C", "L": "L"}.get(p, "")


_UNIT_HINTS = {"R": re.compile(r"ohm|Ω|widerst|resist|\d[kKmM]\d|\dR\d|\b\d+R\b", re.I),
               "C": re.compile(r"\d\s*[pnuµm]?F\b|kondens|capac|elko|keramik|mlcc|\b\d+[pnuµ]\b", re.I),
               "L": re.compile(r"\d\s*[nuµm]?H\b|induk|spule|drossel|inductor", re.I)}


@dataclass
class ItemKey:
    item: Item
    compacts: set = field(default_factory=set)
    values: dict = field(default_factory=dict)  # Art (R/C/L/"") -> Zahlen
    packages: set = field(default_factory=set)


def _numbers(text: str) -> list[float]:
    out = []
    for tok in re.findall(r"\d+(?:[.,]\d+)?\s*[pnuµmkKMG]?\s*(?:Ohm|Ω|F|H|R)?\d*", text or ""):
        v = parse_value(tok.replace(" ", ""))
        if v is not None:
            out.append(v)
    return out


def build_index(db: Session) -> list[ItemKey]:
    items = list(db.scalars(select(Item).where(Item.active.is_(True))))
    params: dict[int, list[ItemParameter]] = {}
    for p in db.scalars(select(ItemParameter)):
        params.setdefault(p.item_id, []).append(p)
    out = []
    for it in items:
        text = " ".join(filter(None, [it.name, it.description, it.mpn, it.footprint.name if it.footprint else "",
                                      it.category.name if it.category else ""]))
        k = ItemKey(it)
        k.compacts = {compact(it.name), compact(it.mpn or "")} - {""}
        kinds = [kd for kd, rx in _UNIT_HINTS.items() if rx.search(text)] or [""]
        nums = _numbers(it.name) + _numbers(it.description or "")
        for p in params.get(it.id, []):
            if p.value_num is not None:
                nums.append(p.value_num)
        for kd in kinds:
            k.values[kd] = nums
        for src in (it.footprint.name if it.footprint else "", it.name, it.description or "", " ".join(p.value_text or "" for p in params.get(it.id, []))):
            pk = package_of(src)
            if pk:
                k.packages.add(pk)
        out.append(k)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-15, 0.001 * max(abs(a), abs(b)))


@dataclass
class Match:
    item: Item | None = None
    how: str = ""  # code, mpn, lcsc, gemerkt, wert, text
    candidates: list = field(default_factory=list)  # (Item, Punkte)


def remember_key(value: str, footprint: str) -> str:
    return f"KICAD:{compact(value)}|{package_of(footprint) or compact(footprint.split(':')[-1])}"


def match_row(db: Session, value: str, footprint: str, mpn: str = "", lcsc: str = "", code: str = "", ref: str = "",
              index: list[ItemKey] | None = None) -> Match:
    for raw, how in ((code, "code"), (mpn, "mpn")):
        if raw:
            r = resolve(db, raw)
            if r.kind == "item":
                return Match(db.get(Item, r.id), how)
            it = db.scalar(select(Item).where(Item.active.is_(True), Item.mpn.ilike(raw.strip())))
            if it is not None:
                return Match(it, how)
    if lcsc:
        sp = db.scalar(select(SupplierPart).join(Supplier).where(SupplierPart.sku.ilike(lcsc.strip())))
        if sp is not None and sp.item is not None:
            return Match(sp.item, "lcsc")
    ic = db.scalar(select(ItemCode).where(ItemCode.code == remember_key(value, footprint)))
    if ic is not None and db.get(Item, ic.item_id) is not None:
        return Match(db.get(Item, ic.item_id), "gemerkt")
    index = index if index is not None else build_index(db)
    kind = kind_of_ref(ref)
    pkg = package_of(footprint)
    num = parse_value(value.replace(" ", "")) if kind else None
    cv = compact(value)
    scored = []
    for k in index:
        score = 0
        if num is not None and any(_close(num, x) for x in k.values.get(kind, [])):
            score = 60
        elif cv and len(cv) >= 3 and (cv in k.compacts or any(c.startswith(cv) or cv.startswith(c) for c in k.compacts if len(c) >= 4)):
            score = 55 if cv in k.compacts else 40
        if not score:
            continue
        if pkg and pkg in k.packages:
            score += 30
        elif pkg and k.packages:
            score -= 25  # anderes Gehäuse
        if k.item.lots:
            score += 5
        scored.append((k.item, score))
    scored.sort(key=lambda t: (-t[1], t[0].name.lower()))
    if scored and scored[0][1] >= 60 and (len(scored) == 1 or scored[0][1] - scored[1][1] >= 20):
        return Match(scored[0][0], "wert", scored[:6])
    return Match(None, "", scored[:6])


def remember(db: Session, line: ProjectLine, item: Item) -> None:
    """Manuelle Zuordnung merken: Wert + Gehäuse -> Artikel (für die nächsten KiCad-Importe)."""
    if line.value:
        add_item_code(db, item.id, remember_key(line.value, line.footprint), "kicad", "zuordnung")


# ------------------------------------------------------------------ Projekte
def create_project(db: Session, name: str, user_id: int | None = None, **kw) -> Project:
    name = (name or "").strip()[:200]
    if not name:
        raise ValueError("Bitte einen Namen angeben.")
    p = Project(code=next_code(db, "project"), name=name, created_by=user_id, **kw)
    db.add(p)
    db.flush()
    return p


def import_rows(db: Session, project: Project, rows: list[BomRow], replace: bool = True) -> dict:
    grouped = group_rows(rows)
    if replace:
        for ln in list(project.lines):
            db.delete(ln)
        db.flush()
    index = build_index(db)
    stats = {"lines": 0, "parts": 0, "matched": 0, "dnp": 0}
    for i, g in enumerate(grouped):
        m = match_row(db, g.value, g.footprint, g.mpn, g.lcsc, g.code, g.refs[0] if g.refs else "", index)
        ln = ProjectLine(project_id=project.id, refs=", ".join(g.refs)[:1000], value=g.value[:200], footprint=g.footprint[:200],
                         mpn=(g.mpn or g.lcsc)[:120], qty=g.quantity, dnp=g.dnp, item_id=m.item.id if m.item else None,
                         match=m.how, sort=i)
        db.add(ln)
        stats["lines"] += 1
        stats["parts"] += int(g.quantity) if not g.dnp else 0
        stats["matched"] += 1 if m.item else 0
        stats["dnp"] += 1 if g.dnp else 0
    db.flush()
    db.refresh(project)
    return stats


@dataclass
class Need:
    line: ProjectLine
    need: float
    stock: float
    free: float
    lots: list

    @property
    def short(self) -> float:
        return max(0.0, self.need - self.free) if self.free != math.inf else 0.0


def availability(db: Session, project: Project, boards: int = 1) -> list[Need]:
    from . import orders as osvc

    out = []
    for ln in project.lines:
        if ln.dnp:
            continue
        need = (ln.qty or 0) * boards
        if ln.item is None:
            out.append(Need(ln, need, 0.0, 0.0, []))
            continue
        res = osvc.reservation(db, ln.item)
        out.append(Need(ln, need, res.stock, res.free, osvc.pick_lots(db, ln.item)))
    return out


def buildable(needs_one: list[Need]) -> int:
    """Wie viele Platinen gehen mit freiem Bestand (ohne Reservierungen anderer Aufträge)?"""
    n = math.inf
    for nd in needs_one:
        if nd.need <= 0:
            continue
        if nd.line.item is None:
            return 0
        if nd.free == math.inf:
            continue
        n = min(n, math.floor(nd.free / nd.need + 1e-9))
    return 0 if n == math.inf and not needs_one else (int(n) if n != math.inf else 999)


def create_missing_items(db: Session, project: Project, user_id: int | None = None) -> int:
    """Nicht zugeordnete Stücklistenzeilen als neue Lagerartikel anlegen (Name aus Wert + Gehäuse, MPN übernehmen)."""
    from .items import create_item

    missing = [ln for ln in project.lines if not ln.dnp and ln.item is None]
    for ln in missing:
        name = " ".join(x for x in (ln.value, package_of(ln.footprint)) if x) or ln.refs
        it = create_item(db, name[:200], user_id, description=f"aus Projekt {project.name}: {ln.refs}"[:500], mpn=ln.mpn or "")
        ln.item_id = it.id
        ln.match = "neu"
        remember(db, ln, it)
    db.flush()
    db.refresh(project)
    return len(missing)


def build_order(db: Session, project: Project, boards: int, user_id: int | None = None, create_missing: bool = False):
    """Auftrag „Lichtschranke ×3“ aus dem Projekt: Positionen mit Menge × Anzahl, Fehlteile -> Einkaufsliste."""
    from . import orders as osvc

    if boards < 1:
        raise ValueError("Mindestens 1 Platine.")
    missing = [ln for ln in project.lines if not ln.dnp and ln.item is None]
    if missing and not create_missing:
        raise ValueError(f"{len(missing)} Bauteile sind noch keinem Artikel zugeordnet – zuordnen oder „als neue Artikel anlegen“ wählen.")
    create_missing_items(db, project, user_id)
    order = osvc.create_order(db, f"{project.name} ×{boards}", user_id, requester=project.name[:120],
                              description=f"Aus Projekt {project.code} {project.name}" + (f" Rev. {project.revision}" if project.revision else ""))
    order.project_id = project.id
    order.build_qty = boards
    for ln in project.lines:
        if ln.dnp or ln.item is None:
            continue
        osvc.add_line(db, order, ln.item, (ln.qty or 0) * boards, ln.refs[:255])
    n = osvc.sync_shortfalls(db, order, user_id)
    db.flush()
    return order, n
