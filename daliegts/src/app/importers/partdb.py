"""Import aus alter Part-DB 0.x (phpMyAdmin-/mysqldump-Dump).

- Tabellen users/groups/log werden nie übernommen (Passwort-Hashes!).
- Lagerorte landen unter "Part-DB (Altbestand)", Bestände werden zur Prüfung per Inventur markiert.
- Idempotent: erneuter Import aktualisiert statt zu duplizieren (external_ids).
"""
from __future__ import annotations

import sqlite3
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Attachment, Category, Footprint, Item, Location, Manufacturer, PriceInfo, StockLot, StockMovement, Supplier, SupplierPart
from ..services.codes import add_item_code, add_location_alias, next_code
from ..services.items import store_upload
from ..services.locations import create_location, guess_type
from ..services.search import index_item
from .common import Report, clean_html, ext_get, ext_set, guess_footprint_image
from .sqldump import load_dump

SRC = "partdb"
ROOT_NAME = "Part-DB (Altbestand)"


def _merge_newer(base: sqlite3.Connection, newer: sqlite3.Connection, report: Report) -> None:
    """Übernimmt Feldwerte aus einem neueren Dump (gleiche IDs), z. B. umbenannte Lagerorte."""
    for table in ("storelocations", "categories", "parts", "footprints", "suppliers", "manufacturers"):
        try:
            new_rows = {r["id"]: dict(r) for r in newer.execute(f'SELECT * FROM "{table}"')}
        except sqlite3.Error:
            continue
        base_cols = {r[1] for r in base.execute(f'PRAGMA table_info("{table}")')}
        changed = 0
        for rid, row in new_rows.items():
            old = base.execute(f'SELECT * FROM "{table}" WHERE id=?', (rid,)).fetchone()
            cols = [c for c in row if c in base_cols]
            if old is None:
                base.execute(f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({",".join("?" for _ in cols)})', [row[c] for c in cols])
                changed += 1
            elif any(old[c] != row[c] for c in cols):
                base.execute(f'UPDATE "{table}" SET {",".join(chr(34)+c+chr(34)+"=?" for c in cols)} WHERE id=?', [row[c] for c in cols] + [rid])
                changed += 1
        if changed:
            report.notes.append(f"Neuerer Dump: {changed} Datensätze in {table} aktualisiert.")


def _tree(rows: dict[int, sqlite3.Row]) -> list[int]:
    """Topologische Reihenfolge (Eltern vor Kindern)."""
    order, seen = [], set()

    def visit(i: int, depth=0):
        if i in seen or depth > 50:
            return
        p = rows[i]["parent_id"]
        if p and p in rows:
            visit(p, depth + 1)
        seen.add(i)
        order.append(i)

    for i in rows:
        visit(i)
    return order


def _media_extractor(archive: Path | None):
    """Liefert Funktion filename -> bytes|None für ein ZIP/TAR mit dem alten data/media-Ordner."""
    if archive is None:
        return lambda name: None
    names: dict[str, tuple] = {}
    if zipfile.is_zipfile(archive):
        zf = zipfile.ZipFile(archive)
        for n in zf.namelist():
            names[Path(n).name.lower()] = ("zip", n)
        return lambda fn: zf.read(names[fn.lower()][1]) if fn.lower() in names else None
    tf = tarfile.open(archive)
    for m in tf.getmembers():
        if m.isfile():
            names[Path(m.name).name.lower()] = ("tar", m)
    return lambda fn: tf.extractfile(names[fn.lower()][1]).read() if fn.lower() in names else None


def import_partdb(db: Session, dump: Path, newer_dump: Path | None = None, media_archive: Path | None = None,
                  user_id: int | None = None, dry_run: bool = False) -> Report:
    report = Report("Import Part-DB 0.x", dry_run)
    src = load_dump(dump)
    if newer_dump:
        _merge_newer(src, load_dump(newer_dump), report)
    get_media = _media_extractor(media_archive)
    try:
        db_version = src.execute("SELECT keyValue FROM internal WHERE keyName='dbVersion'").fetchone()
        if db_version:
            report.notes.append(f"Part-DB Datenbank-Version: {db_version[0]}")
    except sqlite3.Error:
        pass

    # --- Wurzel für Alt-Lagerorte
    root_id = ext_get(db, SRC, "root", 0)
    root = db.get(Location, root_id) if root_id else None
    if root is None:
        root = create_location(db, ROOT_NAME, None, "bereich", structural=True,
                               description="Lagerorte aus der alten Part-DB (Stand 2020). Bitte per Inventur prüfen und in die aktuelle Struktur umlagern.")
        ext_set(db, SRC, "root", 0, "location", root.id)

    # --- Lagerorte
    locs = {r["id"]: r for r in src.execute("SELECT * FROM storelocations")}
    loc_map: dict[int, int] = {}
    for sid in _tree(locs):
        r = locs[sid]
        parent = loc_map.get(r["parent_id"]) if r["parent_id"] else root.id
        existing = ext_get(db, SRC, "storelocation", sid)
        loc = db.get(Location, existing) if existing else None
        if loc is None:
            loc = create_location(db, r["name"], parent, guess_type(r["name"]))
            loc.is_full = bool(r["is_full"]) if "is_full" in r.keys() else False
            report.inc("Lagerorte neu")
        else:
            report.inc("Lagerorte vorhanden (unverändert)")
        loc_map[sid] = loc.id
        ext_set(db, SRC, "storelocation", sid, "location", loc.id)
        add_location_alias(db, loc.id, f"partdb:storelocation:{sid}", "partdb")
    db.flush()

    # --- Kategorien
    cats = {r["id"]: r for r in src.execute("SELECT * FROM categories")}
    cat_map: dict[int, int] = {}
    for cid in _tree(cats):
        r = cats[cid]
        parent = cat_map.get(r["parent_id"]) if r["parent_id"] else None
        existing = ext_get(db, SRC, "category", cid)
        cat = db.get(Category, existing) if existing else None
        if cat is None:
            q = select(Category).where(Category.name == r["name"])
            q = q.where(Category.parent_id.is_(None)) if parent is None else q.where(Category.parent_id == parent)
            cat = db.scalar(q) or Category(name=r["name"], parent_id=parent)
            if cat.id is None:
                db.add(cat)
                db.flush()
                report.inc("Kategorien neu")
        cat.name = r["name"]
        cat_map[cid] = cat.id
        ext_set(db, SRC, "category", cid, "category", cat.id)

    # --- Footprints (flach, mit Bild-Vorschlag)
    fp_map: dict[int, int] = {}
    for r in src.execute("SELECT * FROM footprints"):
        name = (r["name"] or "").strip() or f"Footprint {r['id']}"
        fp = db.scalar(select(Footprint).where(Footprint.name == name))
        if fp is None:
            fp = Footprint(name=name, image=guess_footprint_image(f"{name} {r['filename'] or ''}"))
            db.add(fp)
            db.flush()
            report.inc("Footprints neu")
        elif not fp.image:
            fp.image = guess_footprint_image(f"{name} {r['filename'] or ''}")
        fp_map[r["id"]] = fp.id

    # --- Hersteller / Lieferanten
    man_map, sup_map = {}, {}
    for table, model, mp in (("manufacturers", Manufacturer, man_map), ("suppliers", Supplier, sup_map)):
        try:
            rows = list(src.execute(f"SELECT * FROM {table}"))
        except sqlite3.Error:
            rows = []
        for r in rows:
            obj = db.scalar(select(model).where(model.name == r["name"]))
            if obj is None:
                obj = model(name=r["name"], website=(r["website"] if "website" in r.keys() else "") or "")
                db.add(obj)
                db.flush()
                report.inc(f"{'Hersteller' if model is Manufacturer else 'Lieferanten'} neu")
            mp[r["id"]] = obj.id

    # --- Bauteile
    seen_name_loc: dict[tuple, str] = {}
    part_map: dict[int, int] = {}
    for r in src.execute("SELECT * FROM parts ORDER BY id"):
        pid = r["id"]
        existing = ext_get(db, SRC, "part", pid)
        item = db.get(Item, existing) if existing else None
        fields = dict(
            name=(r["name"] or f"Bauteil {pid}").strip(), description=clean_html(r["description"]), notes=clean_html(r["comment"]),
            category_id=cat_map.get(r["id_category"]), footprint_id=fp_map.get(r["id_footprint"]),
            manufacturer_id=man_map.get(r["id_manufacturer"]), min_stock=float(r["mininstock"] or 0), kind="bauteil",
        )
        is_new = item is None
        if is_new:
            item = Item(code=next_code(db, "item"), created_by=user_id, **fields)
            db.add(item)
            db.flush()
            report.inc("Bauteile neu")
            created = r["datetime_added"] if "datetime_added" in r.keys() else None
            try:
                if created:
                    item.created_at = datetime.fromisoformat(str(created))
            except ValueError:
                pass
        else:
            report.inc("Bauteile vorhanden (unverändert)")
        if is_new and r["manufacturer_product_url"] and r["manufacturer_product_url"].lower().startswith(("http://", "https://")):
            if not db.scalar(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == item.id, Attachment.url == r["manufacturer_product_url"])):
                db.add(Attachment(owner_type="item", owner_id=item.id, kind="other", title="Herstellerseite", url=r["manufacturer_product_url"]))
        part_map[pid] = item.id
        ext_set(db, SRC, "part", pid, "item", item.id)
        add_item_code(db, item.id, f"partdb:part:{pid}", "altsystem", "partdb")
        # Bestand: genau ein Lot aus Part-DB (idempotent über external id)
        lot_ext = ext_get(db, SRC, "stock", pid)
        lot = db.get(StockLot, lot_ext) if lot_ext else None
        loc_id = loc_map.get(r["id_storelocation"]) if r["id_storelocation"] else None
        qty = float(r["instock"] or 0)
        if lot is None:
            lot = StockLot(item_id=item.id, location_id=loc_id, quantity=qty, code=next_code(db, "lot"), needs_review=True,
                           notes="Import Part-DB – bitte per Inventur bestätigen")
            db.add(lot)
            db.flush()
            db.add(StockMovement(item_id=item.id, lot_id=lot.id, kind="imported", delta=qty, quantity_after=qty, to_location_id=loc_id,
                                 user_id=user_id, note="Import aus Part-DB", ref=f"partdb:{pid}"))
            ext_set(db, SRC, "stock", pid, "lot", lot.id)
        if loc_id is None:
            report.warnings.append(f"„{item.name}“ hat in Part-DB keinen Lagerort.")
        key = (item.name.strip().lower(), loc_id)
        if key in seen_name_loc:
            report.duplicates.append(f"„{item.name}“ doppelt am selben Ort (Part-DB-IDs {seen_name_loc[key]} und {pid})")
        else:
            seen_name_loc[key] = str(pid)
    db.flush()

    # --- Bestelldaten & Preise
    try:
        for r in src.execute("SELECT * FROM orderdetails"):
            item_id = part_map.get(r["part_id"])
            sup_id = sup_map.get(r["id_supplier"])
            if not item_id or not sup_id:
                continue
            sp_ext = ext_get(db, SRC, "orderdetail", r["id"])
            sp = db.get(SupplierPart, sp_ext) if sp_ext else None
            if sp is None:
                sp = SupplierPart(item_id=item_id, supplier_id=sup_id)
                db.add(sp)
                report.inc("Bestellnummern")
            sp.sku = r["supplierpartnr"] or ""
            url = r["supplier_product_url"] or ""
            sp.url = url if url.lower().startswith(("http://", "https://")) else ""
            sp.obsolete = bool(r["obsolete"])
            db.flush()
            ext_set(db, SRC, "orderdetail", r["id"], "supplier_part", sp.id)
            for pr in src.execute("SELECT * FROM pricedetails WHERE orderdetails_id=?", (r["id"],)):
                if pr["price"] is None:
                    continue
                pq = float(pr["price_related_quantity"] or 1) or 1
                pi_ext = ext_get(db, SRC, "price", pr["id"])
                pi = db.get(PriceInfo, pi_ext) if pi_ext else None
                if pi is None:
                    pi = PriceInfo(item_id=item_id, supplier_part_id=sp.id, source="historisch")
                    db.add(pi)
                    report.inc("Preise (historisch)")
                pi.min_qty = float(pr["min_discount_quantity"] or 1)
                pi.unit_price = float(pr["price"]) / pq
                try:
                    pi.date = datetime.fromisoformat(str(pr["last_modified"]))
                except (ValueError, TypeError):
                    pi.date = None
                db.flush()
                ext_set(db, SRC, "price", pr["id"], "price", pi.id)
    except sqlite3.Error:
        report.notes.append("Keine Bestell-/Preisdaten im Dump.")

    # --- Anhänge
    try:
        rows = list(src.execute("SELECT * FROM attachements"))
    except sqlite3.Error:
        rows = []
    for r in rows:
        if r["class_name"] != "Part" or r["element_id"] not in part_map:
            continue
        fn = Path(str(r["filename"]).replace("%BASE%/", "")).name
        data = get_media(fn)
        kind = "image" if r["type_id"] == 1 else "datasheet"
        existing_att = ext_get(db, SRC, "attachment", r["id"])
        att = db.get(Attachment, existing_att) if existing_att else None
        if att is not None and not (att.missing and data):
            continue  # schon vorhanden (Datei nachliefern nur, wenn sie bisher fehlte)
        stored = store_upload(data, fn, "attachments" if kind != "image" else "images") if data and not dry_run else None
        if att is None:
            att = Attachment(owner_type="item", owner_id=part_map[r["element_id"]], kind=kind, title=r["name"] or fn)
            db.add(att)
        att.filename, att.missing = stored, data is None
        db.flush()
        ext_set(db, SRC, "attachment", r["id"], "attachment", att.id)
        if kind == "image" and stored:
            it = db.get(Item, part_map[r["element_id"]])
            if not it.image:
                it.image = stored
        report.inc("Anhänge" + (" (Datei fehlt)" if data is None else ""))
    if any("Datei fehlt" in k for k in report.counts):
        report.notes.append("Anhang-Dateien lagen im Ordner data/media der alten Installation. Mit einem ZIP/TAR davon erneut importieren, um sie zu übernehmen.")

    report.notes.append(f"Alle Lagerorte liegen unter „{ROOT_NAME}“. Bestände sind mit „prüfen“ markiert (Suche → Filter „Inventur offen“).")
    report.notes.append("Tabellen users/groups/log wurden bewusst nicht übernommen.")
    db.flush()
    if dry_run:
        db.rollback()
    else:
        for iid in set(part_map.values()):
            index_item(db, db.get(Item, iid))
        db.commit()
    return report


def _unused():  # pragma: no cover
    return tempfile
