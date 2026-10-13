"""Aufträge: Materialliste, Reservierung, Fehlteile, schrittweise Entnahme (v0.9)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ORDER_ACTIVE, ORDER_STATUS, Item, Order, OrderLine, ShoppingItem, StockLot
from ..security import check, require
from ..services import audit
from ..services import orders as osvc
from ..services.items import create_item
from ..web import flash, redirect, render

router = APIRouter()


def _order(db: Session, oid: int) -> Order:
    o = db.get(Order, oid)
    if o is None:
        raise HTTPException(404, "Auftrag nicht gefunden")
    return o


def _line(db: Session, lid: int) -> OrderLine:
    ln = db.get(OrderLine, lid)
    if ln is None:
        raise HTTPException(404, "Position nicht gefunden")
    return ln


def _num(v, default: float | None = None) -> float | None:
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise ValueError(f"„{v}“ ist keine Zahl.")


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def _after_lines(db: Session, o: Order, user_id) -> str:
    n = osvc.sync_shortfalls(db, o, user_id)
    return f" {n} Fehlteil(e) auf die Einkaufsliste gesetzt." if n else ""


@router.get("/auftraege")
def order_list(request: Request, status: str = "aktiv", db: Session = Depends(get_db), ctx=Depends(require("orders", 1))):
    q = select(Order).order_by(Order.created_at.desc())
    if status == "aktiv":
        q = q.where(Order.status.in_(ORDER_ACTIVE))
    elif status in ORDER_STATUS:
        q = q.where(Order.status == status)
    orders = list(db.scalars(q))
    states = {o.id: osvc.order_state(db, o) for o in orders}
    buy: dict[int, dict] = {}
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.order_id.in_([o.id for o in orders]), ShoppingItem.status.in_(("open", "ordered")))):
        d = buy.setdefault(e.order_id, {"open": 0, "ordered": 0})
        d[e.status] += 1
    return render(request, "orders.html", {"orders": orders, "states": states, "status": status, "today": date.today(), "buy": buy})


@router.post("/auftraege/neu")
async def order_create(request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 3))):
    f = await request.form()
    try:
        o = osvc.create_order(db, str(f.get("title") or ""), ctx.user_id, requester=str(f.get("requester") or "")[:120],
                              description=str(f.get("description") or ""), due=_date(f.get("due")))
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect("/auftraege")
    audit.log(db, ctx.user_id, "create", "order", o.id, f"Auftrag {o.code} {o.title} angelegt")
    db.commit()
    flash(request, f"Auftrag {o.code} angelegt – jetzt Bauteile hinzufügen.")
    return redirect(f"/auftraege/{o.id}")


@router.get("/auftraege/{oid}")
def order_detail(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 1))):
    o = _order(db, oid)
    st = osvc.order_state(db, o)
    shop = {e.item_id: e for e in db.scalars(select(ShoppingItem).where(ShoppingItem.order_id == o.id, ShoppingItem.status.in_(("open", "ordered"))))}
    lots = {ln.id: osvc.pick_lots(db, ln.item) for ln in o.lines}
    from ..services import walls
    from ..services.locations import TreeCache

    where = {lid: (w.id if (w := walls.wall_of(lts[0].location)) is not None else None) for lid, lts in lots.items() if lts and lts[0].location}
    take, blocked = {}, {}
    for ln in o.lines:
        cov = st["lines"][ln.id].covered
        stock = osvc._stock(ln.item)
        take[ln.id] = ln.remaining if cov == float("inf") or stock == float("inf") else (min(ln.remaining, cov) if cov > 0 else min(ln.remaining, stock))
        if o.active and st["lines"][ln.id].short > 0 and stock > cov:
            others = [x for x, _q in osvc.reservation(db, ln.item).orders if x.id != o.id]
            blocked[ln.id] = (stock, others)
    return render(request, "order_detail.html", {"o": o, "st": st, "shop": shop,
                                                 "shop_open": sum(1 for e in shop.values() if e.status == "open"),
                                                 "shop_ordered": sum(1 for e in shop.values() if e.status == "ordered"), "lots": lots, "tc": TreeCache(db), "where": where, "take": take, "blocked": blocked,
                                                 "picks": osvc.pick_list(db, o), "today": date.today()})


@router.get("/auftraege/{oid}/pickliste")
def order_picklist(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 1))):
    o = _order(db, oid)
    return render(request, "order_picklist.html", {"o": o, "picks": osvc.pick_list(db, o), "st": osvc.order_state(db, o), "today": date.today()})


@router.post("/auftraege/{oid}/bearbeiten")
async def order_edit(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    o = _order(db, oid)
    f = await request.form()
    title = str(f.get("title") or "").strip()
    if title:
        o.title = title[:200]
    o.requester = str(f.get("requester") or "")[:120]
    o.description = str(f.get("description") or "")
    o.due = _date(f.get("due"))
    try:
        o.priority = int(f.get("priority") or 0)
    except ValueError:
        pass
    db.commit()
    flash(request, "Gespeichert.")
    return redirect(f"/auftraege/{oid}")


@router.post("/auftraege/{oid}/status")
async def order_status(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    o = _order(db, oid)
    f = await request.form()
    new = str(f.get("status") or "")
    try:
        osvc.set_status(db, o, new)
        msg = ""
        if o.active:
            msg = _after_lines(db, o, ctx.user_id)
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect(f"/auftraege/{oid}")
    audit.log(db, ctx.user_id, "update", "order", o.id, f"Auftrag {o.code}: Status {ORDER_STATUS.get(new, new)}")
    db.commit()
    flash(request, f"Auftrag {o.code}: {ORDER_STATUS.get(new, new)}." + msg
          + (" Reservierungen freigegeben, offene Fehlteile von der Einkaufsliste genommen." if new in ("erledigt", "storniert") else ""))
    return redirect(f"/auftraege/{oid}")


@router.post("/auftraege/{oid}/loeschen")
def order_delete(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 4))):
    o = _order(db, oid)
    osvc.release_open_shortfalls(db, o)
    code = o.code
    audit.log(db, ctx.user_id, "delete", "order", o.id, f"Auftrag {o.code} {o.title} gelöscht")
    db.delete(o)
    db.commit()
    flash(request, f"Auftrag {code} gelöscht (Entnahmen bleiben in der Historie).")
    return redirect("/auftraege")


@router.post("/auftraege/{oid}/position")
async def line_add(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    o = _order(db, oid)
    f = await request.form()
    item = None
    raw = str(f.get("item_id") or "").strip()
    if raw.isdigit():
        item = db.get(Item, int(raw))
    if item is None:
        item = osvc._find_item(db, str(f.get("item") or ""))
    try:
        if item is None:
            name = str(f.get("item") or "").strip()
            if not name:
                raise ValueError("Bitte einen Artikel wählen.")
            if not f.get("neu"):
                raise ValueError(f"Artikel „{name}“ nicht gefunden. Haken „als neuen Artikel anlegen“ setzen, um ihn anzulegen.")
            check(ctx, "items", 3)
            item = create_item(db, name, ctx.user_id)
        ln = osvc.add_line(db, o, item, _num(f.get("qty"), 1.0), str(f.get("note") or ""))
        msg = _after_lines(db, o, ctx.user_id)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/auftraege/{oid}")
    db.commit()
    flash(request, f"{ln.item.name}: {ln.qty_required:g} {ln.item.unit} im Auftrag." + msg)
    return redirect(f"/auftraege/{oid}#pos-{ln.id}")


@router.post("/auftraege/{oid}/stueckliste")
async def bom_add(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    o = _order(db, oid)
    f = await request.form()
    rows = osvc.parse_bom(db, str(f.get("text") or ""))
    create_new = bool(f.get("neu")) and ctx.can("items", 3)
    added, created, missing = 0, 0, []
    for r in rows:
        item = r.item
        if item is None:
            if not create_new:
                missing.append(r.raw)
                continue
            item = create_item(db, r.raw[:200], ctx.user_id)
            created += 1
        osvc.add_line(db, o, item, r.qty, r.ref)
        added += 1
    msg = _after_lines(db, o, ctx.user_id)
    db.commit()
    text = f"{added} Positionen übernommen" + (f", {created} Artikel neu angelegt" if created else "") + "."
    if missing:
        text += f" Nicht gefunden ({len(missing)}): " + ", ".join(missing[:10]) + (" …" if len(missing) > 10 else "") + " – mit „neu anlegen“ erneut einfügen."
    flash(request, text + msg, "warn" if missing else "ok")
    return redirect(f"/auftraege/{oid}")


@router.post("/auftraege/position/{lid}/aendern")
async def line_edit(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    ln = _line(db, lid)
    f = await request.form()
    try:
        qty = _num(f.get("qty_required"), ln.qty_required)
        if qty <= 0:
            raise ValueError("Menge muss größer als 0 sein (zum Entfernen „✕“ verwenden).")
        ln.qty_required = qty
        ln.note = str(f.get("note") if f.get("note") is not None else ln.note)[:255]
        msg = _after_lines(db, ln.order, ctx.user_id)
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect(f"/auftraege/{ln.order_id}")
    db.commit()
    flash(request, "Position geändert." + msg)
    return redirect(f"/auftraege/{ln.order_id}#pos-{lid}")


@router.post("/auftraege/position/{lid}/loeschen")
def line_delete(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    ln = _line(db, lid)
    oid = ln.order_id
    if ln.qty_issued:
        flash(request, "Es wurde schon etwas entnommen – bitte erst zurückgeben oder die Menge anpassen.", "err")
        return redirect(f"/auftraege/{oid}")
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.order_id == oid, ShoppingItem.item_id == ln.item_id, ShoppingItem.status == "open")):
        db.delete(e)
    db.delete(ln)
    db.commit()
    flash(request, "Position entfernt.")
    return redirect(f"/auftraege/{oid}")


@router.post("/auftraege/position/{lid}/entnehmen")
async def line_issue(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    ln = _line(db, lid)
    f = await request.form()
    try:
        qty = _num(f.get("qty"), ln.remaining)
        lot = db.get(StockLot, int(f.get("lot_id"))) if str(f.get("lot_id") or "").isdigit() else None
        osvc.issue(db, ln, qty, lot, ctx.user_id)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/auftraege/{ln.order_id}#pos-{lid}")
    db.commit()
    rest = ln.remaining
    flash(request, f"{qty:g} {ln.item.unit} {ln.item.name} entnommen." + (f" Noch offen: {rest:g}." if rest > 0 else " Position vollständig."))
    return redirect(f"/auftraege/{ln.order_id}#pos-{lid}")


@router.post("/auftraege/{oid}/alles-entnehmen")
def issue_all(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    """Alles Verfügbare auf einmal entnehmen (z. B. Bausatz komplett zusammenstellen)."""
    o = _order(db, oid)
    st = osvc.order_state(db, o)["lines"]
    n, missing = 0, []
    for ln in o.lines:
        take = min(ln.remaining, st[ln.id].covered)
        if take == float("inf"):
            take = ln.remaining
        if take > 0:
            try:
                osvc.issue(db, ln, take, None, ctx.user_id)
                n += 1
            except ValueError:
                missing.append(ln.item.name)
        if ln.remaining > 0:
            missing.append(ln.item.name)
    db.commit()
    flash(request, f"{n} Positionen entnommen." + (f" Noch offen: {', '.join(sorted(set(missing))[:10])}." if missing else " Alles vollständig."),
          "warn" if missing else "ok")
    return redirect(f"/auftraege/{oid}")


@router.post("/auftraege/position/{lid}/zurueck")
async def line_return(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    from .items import resolve_location

    ln = _line(db, lid)
    f = await request.form()
    try:
        qty = _num(f.get("qty"), ln.qty_issued)
        loc = resolve_location(db, str(f.get("location") or ""))
        if loc is None:
            raise ValueError("Bitte den Lagerort für die Rückgabe angeben.")
        osvc.return_parts(db, ln, qty, loc, ctx.user_id)
        msg = _after_lines(db, ln.order, ctx.user_id) if ln.order.active else ""
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/auftraege/{ln.order_id}#pos-{lid}")
    db.commit()
    flash(request, f"{qty:g} {ln.item.unit} zurück ins Lager." + msg)
    return redirect(f"/auftraege/{ln.order_id}#pos-{lid}")


@router.post("/auftraege/{oid}/bestellt")
async def mark_ordered(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    from datetime import datetime

    o = _order(db, oid)
    f = await request.form()
    ref = str(f.get("order_ref") or "")[:80]
    n = 0
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.order_id == o.id, ShoppingItem.status == "open")):
        e.status, e.ordered_at, e.order_ref = "ordered", datetime.now().replace(microsecond=0), ref or e.order_ref
        n += 1
    audit.log(db, ctx.user_id, "update", "order", o.id, f"{n} Fehlteile für {o.code} als bestellt markiert")
    db.commit()
    flash(request, f"{n} Fehlteil(e) als bestellt markiert." if n else "Nichts offen.")
    return redirect(f"/auftraege/{oid}")


@router.post("/auftraege/{oid}/fehlteile")
def shortfalls(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    o = _order(db, oid)
    n = osvc.sync_shortfalls(db, o, ctx.user_id)
    db.commit()
    flash(request, f"{n} Fehlteil(e) auf die Einkaufsliste gesetzt." if n else "Alle Fehlteile stehen schon auf der Einkaufsliste.")
    return redirect(f"/auftraege/{oid}")


# ------------------------------------------------------------------ Auslager-Assistent
def _skip_key(oid: int) -> str:
    return f"pick_skip_{oid}"


@router.get("/auftraege/{oid}/entnahme")
def pick_wizard(oid: int, request: Request, neu: int = 0, db: Session = Depends(get_db), ctx=Depends(require("orders", 1))):
    o = _order(db, oid)
    if neu:
        request.session.pop(_skip_key(oid), None)
    skip = set(request.session.get(_skip_key(oid), []))
    steps, missing = osvc.pick_steps(db, o, skip)
    all_steps, _ = osvc.pick_steps(db, o)
    step = steps[0] if steps else None
    done_lines = sum(1 for ln in o.lines if ln.remaining <= 0)
    from ..services import walls

    wall = walls.wall_of(step.lot.location) if step and step.lot.location else None
    return render(request, "order_pick.html", {
        "o": o, "step": step, "left": len(steps), "skipped": len(all_steps) - len(steps), "missing": missing,
        "map": osvc.module_map(db, step.lot.location_id) if step else None, "wall": wall, "done_lines": done_lines,
        "last": request.session.get(f"pick_last_{oid}"), "next": steps[1] if len(steps) > 1 else None,
    })


@router.post("/auftraege/{oid}/entnahme")
async def pick_action(oid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    o = _order(db, oid)
    f = await request.form()
    action = str(f.get("action") or "")
    url = f"/auftraege/{oid}/entnahme"
    try:
        if action == "take":
            ln = _line(db, int(f.get("line_id") or 0))
            lot = db.get(StockLot, int(f.get("lot_id") or 0))
            if ln.order_id != o.id or lot is None:
                raise ValueError("Schritt passt nicht zum Auftrag – bitte neu laden.")
            qty = _num(f.get("qty"), 0.0)
            if qty > 0:
                loc_id = lot.location_id
                osvc.issue(db, ln, qty, lot, ctx.user_id)
                request.session[f"pick_last_{oid}"] = [ln.id, loc_id, qty, ln.item.name]
            if f.get("rest_skip"):  # weniger im Fach als gedacht: Rest dieses Fachs überspringen
                sk = set(request.session.get(_skip_key(oid), []))
                sk.add(f"{ln.id}-{lot.id}")
                request.session[_skip_key(oid)] = sorted(sk)
            db.commit()
        elif action == "skip":
            sk = set(request.session.get(_skip_key(oid), []))
            sk.add(str(f.get("key") or ""))
            request.session[_skip_key(oid)] = sorted(sk)
        elif action == "undo":
            last = request.session.pop(f"pick_last_{oid}", None)
            if last:
                ln = _line(db, int(last[0]))
                osvc.return_parts(db, ln, float(last[2]), last[1], ctx.user_id)
                db.commit()
                flash(request, f"Rückgängig: {last[2]:g} × {last[3]} zurück ins Fach.")
        elif action == "finish":
            check(ctx, "orders", 2)
            osvc.set_status(db, o, "erledigt")
            audit.log(db, ctx.user_id, "update", "order", o.id, f"Auftrag {o.code} nach Entnahme abgeschlossen")
            db.commit()
            request.session.pop(_skip_key(oid), None)
            flash(request, f"Auftrag {o.code} abgeschlossen.")
            return redirect(f"/auftraege/{oid}")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
    return redirect(url)
