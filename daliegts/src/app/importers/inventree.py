"""Import aus InvenTree: SQLite-Datenbank, Tabellen-CSVs und/oder Tracking-CSV, optional Medien-Archiv.

Besonderheiten:
- Lagerorte bekommen den Alias {"stocklocation": <id>} → vorhandene InvenTree-Etiketten funktionieren sofort.
- Liegt nur die Tracking-CSV vor, wird der letzte Stand je Lagerartikel rekonstruiert. Unbekannte Artikel werden als
  „Unbenannter Lagerartikel #<id>“ angelegt, unbekannte Lagerorte als Platzhalter „InvenTree-Lagerort <id>“ –
  beides zur Klärung per Inventur (Etikett scannen → umbenennen / zusammenführen).
"""
from __future__ import annotations

import csv
import json
import sqlite3
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Category, Item, Location, StockLot, StockMovement
from ..services.codes import add_item_code, add_location_alias, next_code
from ..services.items import store_upload
from ..services.locations import create_location, guess_type
from ..services.search import index_item
from .common import Report, clean_html, ext_get, ext_set

SRC = "inventree"
STATUS = {10: "ok", 50: "attention", 55: "damaged", 60: "defect", 65: "defect", 70: "lost", 75: "attention", 85: "ok"}
TRACKING = {1: "created", 10: "counted", 11: "added", 12: "removed", 20: "moved", 25: "edited", 40: "split", 42: "split", 45: "merged"}
UNKNOWN_ROOT = "InvenTree (unbekannte Lagerorte)"


def _rows(con: sqlite3.Connection | None, table: str) -> list[sqlite3.Row]:
    if con is None:
        return []
    try:
        return list(con.execute(f'SELECT * FROM "{table}"'))
    except sqlite3.Error:
        return []


def _open_sources(sqlite_path: Path | None, csv_dir: Path | None) -> sqlite3.Connection | None:
    con = None
    if sqlite_path:
        con = sqlite3.connect(sqlite_path)
        con.row_factory = sqlite3.Row
    if csv_dir:
        con = con or sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        for f in Path(csv_dir).glob("*.csv"):
            table = f.stem.split("_", 1)[1] if f.stem[:1].isdigit() and "_" in f.stem else f.stem
            with open(f, encoding="utf-8-sig", newline="") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                rows = list(csv.DictReader(fh, dialect=dialect))
            if not rows:
                continue
            cols = list(rows[0].keys())
            con.execute(f'DROP TABLE IF EXISTS "{table}"')
            con.execute(f'CREATE TABLE "{table}" ({",".join(chr(34)+c+chr(34) for c in cols)})')
            con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in cols)})', [[None if r[c] in ("", "NULL") else r[c] for c in cols] for r in rows])
    return con


def _tracking_state(rows: list[dict]) -> tuple[dict[int, dict], dict[int, list[dict]]]:
    rows = sorted(rows, key=lambda r: (str(r.get("date")), int(r["id"])))
    state: dict[int, dict] = {}
    hist: dict[int, list[dict]] = {}
    for r in rows:
        item_id = int(r["item_id"])
        deltas = r.get("deltas") or "{}"
        try:
            d = json.loads(deltas) if isinstance(deltas, str) else dict(deltas)
        except json.JSONDecodeError:
            d = {}
        s = state.setdefault(item_id, {"location": None, "quantity": None, "status": 10, "created": r.get("date")})
        for k in ("location", "quantity", "status"):
            if k in d:
                s[k] = d[k]
        s["last"] = r.get("date")
        hist.setdefault(item_id, []).append({"date": r.get("date"), "type": int(r.get("tracking_type") or 0), "deltas": d, "notes": r.get("notes") or ""})
    return state, hist


def _media(archive: Path | None):
    if archive is None:
        return lambda p: None
    if zipfile.is_zipfile(archive):
        zf = zipfile.ZipFile(archive)
        names = set(zf.namelist())
        return lambda p: zf.read(p) if p in names else None
    tf = tarfile.open(archive)
    members = {m.name: m for m in tf.getmembers() if m.isfile()}
    return lambda p: tf.extractfile(members[p]).read() if p in members else None


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).replace(microsecond=0, tzinfo=None)
    except ValueError:
        return None


def import_inventree(db: Session, sqlite_path: Path | None = None, tracking_csv: Path | None = None, csv_dir: Path | None = None,
                     media_archive: Path | None = None, user_id: int | None = None, dry_run: bool = False) -> Report:
    report = Report("Import InvenTree", dry_run)
    con = _open_sources(sqlite_path, csv_dir)
    get_media = _media(media_archive)

    # ---------------- Lagerorte
    locs = {int(r["id"]): r for r in _rows(con, "stock_stocklocation")}
    loc_map: dict[int, int] = {}

    def ensure_loc(inv_id: int) -> int:
        if inv_id in loc_map:
            return loc_map[inv_id]
        existing = ext_get(db, SRC, "stocklocation", inv_id)
        loc = db.get(Location, existing) if existing else None
        r = locs.get(inv_id)
        if r is not None:
            parent = ensure_loc(int(r["parent_id"])) if r["parent_id"] not in (None, "") else None
            if loc is None:
                loc = create_location(db, r["name"], parent, guess_type(r["name"]), description=clean_html(r["description"] if "description" in r.keys() else ""))
                if "structural" in r.keys() and str(r["structural"]) in ("1", "True", "true"):
                    loc.structural = True
                report.inc("Lagerorte neu")
            elif loc.name.startswith("InvenTree-Lagerort ") and loc.name != r["name"]:
                loc.name = r["name"]  # Platzhalter bekommt jetzt den echten Namen
                report.inc("Platzhalter-Lagerorte benannt")
            else:
                report.inc("Lagerorte vorhanden (unverändert)")
            if "barcode_data" in r.keys() and r["barcode_data"]:
                add_location_alias(db, loc.id, r["barcode_data"], "inventree")
        elif loc is None:
            root_id = ext_get(db, SRC, "unknown_root", 0)
            root = db.get(Location, root_id) if root_id else None
            if root is None:
                root = create_location(db, UNKNOWN_ROOT, None, "bereich", structural=True,
                                       description="Platzhalter für InvenTree-Lagerorte, deren Namen nicht im Import enthalten waren. "
                                                   "Etikett am Fach scannen → Ort umbenennen bzw. in die richtige Struktur verschieben.")
                ext_set(db, SRC, "unknown_root", 0, "location", root.id)
            loc = create_location(db, f"InvenTree-Lagerort {inv_id}", root.id, "fach")
            report.inc("Lagerorte als Platzhalter")
        loc_map[inv_id] = loc.id
        ext_set(db, SRC, "stocklocation", inv_id, "location", loc.id)
        add_location_alias(db, loc.id, json.dumps({"stocklocation": inv_id}), "inventree")
        return loc.id

    for inv_id in sorted(locs):
        ensure_loc(inv_id)
    db.flush()

    # ---------------- Kategorien
    cats = {int(r["id"]): r for r in _rows(con, "part_partcategory")}
    cat_map: dict[int, int] = {}

    def ensure_cat(cid: int) -> int:
        if cid in cat_map:
            return cat_map[cid]
        r = cats[cid]
        parent = ensure_cat(int(r["parent_id"])) if r["parent_id"] not in (None, "") and int(r["parent_id"]) in cats else None
        existing = ext_get(db, SRC, "category", cid)
        cat = db.get(Category, existing) if existing else None
        if cat is None:
            q = select(Category).where(Category.name == r["name"])
            q = q.where(Category.parent_id.is_(None)) if parent is None else q.where(Category.parent_id == parent)
            cat = db.scalar(q)
            if cat is None:
                cat = Category(name=r["name"], parent_id=parent, description=clean_html(r["description"] if "description" in r.keys() else ""))
                db.add(cat)
                db.flush()
                report.inc("Kategorien neu")
        cat_map[cid] = cat.id
        ext_set(db, SRC, "category", cid, "category", cat.id)
        return cat.id

    for cid in sorted(cats):
        ensure_cat(cid)

    # ---------------- Teile
    parts = {int(r["id"]): r for r in _rows(con, "part_part")}
    part_map: dict[int, int] = {}
    for pid, r in sorted(parts.items()):
        existing = ext_get(db, SRC, "part", pid)
        item = db.get(Item, existing) if existing else None
        keys = r.keys()
        fields = dict(
            name=r["name"], description=clean_html(r["description"]) if "description" in keys else "",
            tags=r["keywords"] or "" if "keywords" in keys else "", unit=(r["units"] or "Stk") if "units" in keys else "Stk",
            min_stock=float(r["minimum_stock"] or 0) if "minimum_stock" in keys else 0, notes=clean_html(r["notes"]) if "notes" in keys else "",
            category_id=cat_map.get(int(r["category_id"])) if "category_id" in keys and r["category_id"] not in (None, "") else None,
            is_template=str(r["is_template"]) in ("1", "True", "true") if "is_template" in keys else False,
            active=str(r["active"]) not in ("0", "False", "false") if "active" in keys else True,
        )
        if item is None:
            item = Item(code=next_code(db, "item"), created_by=user_id, **fields)
            db.add(item)
            db.flush()
            report.inc("Teile neu")
        else:
            report.inc("Teile vorhanden (unverändert)")
        if "IPN" in keys and r["IPN"]:
            item.mpn = item.mpn or r["IPN"]
        if "image" in keys and r["image"] and not item.image and not dry_run:
            data = get_media(r["image"])
            if data:
                item.image = store_upload(data, r["image"])
                report.inc("Bilder übernommen")
        part_map[pid] = item.id
        ext_set(db, SRC, "part", pid, "item", item.id)
        add_item_code(db, item.id, json.dumps({"part": pid}), "altsystem", "inventree")
    for pid, r in parts.items():
        if "variant_of_id" in r.keys() and r["variant_of_id"] not in (None, "") and int(r["variant_of_id"]) in part_map:
            db.get(Item, part_map[pid]).variant_of_id = part_map[int(r["variant_of_id"])]

    # ---------------- Lagerartikel (StockItems) + Tracking
    stockitems = {int(r["id"]): r for r in _rows(con, "stock_stockitem")}
    tracking_rows = [dict(r) for r in _rows(con, "stock_stockitemtracking")]
    if tracking_csv:
        with open(tracking_csv, encoding="utf-8-sig", newline="") as fh:
            csv_rows = list(csv.DictReader(fh))
        have = {str(r["id"]) for r in tracking_rows}
        tracking_rows += [r for r in csv_rows if str(r["id"]) not in have]
        report.notes.append(f"Tracking-CSV: {len(csv_rows)} Einträge.")
    state, hist = _tracking_state(tracking_rows)
    all_ids = sorted(set(stockitems) | set(state))
    placeholders = 0
    for sid in all_ids:
        r = stockitems.get(sid)
        st = state.get(sid, {})
        if r is not None:
            part_item = part_map.get(int(r["part_id"])) if r["part_id"] not in (None, "") else None
            loc_inv = int(r["location_id"]) if r["location_id"] not in (None, "") else None
            qty = float(r["quantity"] or 0)
            status = int(r["status"] or 10) if "status" in r.keys() else 10
            serial = (r["serial"] or "") if "serial" in r.keys() else ""
            batch = (r["batch"] or "") if "batch" in r.keys() else ""
            notes = clean_html(r["notes"]) if "notes" in r.keys() else ""
        else:
            part_item, loc_inv = None, st.get("location")
            qty, status, serial, batch, notes = float(st.get("quantity") or 0), int(st.get("status") or 10), "", "", ""
        if STATUS.get(status) in ("lost",) or (status == 60):
            report.inc("Lagerartikel übersprungen (zerstört/verloren)")
            continue
        if part_item is None:
            existing_item = ext_get(db, SRC, "stockitem_placeholder", sid)
            item = db.get(Item, existing_item) if existing_item else None
            if item is None:
                item = Item(code=next_code(db, "item"), name=f"Unbenannter Lagerartikel #{sid} (InvenTree)", created_by=user_id,
                            description="Aus der InvenTree-Bestandshistorie rekonstruiert – Name unbekannt. "
                                        "Bitte beim Durchgehen des Fachs umbenennen oder mit einem vorhandenen Artikel zusammenführen.",
                            tags="inventree, klären")
                db.add(item)
                db.flush()
                placeholders += 1
                ext_set(db, SRC, "stockitem_placeholder", sid, "item", item.id)
            part_item = item.id
        loc_id = ensure_loc(int(loc_inv)) if loc_inv not in (None, "") else None
        lot_ext = ext_get(db, SRC, "stockitem", sid)
        lot = db.get(StockLot, lot_ext) if lot_ext else None
        if lot is None:
            lot = StockLot(item_id=part_item, location_id=loc_id, quantity=qty, code=next_code(db, "lot"), status=STATUS.get(status, "ok"),
                           serial=serial, batch=batch, notes=notes, needs_review=True)
            db.add(lot)
            db.flush()
            report.inc("Bestände neu")
            for h in hist.get(sid, []):
                d = h["deltas"]
                delta = float(d.get("added") or 0) - float(d.get("removed") or 0)
                if h["type"] == 1:
                    delta = float(d.get("quantity") or 0)
                db.add(StockMovement(item_id=part_item, lot_id=lot.id, kind=TRACKING.get(h["type"], "imported"), delta=delta,
                                     quantity_after=d.get("quantity"), to_location_id=ensure_loc(int(d["location"])) if d.get("location") else None,
                                     user_id=user_id, ts=_parse_dt(h["date"]) or datetime.now(), note=f"InvenTree ({h['type']}) {json.dumps(d)} {h['notes']}".strip(),
                                     ref=f"inventree:stockitem:{sid}"))
            if not hist.get(sid):
                db.add(StockMovement(item_id=part_item, lot_id=lot.id, kind="imported", delta=qty, quantity_after=qty, to_location_id=loc_id,
                                     user_id=user_id, note="Import aus InvenTree", ref=f"inventree:stockitem:{sid}"))
            ext_set(db, SRC, "stockitem", sid, "lot", lot.id)
        else:
            report.inc("Bestände vorhanden (unverändert)")
        add_item_code(db, part_item, json.dumps({"stockitem": sid}), "altsystem", "inventree")
        if loc_id is None:
            report.warnings.append(f"Lagerartikel #{sid}: ohne Lagerort.")
    if placeholders:
        report.notes.append(f"{placeholders} Lagerartikel ohne Namen als Platzhalter angelegt (Suche: „Unbenannter Lagerartikel“).")
    report.notes.append("Vorhandene InvenTree-QR-Etiketten ({\"stocklocation\": n}) sind den Lagerorten zugeordnet und direkt scannbar.")
    db.flush()
    if dry_run:
        db.rollback()
    else:
        for iid in set(part_map.values()) | {l.item_id for l in db.scalars(select(StockLot))}:
            it = db.get(Item, iid)
            if it:
                index_item(db, it)
        db.commit()
    return report
