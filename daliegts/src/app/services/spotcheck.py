"""Stichproben-Inventur (v0.6): zufällige Fächer eines Raums prüfen, ohne dieselben immer wieder zu ziehen.

Rotation: Fächer, die noch nie oder am längsten nicht geprüft wurden, kommen zuerst dran. Unter gleich
„alten“ Fächern entscheidet der Zufall. So ist jede Stichprobe zufällig, aber erst wenn alle Fächer des
Raums einmal dran waren, kommt ein Fach ein zweites Mal.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Location, StockLot
from . import locations as locsvc
from . import stock as stocksvc

NEVER = datetime(1970, 1, 1)


def spots(db: Session, root_id: int, include_empty: bool = False) -> list[int]:
    """Prüfbare Lagerplätze unter einem Ort: Fächer ohne weitere Unterorte (Abteile zählen zum Fach)."""
    tc = locsvc.TreeCache(db)
    ids = [root_id, *tc.descendants(root_id)]
    occupied = set(db.scalars(select(StockLot.location_id).where(StockLot.location_id.in_(ids), StockLot.status.notin_(("lost", "used")))))
    out = []
    for lid in ids:
        node = tc.nodes[lid]
        if node.type == "abteil" or lid == root_id and tc.children.get(lid):
            continue
        kids = [k for k in tc.children.get(lid, []) if tc.nodes[k].type != "abteil"]
        if kids:
            continue  # Möbel/Bereich: geprüft werden seine Fächer
        has = lid in occupied or any(k in occupied for k in tc.children.get(lid, []))
        if has or (include_empty and node.type in ("fach", "kiste", "sortierbox")):
            out.append(lid)
    return out


def draw(db: Session, root_id: int, n: int = 10, include_empty: bool = False, rng: random.Random | None = None) -> list[int]:
    rng = rng or random.Random()
    ids = spots(db, root_id, include_empty)
    if not ids:
        return []
    last = dict(db.execute(select(Location.id, Location.last_checked_at).where(Location.id.in_(ids))).all())
    rng.shuffle(ids)  # Zufall unter Gleichen …
    ids.sort(key=lambda i: last.get(i) or NEVER)  # … aber die am längsten ungeprüften zuerst (stabile Sortierung)
    chosen = ids[:max(1, n)]
    tc = locsvc.TreeCache(db)
    return sorted(chosen, key=lambda i: locsvc.natural_key(tc.path(i)))


@dataclass
class Coverage:
    total: int = 0
    checked: int = 0  # mindestens einmal geprüft
    oldest: datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def percent(self) -> int:
        return round(self.checked * 100 / self.total) if self.total else 0


def coverage(db: Session, root_id: int, include_empty: bool = False) -> Coverage:
    ids = spots(db, root_id, include_empty)
    last = [t for (t,) in db.execute(select(Location.last_checked_at).where(Location.id.in_(ids)))] if ids else []
    seen = [t for t in last if t]
    return Coverage(total=len(ids), checked=len(seen), oldest=min(seen) if len(seen) == len(ids) and seen else None)


def lots_in(db: Session, loc_id: int) -> list[StockLot]:
    tc = locsvc.TreeCache(db)
    ids = [loc_id, *tc.descendants(loc_id)]
    lots = db.scalars(select(StockLot).where(StockLot.location_id.in_(ids), StockLot.status.notin_(("lost", "used")))).all()
    return sorted(lots, key=lambda x: locsvc.natural_key(x.item.name))


def mark_checked(db: Session, loc: Location, when: datetime | None = None) -> None:
    when = when or datetime.now()
    loc.last_checked_at = when
    for k in loc.children:
        if k.type == "abteil":
            k.last_checked_at = when
    if loc.type == "abteil" and loc.parent is not None:
        loc.parent.last_checked_at = when


def check_spot(db: Session, loc: Location, values: dict[int, str], user_id: int | None) -> int:
    """Ein Fach als geprüft buchen. values: lot_id -> gezählte Menge (exakt) bzw. Ampelstufe (grob).
    Nur Abweichungen und offene Prüfungen erzeugen eine Buchung. Rückgabe: Anzahl Korrekturen."""
    changed = 0
    for lot in lots_in(db, loc.id):
        raw = values.get(lot.id)
        if lot.item.stock_mode == "exact" and raw not in (None, ""):
            try:
                qty = float(str(raw).replace(",", "."))
            except ValueError:
                raise ValueError(f"„{raw}“ ist keine Zahl ({lot.item.name}).")
            if qty < 0:
                raise ValueError("Mengen dürfen nicht negativ sein.")
            if abs(qty - (lot.quantity or 0)) > 1e-9:
                stocksvc.count_stock(db, lot, qty, user_id, "Stichproben-Inventur")
                changed += 1
                continue
        elif lot.item.stock_mode == "coarse" and raw and raw != lot.coarse:
            stocksvc.set_coarse(db, lot, str(raw), user_id, "Stichproben-Inventur")
            changed += 1
            continue
        if lot.needs_review:
            stocksvc.confirm_lot(db, lot, user_id)
    mark_checked(db, loc)
    db.flush()
    return changed
