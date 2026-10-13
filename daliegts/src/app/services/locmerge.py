"""Lagerorte zusammenführen und Fächer übertragen (v0.8).

Typischer Fall nach dem Import: Die alten Orte aus Part-DB/InvenTree („Wandregal oben104“) beschreiben dieselben
Schubladen wie die neu angelegten Magazine („Regalwand / Kleinteilemagazin2 / Fach4“). Zusammenführen verschiebt
Inhalt, Unterorte und Etiketten des alten Orts in den neuen und löscht den alten – alte Etiketten funktionieren weiter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Location, LocationAlias, StockLot, StockMovement
from . import locations as locsvc
from .codes import add_location_alias


@dataclass
class MergeStats:
    locations: int = 0
    lots: int = 0
    merged_lots: int = 0
    labels: int = 0
    notes: list = field(default_factory=list)


def _same_kind_lot(dst_lots: list[StockLot], lot: StockLot) -> StockLot | None:
    """Gleicher Artikel ohne Seriennummer/Charge im Ziel -> Mengen zusammenlegen statt zwei Bestände im selben Fach."""
    if lot.serial or lot.batch or lot.item.stock_mode != "exact":
        return None
    for d in dst_lots:
        if d.item_id == lot.item_id and not d.serial and not d.batch and d.status == lot.status and d.id != lot.id:
            return d
    return None


def merge_into(db: Session, src: Location, dst: Location, stats: MergeStats | None = None) -> MergeStats:
    """src (samt Unterorten) in dst aufgehen lassen und src löschen."""
    stats = stats or MergeStats()
    if src.id == dst.id:
        raise ValueError("Quelle und Ziel sind derselbe Lagerort.")
    tc = locsvc.TreeCache(db)
    if dst.id in tc.descendants(src.id):
        raise ValueError(f"„{dst.name}“ liegt innerhalb von „{src.name}“ – umgekehrt zusammenführen.")
    # 1. Inhalt
    dst_lots = list(db.scalars(select(StockLot).where(StockLot.location_id == dst.id)))
    for lot in list(db.scalars(select(StockLot).where(StockLot.location_id == src.id))):
        twin = _same_kind_lot(dst_lots, lot)
        if twin is not None:
            twin.quantity = (twin.quantity or 0) + (lot.quantity or 0)
            twin.needs_review = twin.needs_review or lot.needs_review
            for mv in db.scalars(select(StockMovement).where(StockMovement.lot_id == lot.id)):
                mv.lot_id = twin.id
            db.delete(lot)
            stats.merged_lots += 1
        else:
            lot.location_id = dst.id
            dst_lots.append(lot)
            stats.lots += 1
    # 2. Etiketten: externe Codes umhängen, eigener Code bleibt als Alias gültig
    for a in list(db.scalars(select(LocationAlias).where(LocationAlias.location_id == src.id))):
        a.location_id = dst.id
        stats.labels += 1
    db.flush()
    add_location_alias(db, dst.id, src.code, "zusammengeführt")
    stats.labels += 1
    # 3. Unterorte: gleichnamige zusammenführen, andere umhängen
    dst_kids = {k.name.strip().lower(): k for k in dst.children}
    for kid in list(src.children):
        same = dst_kids.get(kid.name.strip().lower())
        if same is not None:
            merge_into(db, kid, same, stats)
        else:
            kid.parent_id = dst.id
            kid.wall_x = kid.wall_y = None  # Platz auf der alten Fläche gilt nicht mehr
    db.flush()
    db.refresh(src)
    for a in list(db.scalars(select(LocationAlias).where(LocationAlias.location_id == src.id))):
        a.location_id = dst.id  # der eigene Code (s. o.) darf nicht mit src gelöscht werden
    db.delete(src)
    db.flush()
    stats.locations += 1
    return stats


# ------------------------------------------------------------------ Fächer 1:1 übertragen
def natural_sources(db: Session, loc: Location, prefix: str | None = None) -> list[Location]:
    """Quelle für die Übertragung: die Fächer des Orts – oder, wenn er keine hat, gleichnamige Geschwister
    („Wandregal oben101“ … „Wandregal oben133“ bei Präfix „Wandregal oben1“)."""
    if prefix:
        pool = loc.parent.children if loc.parent is not None else list(db.scalars(select(Location).where(Location.parent_id.is_(None))))
        items = [x for x in pool if x.name.lower().startswith(prefix.lower()) and x.type != "abteil"]
    else:
        items = [x for x in loc.children if x.type != "abteil"]
    return sorted(items, key=lambda x: locsvc.natural_key(x.name))


def default_prefix(name: str) -> str:
    """„Wandregal oben104“ -> „Wandregal oben1“ (letzte zwei Ziffern = Fachnummer)."""
    m = re.match(r"^(.*?)(\d+)$", name.strip())
    if not m:
        return name
    digits = m.group(2)
    return m.group(1) + (digits[:-2] if len(digits) > 2 else "")


def target_cells(furniture: Location) -> list[Location]:
    cells = [c for c in furniture.children if c.grid_row]
    if cells:
        return sorted(cells, key=lambda c: (c.grid_row, c.grid_col))
    return sorted((c for c in furniture.children if c.type != "abteil"), key=lambda c: locsvc.natural_key(c.name))


def _number(name: str, prefix: str = "") -> int | None:
    rest = name[len(prefix):] if prefix and name.lower().startswith(prefix.lower()) else name
    m = re.search(r"(\d+)\s*$", rest)
    return int(m.group(1)) if m else None


def pairing_mode(sources: list[Location], targets: list[Location], prefix: str = "", offset: int = 0) -> str:
    """„nummer“: Fachnummer im alten Namen bestimmt das Zielfach (oben104 -> Fach 4), sonst „reihe“ (der Reihe nach)."""
    nums = [_number(s.name, prefix) for s in sources]
    if sources and all(n is not None for n in nums) and len(set(nums)) == len(nums):
        base = 0 if min(nums) == 0 else 1
        if max(nums) - base + offset < len(targets):
            return "nummer"
    return "reihe"


def pairing(sources: list[Location], targets: list[Location], offset: int = 0, prefix: str = "",
            mode: str = "auto") -> list[tuple[Location, Location | None]]:
    if mode == "auto":
        mode = pairing_mode(sources, targets, prefix, offset)
    out = []
    nums = [_number(s.name, prefix) for s in sources]
    base = 0 if mode == "nummer" and min(nums) == 0 else 1
    for i, s in enumerate(sources):
        j = (nums[i] - base if mode == "nummer" else i) + offset
        out.append((s, targets[j] if 0 <= j < len(targets) else None))
    return out


def content_count(db: Session, loc: Location) -> int:
    tc = locsvc.TreeCache(db)
    ids = [loc.id, *tc.descendants(loc.id)]
    return len(db.scalars(select(StockLot.id).where(StockLot.location_id.in_(ids))).all())
