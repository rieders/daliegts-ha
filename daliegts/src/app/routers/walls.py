"""Wandansicht: Module anordnen (Drag & Drop, neben/über/unter), Regale umbauen, LED-Ketten über mehrere Module."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, Location, StorageTemplate, WledController
from ..security import check, require
from ..services import audit
from ..services import edges as edgesvc
from ..services import locations as locsvc
from ..services import walls
from ..web import flash, redirect, render

router = APIRouter()


def _loc(db: Session, loc_id: int) -> Location:
    loc = db.get(Location, loc_id)
    if loc is None:
        raise HTTPException(404, "Lagerort nicht gefunden")
    return loc


def _module(db: Session, wall: Location, mid) -> Location:
    try:
        m = db.get(Location, int(mid))
    except (TypeError, ValueError):
        m = None
    if m is None or m.parent_id != wall.id:
        raise HTTPException(404, "Modul nicht gefunden")
    return m


def _f(v, default=None):
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise ValueError(f"„{v}“ ist keine Zahl.")


@router.get("/lager")
def storage_view(request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    """Hauptansicht Lager: zuletzt gewählter Raum, sonst der erste mit angeordneten Modulen."""
    rooms = walls.rooms(db)
    last = request.cookies.get("lw_raum", "")
    ids = {r["id"] for r in rooms}
    if last.isdigit() and int(last) in ids:
        return redirect(f"/orte/{last}/wand")
    if rooms:
        return redirect(f"/orte/{rooms[0]['id']}/wand")
    flash(request, "Noch kein Raum mit angeordneten Lagermöbeln – Raum öffnen und „🧱 Wandansicht“ wählen.")
    return redirect("/orte")


def _int_list(v: str) -> list[int]:
    return [int(x) for x in (v or "").split(",") if x.strip().isdigit()][:500]


@router.get("/orte/{loc_id}/wand")
def wall_page(loc_id: int, request: Request, artikel: int | None = None, orte: str = "", db: Session = Depends(get_db),
              ctx=Depends(require("locations", 1))):
    wall = _loc(db, loc_id)
    tc = locsvc.TreeCache(db)
    templates = list(db.scalars(select(StorageTemplate).order_by(StorageTemplate.name)))
    preset, preset_label = None, ""
    if artikel and ctx.can("items", 1):
        item = db.get(Item, artikel)
        if item is not None:
            preset, preset_label = walls.hits_for_items(db, wall, [item]), item.name
    elif orte:
        ids = _int_list(orte)
        preset = {"cells": walls.visible_ids(db, wall, ids), "locations": ids, "count": 0, "places": []}
        preset_label = f"{len(ids)} Fächer der Stichprobe"
    root = walls.area_root(wall)
    tabs = [{"id": t.id, "name": t.name} for t in ([root, *walls.areas_of(root)] if root is not None else [])]
    targets = [{"id": a.id, "name": a.name} for a in walls.areas_of(wall)]
    if walls.is_area(wall) and wall.parent is not None:
        targets += [{"id": a.id, "name": a.name} for a in walls.areas_of(wall.parent) if a.id != wall.id]
        targets.append({"id": wall.parent.id, "name": f"↑ {wall.parent.name} (ohne Bereich)"})
    rooms = walls.rooms(db)
    if wall.id not in {r["id"] for r in rooms}:
        rooms.insert(0, {"id": wall.id, "name": wall.name, "path": tc.path(wall.id), "modules": True})
    resp = render(request, "wall.html", {
        "wall": wall, "tc": tc, "path_ids": tc.ancestors(wall.id), "rooms": rooms, "tabs": tabs, "area_targets": targets, "preset": preset, "preset_label": preset_label,
        "templates": [{"id": t.id, "name": t.name, "kind": t.kind, "rows": t.definition.get("rows"), "cols": t.definition.get("cols"),
                       "cells": len(locsvc._cells(locsvc.spec_from_template(t, "x"))),
                       "row_counts": t.definition.get("row_counts"), "merges": t.definition.get("merges", []), "gaps": t.definition.get("gaps", []),
                       "w": t.definition.get("width_cm"), "h": t.definition.get("height_cm"), "notes": t.notes or "",
                       "group": walls.template_group(t.name, t.builtin, t.definition)} for t in templates],
        "controllers": [{"id": c.id, "name": c.name, "leds": c.led_count} for c in db.scalars(select(WledController).order_by(WledController.name))],
    })
    if walls.placed_modules(wall) or walls.areas_of(wall):
        resp.set_cookie("lw_raum", str(wall.id), max_age=365 * 86400, samesite="lax", httponly=True)
    return resp


@router.get("/orte/{loc_id}/wand.json")
def wall_json(loc_id: int, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    return walls.wall_data(db, _loc(db, loc_id))


@router.get("/orte/{loc_id}/wand/suche")
def wall_search(loc_id: int, q: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    if len(q.strip()) < 2:
        return {"cells": [], "locations": [], "count": 0}
    return walls.search_hits(db, _loc(db, loc_id), q)


@router.post("/orte/{loc_id}/wand/aktion")
async def wall_action(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 2))):
    """Eine Aktion als JSON: move, place, unplace, resize, add (neues Modul), chains (LED-Ketten)."""
    if request.headers.get("x-lagerwerk") != "1":  # JSON-Aufrufe nur aus der eigenen Oberfläche
        raise HTTPException(403, "Header X-Lagerwerk: 1 fehlt.")
    wall = _loc(db, loc_id)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON erwartet")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON-Objekt erwartet")
    act = body.get("action")
    msg = ""
    try:
        if act == "move":
            m = _module(db, wall, body.get("id"))
            walls.move(db, wall, m, _f(body.get("x"), 0), _f(body.get("y"), 0))
        elif act == "resize":
            m = _module(db, wall, body.get("id"))
            walls.move(db, wall, m, m.wall_x or 0, m.wall_y or 0, _f(body.get("w")), _f(body.get("h")))
        elif act == "place":
            m = _module(db, wall, body.get("id"))
            ref = _module(db, wall, body["ref"]) if body.get("ref") else None
            walls.place(db, wall, m, ref, body.get("side", "right"))
        elif act == "unplace":
            walls.unplace(db, wall, _module(db, wall, body.get("id")))
        elif act == "add":
            check(ctx, "locations", 3)
            ref = _module(db, wall, body["ref"]) if body.get("ref") else None
            side = body.get("side", "right")
            name = (body.get("name") or "").strip()[:200] or "Neues Modul"
            w, h = _f(body.get("w")), _f(body.get("h"))
            if body.get("kind") == "box":
                m = walls.create_box(db, wall, name, body.get("type") or "kiste", w, h, ref, side)
            else:
                tpl = db.get(StorageTemplate, int(body["template_id"])) if body.get("template_id") else None
                if tpl is not None:
                    spec = locsvc.spec_from_template(tpl, name)
                    if not (w and h):
                        w, h = _f(tpl.definition.get("width_cm")), _f(tpl.definition.get("height_cm"))
                else:
                    rc = [int(x) for x in str(body.get("row_counts") or "").replace(";", ",").split(",") if x.strip().isdigit()] or None
                    spec = locsvc.FurnitureSpec(name=name, type=body.get("type") or "magazin", rows=int(body.get("rows") or 1),
                                                cols=int(body.get("cols") or 1), row_counts=rc,
                                                naming=body.get("naming") or ("row_col" if body.get("type") == "regal" else "letter_number"),
                                                subdivision=int(body.get("subdivision") or 0))
                m = walls.create_module(db, wall, spec, w, h, ref, side)
            audit.log(db, ctx.user_id, "create", "location", m.id, f"Modul {m.name} in Wandansicht {wall.name} angelegt")
            msg = f"„{m.name}“ angelegt."
        elif act == "grid":
            check(ctx, "locations", 3)
            m = _module(db, wall, body.get("id"))
            tpl = db.get(StorageTemplate, int(body["template_id"])) if body.get("template_id") else None
            if tpl is not None:
                spec = locsvc.spec_from_template(tpl, m.name)
                w, h = _f(tpl.definition.get("width_cm")), _f(tpl.definition.get("height_cm"))
            else:
                merges = []
                for part in str(body.get("merges") or "").replace(";", "\n").splitlines():
                    nums = [int(x) for x in part.replace(",", " ").split() if x.isdigit()]
                    if len(nums) == 4:
                        merges.append(tuple(nums))
                spec = locsvc.FurnitureSpec(name=m.name, rows=int(body.get("rows") or 1), cols=int(body.get("cols") or 1), merges=merges,
                                            bottom_up=bool(body.get("bottom_up")),
                                            count_direction=body.get("count_direction") if body.get("count_direction") in ("rows", "cols", "bands") else "rows")
                w = h = None
            n = walls.apply_grid(db, m, spec, w, h)
            if any(e["id"] == m.id for c in walls.chains_of(wall) for e in c["modules"]):
                walls.apply_chains(db, wall)  # LED-Nummern an die neue Anordnung anpassen
            count = len([c for c in m.children if not c.compartment_index])
            audit.log(db, ctx.user_id, "update", "location", m.id, f"Raster {spec.rows}×{spec.cols} für {m.name} ({n} Fächer)")
            msg = f"{n} Fächer im Raster angeordnet." + (f" {count - n} Fächer passen nicht ins Raster – bitte größeres Raster wählen." if count > n else "")
        elif act == "area":
            check(ctx, "locations", 3)
            a = walls.add_area(db, wall, body.get("name"))
            audit.log(db, ctx.user_id, "create", "location", a.id, f"Bereich {a.name} in {wall.name} angelegt")
            msg = f"Bereich „{a.name}“ angelegt – Module im Anordnen-Modus hineinverschieben."
        elif act == "move_area":
            m = _module(db, wall, body.get("id"))
            target = db.get(Location, int(body.get("target") or 0))
            if target is None:
                raise ValueError("Bereich nicht gefunden.")
            walls.move_to_area(db, wall, m, target)
            audit.log(db, ctx.user_id, "update", "location", m.id, f"{m.name} von {wall.name} nach {target.name} verschoben")
            msg = f"„{m.name}“ liegt jetzt in „{target.name}“."
        elif act == "chains":
            check(ctx, "wled_config", 2)
            layout = dict(wall.layout or {})
            layout["chains"] = walls.clean_chains(db, wall, body.get("chains") or [])
            wall.layout = layout
            db.flush()
            specs = body.get("specs") or {}
            for mid, sp in specs.items():  # Verkabelung einzelner Module
                m = _module(db, wall, mid)
                clean = {k: sp[k] for k in walls.LED_KEYS if k in sp}
                spec = locsvc.FurnitureSpec(name=m.name, **clean)  # validiert/klemmt die Werte
                m.led_spec = {k: getattr(spec, k) for k in walls.LED_KEYS}
            db.flush()
            starts = walls.apply_chains(db, wall)
            audit.log(db, ctx.user_id, "update", "location", wall.id, f"LED-Ketten für {len(starts)} Module in {wall.name} neu berechnet")
            msg = f"LED-Nummern für {len(starts)} Module neu berechnet."
        elif act == "edges":
            check(ctx, "wled_config", 2)
            edgesvc.clear_edges(db, wall)  # bisherige Zuordnungen weg, bevor sich Bereiche ändern
            layout = dict(wall.layout or {})
            layout["edges"] = edgesvc.clean_edges(db, wall, body.get("edges") or [])
            wall.layout = layout
            db.flush()
            counts = edgesvc.apply_edges(db, wall)
            audit.log(db, ctx.user_id, "update", "location", wall.id, f"Kantenleisten in {wall.name}: {sum(counts.values())} LEDs neu berechnet")
            msg = f"{len(counts)} Kantenleiste(n) mit {sum(counts.values())} LEDs berechnet." if counts else "Keine Kantenleiste (mehr) eingerichtet."
        else:
            raise ValueError("Unbekannte Aktion.")
    except ValueError as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    db.commit()
    db.expire_all()
    return {"ok": True, "message": msg, "data": walls.wall_data(db, _loc(db, loc_id))}


@router.post("/orte/{loc_id}/umbauen")
async def reshape(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 3))):
    furniture = _loc(db, loc_id)
    f = await request.form()
    try:
        rows = max(1, min(int(f.get("rows") or furniture.grid_rows or 1), locsvc.MAX_GRID))
        cols = max(1, min(int(f.get("cols") or furniture.grid_cols or 1), locsvc.MAX_GRID))
    except ValueError:
        flash(request, "Reihen/Fächer müssen Zahlen sein.", "err")
        return redirect(f"/orte/{loc_id}")
    rc = [int(x) for x in str(f.get("row_counts") or "").replace(";", ",").split(",") if x.strip().isdigit()] or None
    res = walls.reshape(db, furniture, rows, cols, rc)
    if res.blocked:
        db.rollback()
        flash(request, "Nicht umgebaut – diese Fächer würden wegfallen, sind aber nicht leer: " + ", ".join(res.blocked[:12])
              + ". Bitte erst umlagern.", "err")
        return redirect(f"/orte/{loc_id}")
    parent = furniture.parent
    if parent is not None and parent.layout and parent.layout.get("chains"):
        walls.apply_chains(db, parent)  # LED-Nummern der Kette passen sich an
    audit.log(db, ctx.user_id, "update", "location", furniture.id,
              f"{furniture.name} umgebaut: +{len(res.added)} / −{len(res.removed)} Fächer")
    db.commit()
    flash(request, f"Umgebaut: {len(res.added)} Fächer neu ({', '.join(res.added[:8])}{' …' if len(res.added) > 8 else ''}), "
                   f"{len(res.removed)} entfernt. Bestehende Fächer und Etiketten bleiben unverändert.")
    return redirect(f"/orte/{loc_id}")


@router.get("/orte/{loc_id}/wand/leiste/{edge_id}.{ext}")
def edge_holder(loc_id: int, edge_id: str, ext: str, request: Request, db: Session = Depends(get_db), ctx=Depends(require("locations", 1))):
    """3D-Druck-Halterung einer Kantenleiste: OpenSCAD-Quelltext (.scad) oder fertige STL-Datei (.stl, falls OpenSCAD installiert ist)."""
    if ext not in ("scad", "stl"):
        raise HTTPException(404, "Format nicht bekannt")
    wall = _loc(db, loc_id)
    data = {e["id"]: e for e in edgesvc.edge_data(wall)}
    e = data.get(edge_id)
    if e is None:
        raise HTTPException(404, "Leiste nicht gefunden")
    title = f"{wall.name} {e['side']}"
    try:
        src = edgesvc.scad_source(e, e["pos"], e["nums"], title, dict(request.query_params))
        body, media = (src.encode("utf-8"), "text/plain; charset=utf-8") if ext == "scad" else (edgesvc.scad_to_stl(src), "model/stl")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    name = "".join(ch if ch.isalnum() else "_" for ch in title).strip("_") or "leiste"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})
