"""Interne Codes (L-/A-/S-) und Auflösung gescannter Codes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CodeSequence, Item, ItemCode, Location, LocationAlias, LotCode, Setting, StockLot

PREFIXES = {"location": "L", "item": "A", "lot": "S", "order": "AU", "project": "PR"}
INTERNAL_RE = re.compile(r"^(?P<p>[LAS])-?(?P<n>\d{1,9})$", re.I)


def next_code(db: Session, kind: str) -> str:
    prefix = PREFIXES[kind]
    seq = db.get(CodeSequence, prefix)
    if seq is None:
        seq = CodeSequence(prefix=prefix, next_value=1)
        db.add(seq)
        db.flush()
    value = seq.next_value
    seq.next_value = value + 1
    db.flush()
    return f"{prefix}-{value:05d}"


def reserve_at_least(db: Session, kind: str, value: int) -> None:
    """Sorgt dafür, dass künftige Codes > value sind (z. B. nach Importen)."""
    prefix = PREFIXES[kind]
    seq = db.get(CodeSequence, prefix) or CodeSequence(prefix=prefix, next_value=1)
    db.add(seq)
    seq.next_value = max(seq.next_value or 1, value + 1)
    db.flush()


_INV_KEYS = ("stocklocation", "stockitem", "supplierpart", "part", "location")
_INV_SHORT = {"SL": "stocklocation", "SI": "stockitem", "PA": "part", "SP": "supplierpart"}
# Handscanner mit US-Tastaturbelegung an deutschem PC tippen {"stockitem": 12} als ÜÄstockitemÄÖ 12* – Satzzeichen egal
_INV_LOOSE = re.compile(r"^[^A-Za-z0-9]{1,4}(stocklocation|stockitem|supplierpart|part|location)[^A-Za-z0-9]{1,6}(\d{1,9})[^A-Za-z0-9]{0,4}$", re.I)


def inventree_key(s: str) -> str | None:
    """InvenTree-Barcodes: JSON {"stockitem": 12}, verstümmeltes JSON (Tastaturbelegung) oder Kurzform INV-SI12."""
    m = re.fullmatch(r"INV-(SL|SI|PA|SP)(\d{1,9})", s.strip(), re.I)
    if m:
        return f"inventree:{_INV_SHORT[m.group(1).upper()]}:{int(m.group(2))}"
    m = _INV_LOOSE.match(s.strip())
    if m:
        return f"inventree:{m.group(1).lower()}:{int(m.group(2))}"
    return None


def normalize(raw: str) -> str:
    """Normalisiert Scan-Inhalte zu einem Vergleichsschlüssel.

    - InvenTree-JSON {"stocklocation": 584} -> inventree:stocklocation:584
    - URLs .../s/L-00042 -> L-00042
    - interne Codes -> L-00042 (Großbuchstaben, 5 Stellen)
    - sonst: getrimmt, Großbuchstaben
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("{") and s.endswith("}"):
        try:
            data = json.loads(s)
            if isinstance(data, dict) and len(data) >= 1:
                for key in ("stocklocation", "stockitem", "part", "supplierpart", "location"):
                    if key in data:
                        return f"inventree:{key}:{int(data[key])}"
        except (ValueError, TypeError):
            pass
    inv = inventree_key(s)
    if inv:
        return inv
    if s.lower().startswith(("http://", "https://")):
        path = urlparse(s).path.rstrip("/")
        m = re.search(r"/s/([^/]+)$", path)
        if m:
            s = m.group(1)
        else:
            return s
    m = INTERNAL_RE.match(s)
    if m:
        return f"{m.group('p').upper()}-{int(m.group('n')):05d}"
    return s.upper()


@dataclass
class Resolved:
    kind: str  # location | item | lot | unknown
    id: int | None = None
    code: str = ""
    raw: str = ""


def _regex_mappings(db: Session) -> list[dict]:
    s = db.get(Setting, "scan_regex_mappings")
    return list(s.value) if s and isinstance(s.value, list) else []


def resolve(db: Session, raw: str) -> Resolved:
    key = normalize(raw)
    if not key:
        return Resolved("unknown", raw=raw)
    if re.match(r"^PR-?\d{1,9}$", key, re.I):
        from ..models import Project

        pr = db.scalar(select(Project).where(Project.code == "PR-" + key.upper().replace("PR", "").lstrip("-").zfill(5)))
        if pr:
            return Resolved("project", pr.id, pr.code, raw)
    if re.match(r"^AU-?\d{1,9}$", key, re.I):
        from ..models import Order

        o = db.scalar(select(Order).where(Order.code == "AU-" + key.split("-")[-1].lstrip("AU").zfill(5)))
        if o:
            return Resolved("order", o.id, o.code, raw)
    m = INTERNAL_RE.match(key)
    if m:
        p = m.group("p")
        model = {"L": Location, "A": Item, "S": StockLot}[p]
        obj = db.scalar(select(model).where(model.code == key))
        if obj:
            return Resolved({"L": "location", "A": "item", "S": "lot"}[p], obj.id, key, raw)
    if key.startswith("inventree:stockitem:"):  # InvenTree-Bestand -> genau dieser Bestand (Lot), sonst der Artikel
        from ..models import StockMovement

        lot_id = db.scalar(select(StockMovement.lot_id).where(StockMovement.ref == key, StockMovement.lot_id.is_not(None))
                           .order_by(StockMovement.id))
        lot = db.get(StockLot, lot_id) if lot_id else None
        if lot is not None and lot.status not in ("lost", "used"):
            return Resolved("lot", lot.id, key, raw)
    lc = db.scalar(select(LotCode).where(LotCode.code == key))
    if lc:
        return Resolved("lot", lc.lot_id, key, raw)
    alias = db.scalar(select(LocationAlias).where(LocationAlias.code == key))
    if alias:
        return Resolved("location", alias.location_id, key, raw)
    ic = db.scalar(select(ItemCode).where(ItemCode.code == key))
    if ic:
        return Resolved("item", ic.item_id, key, raw)
    if key.startswith("inventree:stockitem:"):
        ic = db.scalar(select(ItemCode).where(ItemCode.code == key))
        if ic:
            return Resolved("item", ic.item_id, key, raw)
    for rule in _regex_mappings(db):
        try:
            mm = re.search(rule["pattern"], raw.strip())
        except re.error:
            continue
        if mm:
            mapped = f"{rule.get('prefix', '')}{mm.group(1) if mm.groups() else mm.group(0)}"
            if mapped.upper() != key:
                r = resolve(db, mapped)
                if r.kind != "unknown":
                    return r
    # EAN/MPN direkt am Artikel
    item = db.scalar(select(Item).where(Item.mpn != "", Item.mpn == raw.strip()))
    if item:
        return Resolved("item", item.id, key, raw)
    # Etikett einer Lieferantentüte (DigiKey/Mouser/LCSC/TME): über Bestellnummer oder Hersteller-Nr. zuordnen
    from .labelparse import parse_label

    lab = parse_label(raw)
    if lab:
        from ..models import SupplierPart
        from .providers import normalize_mpn

        if lab["sku"]:
            want = normalize_mpn(lab["sku"])
            for sp in db.scalars(select(SupplierPart).where(SupplierPart.sku != "")):
                if normalize_mpn(sp.sku) == want:
                    return Resolved("item", sp.item_id, key, raw)
        if lab["mpn"]:
            want = normalize_mpn(lab["mpn"])
            for iid, mpn in db.execute(select(Item.id, Item.mpn).where(Item.mpn != "")):
                if normalize_mpn(mpn) == want:
                    return Resolved("item", iid, key, raw)
        return Resolved("label", None, key, raw)
    return Resolved("unknown", None, key, raw)


def add_location_alias(db: Session, location_id: int, raw: str, source: str = "manuell") -> LocationAlias:
    key = normalize(raw)
    existing = db.scalar(select(LocationAlias).where(LocationAlias.code == key))
    if existing:
        if existing.location_id != location_id:
            existing.location_id = location_id
            existing.source = source
        return existing
    a = LocationAlias(location_id=location_id, code=key, source=source)
    db.add(a)
    db.flush()
    return a


def add_item_code(db: Session, item_id: int, raw: str, kind: str = "barcode", source: str = "manuell") -> ItemCode:
    key = normalize(raw)
    existing = db.scalar(select(ItemCode).where(ItemCode.code == key))
    if existing:
        existing.item_id = item_id
        return existing
    c = ItemCode(item_id=item_id, code=key, kind=kind, source=source)
    db.add(c)
    db.flush()
    return c


class CodeConflict(ValueError):
    """Der Code gehört schon zu etwas anderem."""


def describe_owner(db: Session, r: Resolved) -> str:
    if r.kind == "location":
        o = db.get(Location, r.id)
        return f"dem Lagerort „{o.name}“" if o else "einem Lagerort"
    if r.kind == "item":
        o = db.get(Item, r.id)
        return f"dem Artikel „{o.name}“" if o else "einem Artikel"
    if r.kind == "lot":
        o = db.get(StockLot, r.id)
        return f"dem Bestand {o.code} („{o.item.name}“)" if o else "einem Bestand"
    return "etwas anderem"


def check_free(db: Session, raw: str, kind: str, target_id: int) -> str:
    """Prüft, ob ein Fremdcode frei ist. Gibt den Schlüssel zurück, sonst CodeConflict.

    Schon vorhanden am selben Ziel ist in Ordnung. Eigene Codes (L-/A-/S-) und Codes, die schon etwas anderem
    gehören, werden nicht stillschweigend umgehängt.
    """
    key = normalize(raw)
    if not key:
        raise CodeConflict("Der Code ist leer.")
    r = resolve(db, key)
    if r.kind in ("unknown", "label"):
        return key
    if r.kind == kind and r.id == target_id:
        return key
    if INTERNAL_RE.match(key):
        raise CodeConflict(f"{key} ist ein eigener DaLiegt’s-Code und kann nicht als Fremdcode dienen.")
    raise CodeConflict(f"Dieser Code gehört schon zu {describe_owner(db, r)}. Dort zuerst entfernen.")


def add_lot_code(db: Session, lot_id: int, raw: str, source: str = "manuell") -> LotCode:
    key = normalize(raw)
    existing = db.scalar(select(LotCode).where(LotCode.code == key))
    if existing:
        existing.lot_id = lot_id
        return existing
    c = LotCode(lot_id=lot_id, code=key, source=source)
    db.add(c)
    db.flush()
    return c


def list_foreign(db: Session, q: str = "") -> list[dict]:
    """Alle Fremdcodes (Lagerort, Artikel, Bestand) mit Ziel – für die Übersichtsseite."""
    rows: list[dict] = []
    for a in db.scalars(select(LocationAlias)):
        rows.append({"id": a.id, "kind": "location", "code": a.code, "source": a.source, "target": a.location_id,
                     "label": (db.get(Location, a.location_id).name if db.get(Location, a.location_id) else f"Ort {a.location_id}"), "url": f"/orte/{a.location_id}"})
    for c in db.scalars(select(ItemCode)):
        it = db.get(Item, c.item_id)
        rows.append({"id": c.id, "kind": "item", "code": c.code, "source": c.source, "target": c.item_id,
                     "label": it.name if it else "?", "url": f"/artikel/{c.item_id}"})
    for c in db.scalars(select(LotCode)):
        lot = db.get(StockLot, c.lot_id)
        rows.append({"id": c.id, "kind": "lot", "code": c.code, "source": c.source, "target": c.lot_id,
                     "label": f"{lot.item.name} · {lot.code}" if lot else "?", "url": f"/artikel/{lot.item_id}#lot-{lot.id}" if lot else "#"})
    if q.strip():
        n = q.strip().lower()
        rows = [r for r in rows if n in r["code"].lower() or n in r["label"].lower()]
    rows.sort(key=lambda r: (r["kind"], r["label"].lower(), r["code"]))
    return rows
