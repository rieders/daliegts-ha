"""Bauteil erkennen: Tüten-Etikett scannen oder Foto aufnehmen -> Vorschlag prüfen -> anlegen (v0.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ITEM_KINDS
from ..security import require
from ..services import audit, infosync, recognize, vision
from ..services import stock as stocksvc
from ..services.items import create_item, ensure_category_path, ensure_footprint, ensure_manufacturer, reindex, set_parameters_from_form, store_upload
from ..services.labelparse import parse_label
from ..services.providers import active_providers, load_config
from ..web import flash, redirect, render

router = APIRouter()

MAX_PHOTOS = 3


def _page(request: Request, db: Session, sug: dict | None = None, code: str = "", ort: str = "", error: str = ""):
    return render(request, "recognize.html", {
        "sug": sug, "code": code, "ort": ort, "error": error, "vision_active": vision.active(db), "online_active": bool(active_providers(db)),
        "categories": recognize.category_choices(db), "params": recognize.param_names(db), "names": recognize.names_for_rows(),
        "ITEM_KINDS": ITEM_KINDS,
    })


@router.get("/artikel/erkennen")
async def page(request: Request, code: str = "", ort: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    if not code:
        return _page(request, db, ort=ort)
    lab = parse_label(code)
    if lab is None:
        return _page(request, db, code=code, ort=ort, error="Das ist kein bekanntes Tüten-Etikett (DigiKey, Mouser, LCSC, TME). "
                                                               "Foto aufnehmen oder Daten von Hand eintragen.")
    sug = recognize.empty_suggestion()
    recognize.apply_label(sug, lab)
    await recognize.add_offers(db, sug)
    recognize.add_existing(db, sug)
    return _page(request, db, sug, code=code, ort=ort)


@router.post("/artikel/erkennen")
async def analyze(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    f = await request.form()
    code, ort, hint = (f.get("code") or "").strip(), (f.get("ort") or "").strip(), (f.get("hint") or "").strip()[:500]
    images, stored = [], []
    for up in f.getlist("photos")[:MAX_PHOTOS]:
        if getattr(up, "filename", "") and (data := await up.read()):
            if len(data) > 25_000_000:
                return _page(request, db, code=code, ort=ort, error="Foto zu groß (max. 25 MB).")
            images.append(data)
            stored.append(store_upload(data, up.filename))
    for name in f.getlist("stored")[:MAX_PHOTOS]:  # erneute Analyse mit Hinweis (Rückfragen beantwortet)
        data = recognize.stored_image_bytes(name)
        if data and name not in stored:
            images.append(data)
            stored.append(name)
    sug = recognize.empty_suggestion()
    sug["images"] = stored[:MAX_PHOTOS]
    lab = parse_label(code) if code else None
    if lab:
        recognize.apply_label(sug, lab)
    if images:
        if not vision.active(db):
            if not lab:
                return _page(request, db, code=code, ort=ort, error="Die Foto-Erkennung ist nicht eingerichtet (Verwaltung › Info-Provider › Bilderkennung). "
                                                                   "Tüten-Etikett scannen oder Daten von Hand eintragen.")
        else:
            try:
                res = await vision.analyze(images, vision.load_config(db), hint, recognize.category_choices(db), recognize.param_names(db))
                recognize.apply_vision(sug, res)
            except vision.VisionError as e:
                if not lab:
                    return _page(request, db, code=code, ort=ort, error=str(e))
                sug["hinweise"].append(str(e))
    elif not lab:
        return _page(request, db, code=code, ort=ort, error="Bitte ein Foto aufnehmen oder ein Tüten-Etikett scannen.")
    if hint:
        sug["hinweise"].insert(0, f"Dein Hinweis: {hint}")
    await recognize.add_offers(db, sug)
    recognize.add_existing(db, sug)
    return _page(request, db, sug, code=code, ort=ort)


@router.post("/artikel/erkennen/anlegen")
async def create(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    from .items import resolve_location
    from .providers import _clean_hit

    f = await request.form()
    name = (f.get("name") or "").strip()[:255]
    if not name:
        flash(request, "Bitte einen Namen eintragen.", "err")
        return redirect("/artikel/erkennen")
    kind = f.get("kind") if f.get("kind") in ITEM_KINDS else "bauteil"
    item = create_item(db, name, ctx.user_id, kind=kind, description=(f.get("description") or "")[:2000], mpn=(f.get("mpn") or "").strip()[:120],
                       origin=(f.get("origin") or "")[:20])
    man = (f.get("manufacturer") or "").strip()
    if man:
        item.manufacturer_id = ensure_manufacturer(db, man).id
    fp = (f.get("footprint") or "").strip()
    if fp:
        item.footprint_id = ensure_footprint(db, fp).id
    cat = (f.get("category") or "").strip()
    if cat:
        c = ensure_category_path(db, cat)
        item.category_id = c.id if c else None
    set_parameters_from_form(db, item, f.getlist("param_name"), f.getlist("param_value"), f.getlist("param_unit"))
    img = f.get("image") or ""
    if recognize.stored_image_bytes(img) is not None:
        item.image = img
    _, opts = load_config(db)
    n_hits = 0
    for raw in f.getlist("hit"):
        h = _clean_hit(raw)
        if h is not None:
            infosync.apply_hit(db, item, h, origin="manuell", fill={"datasheet"} | ({"image"} if not item.image else set()), rate=opts["usd_eur"])
            n_hits += 1
    try:
        qty = float(str(f.get("quantity") or "0").replace(",", ".") or 0)
        loc = resolve_location(db, f.get("location") or "") if (f.get("location") or "").strip() else None
        if loc or qty:
            lab = parse_label(f.get("code") or "") or {}
            stocksvc.add_stock(db, item, loc, qty, ctx.user_id, note="Neu angelegt (Erkennung)", batch=(lab.get("lot") or "")[:80])
    except ValueError as e:
        flash(request, str(e), "err")
    reindex(db, item)
    audit.log(db, ctx.user_id, "create", "item", item.id, f"Artikel {item.name} per Erkennung angelegt")
    db.commit()
    if not n_hits and item.mpn:
        from .providers import schedule_after_save

        schedule_after_save(db, item, ctx)
    flash(request, f"„{item.name}“ angelegt" + (f" mit {n_hits} Bezugsquelle(n)." if n_hits else "."))
    return redirect(f"/artikel/{item.id}")

