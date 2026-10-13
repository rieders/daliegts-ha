"""Aufträge (v0.9): Materialliste, Reservierung, Fehlteile -> Einkaufsliste, schrittweise Entnahme.

Reservierung wird nicht gespeichert, sondern immer aus den offenen Auftragspositionen berechnet:
Bestand eines Artikels wird den aktiven Aufträgen in Reihenfolge (Priorität, dann Anlagedatum) zugeteilt.
Was nicht gedeckt ist, ist Fehlmenge. Kommt Ware herein, deckt sie automatisch den ältesten offenen Bedarf.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ORDER_ACTIVE, Item, Order, OrderLine, ShoppingItem, StockLot
from . import locations as locsvc
from . import stock as stocksvc
from .codes import next_code, resolve


def create_order(db: Session, title: str, user_id: int | None = None, **kw) -> Order:
    title = (title or "").strip()[:200]
    if not title:
        raise ValueError("Bitte einen Titel angeben.")
    o = Order(code=next_code(db, "order"), title=title, created_by=user_id, **kw)
    db.add(o)
    db.flush()
    return o


def add_line(db: Session, order: Order, item: Item, qty: float, note: str = "") -> OrderLine:
    if qty <= 0:
        raise ValueError("Menge muss größer als 0 sein.")
    for ln in order.lines:  # gleicher Artikel: Menge erhöhen statt Doppelzeile
        if ln.item_id == item.id:
            ln.qty_required += qty
            if note and note not in ln.note:
                ln.note = (ln.note + ", " + note).strip(", ")[:255]
            db.flush()
            return ln
    ln = OrderLine(order_id=order.id, item_id=item.id, qty_required=qty, note=note[:255], sort=len(order.lines))
    db.add(ln)
    db.flush()
    db.refresh(order)
    return ln


# ------------------------------------------------------------------ Reservierung
def _stock(item: Item) -> float:
    lots = [lt for lt in item.lots if lt.status not in ("lost", "used")]
    if item.stock_mode == "exact":
        return sum(lt.quantity or 0 for lt in lots)
    # grob/ohne Menge: vorhanden = beliebig viel, leer = nichts
    return float("inf") if any((lt.coarse or "ok") != "empty" for lt in lots) else 0.0


@dataclass
class LineState:
    covered: float = 0.0
    short: float = 0.0


@dataclass
class ItemReservation:
    stock: float = 0.0
    reserved: float = 0.0  # offener Bedarf aller aktiven Aufträge
    orders: list = field(default_factory=list)  # (Order, offene Menge)

    @property
    def free(self) -> float:
        return max(0.0, self.stock - self.reserved) if self.stock != float("inf") else float("inf")


def _active_lines(db: Session, item_ids: set[int] | None = None) -> list[OrderLine]:
    q = select(OrderLine).join(Order).where(Order.status.in_(ORDER_ACTIVE))
    if item_ids is not None:
        q = q.where(OrderLine.item_id.in_(item_ids))
    lines = [ln for ln in db.scalars(q) if ln.remaining > 0]
    return sorted(lines, key=lambda ln: (-(ln.order.priority or 0), ln.order.created_at, ln.order_id, ln.id))


def allocate(db: Session, item_ids: set[int] | None = None) -> dict[int, LineState]:
    """Zuteilung des Bestands an die offenen Positionen (Priorität, dann älteste zuerst)."""
    out: dict[int, LineState] = {}
    left: dict[int, float] = {}
    for ln in _active_lines(db, item_ids):
        if ln.item_id not in left:
            left[ln.item_id] = _stock(ln.item)
        have = left[ln.item_id]
        cov = min(ln.remaining, have)
        left[ln.item_id] = have - cov
        out[ln.id] = LineState(covered=cov, short=ln.remaining - cov)
    return out


def reservation(db: Session, item: Item) -> ItemReservation:
    r = ItemReservation(stock=_stock(item))
    for ln in _active_lines(db, {item.id}):
        r.reserved += ln.remaining
        r.orders.append((ln.order, ln.remaining))
    return r


def order_state(db: Session, order: Order) -> dict:
    alloc = allocate(db, {ln.item_id for ln in order.lines})
    states = {ln.id: alloc.get(ln.id, LineState()) for ln in order.lines}
    req = sum(ln.qty_required for ln in order.lines)
    iss = sum(min(ln.qty_issued, ln.qty_required) for ln in order.lines)
    return {"lines": states, "short_lines": sum(1 for s in states.values() if s.short > 0) if order.active else 0,
            "progress": round(iss * 100 / req) if req else 0, "done": bool(order.lines) and all(ln.remaining <= 0 for ln in order.lines)}


# ------------------------------------------------------------------ Fehlteile -> Einkaufsliste
def sync_shortfalls(db: Session, order: Order, user_id: int | None = None) -> int:
    """Für jede Fehlmenge einen Einkaufslisten-Eintrag „für Auftrag …“ anlegen bzw. anpassen. Rückgabe: neue/erhöhte Einträge."""
    if not order.active:
        return 0
    state = order_state(db, order)["lines"]
    changed = 0
    for ln in order.lines:
        short = state[ln.id].short
        entries = list(db.scalars(select(ShoppingItem).where(ShoppingItem.order_id == order.id, ShoppingItem.item_id == ln.item_id,
                                                            ShoppingItem.status.in_(("open", "ordered")))))
        on_list = sum(e.quantity for e in entries)
        if short > on_list + 1e-9:
            open_e = next((e for e in entries if e.status == "open"), None)
            if open_e is not None:
                open_e.quantity += short - on_list
            else:
                db.add(ShoppingItem(item_id=ln.item_id, quantity=short - on_list, order_id=order.id, created_by=user_id,
                                    note=f"Fehlteil für Auftrag {order.code} – {order.title}"[:500]))
            changed += 1
    db.flush()
    return changed


def release_open_shortfalls(db: Session, order: Order) -> int:
    """Auftrag storniert/erledigt: noch nicht bestellte Fehlteil-Einträge entfernen."""
    n = 0
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.order_id == order.id, ShoppingItem.status == "open")):
        db.delete(e)
        n += 1
    db.flush()
    return n


# ------------------------------------------------------------------ Entnehmen / Zurückgeben
def pick_lots(db: Session, item: Item) -> list[StockLot]:
    """Bestände zum Entnehmen, sortiert nach Lagerort (Pickliste)."""
    tc = locsvc.TreeCache(db)
    lots = [lt for lt in item.lots if lt.status not in ("lost", "used") and (item.stock_mode != "exact" or (lt.quantity or 0) > 0)]
    return sorted(lots, key=lambda lt: locsvc.natural_key(tc.path(lt.location_id) if lt.location_id else "~"))


def issue(db: Session, line: OrderLine, qty: float, lot: StockLot | None = None, user_id: int | None = None) -> float:
    """Menge für die Position entnehmen – aus einem gewählten Bestand oder der Reihe nach aus der Pickliste."""
    order = line.order
    if not order.active:
        raise ValueError("Der Auftrag ist nicht offen.")
    if qty <= 0:
        raise ValueError("Menge muss größer als 0 sein.")
    item = line.item
    ref = f"Auftrag {order.code}"
    note = order.title[:120]
    if lot is not None:
        if lot.item_id != item.id:
            raise ValueError("Bestand gehört zu einem anderen Artikel.")
        stocksvc.remove_stock(db, lot, qty, user_id, note=note, ref=ref)
    else:
        lots = pick_lots(db, item)
        if item.stock_mode == "exact":
            have = sum(lt.quantity or 0 for lt in lots)
            if qty > have + 1e-9:
                raise ValueError(f"Nur {have:g} {item.unit} von „{item.name}“ auf Lager.")
            rest = qty
            for lt in lots:
                take = min(rest, lt.quantity or 0)
                if take > 0:
                    stocksvc.remove_stock(db, lt, take, user_id, note=note, ref=ref)
                    rest -= take
                if rest <= 1e-9:
                    break
        elif lots:
            stocksvc.remove_stock(db, lots[0], qty, user_id, note=note, ref=ref)
        else:
            raise ValueError(f"„{item.name}“ ist nicht auf Lager.")
    line.qty_issued = (line.qty_issued or 0) + qty
    if order.status == "offen":
        order.status = "in_arbeit"
    db.flush()
    return qty


def return_parts(db: Session, line: OrderLine, qty: float, location_id: int | None, user_id: int | None = None) -> None:
    if qty <= 0 or qty > (line.qty_issued or 0) + 1e-9:
        raise ValueError(f"Zurückgeben geht höchstens mit der entnommenen Menge ({line.qty_issued:g}).")
    stocksvc.add_stock(db, line.item, location_id, qty, user_id, note=f"Rückgabe aus Auftrag {line.order.code}",
                       ref=f"Auftrag {line.order.code}")
    line.qty_issued -= qty
    db.flush()


def set_status(db: Session, order: Order, status: str) -> None:
    if status not in ("offen", "in_arbeit", "erledigt", "storniert"):
        raise ValueError("Unbekannter Status.")
    order.status = status
    order.closed_at = datetime.now().replace(microsecond=0) if status in ("erledigt", "storniert") else None
    if status in ("erledigt", "storniert"):
        release_open_shortfalls(db, order)
    db.flush()


# ------------------------------------------------------------------ Stückliste einfügen
@dataclass
class BomRow:
    raw: str
    qty: float
    ref: str
    item: Item | None


_QTY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:x|×|stk\.?|stück)?\s+(.+)$", re.I)


def _find_item(db: Session, key: str) -> Item | None:
    key = key.strip()
    if not key:
        return None
    r = resolve(db, key)
    if r.kind == "item":
        return db.get(Item, r.id)
    if r.kind == "lot":
        lot = db.get(StockLot, r.id)
        return lot.item if lot else None
    for col in (Item.mpn, Item.name):
        it = db.scalar(select(Item).where(Item.active.is_(True), col.ilike(key)))
        if it is not None:
            return it
    return None


def parse_bom(db: Session, text: str) -> list[BomRow]:
    """Zeilen wie „10 BC547“, „4x 100nF 0805“ oder CSV aus KiCad/Excel (Spalten Menge/Qty, Wert/Value/Artikel, Referenz)."""
    text = (text or "").strip()
    if not text:
        return []
    first = text.splitlines()[0].lower()
    rows: list[BomRow] = []
    if any(sep in first for sep in (";", ",", "\t")) and any(h in first for h in ("menge", "qty", "quantity", "anzahl")):
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
        for rec in csv.DictReader(io.StringIO(text), dialect=dialect):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in rec.items()}
            qraw = next((low[k] for k in ("menge", "qty", "quantity", "anzahl") if low.get(k)), "1")
            name = next((low[k] for k in ("artikel", "code", "mpn", "value", "wert", "name", "bezeichnung", "part") if low.get(k)), "")
            ref = next((low[k] for k in ("reference", "referenz", "references", "designator", "ref") if low.get(k)), "")
            try:
                qty = float(qraw.replace(",", "."))
            except ValueError:
                qty = 1.0
            if name:
                rows.append(BomRow(name, qty, ref[:255], _find_item(db, name)))
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _QTY_RE.match(line)
        qty, name = (float(m.group(1).replace(",", ".")), m.group(2)) if m else (1.0, line)
        rows.append(BomRow(name.strip(), qty, "", _find_item(db, name)))
    return rows


def pick_list(db: Session, order: Order) -> list[dict]:
    """Pickliste: offene Positionen nach Lagerort sortiert (ein Gang durchs Lager)."""
    tc = locsvc.TreeCache(db)
    out = []
    for ln in order.lines:
        if ln.remaining <= 0:
            continue
        lots = pick_lots(db, ln.item)
        where = [(tc.path(lt.location_id) if lt.location_id else "ohne Ort", lt) for lt in lots]
        out.append({"line": ln, "where": where, "sort": where[0][0] if where else "~"})
    return sorted(out, key=lambda r: locsvc.natural_key(r["sort"]))


# ------------------------------------------------------------------ Auslager-Assistent (v0.11)
@dataclass
class PickStep:
    key: str            # „Position-Bestand“, stabil für Überspringen
    line: OrderLine
    lot: StockLot
    qty: float          # so viel aus diesem Fach nehmen
    path: str
    have: float | None  # Bestand im Fach (None bei grober Führung)


def pick_steps(db: Session, order: Order, skip: set[str] | None = None) -> tuple[list[PickStep], list[OrderLine]]:
    """Alle noch offenen Entnahmen als Schritte in Laufreihenfolge (nach Lagerort sortiert).
    Eine Position mit Bestand in mehreren Fächern ergibt mehrere Schritte. Rückgabe: (Schritte, Positionen ohne Bestand)."""
    skip = skip or set()
    tc = locsvc.TreeCache(db)
    steps: list[PickStep] = []
    missing: list[OrderLine] = []
    for ln in order.lines:
        rest = ln.remaining
        if rest <= 0:
            continue
        lots = pick_lots(db, ln.item)
        exact = ln.item.stock_mode == "exact"
        for lt in lots:
            if rest <= 1e-9:
                break
            take = min(rest, lt.quantity or 0) if exact else rest
            if take <= 0:
                continue
            key = f"{ln.id}-{lt.id}"
            rest -= take
            if key in skip:
                continue
            steps.append(PickStep(key, ln, lt, take, tc.path(lt.location_id) if lt.location_id else "ohne Lagerort",
                                  lt.quantity if exact else None))
        if rest > 1e-9 or not lots:
            missing.append(ln)
    steps.sort(key=lambda s: (locsvc.natural_key(s.path), s.line.sort, s.line.id))
    return steps, missing


def module_map(db: Session, location_id: int | None) -> dict | None:
    """Mini-Ansicht für den Assistenten: das Lagermöbel mit allen Fächern, das Zielfach markiert."""
    from ..models import Location

    loc = db.get(Location, location_id) if location_id else None
    target = loc
    cur = loc
    module = None
    while cur is not None:
        if cur.grid_rows or cur.wall_x is not None:
            module = cur
            break
        target = cur
        cur = cur.parent
    if module is None and loc is not None and loc.parent is not None and len([c for c in loc.parent.children if c.type != "abteil"]) >= 2:
        module, target = loc.parent, loc  # nicht angeordnetes Möbel: Nachbarfächer der Reihe nach
    if module is None or not module.children:
        return None
    if target is None or target.parent_id != module.id:
        target = loc
    kids = [c for c in module.children if c.type != "abteil"]
    if module.grid_rows:
        cells = [{"id": c.id, "name": c.name, "r": c.grid_row, "c": c.grid_col, "rs": c.grid_rowspan or 1, "cs": c.grid_colspan or 1}
                 for c in kids if c.grid_row]
        rows, cols = module.grid_rows, module.grid_cols or 1
    else:  # Raster noch nicht festgelegt: der Reihe nach, 4 je Reihe
        kids = sorted(kids, key=lambda c: locsvc.natural_key(c.name))
        cols = 4 if len(kids) >= 8 else max(1, len(kids))
        rows = -(-len(kids) // cols)
        cells = [{"id": c.id, "name": c.name, "r": i // cols + 1, "c": i % cols + 1, "rs": 1, "cs": 1} for i, c in enumerate(kids)]
    return {"module": module, "target": target.id if target else None, "rows": rows, "cols": cols, "cells": cells}
