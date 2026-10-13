"""REST-API /api/v1 – genutzt von der Handy-PWA, Home Assistant und eigenen Skripten.

Authentifizierung: Sitzung (Browser) oder Header "Authorization: Bearer <token>" (API-Token pro Benutzer).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, Loan, Location, StockLot
from ..security import check, require
from ..services import audit
from ..services import stock as stocksvc
from ..services.codes import CodeConflict, add_item_code, add_location_alias, add_lot_code, check_free, resolve
from ..services.items import catalog_search, create_from_catalog, create_item, image_url, reindex, store_upload
from ..services.locations import TreeCache
from ..services.search import SearchQuery, search
from ..services.walls import wall_of
from ..services.wled import MULTI_PALETTE, clamp_duration, clean_color, controller_infos, manager, resolve_targets, targets_for_locations
from .items import resolve_location

router = APIRouter(prefix="/api/v1", tags=["API"])


def _lot_json(lot: StockLot, tc: TreeCache) -> dict:
    return {"id": lot.id, "code": lot.code, "item_id": lot.item_id, "item": lot.item.name, "unit": lot.item.unit,
            "quantity": lot.quantity, "coarse": lot.coarse, "stock_mode": lot.item.stock_mode, "location_id": lot.location_id,
            "location": tc.path(lot.location_id) if lot.location_id else None, "serial": lot.serial, "batch": lot.batch,
            "status": lot.status, "needs_review": lot.needs_review,
            "wall": (w.id if (w := wall_of(lot.location)) is not None else None)}


def _item_json(item: Item, tc: TreeCache) -> dict:
    return {"id": item.id, "code": item.code, "name": item.name, "description": item.description, "kind": item.kind,
            "unit": item.unit, "stock_mode": item.stock_mode, "total": item.total_quantity, "below_min": item.below_min,
            "image": image_url(item.image), "footprint": item.footprint.name if item.footprint else None, "mpn": item.mpn,
            "url": f"/artikel/{item.id}", "lots": [_lot_json(l, tc) for l in item.lots]}


def _loc_json(db: Session, loc: Location, tc: TreeCache, with_content: bool = True) -> dict:
    d = {"id": loc.id, "code": loc.code, "name": loc.name, "type": loc.type, "path": tc.path(loc.id), "url": f"/orte/{loc.id}",
         "has_lights": bool(resolve_targets(db, loc)), "compartment": loc.compartment_index, "compartments": loc.compartment_count,
         "children": len(tc.children.get(loc.id, [])),
         "wall": (w.id if (w := wall_of(loc)) is not None else (loc.id if loc.children and any(c.wall_x is not None for c in loc.children) else None))}
    if with_content:
        ids = [loc.id, *tc.descendants(loc.id)]
        d["lots"] = [_lot_json(l, tc) for l in db.scalars(select(StockLot).where(StockLot.location_id.in_(ids)))]
    return d


@router.get("/resolve")
def api_resolve(code: str, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    r = resolve(db, code)
    tc = TreeCache(db)
    if r.kind == "location":
        return {"kind": "location", "location": _loc_json(db, db.get(Location, r.id), tc)}
    if r.kind == "item":
        return {"kind": "item", "item": _item_json(db.get(Item, r.id), tc)}
    if r.kind == "lot":
        lot = db.get(StockLot, r.id)
        return {"kind": "lot", "lot": _lot_json(lot, tc), "item": _item_json(lot.item, tc)}
    if r.kind == "project":
        return {"kind": "order", "order": {"id": r.id, "code": r.code, "url": f"/projekte/{r.id}"}}
    if r.kind == "order":
        return {"kind": "order", "order": {"id": r.id, "code": r.code, "url": f"/auftraege/{r.id}"}}
    if r.kind == "label":
        from ..services.labelparse import parse_label

        lab = parse_label(code) or {}
        lab.pop("raw", None)
        return {"kind": "unknown", "code": r.code, "raw": code, "label": lab}
    return {"kind": "unknown", "code": r.code, "raw": code}


@router.get("/search")
def api_search(q: str = "", limit: int = 30, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    items, total = search(db, SearchQuery(q=q, limit=limit))
    tc = TreeCache(db)
    return {"total": total, "items": [_item_json(i, tc) for i in items]}


@router.get("/catalog")
def api_catalog(q: str, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in catalog_search(q, 30)]


@router.get("/items/{item_id}")
def api_item(item_id: int, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "Artikel nicht gefunden")
    return _item_json(item, TreeCache(db))


@router.get("/locations/{loc_id}")
def api_location(loc_id: int, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    loc = db.get(Location, loc_id)
    if not loc:
        raise HTTPException(404, "Lagerort nicht gefunden")
    return _loc_json(db, loc, TreeCache(db))


@router.get("/locations/{loc_id}/cells")
def api_cells(loc_id: int, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    """Fächer eines Möbels in Rasterreihenfolge (für „Etiketten zuordnen“)."""
    loc = db.get(Location, loc_id)
    if not loc:
        raise HTTPException(404, "Lagerort nicht gefunden")
    from ..services.locations import natural_key

    kids = sorted(loc.children, key=lambda c: (c.grid_row or 10**6, c.grid_col or 0, c.sort_order, natural_key(c.name)))
    return [{"id": c.id, "name": c.name, "code": c.code, "row": c.grid_row, "col": c.grid_col,
             "aliases": [a.code for a in c.aliases]} for c in kids]


@router.get("/locations")
def api_locations(q: str = "", furniture: bool = False, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    tc = TreeCache(db)
    rows = []
    ql = q.lower()
    for lid in tc.nodes:
        if furniture and not tc.children.get(lid):
            continue
        p = tc.path(lid)
        if ql and ql not in p.lower():
            continue
        rows.append({"id": lid, "code": tc.nodes[lid].code, "path": p, "children": len(tc.children.get(lid, []))})
    rows.sort(key=lambda r: r["path"].lower())
    return rows[:100]


# ------------------------------------------------------------------ LEDs
class LocateBody(BaseModel):
    code: str | None = None
    location_id: int | None = None
    location_ids: list[int] | None = None
    item_id: int | None = None
    item_ids: list[int] | None = None
    duration: float | None = None
    color: str | None = None


@router.post("/locate")
async def api_locate(body: LocateBody, db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    locs: list[Location] = []
    if body.code:
        r = resolve(db, body.code)
        if r.kind == "location":
            body.location_id = r.id
        elif r.kind == "item":
            body.item_id = r.id
        elif r.kind == "lot":
            locs.append(db.get(StockLot, r.id).location)
    if body.location_id:
        locs.append(db.get(Location, body.location_id))
    for lid in body.location_ids or []:
        locs.append(db.get(Location, lid))
    item_ids = ([body.item_id] if body.item_id else []) + (body.item_ids or [])
    multi = len(item_ids) > 1
    palette_locs: list[Location] = []
    for iid in item_ids:
        item = db.get(Item, iid)
        if item:
            palette_locs += [lot.location for lot in item.lots if lot.location]
    locs = [l for l in locs if l is not None]
    color = clean_color(body.color)
    targets = targets_for_locations(db, locs, [color] if color else None)
    targets += targets_for_locations(db, palette_locs, MULTI_PALETTE if multi else ([color] if color else None))
    if not targets:
        return {"ok": False, "message": "Für diesen Ort sind keine LEDs zugeordnet.", "targets": []}
    infos = controller_infos(db)
    await manager.show(infos, targets, clamp_duration(body.duration))
    errors = {infos[c].name: s["error"] for c, s in manager.status().items() if s["error"] and c in infos}
    notes = sorted({t.note for t in targets if t.note})
    return {"ok": not errors, "targets": len(targets), "notes": notes, "errors": errors}


@router.get("/wo")
async def api_where(q: str = "", licht: int = 1, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """„Wo ist …?“ für Sprachassistenten: Antwortsatz (speech) und – wenn möglich – Fach aufleuchten lassen."""
    from ..services import license as lic
    from ..services.voice import where_is

    if not lic.has(db, "homeassistant"):
        return {"speech": "Die Sprachsuche gehört zu DaLiegt’s Pro.", "item": None, "locations": [], "lit": False, "alternatives": [],
                "pro_required": "homeassistant"}
    a = where_is(db, q)
    lit = False
    if licht and a.location_ids and ctx.can("wled_show", 1):
        targets = targets_for_locations(db, [db.get(Location, i) for i in a.location_ids])
        if targets:
            await manager.show(controller_infos(db), targets)
            lit = True
    speech = a.speech + (" Ich lasse es aufleuchten." if lit else "")
    return {"speech": speech, "item": {"id": a.item.id, "name": a.item.name, "url": f"/artikel/{a.item.id}"} if a.item else None,
            "locations": [TreeCache(db).path(i) for i in a.location_ids], "lit": lit, "alternatives": a.alternatives}


@router.post("/leds/off")
async def api_leds_off(db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    await manager.clear(controller_infos(db))
    return {"ok": True}


@router.post("/presence")
def api_presence(active: bool = Body(..., embed=True), ctx=Depends(require("wled_show", 1))):
    if ctx.user is None:
        raise HTTPException(403, "Nur für angemeldete Benutzer/Tokens")
    manager.presence = active
    return {"ok": True, "presence": active}


@router.post("/idle-pause")
async def api_idle_pause(paused: bool = Body(..., embed=True), db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    if ctx.user is None:
        raise HTTPException(403, "Nur für angemeldete Benutzer/Tokens")
    from ..security import set_setting
    from ..services.wled import controller_infos

    set_setting(db, "wled.idle_paused", bool(paused))
    db.commit()
    await manager.set_paused(controller_infos(db), bool(paused))
    return {"ok": True, "paused": paused}


# ------------------------------------------------------------------ Buchungen
class AddBody(BaseModel):
    item_id: int
    location: str | int | None = None
    quantity: float = 1
    note: str = ""
    serial: str = ""
    batch: str = ""


@router.post("/stock/add")
def api_add(body: AddBody, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    item = db.get(Item, body.item_id)
    if item is None:
        raise HTTPException(404, "Artikel nicht gefunden")
    try:
        lot = stocksvc.add_stock(db, item, resolve_location(db, body.location), body.quantity, ctx.user_id, body.note, body.batch, body.serial)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"ok": True, "lot": _lot_json(lot, TreeCache(db))}


class LotQtyBody(BaseModel):
    lot_id: int
    quantity: float = 1
    note: str = ""


def _lot_or_404(db: Session, lot_id: int) -> StockLot:
    lot = db.get(StockLot, lot_id)
    if lot is None:
        raise HTTPException(404, "Bestand nicht gefunden")
    return lot


@router.post("/stock/remove")
def api_remove(body: LotQtyBody, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    lot = _lot_or_404(db, body.lot_id)
    try:
        stocksvc.remove_stock(db, lot, body.quantity, ctx.user_id, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"ok": True, "lot": _lot_json(lot, TreeCache(db))}


@router.post("/stock/count")
def api_count(body: LotQtyBody, db: Session = Depends(get_db), ctx=Depends(require("inventory", 2))):
    lot = _lot_or_404(db, body.lot_id)
    stocksvc.count_stock(db, lot, body.quantity, ctx.user_id, body.note or "Inventur (Scan)")
    db.commit()
    return {"ok": True, "lot": _lot_json(lot, TreeCache(db))}


@router.post("/stock/confirm")
def api_confirm(lot_id: int = Body(..., embed=True), db: Session = Depends(get_db), ctx=Depends(require("inventory", 2))):
    lot = _lot_or_404(db, lot_id)
    stocksvc.confirm_lot(db, lot, ctx.user_id)
    db.commit()
    return {"ok": True}


class CoarseBody(BaseModel):
    lot_id: int
    level: str


@router.post("/stock/coarse")
def api_coarse(body: CoarseBody, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    if body.level not in ("empty", "low", "ok"):
        raise HTTPException(400, "Ungültiger Füllstand")
    lot = _lot_or_404(db, body.lot_id)
    stocksvc.set_coarse(db, lot, body.level, ctx.user_id)
    db.commit()
    return {"ok": True}


class MoveBody(BaseModel):
    lot_id: int | None = None
    from_location_id: int | None = None
    location: str | int
    quantity: float | None = None


@router.post("/stock/move")
def api_move(body: MoveBody, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    try:
        target = resolve_location(db, body.location)
        if body.lot_id:
            lot = stocksvc.move_stock(db, _lot_or_404(db, body.lot_id), target, body.quantity, ctx.user_id)
            db.commit()
            return {"ok": True, "lot": _lot_json(lot, TreeCache(db))}
        if body.from_location_id:
            check(ctx, "locations", 2)
            src = db.get(Location, body.from_location_id)
            if src is None:
                raise HTTPException(404, "Lagerort nicht gefunden")
            src_loc_parent = src.parent_id
            from ..services.locations import move_location

            if target is None:
                raise ValueError("Ziel fehlt")
            move_location(db, src, target)
            audit.log(db, ctx.user_id, "update", "location", src.id, f"Lagerort {src.name} (mit Inhalt) verschoben von #{src_loc_parent}")
            db.commit()
            return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    raise HTTPException(400, "lot_id oder from_location_id angeben")


class UndoBody(BaseModel):
    kind: str  # out | move | add
    lot_id: int
    quantity: float
    location_id: int | None = None  # bei move: ursprünglicher Lagerort


@router.post("/stock/undo")
def api_undo(body: UndoBody, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    """Letzte Buchung vom Buchen-Bildschirm zurücknehmen (als Gegenbuchung „Storno“ in der Historie)."""
    lot = _lot_or_404(db, body.lot_id)
    if body.quantity < 0:
        raise HTTPException(400, "Menge muss positiv sein")
    try:
        if body.kind == "out":
            lot.quantity = (lot.quantity or 0) + body.quantity
            stocksvc._move(db, lot.item, lot, "added", body.quantity, ctx.user_id, to=lot.location_id, note="Storno Ausbuchung")
            db.flush()
        elif body.kind == "move":
            if body.location_id is not None and db.get(Location, body.location_id) is None:
                raise HTTPException(404, "Ursprünglicher Lagerort existiert nicht mehr")
            lot = stocksvc.move_stock(db, lot, body.location_id, body.quantity if body.quantity < (lot.quantity or 0) else None,
                                      ctx.user_id, note="Storno Umbuchung")
        elif body.kind == "add":
            stocksvc.remove_stock(db, lot, min(body.quantity, lot.quantity or 0), ctx.user_id, note="Storno Zuordnung")
        else:
            raise HTTPException(400, "Unbekannte Buchungsart")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"ok": True, "lot": _lot_json(lot, TreeCache(db))}


class AliasBody(BaseModel):
    location_id: int | None = None
    item_id: int | None = None
    lot_id: int | None = None
    code: str


@router.post("/alias")
def api_alias(body: AliasBody, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    kind, tid = ("lot", body.lot_id) if body.lot_id else ("location", body.location_id) if body.location_id else ("item", body.item_id)
    if tid:
        try:
            check_free(db, body.code, kind, tid)
        except CodeConflict as e:
            raise HTTPException(409, str(e))
    if body.lot_id:
        check(ctx, "stock", 2)
        if db.get(StockLot, body.lot_id) is None:
            raise HTTPException(404, "Bestand nicht gefunden")
        c = add_lot_code(db, body.lot_id, body.code, "scan")
        db.commit()
        return {"ok": True, "code": c.code}
    if body.location_id:
        check(ctx, "locations", 2)
        if db.get(Location, body.location_id) is None:
            raise HTTPException(404, "Lagerort nicht gefunden")
        a = add_location_alias(db, body.location_id, body.code, "scan")
        db.commit()
        return {"ok": True, "code": a.code}
    if body.item_id:
        check(ctx, "items", 2)
        if db.get(Item, body.item_id) is None:
            raise HTTPException(404, "Artikel nicht gefunden")
        c = add_item_code(db, body.item_id, body.code, "barcode", "scan")
        reindex(db, db.get(Item, body.item_id))
        db.commit()
        return {"ok": True, "code": c.code}
    raise HTTPException(400, "location_id, item_id oder lot_id angeben")


@router.post("/items/quick")
async def api_quick_item(name: str = Form(""), catalog_key: str = Form(""), category_id: int | None = Form(None), location: str = Form(""),
                         quantity: float = Form(0), barcode: str = Form(""), photo: UploadFile | None = File(None),
                         db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    if catalog_key:
        try:
            item = create_from_catalog(db, catalog_key, ctx.user_id)
        except KeyError:
            raise HTTPException(404, "Katalogeintrag nicht gefunden")
    else:
        if not name.strip():
            raise HTTPException(400, "Name fehlt")
        item = create_item(db, name, ctx.user_id, category_id=category_id or None)
    if photo is not None and photo.filename:
        data = await photo.read()
        if data:
            item.image = store_upload(data, photo.filename)
    if barcode:
        add_item_code(db, item.id, barcode, "barcode", "scan")
    try:
        loc = resolve_location(db, location)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if loc or quantity:
        stocksvc.add_stock(db, item, loc, quantity, ctx.user_id, "Schnell-Anlage")
    reindex(db, item)
    audit.log(db, ctx.user_id, "create", "item", item.id, f"Schnell-Anlage {item.name}")
    db.commit()
    return {"ok": True, "item": _item_json(item, TreeCache(db))}


def _shopping_open(db: Session) -> int:
    from ..models import ShoppingItem

    return db.scalar(select(func.count(ShoppingItem.id)).where(ShoppingItem.status == "open")) or 0


def _expiry_counts(db: Session) -> dict:
    from ..services.expiry import counts

    return counts(db)


def _printers3d(db: Session) -> list[dict]:
    from ..models import Printer3D
    from ..services.printer3d import loaded

    out = []
    for p in db.scalars(select(Printer3D).where(Printer3D.enabled.is_(True))):
        spools = loaded(db, p)
        si = spools[0][1] if spools else None
        out.append({"name": p.name, "model": p.model, "cfs": p.has_cfs, "state": p.last_state,
                    "file": (p.job or {}).get("file", "").rsplit("/", 1)[-1],
                    "spool": si["item"].name if si else None, "spool_grams": si["grams"] if si else None,
                    "spools": [{"slot": lbl, "name": x["item"].name, "grams": x["grams"]} for lbl, x in spools],
                    "warning": p.warning, "pending": len(p.pending or [])})
    return out


@router.get("/summary")
def api_summary(db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    items = list(db.scalars(select(Item).where((Item.min_stock > 0) | (Item.stock_mode == "coarse"))).unique())
    loans = list(db.scalars(select(Loan).where(Loan.returned_at.is_(None))))
    return {
        "items": db.scalar(select(func.count(Item.id))),
        "locations": db.scalar(select(func.count(Location.id))),
        "below_min": sum(1 for i in items if i.below_min),
        "loans_open": len(loans),
        "loans_overdue": sum(1 for l in loans if l.due and l.due < date.today()),
        "needs_review": db.scalar(select(func.count(StockLot.id)).where(StockLot.needs_review.is_(True))),
        "shopping_open": _shopping_open(db),
        "expiry": _expiry_counts(db),
        "printers3d": _printers3d(db),
        "wled": manager.status(),
    }


class ShoppingBody(BaseModel):
    item_id: int | None = None
    code: str | None = None
    text: str = ""
    quantity: float | None = None


@router.get("/shopping")
def api_shopping(db: Session = Depends(get_db), ctx=Depends(require("purchasing", 1))):
    """Offene und bestellte Positionen der Einkaufsliste (z. B. für eine Home-Assistant-Karte)."""
    from ..models import ShoppingItem

    rows = db.scalars(select(ShoppingItem).where(ShoppingItem.status.in_(("open", "ordered"))).order_by(ShoppingItem.id))
    return [{"id": e.id, "item_id": e.item_id, "name": e.item.name if e.item else e.text, "quantity": e.quantity, "status": e.status,
             "supplier": e.supplier_part.supplier.name if e.supplier_part else None, "sku": e.supplier_part.sku if e.supplier_part else None}
            for e in rows]


@router.post("/shopping")
def api_shopping_add(body: ShoppingBody, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 3))):
    """Auf die Einkaufsliste setzen – per Artikel-ID, gescanntem Code oder als freier Text."""
    from ..services import purchasing as pur

    item = db.get(Item, body.item_id) if body.item_id else None
    if body.code and item is None:
        r = resolve(db, body.code)
        if r.kind == "item":
            item = db.get(Item, r.id)
        elif r.kind == "lot":
            item = db.get(StockLot, r.id).item
        else:
            raise HTTPException(404, "Code gehört zu keinem Artikel.")
    try:
        e = pur.add_to_list(db, item, body.quantity, ctx.user_id, text=body.text)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    db.commit()
    return {"ok": True, "id": e.id, "name": e.item.name if e.item else e.text, "quantity": e.quantity}


@router.get("/me")
def api_me(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    return {"user": ctx.name, "levels": ctx.levels, "admin_mode": ctx.admin_mode}
