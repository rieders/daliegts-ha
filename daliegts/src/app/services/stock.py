"""Bestandsbuchungen: einlagern, entnehmen, umlagern, zählen – immer mit Historie und Benutzer."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, StockLot, StockMovement
from .codes import next_code


class StockError(ValueError):
    pass


def _checked(lot: StockLot) -> None:
    """Gezählt/bestätigt = Fach geprüft (für die Rotation der Stichproben-Inventur)."""
    loc = lot.location
    if loc is not None:
        loc.last_checked_at = datetime.now()
        if loc.type == "abteil" and loc.parent is not None:
            loc.parent.last_checked_at = loc.last_checked_at


def _move(db: Session, item: Item, lot: StockLot | None, kind: str, delta: float, user_id: int | None,
          frm: int | None = None, to: int | None = None, note: str = "", ref: str = "") -> StockMovement:
    mv = StockMovement(item_id=item.id, lot_id=lot.id if lot else None, kind=kind, delta=delta,
                       quantity_after=lot.quantity if lot else None, from_location_id=frm, to_location_id=to,
                       user_id=user_id, note=note, ref=ref)
    db.add(mv)
    return mv


def find_lot(db: Session, item_id: int, location_id: int | None, batch: str = "", serial: str = "") -> StockLot | None:
    q = select(StockLot).where(StockLot.item_id == item_id, StockLot.batch == batch, StockLot.serial == serial,
                               StockLot.status.notin_(["lost", "used"]))
    q = q.where(StockLot.location_id.is_(None)) if location_id is None else q.where(StockLot.location_id == location_id)
    return db.scalars(q.order_by(StockLot.id)).first()


def new_lot(db: Session, item: Item, location_id: int | None, quantity: float = 0, **kw) -> StockLot:
    lot = StockLot(item_id=item.id, location_id=location_id, quantity=quantity, code=next_code(db, "lot"), **kw)
    db.add(lot)
    db.flush()
    return lot


def add_stock(db: Session, item: Item, location_id: int | None, qty: float, user_id: int | None = None,
              note: str = "", batch: str = "", serial: str = "", ref: str = "", coarse: str | None = None) -> StockLot:
    if qty < 0:
        raise StockError("Menge muss positiv sein.")
    if serial:
        qty = 1
        lot = new_lot(db, item, location_id, 1, serial=serial, batch=batch)
        _move(db, item, lot, "created", 1, user_id, to=location_id, note=note, ref=ref)
        return lot
    lot = find_lot(db, item.id, location_id, batch)
    created = lot is None
    if created:
        lot = new_lot(db, item, location_id, 0, batch=batch)
    lot.quantity = (lot.quantity or 0) + qty
    if coarse or item.stock_mode == "coarse":
        lot.coarse = coarse or "ok"
    lot.needs_refill = False
    _move(db, item, lot, "created" if created else "added", qty, user_id, to=location_id, note=note, ref=ref)
    db.flush()
    return lot


def remove_stock(db: Session, lot: StockLot, qty: float, user_id: int | None = None, note: str = "", ref: str = "",
                 allow_negative: bool = False) -> StockLot:
    if qty < 0:
        raise StockError("Menge muss positiv sein.")
    if lot.item.stock_mode == "exact" and not allow_negative and qty > (lot.quantity or 0):
        raise StockError(f"Nur {lot.quantity:g} {lot.item.unit} vorhanden.")
    lot.quantity = max(0.0, (lot.quantity or 0) - qty) if not allow_negative else (lot.quantity or 0) - qty
    _move(db, lot.item, lot, "removed", -qty, user_id, frm=lot.location_id, note=note, ref=ref)
    db.flush()
    return lot


def set_coarse(db: Session, lot: StockLot, level: str, user_id: int | None = None, note: str = "") -> StockLot:
    old = lot.coarse
    lot.coarse = level
    lot.needs_refill = level in ("empty", "low")
    _move(db, lot.item, lot, "counted", 0, user_id, to=lot.location_id, note=note or f"Ampel {old or '–'} → {level}")
    db.flush()
    return lot


def count_stock(db: Session, lot: StockLot, qty: float, user_id: int | None = None, note: str = "") -> StockLot:
    """Inventur: tatsächliche Menge setzen."""
    delta = qty - (lot.quantity or 0)
    lot.quantity = qty
    lot.needs_review = False
    _move(db, lot.item, lot, "counted", delta, user_id, to=lot.location_id, note=note or "Inventur")
    _checked(lot)
    db.flush()
    return lot


def confirm_lot(db: Session, lot: StockLot, user_id: int | None = None) -> StockLot:
    lot.needs_review = False
    _move(db, lot.item, lot, "counted", 0, user_id, to=lot.location_id, note="Inventur bestätigt")
    _checked(lot)
    db.flush()
    return lot


def move_stock(db: Session, lot: StockLot, to_location_id: int | None, qty: float | None = None, user_id: int | None = None,
               note: str = "") -> StockLot:
    """Umlagern. qty=None -> ganzes Lot verschieben, sonst Teilmenge in (ggf. neues) Ziel-Lot."""
    frm = lot.location_id
    if frm == to_location_id:
        return lot
    item = lot.item
    if qty is None or qty >= (lot.quantity or 0) or lot.serial:
        target = find_lot(db, item.id, to_location_id, lot.batch) if not lot.serial else None
        if target and target.id != lot.id and item.stock_mode == "exact":
            moved = lot.quantity or 0
            target.quantity = (target.quantity or 0) + moved
            target.needs_review = target.needs_review or lot.needs_review
            if lot.expiry and (target.expiry is None or lot.expiry < target.expiry):
                target.expiry = lot.expiry
            if lot.notes and lot.notes not in (target.notes or ""):
                target.notes = ((target.notes or "") + "\n" + lot.notes).strip()
            lot.quantity = 0
            _move(db, item, target, "moved", moved, user_id, frm=frm, to=to_location_id, note=note or f"zusammengeführt mit {lot.code}")
            _retire_lot(db, lot, target)
            db.flush()
            return target
        lot.location_id = to_location_id
        _move(db, item, lot, "moved", lot.quantity or 0, user_id, frm=frm, to=to_location_id, note=note)
        db.flush()
        return lot
    if qty <= 0:
        raise StockError("Menge muss positiv sein.")
    lot.quantity = (lot.quantity or 0) - qty
    target = find_lot(db, item.id, to_location_id, lot.batch) or new_lot(db, item, to_location_id, 0, batch=lot.batch)
    target.quantity = (target.quantity or 0) + qty
    _move(db, item, target, "moved", qty, user_id, frm=frm, to=to_location_id, note=note)
    db.flush()
    return target


def _retire_lot(db: Session, lot: StockLot, target: StockLot) -> None:
    """Löst ein Lot in ein anderes auf: Verweise umhängen, gedruckten Code weiter auflösbar halten."""
    from ..models import ExternalId, ItemCode, Loan

    for mv in db.scalars(select(StockMovement).where(StockMovement.lot_id == lot.id)):
        mv.lot_id = target.id
    for loan in db.scalars(select(Loan).where(Loan.lot_id == lot.id)):
        loan.lot_id = target.id
    for ext in db.scalars(select(ExternalId).where(ExternalId.entity == "lot", ExternalId.entity_id == lot.id)):
        ext.entity_id = target.id
    from ..models import LotCode

    for lc in db.scalars(select(LotCode).where(LotCode.lot_id == lot.id)):
        lc.lot_id = target.id
    db.flush()
    db.expire(lot, ["codes"])
    if not db.scalar(select(ItemCode).where(ItemCode.code == lot.code)):
        db.add(ItemCode(item_id=target.item_id, code=lot.code, kind="alter-bestand", source="zusammengeführt"))
    db.flush()
    db.delete(lot)


def move_location_contents(db: Session, from_location_id: int, to_location_id: int, user_id: int | None = None) -> int:
    lots = db.scalars(select(StockLot).where(StockLot.location_id == from_location_id)).all()
    for lot in lots:
        move_stock(db, lot, to_location_id, None, user_id, note="Inhalt umgelagert")
    return len(lots)
