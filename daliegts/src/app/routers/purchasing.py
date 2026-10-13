"""Lieferanten & Hersteller, Bezugsquellen am Artikel, Einkaufsliste mit Preisvergleich und Wareneingang."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SHOPPING_STATUS, Item, Manufacturer, ShoppingItem, Supplier, SupplierPart
from ..security import check, require
from ..services import audit
from ..services import purchasing as pur
from ..services.codes import resolve
from ..services.items import safe_url
from ..services.locations import TreeCache
from ..web import back, flash, redirect, render

router = APIRouter()


def _num(v, default: float | None = None) -> float | None:
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(str(v).replace(",", ".").replace("€", "").strip())
    except ValueError:
        raise ValueError(f"„{v}“ ist keine Zahl.")


def _get(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, "Nicht gefunden")
    return obj


# ------------------------------------------------------------------ Lieferanten & Hersteller
@router.get("/lieferanten")
def suppliers_page(request: Request, tab: str = "lieferanten", db: Session = Depends(get_db), ctx=Depends(require("suppliers", 1))):
    return render(request, "suppliers.html", {
        "tab": tab if tab in ("lieferanten", "hersteller") else "lieferanten",
        "suppliers": list(db.scalars(select(Supplier).order_by(Supplier.active.desc(), Supplier.name))),
        "manufacturers": list(db.scalars(select(Manufacturer).order_by(Manufacturer.name))),
        "sstats": pur.supplier_stats(db), "mstats": pur.manufacturer_stats(db),
        "presets": [p["name"] for p in pur.SUPPLIER_PRESETS],
    })


@router.post("/lieferanten/vorlagen")
def supplier_presets(request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 3))):
    n = pur.ensure_supplier_presets(db)
    audit.log(db, ctx.user_id, "create", "supplier", None, f"{n} Standard-Shops angelegt")
    db.commit()
    flash(request, f"{n} Shops angelegt, vorhandene um fehlende Such-Links ergänzt.")
    return redirect("/lieferanten")


def _apply_supplier(s: Supplier, f) -> None:
    s.name = (f.get("name") or s.name or "").strip()[:200]
    if not s.name:
        raise ValueError("Name fehlt.")
    s.website = safe_url((f.get("website") or "").strip())[:255]
    search = (f.get("search_url") or "").strip()
    if search and (not safe_url(search) or "{query}" not in search):
        raise ValueError("Such-Link muss mit http(s) beginnen und {query} enthalten.")
    s.search_url = search[:500]
    s.shipping_cost = _num(f.get("shipping_cost"))
    s.free_shipping_from = _num(f.get("free_shipping_from"))
    s.min_order_value = _num(f.get("min_order_value"))
    s.customer_no = (f.get("customer_no") or "").strip()[:80]
    s.notes = f.get("notes") or ""
    s.active = bool(f.get("active"))


@router.post("/lieferanten/speichern")
async def supplier_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 2))):
    f = await request.form()
    sid = int(f.get("id") or 0)
    if sid:
        s = _get(db, Supplier, sid)
        before = audit.snapshot(s)
    else:
        check(ctx, "suppliers", 3)
        s, before = Supplier(name=""), None
        db.add(s)
    try:
        _apply_supplier(s, f)
        if db.scalar(select(Supplier).where(func.lower(Supplier.name) == s.name.lower(), Supplier.id != (s.id or 0))):
            raise ValueError(f"Lieferant „{s.name}“ gibt es schon.")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return back(request, "/lieferanten")
    db.flush()
    audit.log(db, ctx.user_id, "update" if before else "create", "supplier", s.id, f"Lieferant {s.name}",
              audit.diff(before, audit.snapshot(s)) if before else None)
    db.commit()
    flash(request, "Gespeichert.")
    return redirect(f"/lieferanten/{s.id}")


@router.get("/lieferanten/{sid}")
def supplier_detail(sid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 1))):
    s = _get(db, Supplier, sid)
    parts = list(db.scalars(select(SupplierPart).where(SupplierPart.supplier_id == s.id)))
    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_([p.item_id for p in parts])))} if parts else {}
    parts.sort(key=lambda sp: items[sp.item_id].name.lower() if sp.item_id in items else "")
    return render(request, "supplier_detail.html", {
        "s": s, "parts": parts, "items": items, "shop_link": pur.shop_link, "format_tiers": pur.format_tiers,
        "others": [o for o in db.scalars(select(Supplier).order_by(Supplier.name)) if o.id != s.id],
    })


@router.post("/lieferanten/{sid}/zusammenfuehren")
def supplier_merge(sid: int, request: Request, target_id: int = Form(...), db: Session = Depends(get_db), ctx=Depends(require("suppliers", 4))):
    src, tgt = _get(db, Supplier, sid), _get(db, Supplier, target_id)
    name = src.name
    n = pur.merge_suppliers(db, src, tgt)
    audit.log(db, ctx.user_id, "update", "supplier", tgt.id, f"Lieferant {name} in {tgt.name} zusammengeführt ({n} Bezugsquellen)")
    db.commit()
    flash(request, f"„{name}“ in „{tgt.name}“ übernommen ({n} Bezugsquellen).")
    return redirect(f"/lieferanten/{tgt.id}")


@router.post("/lieferanten/{sid}/loeschen")
def supplier_delete(sid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 4))):
    s = _get(db, Supplier, sid)
    if db.scalar(select(func.count(SupplierPart.id)).where(SupplierPart.supplier_id == s.id)):
        flash(request, "Lieferant hat noch Bezugsquellen – bitte zusammenführen oder deaktivieren.", "err")
        return redirect(f"/lieferanten/{s.id}")
    name = s.name
    db.delete(s)
    audit.log(db, ctx.user_id, "delete", "supplier", sid, f"Lieferant {name} gelöscht")
    db.commit()
    flash(request, f"„{name}“ gelöscht.")
    return redirect("/lieferanten")


@router.post("/hersteller/speichern")
async def manufacturer_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 2))):
    f = await request.form()
    mid = int(f.get("id") or 0)
    name = (f.get("name") or "").strip()[:200]
    if not name:
        flash(request, "Name fehlt.", "err")
        return back(request, "/lieferanten?tab=hersteller")
    if db.scalar(select(Manufacturer).where(func.lower(Manufacturer.name) == name.lower(), Manufacturer.id != mid)):
        flash(request, f"Hersteller „{name}“ gibt es schon – ggf. zusammenführen.", "err")
        return back(request, "/lieferanten?tab=hersteller")
    if mid:
        m = _get(db, Manufacturer, mid)
    else:
        check(ctx, "suppliers", 3)
        m = Manufacturer(name=name)
        db.add(m)
    m.name, m.website, m.notes = name, safe_url((f.get("website") or "").strip())[:255], f.get("notes") or ""
    db.flush()
    audit.log(db, ctx.user_id, "update" if mid else "create", "manufacturer", m.id, f"Hersteller {m.name}")
    db.commit()
    flash(request, "Gespeichert.")
    return redirect(f"/hersteller/{m.id}")


@router.get("/hersteller/{mid}")
def manufacturer_detail(mid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 1))):
    m = _get(db, Manufacturer, mid)
    items = list(db.scalars(select(Item).where(Item.manufacturer_id == m.id).order_by(Item.name)))
    return render(request, "manufacturer_detail.html", {
        "m": m, "items": items, "others": [o for o in db.scalars(select(Manufacturer).order_by(Manufacturer.name)) if o.id != m.id]})


@router.post("/hersteller/{mid}/zusammenfuehren")
def manufacturer_merge(mid: int, request: Request, target_id: int = Form(...), db: Session = Depends(get_db), ctx=Depends(require("suppliers", 4))):
    src, tgt = _get(db, Manufacturer, mid), _get(db, Manufacturer, target_id)
    name = src.name
    n = pur.merge_manufacturers(db, src, tgt)
    audit.log(db, ctx.user_id, "update", "manufacturer", tgt.id, f"Hersteller {name} in {tgt.name} zusammengeführt ({n} Artikel)")
    db.commit()
    flash(request, f"„{name}“ in „{tgt.name}“ übernommen ({n} Artikel).")
    return redirect(f"/hersteller/{tgt.id}")


@router.post("/hersteller/{mid}/loeschen")
def manufacturer_delete(mid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("suppliers", 4))):
    m = _get(db, Manufacturer, mid)
    if db.scalar(select(func.count(Item.id)).where(Item.manufacturer_id == m.id)):
        flash(request, "Hersteller ist noch Artikeln zugeordnet – bitte zusammenführen.", "err")
        return redirect(f"/hersteller/{m.id}")
    name = m.name
    db.delete(m)
    audit.log(db, ctx.user_id, "delete", "manufacturer", mid, f"Hersteller {name} gelöscht")
    db.commit()
    flash(request, f"„{name}“ gelöscht.")
    return redirect("/lieferanten?tab=hersteller")


# ------------------------------------------------------------------ Bezugsquellen am Artikel
@router.post("/artikel/{item_id}/bezugsquelle")
async def supplier_part_save(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    item = _get(db, Item, item_id)
    f = await request.form()
    try:
        spid = int(f.get("id") or 0)
        sp = _get(db, SupplierPart, spid) if spid else None
        if sp is not None and sp.item_id != item.id:
            raise ValueError("Bezugsquelle gehört zu einem anderen Artikel.")
        sup_name = (f.get("supplier") or "").strip()
        supplier = db.scalar(select(Supplier).where(func.lower(Supplier.name) == sup_name.lower())) if sup_name else None
        if supplier is None:
            if not sup_name:
                raise ValueError("Bitte einen Lieferanten wählen.")
            check(ctx, "suppliers", 3)
            supplier = Supplier(name=sup_name[:200])
            preset = next((p for p in pur.SUPPLIER_PRESETS if p["name"].lower() == sup_name.lower()), None)
            if preset:
                supplier.website, supplier.search_url = preset["website"], preset["search_url"]
            db.add(supplier)
            db.flush()
        if sp is None:
            sp = SupplierPart(item_id=item.id, supplier_id=supplier.id)
            db.add(sp)
        sp.supplier_id = supplier.id
        sp.sku = (f.get("sku") or "").strip()[:120]
        url = (f.get("url") or "").strip()
        if url and not safe_url(url):
            raise ValueError("Produkt-Link muss mit http(s) beginnen.")
        sp.url = url[:500]
        sp.pack_qty = max(_num(f.get("pack_qty"), 1) or 1, 0.001)
        sp.preferred = bool(f.get("preferred"))
        sp.obsolete = bool(f.get("obsolete"))
        sp.notes = f.get("notes") or ""
        db.flush()
        if sp.preferred:
            for other in db.scalars(select(SupplierPart).where(SupplierPart.item_id == item.id, SupplierPart.id != sp.id)):
                other.preferred = False
        if "prices" in f and ctx.can("prices", 2):
            pur.set_prices(db, sp, pur.parse_tiers(f.get("prices") or ""))
        if f.get("reorder_qty") not in (None, ""):
            item.reorder_qty = max(0.0, _num(f.get("reorder_qty"), 0) or 0)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/artikel/{item.id}#bezug")
    audit.log(db, ctx.user_id, "update", "item", item.id, f"Bezugsquelle {supplier.name} {sp.sku} für {item.name}")
    db.commit()
    flash(request, "Bezugsquelle gespeichert.")
    return redirect(f"/artikel/{item.id}#bezug")


@router.post("/bezugsquelle/{spid}/loeschen")
def supplier_part_delete(spid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    sp = _get(db, SupplierPart, spid)
    item_id = sp.item_id
    audit.log(db, ctx.user_id, "update", "item", item_id, f"Bezugsquelle {sp.supplier.name} {sp.sku} entfernt")
    db.delete(sp)
    db.commit()
    return redirect(f"/artikel/{item_id}#bezug")


@router.post("/artikel/{item_id}/mindestbestand")
def item_reorder_settings(item_id: int, request: Request, min_stock: str = Form(""), reorder_qty: str = Form(""),
                          db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    item = _get(db, Item, item_id)
    try:
        item.min_stock = max(0.0, _num(min_stock, 0) or 0)
        item.reorder_qty = max(0.0, _num(reorder_qty, 0) or 0)
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect(f"/artikel/{item.id}#bezug")
    db.commit()
    flash(request, "Gespeichert.")
    return redirect(f"/artikel/{item.id}#bezug")


# ------------------------------------------------------------------ Einkaufsliste
def _open_entries(db: Session) -> list[ShoppingItem]:
    return list(db.scalars(select(ShoppingItem).where(ShoppingItem.status == "open").order_by(ShoppingItem.id)))


@router.get("/einkauf")
def shopping_page(request: Request, opt: int = 1, job: str = "", db: Session = Depends(get_db), ctx=Depends(require("purchasing", 1))):
    plan = pur.make_plan(db, _open_entries(db), optimize_shipping=bool(opt))
    ordered = list(db.scalars(select(ShoppingItem).where(ShoppingItem.status == "ordered").order_by(ShoppingItem.ordered_at, ShoppingItem.id)))
    done = list(db.scalars(select(ShoppingItem).where(ShoppingItem.status.in_(("received", "cancelled")))
                           .order_by(ShoppingItem.id.desc()).limit(30)))
    tc = TreeCache(db)
    default_locs = {e.id: pur.default_location(db, e.item) for e in ordered if e.item}
    return render(request, "shopping.html", {
        "plan": plan, "opt": bool(opt), "ordered": ordered, "done": done, "suggestions": pur.suggestions(db), "tc": tc,
        "default_locs": default_locs, "SHOPPING_STATUS": SHOPPING_STATUS, "shop_link": pur.shop_link,
        "quick_order": pur.quick_order_text, "job": job,
        "online_active": bool(_providers_active(db)), "questions": _questions(db),
    })


def _providers_active(db: Session) -> list:
    from ..services.providers import active_providers

    return active_providers(db)


def _questions(db: Session) -> int:
    from ..services.infosync import open_candidate_count

    return open_candidate_count(db)


def _find_item(db: Session, q: str) -> Item | None:
    q = (q or "").strip()
    if not q:
        return None
    if q.isdigit() and db.get(Item, int(q)):
        return db.get(Item, int(q))
    r = resolve(db, q)
    if r.kind == "item":
        return db.get(Item, r.id)
    if r.kind == "lot":
        from ..models import StockLot

        lot = db.get(StockLot, r.id)
        return lot.item if lot else None
    return db.scalar(select(Item).where(func.lower(Item.name) == q.lower()))


@router.post("/einkauf/neu")
def shopping_add(request: Request, q: str = Form(""), quantity: str = Form(""), note: str = Form(""),
                 db: Session = Depends(get_db), ctx=Depends(require("purchasing", 3))):
    try:
        item = _find_item(db, q)
        e = pur.add_to_list(db, item, _num(quantity), ctx.user_id, text="" if item else q, note=note)
    except ValueError as ex:
        flash(request, str(ex), "err")
        return redirect("/einkauf")
    db.commit()
    flash(request, f"„{e.item.name if e.item else e.text}“ auf der Einkaufsliste." + ("" if item else " (ohne Artikel – freier Text)"))
    return redirect("/einkauf")


@router.post("/artikel/{item_id}/einkaufsliste")
def shopping_add_item(item_id: int, request: Request, quantity: str = Form(""), supplier_part_id: str = Form(""),
                      db: Session = Depends(get_db), ctx=Depends(require("purchasing", 3))):
    item = _get(db, Item, item_id)
    try:
        e = pur.add_to_list(db, item, _num(quantity), ctx.user_id, supplier_part_id=int(supplier_part_id) if supplier_part_id else None)
    except ValueError as ex:
        flash(request, str(ex), "err")
        return back(request, f"/artikel/{item.id}")
    db.commit()
    flash(request, f"🛒 {e.quantity:g} {item.unit} „{item.name}“ auf der Einkaufsliste.")
    return back(request, f"/artikel/{item.id}")


@router.post("/einkauf/vorschlaege")
async def shopping_add_suggestions(request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 3))):
    f = await request.form()
    chosen = {int(v) for v in f.getlist("item_id") if str(v).isdigit()}
    n = 0
    for item, qty in pur.suggestions(db):
        if not chosen or item.id in chosen:
            pur.add_to_list(db, item, qty, ctx.user_id, note="Mindestbestand")
            n += 1
    db.commit()
    flash(request, f"{n} Artikel übernommen.")
    return redirect("/einkauf")


@router.post("/einkauf/{eid}/aendern")
def shopping_edit(eid: int, request: Request, quantity: str = Form(""), supplier_part_id: str = Form("auto"), note: str | None = Form(None),
                  db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    e = _get(db, ShoppingItem, eid)
    try:
        q = _num(quantity)
        if q is not None:
            if q <= 0:
                raise ValueError("Menge muss größer 0 sein.")
            e.quantity = q
        if supplier_part_id in ("", "auto"):
            e.supplier_part_id = None
        else:
            sp = _get(db, SupplierPart, int(supplier_part_id))
            if sp.item_id != e.item_id:
                raise ValueError("Bezugsquelle passt nicht zum Artikel.")
            e.supplier_part_id = sp.id
        if note is not None:
            e.note = note
    except ValueError as ex:
        flash(request, str(ex), "err")
        return redirect("/einkauf")
    db.commit()
    if request.headers.get("hx-request"):
        return {"ok": True}
    return redirect("/einkauf")


@router.post("/einkauf/{eid}/loeschen")
def shopping_delete(eid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    e = _get(db, ShoppingItem, eid)
    if e.status == "open":
        db.delete(e)
    else:
        e.status = "cancelled"
    db.commit()
    return redirect("/einkauf")


@router.post("/einkauf/{eid}/offen")
def shopping_reopen(eid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    e = _get(db, ShoppingItem, eid)
    if e.status == "received":
        flash(request, "Bereits eingebucht – bitte Bestand direkt korrigieren.", "err")
    else:
        e.status, e.ordered_at = "open", None
        db.commit()
    return redirect("/einkauf")


def _group_for(db: Session, supplier_id: int, opt: bool) -> pur.Group | None:
    plan = pur.make_plan(db, _open_entries(db), optimize_shipping=opt)
    return next((g for g in plan.groups if g.supplier and g.supplier.id == supplier_id), None)


@router.post("/einkauf/bestellt")
def shopping_mark_ordered(request: Request, supplier_id: int = Form(...), opt: int = Form(1), order_ref: str = Form(""),
                          db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    g = _group_for(db, supplier_id, bool(opt))
    if g is None:
        flash(request, "Keine offenen Positionen für diesen Lieferanten.", "err")
        return redirect("/einkauf")
    n = pur.mark_ordered(db, g.lines, order_ref)
    audit.log(db, ctx.user_id, "update", "shopping", None, f"{n} Positionen bei {g.supplier.name} bestellt {order_ref}".strip())
    db.commit()
    flash(request, f"{n} Positionen bei {g.supplier.name} als bestellt markiert.")
    return redirect("/einkauf")


@router.get("/einkauf/lieferant/{supplier_id}.csv")
def shopping_csv(supplier_id: int, opt: int = 1, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 1))):
    g = _group_for(db, supplier_id, bool(opt))
    if g is None:
        raise HTTPException(404, "Keine offenen Positionen für diesen Lieferanten")
    name = "".join(ch for ch in g.supplier.name if ch.isalnum()) or "lieferant"
    return Response("﻿" + pur.order_csv(g), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="bestellung-{name}.csv"'})


@router.post("/einkauf/{eid}/erhalten")
def shopping_receive(eid: int, request: Request, quantity: str = Form(""), location: str = Form(""),
                     db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    e = _get(db, ShoppingItem, eid)
    if e.status == "received":
        flash(request, "Schon eingebucht.", "err")
        return redirect("/einkauf")
    try:
        if e.item is not None:
            check(ctx, "stock", 2)
        from .items import resolve_location

        loc = resolve_location(db, location) if location else None
        qty = _num(quantity)
        lot = pur.receive(db, e, qty, loc, ctx.user_id)
    except ValueError as ex:
        db.rollback()
        flash(request, str(ex), "err")
        return redirect("/einkauf")
    audit.log(db, ctx.user_id, "update", "shopping", e.id, f"Wareneingang {e.item.name if e.item else e.text}")
    db.commit()
    if lot is not None and lot.location_id is None:
        flash(request, "Eingebucht, aber ohne Lagerort – bitte am Artikel einlagern/umlagern.", "warn")
    else:
        flash(request, "Wareneingang gebucht.")
    return redirect("/einkauf")


@router.post("/lieferanten/{sid}/versand")
def supplier_shipping(sid: int, request: Request, shipping_cost: str = Form(""), free_shipping_from: str = Form(""), min_order_value: str = Form(""),
                      db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    """Versandkosten schnell nachtragen (Bestellplaner, Einkaufsliste)."""
    s = db.get(Supplier, sid)
    if s is None:
        raise HTTPException(404, "Lieferant nicht gefunden")

    def num(v: str) -> float | None:
        v = (v or "").strip().replace("€", "").replace(",", ".")
        if not v:
            return None
        try:
            return max(0.0, float(v))
        except ValueError:
            raise HTTPException(400, f"„{v}“ ist keine Zahl")

    s.shipping_cost, s.free_shipping_from, s.min_order_value = num(shipping_cost), num(free_shipping_from), num(min_order_value)
    audit.log(db, ctx.user_id, "update", "supplier", s.id, f"Versandkosten {s.name}: {s.shipping_cost} € (frei ab {s.free_shipping_from})")
    db.commit()
    flash(request, f"Versandkosten für {s.name} gespeichert.")
    return back(request, "/lieferanten")
