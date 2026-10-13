"""Artikel, Bestände (Lots), Katalog-Übernahme, Verleih."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (Attachment, Equivalent, Footprint, Item, ItemParameter, Loan, Location, Manufacturer, ParameterTemplate,
                      PriceInfo, StockLot, StockMovement, SupplierPart, User)
from ..security import check, current, get_setting, require
from ..services import audit
from ..services import stock as stocksvc
from ..services import walls
from ..services.codes import CodeConflict, add_item_code, add_lot_code, check_free, list_foreign, resolve
from ..services.items import (catalog_part, catalog_search, category_path, create_from_catalog, create_item, ensure_footprint,
                              ensure_manufacturer, reindex, set_parameter, set_parameters_from_form, store_upload)
from ..services.locations import TreeCache
from ..services.search import remove_from_index
from ..web import back, flash, redirect, render

router = APIRouter()


def resolve_location(db: Session, value: str | int | None) -> int | None:
    """Akzeptiert ID, interner Code, gescannter Code oder exakten Pfad."""
    if value in (None, ""):
        return None
    v = str(value).strip()
    if v.isdigit() and db.get(Location, int(v)):
        return int(v)
    r = resolve(db, v)
    if r.kind == "location":
        return r.id
    tc = TreeCache(db)
    low = " / ".join(p.strip() for p in v.lower().replace("›", "/").split("/"))
    paths = {lid: tc.path(lid).lower() for lid in tc.nodes}
    for lid, p in paths.items():  # exakter Pfad
        if p == low:
            return lid
    # Pfadende („Regalwand / Kleinteilemagazin2 / Fach4“) oder eindeutiger Name („Kleinteilemagazin1“)
    hits = [lid for lid, p in paths.items() if p.endswith(" / " + low)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(f"„{v}“ gibt es mehrfach – bitte genauer angeben, z. B. „{tc.path(hits[0])}“ oder „{tc.path(hits[1])}“.")
    raise ValueError(f"Lagerort „{v}“ nicht gefunden.")


def try_resolve_location(db: Session, value: str | int | None) -> tuple[int | None, str]:
    """Wie resolve_location, aber ohne Ausnahme: (ID oder None, Fehlermeldung)."""
    try:
        return resolve_location(db, value), ""
    except ValueError as e:
        return None, str(e)


def _item_or_404(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Artikel nicht gefunden")
    return item


# ------------------------------------------------------------------ Katalog
@router.get("/katalog")
def catalog_page(request: Request, q: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    results = catalog_search(q, 60) if q else []
    existing = {}
    if results:
        keys = [r["key"] for r in results]
        existing = {k: i for k, i in db.execute(select(Item.catalog_key, Item.id).where(Item.catalog_key.in_(keys))).all()}
    tpl = "partials/catalog_results.html" if request.headers.get("hx-request") else "catalog.html"
    return render(request, tpl, {"q": q, "results": results, "existing": existing})


@router.post("/katalog/uebernehmen")
def catalog_take(request: Request, key: str = Form(...), location: str = Form(""), quantity: float = Form(0),
                 db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    try:
        loc_id = resolve_location(db, location)
    except ValueError as e:
        flash(request, str(e), "err")
        return back(request)
    try:
        item = create_from_catalog(db, key, ctx.user_id)
    except KeyError:
        flash(request, "Katalogeintrag nicht gefunden.", "err")
        return back(request)
    if loc_id or quantity:
        stocksvc.add_stock(db, item, loc_id, quantity, ctx.user_id, note="Neu aus Katalog")
    audit.log(db, ctx.user_id, "create", "item", item.id, f"Aus Katalog: {item.name}")
    db.commit()
    flash(request, f"„{item.name}“ aus dem Katalog angelegt.")
    return redirect(f"/artikel/{item.id}")


# ------------------------------------------------------------------ Community-Bauteile (v0.28)
@router.get("/katalog/community")
def community_catalog(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    from ..services import sharing
    from ..services import template_catalog as tc

    url = sharing.parts_url(db)
    entries, error = [], ""
    try:
        entries = sharing.fetch_parts(url)
    except tc.CatalogError as e:
        error = str(e)
    have_names = {n.lower() for n in db.scalars(select(Item.name))}
    have_mpn = {m.lower() for m in db.scalars(select(Item.mpn)) if m}
    for i, e in enumerate(entries):
        e["idx"] = i
        e["have"] = e["name"].lower() in have_names or bool(e["mpn"] and e["mpn"].lower() in have_mpn)
        e["warn"] = sharing.part_problems(db, e)[1]
    cats = sorted({e["category"].split("/")[0] for e in entries})
    return render(request, "community_parts.html", {"entries": entries, "error": error, "url": url, "default_url": sharing.PARTS_URL, "cats": cats})


@router.post("/katalog/community/import")
async def community_import(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    from ..services import sharing
    from ..services import template_catalog as tc

    f = await request.form()
    wanted = {n.lower() for n in f.getlist("name")}
    if not wanted:
        flash(request, "Nichts ausgewählt.", "err")
        return redirect("/katalog/community")
    try:
        entries = [e for e in sharing.fetch_parts(sharing.parts_url(db)) if e["name"].lower() in wanted]  # frisch holen
    except tc.CatalogError as e:
        flash(request, str(e), "err")
        return redirect("/katalog/community")
    done = sharing.import_parts(db, entries, ctx.user_id)
    audit.log(db, ctx.user_id, "create", "item", None, f"{len(done)} Bauteile aus dem Community-Katalog importiert")
    db.commit()
    flash(request, f"{len(done)} Bauteil(e) angelegt (ohne Bestand)." if done else "Alles schon vorhanden.")
    return redirect("/katalog/community" if not done else "/suche")


@router.post("/katalog/community/adresse")
def community_url(request: Request, url: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("items", 4))):
    from ..security import set_setting

    url = url.strip()
    if url and not url.lower().startswith("https://"):
        flash(request, "Die Adresse muss mit https:// beginnen.", "err")
    else:
        set_setting(db, "parts_catalog_url", url)
        db.commit()
        flash(request, "Adresse gespeichert." if url else "Standard-Katalog wird verwendet.")
    return redirect("/katalog/community")


@router.get("/artikel/{item_id}/teilen")
def item_share(item_id: int, request: Request, author: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    from ..services import sharing

    item = _item_or_404(db, item_id)
    contrib = sharing.part_contribution(db, item, author)
    errs, warns = sharing.part_problems(db, contrib)
    url, full = ("", True) if errs else sharing.issue_url("bauteil", contrib)
    return render(request, "share.html", {"what": "Bauteil", "title": item.name, "contrib": contrib, "errs": errs, "warns": warns, "url": url,
                                           "full": full, "author": author, "neu": 0, "action": f"/artikel/{item.id}/teilen",
                                           "back": f"/artikel/{item.id}",
                                           "shared": ["Name, Kategorie, Beschreibung", "Gehäuse, Hersteller, Typnummer, Einheit, Schlagwörter", "Parameter (Wert, Einheit)"],
                                           "not_shared": ["Bestand, Lagerorte, Preise, Lieferanten", "Fotos, Notizen, Barcodes und alles andere aus deiner Installation"],
                                           "json_name": "bauteil.json", "repo": sharing.REPO})


# ------------------------------------------------------------------ Artikel anlegen/bearbeiten
def _form_ctx(db: Session, item: Item | None = None) -> dict:
    from .pages import _category_options

    return {
        "item": item,
        "cats": _category_options(db),
        "footprints": list(db.scalars(select(Footprint).order_by(Footprint.name))),
        "manufacturers": list(db.scalars(select(Manufacturer).order_by(Manufacturer.name))),
        "param_templates": list(db.scalars(select(ParameterTemplate).order_by(ParameterTemplate.name))),
        "templates": list(db.scalars(select(Item).where(Item.is_template.is_(True)).order_by(Item.name))),
    }


@router.get("/artikel/neu")
def item_new_form(request: Request, kategorie: int | None = None, ort: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    c = _form_ctx(db)
    c.update({"preset_category": kategorie, "preset_location": ort})
    return render(request, "item_form.html", c)


async def _apply_form(request: Request, db: Session, item: Item) -> None:
    f = await request.form()
    item.name = (f.get("name") or item.name or "").strip()
    item.description = f.get("description", "")
    item.kind = f.get("kind", "bauteil")
    item.category_id = int(f["category_id"]) if f.get("category_id") else None
    fp_name = (f.get("footprint") or "").strip()
    item.footprint_id = ensure_footprint(db, fp_name).id if fp_name else None
    man = (f.get("manufacturer") or "").strip()
    item.manufacturer_id = ensure_manufacturer(db, man).id if man else None
    item.mpn = (f.get("mpn") or "").strip()
    item.tags = f.get("tags", "")
    item.unit = f.get("unit") or "Stk"
    item.stock_mode = f.get("stock_mode", "exact")
    item.min_stock = float((f.get("min_stock") or "0").replace(",", ".") or 0)
    if "reorder_qty" in f:
        item.reorder_qty = max(0.0, float((f.get("reorder_qty") or "0").replace(",", ".") or 0))
    item.notes = f.get("notes", "")
    item.origin = f.get("origin", "")
    item.historic_manufacturer = f.get("historic_manufacturer", "")
    item.date_code = f.get("date_code", "")
    item.condition = f.get("condition", "")
    cv = (f.get("collector_value") or "").replace(",", ".")
    item.collector_value = float(cv) if cv else None
    item.is_template = bool(f.get("is_template"))
    item.variant_of_id = int(f["variant_of_id"]) if f.get("variant_of_id") else None
    item.purchase_vendor = f.get("purchase_vendor", "")
    for fld in ("purchase_date", "warranty_until"):
        v = f.get(fld)
        setattr(item, fld, date.fromisoformat(v) if v else None)
    pp = (f.get("purchase_price") or "").replace(",", ".")
    item.purchase_price = float(pp) if pp else None
    set_parameters_from_form(db, item, f.getlist("pname"), f.getlist("pvalue"), f.getlist("punit"))
    img = f.get("image_file")
    if img is not None and getattr(img, "filename", ""):
        data = await img.read()
        if data:
            item.image = store_upload(data, img.filename)
    img_url = (f.get("image_url") or "").strip()
    if img_url:
        item.image = _download_image(img_url) or item.image


def _download_image(url: str) -> str | None:
    """Lädt ein Bild von einer http(s)-URL (max. 10 MB, nur Rasterbilder)."""
    import httpx

    from ..services.items import IMAGE_EXTENSIONS, safe_url

    url = safe_url(url)
    if not url:
        return None
    try:
        with httpx.stream("GET", url, timeout=10, follow_redirects=True) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp"}.get(ctype)
            if ext not in IMAGE_EXTENSIONS:
                return None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > 10_000_000:
                    return None
        return store_upload(bytes(buf), f"bild.{ext}")
    except Exception:  # noqa: BLE001
        return None


@router.post("/artikel/neu")
async def item_new(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    f = await request.form()
    item = create_item(db, f.get("name", "Neuer Artikel"), ctx.user_id)
    await _apply_form(request, db, item)
    loc = f.get("location", "")
    qty = float((f.get("quantity") or "0").replace(",", ".") or 0)
    if loc or qty:
        try:
            stocksvc.add_stock(db, item, resolve_location(db, loc), qty, ctx.user_id, note="Neu angelegt")
        except ValueError as e:
            flash(request, str(e), "err")
    if f.get("barcode"):
        add_item_code(db, item.id, f["barcode"], "barcode")
    reindex(db, item)
    audit.log(db, ctx.user_id, "create", "item", item.id, f"Artikel {item.name}")
    db.commit()
    from .providers import schedule_after_save

    schedule_after_save(db, item, ctx)
    flash(request, f"„{item.name}“ angelegt." + (" Bestellnummern und Preise werden im Hintergrund gesucht." if item.mpn else ""))
    return redirect(f"/artikel/{item.id}")


@router.get("/artikel/{item_id}")
def item_detail(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    item = _item_or_404(db, item_id)
    tc = TreeCache(db)
    moves = list(db.scalars(select(StockMovement).where(StockMovement.item_id == item.id).order_by(StockMovement.id.desc()).limit(50)))
    names = {item.name, item.mpn} - {""}
    eqs, seen_eq = [], set()
    for e in db.scalars(select(Equivalent).where(or_(Equivalent.type_a.in_(names), Equivalent.type_b.in_(names)))):
        other = e.type_b if e.type_a in names else e.type_a
        key = other.split(" ")[0].upper()
        if key not in seen_eq:
            seen_eq.add(key)
            eqs.append(e)
    attachments = list(db.scalars(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == item.id)))
    sparts = list(db.scalars(select(SupplierPart).where(SupplierPart.item_id == item.id)))
    # Preise ohne Bezugsquelle (Altdaten) separat zeigen; die übrigen hängen an der Bezugsquelle
    prices = list(db.scalars(select(PriceInfo).where(PriceInfo.item_id == item.id, PriceInfo.supplier_part_id.is_(None)).order_by(PriceInfo.min_qty)))
    from ..models import ShoppingItem, Supplier
    from ..services import purchasing as pur

    need = pur.need_qty(item)
    loans = list(db.scalars(select(Loan).where(Loan.item_id == item.id).order_by(Loan.id.desc())))
    users = list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.display_name)))
    query = item.mpn or item.name
    web_links = [(w["name"], w["url"].replace("{query}", query.replace(" ", "+"))) for w in get_setting(db, "web_search_urls", [])]
    catalog = catalog_part(item.catalog_key) if item.catalog_key else None
    from ..services.labeldesign import spool_templates

    spool_tpls = spool_templates(db) if item.kind == "filament" else None
    return render(request, "item_detail.html", {
        "spool_tpls": spool_tpls, "item": item, "tc": tc, "moves": moves, "eqs": eqs, "attachments": attachments, "sparts": sparts, "prices": prices,
        "loans": loans, "users": users, "web_links": web_links, "cat_path": category_path(item.category), "catalog": catalog,
        "lit": (lit := _lit_locations(db, item)), "has_lights": bool(lit),
        "walls_of": {lot.id: w.id for lot in item.lots if (w := walls.wall_of(lot.location)) is not None},
        "need": need, "offers": pur.offers_for(item, need, sparts), "shop_link": pur.shop_link, "format_tiers": pur.format_tiers,
        "search_link": pur.search_link, "suppliers": list(db.scalars(select(Supplier).where(Supplier.active.is_(True)).order_by(Supplier.name))),
        "on_list": db.scalar(select(ShoppingItem).where(ShoppingItem.item_id == item.id, ShoppingItem.status.in_(("open", "ordered"))).limit(1)),
        "online_active": bool(_active_providers(db)),
        "open_questions": _open_questions(db, item.id),
        "res": _reservation(db, item),
    })


def _active_providers(db: Session) -> list:
    from ..services.providers import active_providers

    return active_providers(db)


def _open_questions(db: Session, item_id: int) -> int:
    from sqlalchemy import func

    from ..models import ProviderCandidate

    return db.scalar(select(func.count(ProviderCandidate.id)).where(ProviderCandidate.item_id == item_id, ProviderCandidate.status == "open")) or 0


def _lit_locations(db: Session, item: Item) -> set[int]:
    from ..services.wled import resolve_targets

    return {lot.location_id for lot in item.lots if lot.location and resolve_targets(db, lot.location)}


@router.get("/artikel/{item_id}/bearbeiten")
def item_edit_form(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    return render(request, "item_form.html", _form_ctx(db, _item_or_404(db, item_id)))


@router.post("/artikel/{item_id}/bearbeiten")
async def item_edit(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    item = _item_or_404(db, item_id)
    before = audit.snapshot(item)
    await _apply_form(request, db, item)
    reindex(db, item)
    audit.log(db, ctx.user_id, "update", "item", item.id, f"Artikel {item.name} geändert", audit.diff(before, audit.snapshot(item)))
    db.commit()
    if item.mpn and item.mpn != before.get("mpn"):  # neue Hersteller-Nr. -> Bestellnummern suchen
        from .providers import schedule_after_save

        schedule_after_save(db, item, ctx)
    flash(request, "Gespeichert.")
    return redirect(f"/artikel/{item.id}")


@router.post("/artikel/{item_id}/duplizieren")
def item_duplicate(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    src = _item_or_404(db, item_id)
    cols = {c: getattr(src, c) for c in ("description", "kind", "category_id", "manufacturer_id", "mpn", "footprint_id", "tags", "unit",
                                          "stock_mode", "min_stock", "reorder_qty", "image", "notes", "origin", "historic_manufacturer", "variant_of_id")}
    new = create_item(db, f"{src.name} (Kopie)", ctx.user_id, **cols)
    for p in src.parameters:
        set_parameter(db, new, p.name, p.value_text, p.unit)
    reindex(db, new)
    audit.log(db, ctx.user_id, "create", "item", new.id, f"Dupliziert von {src.name}")
    db.commit()
    return redirect(f"/artikel/{new.id}/bearbeiten")


@router.post("/artikel/{item_id}/loeschen")
def item_delete(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 4))):
    item = _item_or_404(db, item_id)
    name = item.name
    for v in item.variants:
        v.variant_of_id = None
    remove_from_index(db, item.id)
    audit.log(db, ctx.user_id, "delete", "item", item.id, f"Artikel {name} gelöscht", {"snapshot": [audit.snapshot(item), None]})
    db.delete(item)
    db.commit()
    flash(request, f"„{name}“ gelöscht.")
    return redirect("/suche")


def _reservation(db: Session, item: Item):
    from ..services.orders import reservation

    return reservation(db, item)


@router.post("/artikel/sammel")
async def bulk(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Mehrfachauswahl in der Artikelliste: umlagern, Kategorie setzen, löschen."""
    f = await request.form()
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()][:1000]
    items = [i for i in (db.get(Item, x) for x in ids) if i is not None]
    action = str(f.get("action") or "")
    if not items:
        flash(request, "Keine Artikel ausgewählt.", "err")
        return back(request, "/suche")
    if action == "etiketten":
        check(ctx, "labels", 1)
        return redirect("/etiketten?art=item&artikel=" + ",".join(str(i.id) for i in items))
    try:
        if action == "loeschen":
            check(ctx, "items", 4)
            for item in items:
                for v in item.variants:
                    v.variant_of_id = None
                remove_from_index(db, item.id)
                db.delete(item)
            audit.log(db, ctx.user_id, "delete", "item", None, f"{len(items)} Artikel gelöscht: " + ", ".join(i.name for i in items[:20]))
            msg = f"{len(items)} Artikel gelöscht."
        elif action == "umlagern":
            check(ctx, "stock", 2)
            target = resolve_location(db, str(f.get("ziel") or ""))
            if target is None:
                raise ValueError("Ziel-Lagerort nicht gefunden.")
            n = 0
            for item in items:
                for lot in list(item.lots):
                    if lot.location_id != target:
                        stocksvc.move_stock(db, lot, target, None, ctx.user_id, "Sammel-Umlagerung")
                        n += 1
            msg = f"{n} Bestände von {len(items)} Artikeln nach „{db.get(Location, target).name}“ umgelagert."
        elif action == "kategorie":
            check(ctx, "items", 2)
            raw = str(f.get("kategorie") or "")
            cat_id = int(raw) if raw.isdigit() else None
            for item in items:
                item.category_id = cat_id
                reindex(db, item)
            audit.log(db, ctx.user_id, "update", "item", None, f"Kategorie für {len(items)} Artikel gesetzt")
            msg = f"Kategorie für {len(items)} Artikel gesetzt."
        else:
            raise ValueError("Bitte eine Aktion wählen.")
    except (ValueError, stocksvc.StockError) as e:
        db.rollback()
        flash(request, str(e), "err")
        return back(request, "/suche")
    db.commit()
    flash(request, msg)
    return back(request, "/suche")


@router.post("/artikel/{item_id}/zusammenfuehren")
def item_merge(item_id: int, request: Request, target: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("items", 4))):
    from ..services.items import merge_items

    src = _item_or_404(db, item_id)
    r = resolve(db, target)
    tgt = db.get(Item, r.id) if r.kind == "item" else None
    if tgt is None and target.strip().isdigit():
        tgt = db.get(Item, int(target))
    if tgt is None:
        flash(request, "Ziel-Artikel nicht gefunden (Code A-… oder ID angeben).", "err")
        return back(request)
    name = src.name
    try:
        merge_items(db, src, tgt)
    except ValueError as e:
        flash(request, str(e), "err")
        return back(request)
    audit.log(db, ctx.user_id, "update", "item", tgt.id, f"„{name}“ in „{tgt.name}“ zusammengeführt")
    db.commit()
    flash(request, f"„{name}“ wurde in „{tgt.name}“ zusammengeführt.")
    return redirect(f"/artikel/{tgt.id}")


@router.post("/artikel/{item_id}/favorit")
def item_fav(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    item = _item_or_404(db, item_id)
    item.favorite = not item.favorite
    db.commit()
    return back(request, f"/artikel/{item_id}")


@router.post("/artikel/{item_id}/code")
def item_add_code(item_id: int, request: Request, code: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    """Ein oder mehrere Fremdcodes (je Zeile einer) dem Artikel zuordnen – Handscanner senden nach jedem Code Enter."""
    item = _item_or_404(db, item_id)
    added = 0
    for line in [x for x in code.splitlines() if x.strip()]:
        try:
            key = check_free(db, line, "item", item_id)
        except CodeConflict as e:
            flash(request, str(e), "err")
            continue
        add_item_code(db, item_id, key, "barcode")
        added += 1
    reindex(db, item)
    db.commit()
    if added:
        flash(request, "Code zugeordnet." if added == 1 else f"{added} Codes zugeordnet.")
    return redirect(f"/artikel/{item_id}?codes=1#codes")


@router.post("/artikel/code/{code_id}/loeschen")
def item_code_delete(code_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    from ..models import ItemCode

    c = db.get(ItemCode, code_id)
    if c:
        item_id = c.item_id
        db.delete(c)
        db.commit()
        return back(request, f"/artikel/{item_id}")
    return back(request)


@router.post("/lot/{lot_id}/code")
def lot_add_code(lot_id: int, request: Request, code: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    """Fremdcode (z. B. Herstellercode einer Filamentspule) genau diesem Bestand zuordnen."""
    lot = _lot(db, lot_id)
    added = 0
    for line in [x for x in code.splitlines() if x.strip()]:
        try:
            key = check_free(db, line, "lot", lot.id)
        except CodeConflict as e:
            flash(request, str(e), "err")
            continue
        add_lot_code(db, lot.id, key, "manuell")
        added += 1
    db.commit()
    if added:
        flash(request, f"Code dem Bestand {lot.code} zugeordnet.")
    return redirect(f"/artikel/{lot.item_id}#lot-{lot.id}")


@router.post("/lot/code/{code_id}/loeschen")
def lot_code_delete(code_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    from ..models import LotCode, StockLot

    c = db.get(LotCode, code_id)
    if c:
        lot = db.get(StockLot, c.lot_id)
        db.delete(c)
        db.commit()
        return redirect(f"/artikel/{lot.item_id}#lot-{lot.id}" if lot else "/codes")
    return back(request)


@router.get("/codes")
def foreign_codes(request: Request, q: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Übersicht aller Fremdcodes (fremde Barcodes, die auf Lagerorte, Artikel und Bestände zeigen)."""
    return render(request, "codes.html", {"rows": list_foreign(db, q), "q": q})


@router.post("/artikel/{item_id}/anhang")
async def item_attachment(item_id: int, request: Request, title: str = Form(""), kind: str = Form("datasheet"), url: str = Form(""),
                          file: UploadFile | None = File(None), db: Session = Depends(get_db), ctx=Depends(require("attachments", 3))):
    fn = None
    if file is not None and file.filename:
        fn = store_upload(await file.read(), file.filename, "attachments")
    if not fn and not url:
        flash(request, "Datei oder URL angeben.", "err")
        return back(request)
    from ..services.items import safe_url

    url = safe_url(url)
    if not fn and not url:
        flash(request, "Nur http(s)-Links sind erlaubt.", "err")
        return back(request)
    db.add(Attachment(owner_type="item", owner_id=item_id, kind=kind, title=title or (file.filename if file else url), filename=fn, url=url or None))
    db.commit()
    return redirect(f"/artikel/{item_id}#anhaenge")


@router.post("/anhang/{att_id}/loeschen")
def attachment_delete(att_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("attachments", 4))):
    a = db.get(Attachment, att_id)
    if a:
        db.delete(a)
        db.commit()
    return back(request)


# ------------------------------------------------------------------ Bestand
@router.post("/artikel/{item_id}/einlagern")
def item_stock_add(item_id: int, request: Request, location: str = Form(""), quantity: float = Form(1), batch: str = Form(""),
                   serial: str = Form(""), note: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    item = _item_or_404(db, item_id)
    try:
        lot = stocksvc.add_stock(db, item, resolve_location(db, location), quantity, ctx.user_id, note=note, batch=batch, serial=serial)
    except ValueError as e:
        flash(request, str(e), "err")
        return back(request)
    db.commit()
    flash(request, f"{quantity:g} {item.unit} eingelagert ({lot.location.name if lot.location else 'ohne Ort'}).")
    return redirect(f"/artikel/{item.id}")


def _lot(db: Session, lot_id: int) -> StockLot:
    lot = db.get(StockLot, lot_id)
    if lot is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Bestand nicht gefunden")
    return lot


@router.get("/lot/{lot_id}")
def lot_page(lot_id: int, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    lot = _lot(db, lot_id)
    return redirect(f"/artikel/{lot.item_id}#lot-{lot.id}")


@router.post("/lot/{lot_id}/entnehmen")
def lot_remove(lot_id: int, request: Request, quantity: float = Form(1), note: str = Form(""), db: Session = Depends(get_db),
               ctx=Depends(require("stock", 2))):
    lot = _lot(db, lot_id)
    try:
        stocksvc.remove_stock(db, lot, quantity, ctx.user_id, note)
    except ValueError as e:
        flash(request, str(e), "err")
        return back(request)
    db.commit()
    flash(request, f"{quantity:g} {lot.item.unit} entnommen – Rest {lot.quantity:g}.")
    return back(request, f"/artikel/{lot.item_id}")


@router.post("/lot/{lot_id}/umlagern")
def lot_move(lot_id: int, request: Request, location: str = Form(...), quantity: str = Form(""), db: Session = Depends(get_db),
             ctx=Depends(require("stock", 2))):
    lot = _lot(db, lot_id)
    item_id = lot.item_id
    try:
        q = float(quantity.replace(",", ".")) if quantity.strip() else None
        stocksvc.move_stock(db, lot, resolve_location(db, location), q, ctx.user_id)
    except ValueError as e:
        flash(request, str(e), "err")
        return back(request)
    db.commit()
    flash(request, "Umgelagert.")
    return redirect(f"/artikel/{item_id}")


@router.post("/lot/{lot_id}/zaehlen")
def lot_count(lot_id: int, request: Request, quantity: float = Form(...), db: Session = Depends(get_db), ctx=Depends(require("inventory", 2))):
    lot = _lot(db, lot_id)
    stocksvc.count_stock(db, lot, quantity, ctx.user_id)
    db.commit()
    return back(request, f"/artikel/{lot.item_id}")


@router.post("/lot/{lot_id}/ampel")
def lot_coarse(lot_id: int, request: Request, level: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    lot = _lot(db, lot_id)
    stocksvc.set_coarse(db, lot, level, ctx.user_id)
    db.commit()
    return back(request, f"/artikel/{lot.item_id}")


@router.post("/lot/{lot_id}/bestaetigen")
def lot_confirm(lot_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("inventory", 2))):
    lot = _lot(db, lot_id)
    stocksvc.confirm_lot(db, lot, ctx.user_id)
    db.commit()
    return back(request, f"/artikel/{lot.item_id}")


@router.post("/lot/{lot_id}/bearbeiten")
def lot_edit(lot_id: int, request: Request, batch: str = Form(""), serial: str = Form(""), status: str = Form("ok"), notes: str = Form(""),
             expiry: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    lot = _lot(db, lot_id)
    before = audit.snapshot(lot)
    lot.batch, lot.serial, lot.status, lot.notes = batch, serial, status, notes
    lot.expiry = date.fromisoformat(expiry) if expiry else None
    audit.log(db, ctx.user_id, "update", "lot", lot.id, f"Bestand {lot.code} geändert", audit.diff(before, audit.snapshot(lot)))
    db.commit()
    return back(request, f"/artikel/{lot.item_id}")


@router.post("/lot/{lot_id}/loeschen")
def lot_delete(lot_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 4))):
    lot = _lot(db, lot_id)
    check(ctx, "items", 2)
    item_id = lot.item_id
    audit.log(db, ctx.user_id, "delete", "lot", lot.id, f"Bestand {lot.code} gelöscht ({lot.quantity:g})")
    db.delete(lot)
    db.commit()
    return redirect(f"/artikel/{item_id}")


# ------------------------------------------------------------------ Verleih
@router.post("/artikel/{item_id}/verleihen")
def loan_new(item_id: int, request: Request, borrower_user_id: int | None = Form(None), borrower_name: str = Form(""),
             due: str = Form(""), note: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("loans", 3))):
    item = _item_or_404(db, item_id)
    if not borrower_user_id and not borrower_name.strip():
        flash(request, "Bitte angeben, an wen verliehen wird.", "err")
        return back(request)
    db.add(Loan(item_id=item.id, borrower_user_id=borrower_user_id or None, borrower_name=borrower_name.strip(),
                due=date.fromisoformat(due) if due else None, note=note, created_by=ctx.user_id))
    db.add(StockMovement(item_id=item.id, kind="loan", delta=0, user_id=ctx.user_id, note=f"verliehen an {borrower_name or borrower_user_id}"))
    db.commit()
    flash(request, "Verleih eingetragen.")
    return redirect(f"/artikel/{item.id}")


@router.post("/leihe/{loan_id}/zurueck")
def loan_return(loan_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("loans", 2))):
    loan = db.get(Loan, loan_id)
    if loan is None or loan.returned_at:
        return back(request, "/verleih")
    loan.returned_at = date.today()
    db.add(StockMovement(item_id=loan.item_id, kind="returned", delta=0, user_id=ctx.user_id, note="zurückgegeben"))
    db.commit()
    flash(request, "Rückgabe eingetragen.")
    return back(request, f"/artikel/{loan.item_id}")


@router.get("/verleih")
def loans_page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("loans", 1))):
    rows = list(db.scalars(select(Loan).order_by(Loan.returned_at.is_not(None), Loan.due)))
    users = {u.id: u.label for u in db.scalars(select(User))}
    return render(request, "loans.html", {"rows": rows, "users": users, "today": date.today()})


_ = (ItemParameter, current)
