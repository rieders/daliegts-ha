"""Lagerorte: Baum, Detail/Raster, Massenanlage, Lagermöbel-Assistent, Inventur."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Location, LocationAlias, LocationLight, StockLot, StorageTemplate, WledController
from ..security import require
from ..services import audit
from ..services import locations as locsvc
from ..services import walls
from ..services.codes import add_location_alias
from ..services.items import store_upload
from ..services.wled import resolve_targets
from ..web import back, flash, redirect, render

router = APIRouter()


def _loc(db: Session, loc_id: int) -> Location:
    loc = db.get(Location, loc_id)
    if loc is None:
        raise HTTPException(404, "Lagerort nicht gefunden")
    return loc


def _counts(db: Session) -> dict[int, int]:
    return dict(db.execute(select(StockLot.location_id, func.count()).group_by(StockLot.location_id)).all())


@router.get("/orte")
def tree(request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    tc = locsvc.TreeCache(db)
    counts = _counts(db)
    sub: dict[int, int] = {}
    for lid, n in counts.items():  # Bestände inkl. Unterorte
        for a in [lid, *tc.ancestors(lid)]:
            sub[a] = sub.get(a, 0) + n
    tpls = list(db.scalars(select(StorageTemplate).order_by(StorageTemplate.name)))
    return render(request, "locations.html", {"tc": tc, "counts": counts, "sub": sub, "rooms": walls.rooms(db), "templates": tpls,
                                              "tpl_data": [_tpl_preview(t) for t in tpls]})


def _tpl_preview(t: StorageTemplate) -> dict:
    """Daten für die Vorschau einer Möbelvorlage: Rasterzellen, Maße, Hinweis."""
    spec = locsvc.spec_from_template(t, t.name)
    cells = locsvc._cells(spec)
    d = t.definition or {}
    return {"id": t.id, "name": t.name, "short": t.name.split(" (")[0], "kind": t.kind, "builtin": bool(t.builtin),
            "group": walls.template_group(t.name, bool(t.builtin), t.definition), "notes": t.notes or "",
            "w": d.get("width_cm"), "h": d.get("height_cm"), "rows": spec.rows, "cols": locsvc.grid_width(spec, cells),
            "sub": spec.subdivision, "cells": cells}


@router.post("/vorlagen/{tpl_id}/anlegen")
async def template_create(tpl_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    """Möbel aus einer Vorlage direkt unter einem Ort anlegen (Ziehen der Vorschau auf den Ort oder Ortsauswahl)."""
    from .items import try_resolve_location

    tpl = db.get(StorageTemplate, tpl_id)
    if tpl is None:
        raise HTTPException(404, "Vorlage nicht gefunden")
    f = await request.form()
    parent_id: int | None = None
    if (f.get("parent_id") or "").isdigit():
        parent_id = int(f["parent_id"])
        if db.get(Location, parent_id) is None:
            raise HTTPException(404, "Ort nicht gefunden")
    elif (f.get("ziel") or "").strip() and f["ziel"].strip() != "/":
        parent_id, err = try_resolve_location(db, f["ziel"].strip())
        if parent_id is None:
            raise HTTPException(400, err or "Ort nicht gefunden")
    name = (f.get("name") or "").strip() or tpl.name.split(" (")[0]
    spec = locsvc.spec_from_template(tpl, name)
    furniture = locsvc.create_furniture(db, spec, parent_id)
    d = tpl.definition or {}
    if d.get("width_cm") and d.get("height_cm"):
        furniture.wall_w, furniture.wall_h = float(d["width_cm"]), float(d["height_cm"])
    parent = db.get(Location, parent_id) if parent_id else None
    if parent is not None and any(c.wall_x is not None for c in parent.children):
        walls.place(db, parent, furniture)
    audit.log(db, ctx.user_id, "create", "location", furniture.id, f"Lagermöbel {furniture.name} aus Vorlage „{tpl.name}“ angelegt")
    db.commit()
    flash(request, f"„{furniture.name}“ mit {len(furniture.children)} Fächern aus der Vorlage angelegt.")
    return {"ok": True, "id": furniture.id, "url": f"/orte/{furniture.id}"}


@router.get("/orte/suche")
def suggest(q: str = "", db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    """Autovervollständigung: liefert Pfad + Code (für datalist)."""
    tc = locsvc.TreeCache(db)
    ql = q.lower().strip()
    out = []
    for lid, node in tc.nodes.items():
        path = tc.path(lid)
        if not ql or ql in path.lower() or ql == node.code.lower():
            out.append({"id": lid, "code": node.code, "path": path})
    out.sort(key=lambda r: (len(r["path"]), locsvc.natural_key(r["path"])))
    return out[:30]


@router.get("/orte/{loc_id}")
def detail(loc_id: int, request: Request, ansicht: str = "", db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    loc = _loc(db, loc_id)
    from_list = "ansicht=liste" in request.headers.get("referer", "")  # nach Formularen auf der Liste bleiben
    if ansicht != "liste" and not from_list and not loc.grid_rows and (any(c.wall_x is not None for c in loc.children) or walls.areas_of(loc)):
        return redirect(f"/orte/{loc.id}/wand")  # Räume mit angeordneten Möbeln öffnen direkt die Lageransicht
    tc = locsvc.TreeCache(db)
    sub_ids = tc.descendants(loc.id)
    lots_here = list(db.scalars(select(StockLot).where(StockLot.location_id == loc.id)))
    lots_below = list(db.scalars(select(StockLot).where(StockLot.location_id.in_(sub_ids)))) if sub_ids else []
    # Rasterinhalt (für Möbel): Fach -> Artikelnamen
    grid = None
    if loc.grid_rows:
        cell_items: dict[int, list[StockLot]] = {}
        for lot in lots_below:
            cell = lot.location
            while cell is not None and cell.parent_id != loc.id:
                cell = cell.parent
            if cell is not None:
                cell_items.setdefault(cell.id, []).append(lot)
        grid = {"cells": [c for c in loc.children if c.grid_row], "items": cell_items}
    targets = resolve_targets(db, loc)
    controllers = list(db.scalars(select(WledController).order_by(WledController.name)))
    return render(request, "location_detail.html", {
        "loc": loc, "tc": tc, "path_ids": tc.ancestors(loc.id), "lots_here": lots_here, "lots_below": lots_below, "grid": grid,
        "targets": targets, "controllers": controllers, "counts": (counts := _counts(db)),
        "subcounts": {c.id: sum(counts.get(i, 0) for i in [c.id, *tc.descendants(c.id)]) for c in loc.children},
        "templates": list(db.scalars(select(StorageTemplate).order_by(StorageTemplate.name))),
    })


@router.post("/orte/neu")
def create(request: Request, name: str = Form(...), parent_id: int | None = Form(None), type: str = Form("sonstiges"),
           description: str = Form(""), zurueck: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    if not name.strip():
        flash(request, "Bitte einen Namen angeben.", "err")
        return redirect(zurueck if zurueck.startswith("/orte") else "/orte")
    if parent_id and db.get(Location, parent_id) is None:
        raise HTTPException(404, "Übergeordneter Lagerort nicht gefunden")
    loc = locsvc.create_location(db, name, parent_id or None, type, description=description)
    audit.log(db, ctx.user_id, "create", "location", loc.id, f"Lagerort {loc.name}")
    db.commit()
    if zurueck.startswith("/orte"):
        flash(request, f"„{loc.name}“ angelegt.")
        return redirect(f"{zurueck}#ort-{loc.id}")
    return redirect(f"/orte/{loc.id}")


@router.post("/orte/text")
def bulk(request: Request, text: str = Form(""), parent_id: int | None = Form(None), db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    n = locsvc.create_from_outline(db, text, parent_id or None)
    audit.log(db, ctx.user_id, "create", "location", parent_id, f"{n} Lagerorte per Text angelegt")
    db.commit()
    flash(request, f"{n} Lagerorte angelegt.")
    return redirect(f"/orte/{parent_id}" if parent_id else "/orte")


@router.post("/orte/text/vorschau")
def bulk_preview(request: Request, text: str = Form(""), ctx=Depends(require("locations", 1))):
    nodes = locsvc.parse_outline(text)
    return render(request, "partials/outline_preview.html", {"nodes": nodes})


@router.get("/orte-export.txt")
def export_txt(db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    return PlainTextResponse(locsvc.export_outline(db), headers={"Content-Disposition": "attachment; filename=lagerorte.txt"})


@router.post("/orte/{loc_id}/bearbeiten")
async def edit(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 2))):
    loc = _loc(db, loc_id)
    f = await request.form()
    before = audit.snapshot(loc)
    loc.name = (f.get("name") or loc.name).strip()
    loc.type = f.get("type", loc.type)
    loc.description = f.get("description", "")
    loc.structural = bool(f.get("structural"))
    loc.is_full = bool(f.get("is_full"))
    if f.get("light_mode"):
        loc.light_mode = f["light_mode"]
    photo = f.get("photo")
    if photo is not None and getattr(photo, "filename", ""):
        data = await photo.read()
        if data:
            loc.photo = store_upload(data, photo.filename)
    audit.log(db, ctx.user_id, "update", "location", loc.id, f"Lagerort {loc.name} geändert", audit.diff(before, audit.snapshot(loc)))
    db.commit()
    flash(request, "Gespeichert.")
    return redirect(f"/orte/{loc.id}")


@router.post("/orte/{loc_id}/umbenennen")
def rename(loc_id: int, request: Request, name: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("locations", 2))):
    loc = _loc(db, loc_id)
    before = audit.snapshot(loc)
    loc.name = name.strip()
    audit.log(db, ctx.user_id, "update", "location", loc.id, f"Umbenannt: {before['name']} → {loc.name}", audit.diff(before, audit.snapshot(loc)))
    db.commit()
    if request.headers.get("hx-request"):
        return render(request, "partials/inline_name.html", {"name": loc.name})
    return back(request)


@router.post("/orte/{loc_id}/verschieben")
def move(loc_id: int, parent_id: str = Form(""), index: int | None = Form(None), target: str = Form(""), db: Session = Depends(get_db),
         ctx=Depends(require("locations", 2))):
    loc = _loc(db, loc_id)
    try:
        if target.strip():  # „Verschieben nach …“ per Code/Pfad; „/“ = oberste Ebene
            from .items import resolve_location

            new_parent = None if target.strip() == "/" else resolve_location(db, target)
            if target.strip() != "/" and new_parent is None:
                raise ValueError(f"Lagerort „{target}“ nicht gefunden.")
            locsvc.move_location(db, loc, new_parent, None)
        else:
            locsvc.move_location(db, loc, int(parent_id) if parent_id else None, index)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    audit.log(db, ctx.user_id, "update", "location", loc.id, f"Lagerort {loc.name} verschoben")
    db.commit()
    return {"ok": True}


@router.post("/orte/{loc_id}/loeschen")
def delete(loc_id: int, request: Request, target: str = Form(""), zurueck: str = Form(""), db: Session = Depends(get_db),
           ctx=Depends(require("locations", 4))):
    loc = _loc(db, loc_id)
    parent = loc.parent_id
    try:
        from .items import resolve_location

        tgt = resolve_location(db, target) if target else None
        name = loc.name
        locsvc.delete_location(db, loc, tgt)
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect(zurueck if zurueck.startswith("/orte") else f"/orte/{loc_id}")
    audit.log(db, ctx.user_id, "delete", "location", loc_id, f"Lagerort {name} (inkl. Unterorte) gelöscht")
    db.commit()
    flash(request, f"„{name}“ gelöscht.")
    if zurueck.startswith("/orte"):
        return redirect(zurueck)
    return redirect(f"/orte/{parent}" if parent else "/orte")


@router.post("/orte/{loc_id}/alias")
def alias(loc_id: int, request: Request, code: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("locations", 2))):
    from ..services.codes import CodeConflict, check_free

    try:
        check_free(db, code, "location", loc_id)
    except CodeConflict as e:
        flash(request, str(e), "err")
        return back(request, f"/orte/{loc_id}")
    add_location_alias(db, loc_id, code, "manuell")
    db.commit()
    flash(request, "Etikett/Code zugeordnet.")
    return back(request, f"/orte/{loc_id}")


@router.post("/alias/{alias_id}/loeschen")
def alias_delete(alias_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 2))):
    a = db.get(LocationAlias, alias_id)
    if a:
        db.delete(a)
        db.commit()
    return back(request)


@router.post("/orte/{loc_id}/abteile")
def compartments(loc_id: int, request: Request, count: int = Form(...), layout: str = Form("row"), naming: str = Form("dot"),
                 db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    loc = _loc(db, loc_id)
    if any(c.type == "abteil" for c in loc.children):
        flash(request, "Fach hat bereits Abteile.", "err")
        return back(request)
    locsvc.add_compartments(db, loc, max(2, min(count, 12)), layout, naming)
    db.commit()
    return redirect(f"/orte/{loc.id}")


@router.post("/orte/{loc_id}/leds")
def set_lights(loc_id: int, request: Request, controller_id: int = Form(...), led_start: int = Form(...), led_count: int = Form(1),
               color: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    loc = _loc(db, loc_id)
    for lt in list(loc.lights):
        if lt.controller_id == controller_id:
            db.delete(lt)
    db.flush()
    if led_count > 0:
        from ..services.wled import clean_color

        if db.get(WledController, controller_id) is None:
            raise HTTPException(404, "Controller nicht gefunden")
        db.add(LocationLight(location_id=loc.id, controller_id=controller_id, led_start=max(0, led_start), led_count=min(led_count, 500),
                             color=clean_color(color)))
    db.commit()
    if request.headers.get("hx-request"):
        return {"ok": True}
    return redirect(f"/orte/{loc.id}")


# ------------------------------------------------------------------ Lagermöbel-Assistent
def _spec_from_form(f) -> locsvc.FurnitureSpec:
    def _i(k, d=0):
        try:
            return int(f.get(k) or d)
        except ValueError:
            return d

    merges = []
    for part in (f.get("merges") or "").replace(";", "\n").splitlines():
        nums = [int(x) for x in part.replace(",", " ").split() if x.strip().isdigit()]
        if len(nums) == 4:
            merges.append(tuple(nums))
    row_counts = [int(x) for x in (f.get("row_counts") or "").replace(";", ",").split(",") if x.strip().isdigit()] or None
    gaps = []
    for part in (f.get("gaps") or "").replace(";", "\n").splitlines():
        toks = part.replace(",", " ").split()
        if len(toks) == 2 and (toks[0] == "*" or toks[0].isdigit()) and toks[1].isdigit():
            gaps.append((0 if toks[0] == "*" else int(toks[0]), int(toks[1])))
    return locsvc.FurnitureSpec(
        name=(f.get("name") or "Neues Möbel").strip(), type=f.get("type", "magazin"), rows=max(1, _i("rows", 4)), cols=max(1, _i("cols", 8)),
        row_counts=row_counts, merges=merges, gaps=gaps, naming=f.get("naming", "letter_number"), prefix=f.get("prefix", "Fach "),
        count_direction=f.get("count_direction", "rows"), bottom_up=bool(f.get("bottom_up")), subdivision=_i("subdivision", 0),
        subdivision_layout=f.get("subdivision_layout", "row"), subdivision_naming=f.get("subdivision_naming", "dot"),
        light_mode=f.get("light_mode", "both"), controller_id=_i("controller_id", 0) or None, led_start=_i("led_start", 0),
        leds_per_cell=max(1, _i("leds_per_cell", 3)), led_gap=_i("led_gap", 0), led_row_gap=_i("led_row_gap", 0),
        serpentine=bool(f.get("serpentine")), led_start_corner=f.get("led_start_corner", "top_left"), led_direction=f.get("led_direction", "rows"),
    )


def _size_from_form(f) -> tuple[float, float] | None:
    try:
        w, h = float(str(f.get("width_cm") or 0).replace(",", ".")), float(str(f.get("height_cm") or 0).replace(",", "."))
    except ValueError:
        return None
    return (max(2.0, min(w, 2000.0)), max(2.0, min(h, 2000.0))) if w > 0 and h > 0 else None


@router.get("/orte/{parent_id}/moebel-neu")
def wizard(parent_id: int, request: Request, vorlage: int | None = None, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    parent = _loc(db, parent_id) if parent_id else None
    tpl = db.get(StorageTemplate, vorlage) if vorlage else None
    spec = locsvc.spec_from_template(tpl, tpl.name.split(" (")[0]) if tpl else locsvc.FurnitureSpec(name="Kleinteilemagazin", rows=8, cols=4)
    return render(request, "wizard.html", {"parent": parent, "spec": spec, "tpl": tpl,
                                           "templates": list(db.scalars(select(StorageTemplate).order_by(StorageTemplate.name))),
                                           "controllers": list(db.scalars(select(WledController)))})


@router.post("/orte/moebel/vorschau")
async def wizard_preview(request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    spec = _spec_from_form(await request.form())
    cells = locsvc._cells(spec)
    if spec.count_direction != "rows":
        cells = locsvc.order_cells(spec, cells)
    leds = locsvc.led_mapping(spec, cells) if spec.controller_id else {}
    for seq, c in enumerate(cells, 1):
        c["name"] = locsvc._cell_name(spec, c, seq)
        c["led"] = leds.get((c["row"], c["col"]))
    return render(request, "partials/wizard_preview.html", {"spec": spec, "cells": cells, "cols": locsvc.grid_width(spec)})


@router.post("/orte/{parent_id}/moebel-neu")
async def wizard_create(parent_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    f = await request.form()
    spec = _spec_from_form(f)
    furniture = locsvc.create_furniture(db, spec, parent_id or None)
    size = _size_from_form(f)
    if size:
        furniture.wall_w, furniture.wall_h = size
    parent = db.get(Location, parent_id) if parent_id else None
    if parent is not None and any(c.wall_x is not None for c in parent.children):
        from ..services import walls

        walls.place(db, parent, furniture)  # Wandansicht vorhanden -> rechts anfügen
    if f.get("save_template") and f.get("template_name"):
        name = f["template_name"].strip()
        if not db.scalar(select(StorageTemplate).where(StorageTemplate.name == name)):
            definition = spec.to_template()
            if size:
                definition.update(width_cm=size[0], height_cm=size[1])
            db.add(StorageTemplate(name=name, kind=spec.type, definition=definition))
    audit.log(db, ctx.user_id, "create", "location", furniture.id, f"Lagermöbel {furniture.name} mit {len(furniture.children)} Fächern")
    db.commit()
    flash(request, f"„{furniture.name}“ mit {len(furniture.children)} Fächern angelegt.")
    return redirect(f"/orte/{furniture.id}")


@router.post("/orte/{loc_id}/als-vorlage")
def save_template(loc_id: int, request: Request, name: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    loc = _loc(db, loc_id)
    if db.scalar(select(StorageTemplate).where(StorageTemplate.name == name.strip())):
        flash(request, "Vorlagenname existiert bereits.", "err")
        return back(request)
    definition = locsvc.spec_from_furniture(db, loc)
    if loc.wall_w and loc.wall_h:
        definition.update(width_cm=loc.wall_w, height_cm=loc.wall_h)
    tpl = StorageTemplate(name=name.strip(), kind=loc.type, definition=definition)
    db.add(tpl)
    db.commit()
    flash(request, "Als Vorlage gespeichert.")
    return redirect(f"/vorlagen/{tpl.id}/teilen?neu=1")  # freiwillige Rückfrage: mit anderen teilen?


@router.post("/orte/{loc_id}/duplizieren")
def duplicate(loc_id: int, request: Request, name: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    loc = _loc(db, loc_id)
    copy = locsvc.duplicate_furniture(db, loc, name)
    audit.log(db, ctx.user_id, "create", "location", copy.id, f"Möbel dupliziert: {loc.name} → {copy.name}")
    db.commit()
    return redirect(f"/orte/{copy.id}")


@router.get("/vorlagen/{tpl_id}.json")
def template_json(tpl_id: int, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    t = db.get(StorageTemplate, tpl_id)
    if t is None:
        raise HTTPException(404, "Vorlage nicht gefunden")
    return {"name": t.name, "kind": t.kind, "definition": t.definition, "notes": t.notes}


@router.get("/vorlagen/{tpl_id}/teilen")
def template_share(tpl_id: int, request: Request, author: str = "", neu: int = 0, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    from ..services import sharing

    t = db.get(StorageTemplate, tpl_id)
    if t is None:
        raise HTTPException(404, "Vorlage nicht gefunden")
    errs, warns, contrib, url, full = [], [], {}, "", True
    try:
        contrib = sharing.template_contribution(t, author)
        errs, warns = sharing.template_problems(db, contrib)
        if not errs:
            url, full = sharing.issue_url("vorlage", contrib)
    except ValueError as e:
        errs = [str(e)]
    return render(request, "share.html", {"what": "Vorlage", "title": t.name, "contrib": contrib, "errs": errs, "warns": warns, "url": url,
                                           "full": full, "author": author, "neu": neu, "action": f"/vorlagen/{t.id}/teilen",
                                           "back": "/orte", "shared": ["Name, Rubrik und Notiz", "Fachraster (Reihen, Spalten, zusammengefasste und leere Fächer)", "Außenmaße"],
                                           "not_shared": ["LED-Verkabelung und Controller", "Orte, Bestände und alles andere aus deiner Installation"],
                                           "json_name": "vorlage.json", "repo": sharing.REPO})


@router.post("/vorlagen/import")
async def template_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    import json

    from ..services.template_catalog import clean_template

    try:
        t = clean_template(json.loads((await file.read())[:200_000]))
    except (ValueError, KeyError, TypeError) as e:
        flash(request, f"Keine gültige Vorlage: {e}", "err")
        return redirect("/orte")
    name = t["name"]
    while db.scalar(select(StorageTemplate).where(StorageTemplate.name == name)):
        name += " (2)"
    db.add(StorageTemplate(name=name, kind=t["kind"], definition=t["definition"], notes=t["notes"]))
    db.commit()
    flash(request, f"Vorlage „{name}“ importiert.")
    return redirect("/orte")


# ------------------------------------------------------------------ Vorlagen-Katalog (v0.27)
@router.get("/vorlagen/katalog")
def template_catalog(request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    from ..services import template_catalog as cat

    url = cat.catalog_url(db)
    entries, error = [], ""
    try:
        entries = cat.fetch_index(url)
    except cat.CatalogError as e:
        error = str(e)
    have = set(db.scalars(select(StorageTemplate.name)))
    for e in entries:
        e["have"] = e["name"] in have
    groups = sorted({e["group"] for e in entries if e["group"]})
    return render(request, "template_catalog.html", {"entries": entries, "error": error, "url": url, "default_url": cat.DEFAULT_URL, "groups": groups})


@router.post("/vorlagen/katalog/import")
async def template_catalog_import(request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    from ..services import template_catalog as cat

    f = await request.form()
    wanted = set(f.getlist("name"))
    if not wanted:
        flash(request, "Nichts ausgewählt.", "err")
        return redirect("/vorlagen/katalog")
    try:
        entries = [e for e in cat.fetch_index(cat.catalog_url(db)) if e["name"] in wanted]  # frisch holen: nur was im Katalog steht
    except cat.CatalogError as e:
        flash(request, str(e), "err")
        return redirect("/vorlagen/katalog")
    done = cat.import_templates(db, entries)
    audit.log(db, ctx.user_id, "create", "storage_template", None, f"{len(done)} Vorlagen aus dem Katalog importiert")
    db.commit()
    flash(request, f"{len(done)} Vorlage(n) importiert." if done else "Alles schon vorhanden.")
    return redirect("/orte")


@router.post("/vorlagen/katalog/adresse")
def template_catalog_url(request: Request, url: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("locations", 4))):
    from ..security import set_setting

    url = url.strip()
    if url and not url.lower().startswith("https://"):
        flash(request, "Die Adresse muss mit https:// beginnen.", "err")
    else:
        set_setting(db, "template_catalog_url", url)
        db.commit()
        flash(request, "Katalog-Adresse gespeichert." if url else "Standard-Katalog wird verwendet.")
    return redirect("/vorlagen/katalog")


@router.post("/orte/{loc_id}/raster")
async def assign_grid(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    loc = _loc(db, loc_id)
    f = await request.form()
    tpl = db.get(StorageTemplate, int(f["template_id"])) if (f.get("template_id") or "").isdigit() else None
    spec = locsvc.spec_from_template(tpl, loc.name) if tpl else _spec_from_form(f)
    n = locsvc.assign_grid(db, loc, spec.rows, spec.cols, spec.count_direction, spec.bottom_up, spec.merges, spec.row_counts, spec.gaps)
    if tpl and tpl.definition.get("width_cm") and tpl.definition.get("height_cm"):
        loc.wall_w, loc.wall_h = float(tpl.definition["width_cm"]), float(tpl.definition["height_cm"])
    audit.log(db, ctx.user_id, "update", "location", loc.id, f"Raster {spec.rows}×{spec.cols} für {loc.name} festgelegt ({n} Fächer)")
    db.commit()
    flash(request, f"{n} Fächer im Raster angeordnet.")
    return redirect(f"/orte/{loc.id}")


# ------------------------------------------------------------------ Inventur
@router.get("/orte/{loc_id}/inventur")
def inventory(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("inventory", 1))):
    loc = _loc(db, loc_id)
    tc = locsvc.TreeCache(db)
    ids = [loc.id, *tc.descendants(loc.id)]
    lots = sorted(db.scalars(select(StockLot).where(StockLot.location_id.in_(ids))), key=lambda l: locsvc.natural_key(tc.path(l.location_id)))
    return render(request, "inventory.html", {"loc": loc, "lots": lots, "tc": tc})


# ------------------------------------------------------------------ Zusammenführen / Fächer übertragen (v0.8)
def _merge_ctx(db: Session, loc: Location, modus: str, ziel: str, praefix: str, versatz: int, zuordnung: str = "auto") -> dict:
    from ..services import locmerge
    from .items import try_resolve_location

    tc = locsvc.TreeCache(db)
    target = None
    err = ""
    if ziel.strip():
        tid, err = try_resolve_location(db, ziel)
        target = db.get(Location, tid) if tid else None
        if target is None:
            err = err or f"Lagerort „{ziel}“ nicht gefunden."
        elif target.id == loc.id or target.id in tc.descendants(loc.id):
            err = "Das Ziel liegt in der Quelle – bitte ein anderes Ziel wählen."
            target = None
    sources = locmerge.natural_sources(db, loc, praefix or None)
    pairs, mode = [], ""
    if modus == "faecher" and target is not None:
        cells = locmerge.target_cells(target)
        mode = zuordnung if zuordnung in ("nummer", "reihe") else locmerge.pairing_mode(sources, cells, praefix, versatz)
        pairs = [(s, t, locmerge.content_count(db, s), locmerge.content_count(db, t) if t else 0)
                 for s, t in locmerge.pairing(sources, cells, versatz, praefix, mode)]
    return {"loc": loc, "tc": tc, "modus": modus, "ziel": ziel, "target": target, "err": err, "praefix": praefix, "versatz": versatz, "zuordnung": mode,
            "sources": sources, "pairs": pairs, "count": locmerge.content_count(db, loc),
            "suggested_prefix": locmerge.default_prefix(loc.name) if not loc.children else "",
            "merge_kids": {k.name.lower() for k in target.children} & {k.name.lower() for k in loc.children} if target is not None else set()}


@router.get("/orte/{loc_id}/zusammenfuehren")
def merge_page(loc_id: int, request: Request, modus: str = "", ziel: str = "", praefix: str = "", versatz: int = 0, zuordnung: str = "",
               db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    loc = _loc(db, loc_id)
    modus = modus if modus in ("ort", "faecher") else ("faecher" if len(loc.children) > 1 else "ort")
    return render(request, "location_merge.html", _merge_ctx(db, loc, modus, ziel, praefix, versatz, zuordnung))


@router.post("/orte/{loc_id}/zusammenfuehren")
async def merge_run(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 4))):
    from ..services import locmerge
    from ..services.backup import backup_database

    loc = _loc(db, loc_id)
    f = await request.form()
    modus, ziel, praefix = str(f.get("modus") or "ort"), str(f.get("ziel") or ""), str(f.get("praefix") or "")
    try:
        versatz = int(f.get("versatz") or 0)
    except ValueError:
        versatz = 0
    c = _merge_ctx(db, loc, modus, ziel, praefix, versatz, str(f.get("zuordnung") or ""))
    if c["target"] is None:
        flash(request, c["err"] or "Bitte ein Ziel wählen.", "err")
        return redirect(f"/orte/{loc_id}/zusammenfuehren")
    target = c["target"]
    backup_database(label="vor-zusammenfuehren")
    stats = locmerge.MergeStats()
    name, parent_id = loc.name, loc.parent_id
    try:
        if modus == "faecher":
            for s, t, _n, _m in c["pairs"]:
                if t is not None:
                    locmerge.merge_into(db, s, t, stats)
            if not praefix:
                db.refresh(loc)
            if not praefix and f.get("quelle_loeschen") and not loc.children and not locmerge.content_count(db, loc):
                locmerge.merge_into(db, loc, target, stats)  # leere Hülle: Etiketten wandern zum Möbel
        else:
            locmerge.merge_into(db, loc, target, stats)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/orte/{loc_id}/zusammenfuehren")
    audit.log(db, ctx.user_id, "update", "location", target.id,
              f"{name} → {target.name} zusammengeführt: {stats.locations} Orte, {stats.lots + stats.merged_lots} Bestände")
    db.commit()
    flash(request, f"Zusammengeführt: {stats.locations} alte Orte, {stats.lots} Bestände verschoben"
                   + (f", {stats.merged_lots} mit vorhandenen zusammengelegt" if stats.merged_lots else "")
                   + ". Alte Etiketten zeigen jetzt auf die neuen Orte.")
    if db.get(Location, loc_id) is not None:
        return redirect(f"/orte/{loc_id}/zusammenfuehren")
    return redirect(f"/orte/{target.id}" if modus == "ort" else f"/orte/{parent_id or target.id}?ansicht=liste")
