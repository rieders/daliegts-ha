"""WLED-Controller verwalten, testen, LED-Mapping für ganze Lagermöbel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Location, LocationLight, WledController
from ..security import require
from ..services import audit
from ..services import power as power_svc
from ..services import locations as locsvc
from ..services.wled import CtrlInfo, LightTarget, clean_color, controller_infos, manager
from ..web import back, flash, redirect, render

router = APIRouter()

FIELDS_INT = ("port", "led_count", "segment_id", "brightness", "timeout_s", "idle_after_min", "idle_brightness")
LIMITS = {"port": (1, 65535), "led_count": (1, 5000), "segment_id": (0, 31), "brightness": (1, 255), "timeout_s": (1, 600),
          "idle_after_min": (0, 1440), "idle_brightness": (1, 255)}


def _ctrl(db: Session, cid: int) -> WledController:
    c = db.get(WledController, cid)
    if c is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Controller nicht gefunden")
    return c


@router.get("/wled")
def page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    ctrls = list(db.scalars(select(WledController).order_by(WledController.name)))
    counts = {c.id: len(list(db.scalars(select(LocationLight.id).where(LocationLight.controller_id == c.id)))) for c in ctrls}
    pcfg = power_svc.load(db)
    return render(request, "wled.html", {"ctrls": ctrls, "status": manager.status(), "counts": counts,
                                         "paused": manager.paused_idle, "presence": manager.presence,
                                         "pcfg": power_svc.public(pcfg), "pkinds": power_svc.KINDS, "pstatus": power_svc.power.status()})


@router.post("/wled/strom/speichern")
async def power_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    f = dict(await request.form())
    cfg = power_svc.save(db, f)
    power_svc.power.configure(cfg)
    audit.log(db, ctx.user_id, "update", "wled", None, "Steckdosen-Steuerung gespeichert")
    db.commit()
    flash(request, "Steckdosen-Steuerung gespeichert." + (" Sie ist eingeschaltet." if cfg["enabled"] else " Sie ist ausgeschaltet."))
    return redirect("/wled")


@router.post("/wled/strom/{action}")
async def power_test(action: str, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    if action not in ("an", "aus"):
        raise HTTPException(404, "Unbekannte Aktion")
    saved = power_svc.power.cfg
    f = await request.form()
    if f.get("kind"):  # Test mit den Werten aus dem Formular, auch ungespeichert
        old = power_svc.load(db)
        cfg = power_svc.clean({**f})
        for k in power_svc.SECRET_KEYS:
            cfg[k] = cfg[k] or old.get(k, "")
        power_svc.power.cfg = {**cfg, "enabled": True}
    ok, err = await power_svc.power.switch(action == "an")
    power_svc.power.cfg = saved
    flash(request, f"Steckdose {'eingeschaltet' if action == 'an' else 'ausgeschaltet'}." if ok else f"Schalten fehlgeschlagen: {err}", "ok" if ok else "err")
    return redirect("/wled")


@router.post("/wled/speichern")
async def save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    f = await request.form()
    cid = int(f.get("id") or 0)
    c = db.get(WledController, cid) if cid else WledController(name="", host="")
    if not cid:
        from ..services import license as lic

        if not lic.has(db, "wled_multi") and db.query(WledController).count() >= lic.FREE_WLED_CONTROLLERS:
            flash(request, f"Die Grundversion unterstützt {lic.FREE_WLED_CONTROLLERS} WLED-Controller. Weitere Controller gibt es mit DaLiegt’s Pro "
                           "(Verwaltung › Lizenz).", "err")
            return redirect("/wled")
        db.add(c)
    c.name = f.get("name", "WLED").strip()
    c.host = f.get("host", "").strip()
    for k in FIELDS_INT:
        if f.get(k) not in (None, ""):
            lo, hi = LIMITS[k]
            try:
                setattr(c, k, max(lo, min(int(f[k]), hi)))
            except ValueError:
                pass
    c.color = clean_color(f.get("color"), "00FF40")
    c.restore_mode = f.get("restore_mode", "off")
    c.restore_preset = int(f["restore_preset"]) if f.get("restore_preset") else None
    c.idle_mode = f.get("idle_mode", "off")
    c.idle_preset = int(f["idle_preset"]) if f.get("idle_preset") else None
    c.idle_from = f.get("idle_from") or "07:00"
    c.idle_to = f.get("idle_to") or "22:00"
    c.enabled = bool(f.get("enabled"))
    db.flush()
    audit.log(db, ctx.user_id, "update" if cid else "create", "wled", c.id, f"WLED-Controller {c.name}")
    db.commit()
    flash(request, "Controller gespeichert.")
    return redirect("/wled")


@router.post("/wled/{cid}/loeschen")
def delete(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 4))):
    c = _ctrl(db, cid)
    db.delete(c)
    db.commit()
    return redirect("/wled")


@router.post("/wled/{cid}/test")
async def test(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    c = _ctrl(db, cid)
    res = await manager.test(CtrlInfo.of(c))
    if res["ok"]:
        info = res["info"] or {}
        flash(request, f"Verbunden: {info.get('name', '')} – WLED {info.get('ver', '?')}, {info.get('leds', {}).get('count', '?')} LEDs.")
    else:
        flash(request, f"Nicht erreichbar: {res['error']}", "err")
    return redirect("/wled")


@router.post("/wled/{cid}/durchzaehlen")
async def count_through(cid: int, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    await manager.count_through(CtrlInfo.of(_ctrl(db, cid)))
    return {"ok": True}


@router.post("/wled/{cid}/bereich")
async def show_range(cid: int, start: int = Form(...), count: int = Form(1), db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    """Zeigt einen LED-Bereich an (für den Raster-Editor: Start-LED justieren)."""
    c = _ctrl(db, cid)
    start, count = max(0, start), max(1, min(count, 200))
    await manager.show({c.id: CtrlInfo.of(c)}, [LightTarget(c.id, start, count, c.color)], 20)
    return {"ok": True}


# ------------------------------------------------------------------ Einmessen von LED-Ketten (v0.21)
CHECK_COLORS = ["FF3030", "30C030", "3070FF", "FFB000", "C040FF", "00C8C8", "FF6AA0", "FFFFFF"]


async def _json_body(request: Request) -> dict:
    if request.headers.get("x-lagerwerk") != "1":
        raise HTTPException(403, "Header X-Lagerwerk: 1 fehlt.")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON erwartet")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON-Objekt erwartet")
    return body


def _int(v, lo: int, hi: int, default: int = 0) -> int:
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


@router.post("/wled/{cid}/einmessen")
async def calibrate_show(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    """Einmess-Assistent: die erste LED des nächsten Abschnitts pulsiert grün (davor liegende Abschnitte dunkel blau als Orientierung).
    {"pos": 42, "n": 2, "done": [[0, 12], [12, 16]]}  –  {"aus": true} schaltet den Controller aus."""
    c = _ctrl(db, cid)
    body = await _json_body(request)
    info = CtrlInfo.of(c)
    if body.get("aus"):
        await manager.clear({c.id: info})
        return {"ok": True}
    pos = _int(body.get("pos"), 0, max(0, c.led_count - 1))
    n = _int(body.get("n"), 1, 10, 2)
    targets = []
    for rng in (body.get("done") or [])[:64]:  # bereits zugeordnete Module schwach anzeigen
        if isinstance(rng, list) and len(rng) == 2:
            s, cnt = _int(rng[0], 0, c.led_count), _int(rng[1], 0, 2000)
            if cnt and s < pos:
                targets.append(LightTarget(c.id, s, min(cnt, pos - s), "000C30"))
    if pos >= c.led_count:
        return {"ok": False, "error": f"LED {pos} gibt es nicht – der Controller hat {c.led_count} LEDs."}
    targets.append(LightTarget(c.id, pos, min(n, c.led_count - pos), "00FF40", pulse=True))
    await manager.show({c.id: info}, targets, 180)
    return {"ok": True, "pos": pos, "led_count": c.led_count}


@router.post("/wled/{cid}/kette-pruefen")
async def chain_check(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    """Nach dem Einmessen: jedes Modul der Kette leuchtet komplett in einer eigenen Farbe (Reihenfolge wie in der Liste)."""
    c = _ctrl(db, cid)
    body = await _json_body(request)
    targets = []
    for i, rng in enumerate((body.get("ranges") or [])[:64]):
        if isinstance(rng, list) and len(rng) == 2:
            s, cnt = _int(rng[0], 0, c.led_count - 1), _int(rng[1], 1, 2000, 1)
            targets.append(LightTarget(c.id, s, min(cnt, c.led_count - s), CHECK_COLORS[i % len(CHECK_COLORS)]))
    if targets:
        await manager.show({c.id: CtrlInfo.of(c)}, targets, 20)
    return {"ok": True, "colors": CHECK_COLORS}


@router.post("/wled/{cid}/ruhe-test")
async def idle_test(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    """Effekt (Preset) sofort starten, ohne Zeitfenster und Wartezeit – zum Ausprobieren."""
    if request.headers.get("x-lagerwerk") != "1":
        raise HTTPException(403, "Header X-Lagerwerk: 1 fehlt.")
    c = _ctrl(db, cid)
    body = await request.json()
    try:
        preset = int(body.get("preset") or c.idle_preset or 0)
        bri = max(1, min(int(body.get("bri") or c.idle_brightness or 40), 255))
    except (TypeError, ValueError):
        raise HTTPException(400, "Preset-Nummer fehlt.")
    if preset <= 0:
        raise HTTPException(400, "Bitte eine Preset-Nummer eintragen.")
    await manager.wake(CtrlInfo.of(c))
    ok = await manager.post_state(CtrlInfo.of(c), {"ps": preset, "bri": bri})
    return {"ok": bool(ok)}


@router.get("/wled/{cid}/presets")
async def presets(cid: int, db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    return await manager.presets(CtrlInfo.of(_ctrl(db, cid)))


@router.post("/wled/aus")
async def all_off(request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_show", 1))):
    await manager.clear(controller_infos(db))
    if request.headers.get("hx-request"):
        return {"ok": True}
    return back(request, "/wled")


@router.post("/wled/ruhemodus")
async def idle_toggle(request: Request, paused: int = Form(0), db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    from ..security import set_setting

    set_setting(db, "wled.idle_paused", bool(paused))
    db.commit()
    await manager.set_paused(controller_infos(db), bool(paused))
    flash(request, "Ruhemodus pausiert – die Effektwand bleibt aus, bis du ihn fortsetzt." if paused else "Ruhemodus läuft wieder.")
    return back(request, "/wled")


@router.post("/orte/{loc_id}/led-mapping")
async def map_furniture(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("wled_config", 2))):
    """LED-Zuordnung für ein bestehendes Möbel neu berechnen (Wand-Assistent)."""
    from .locations import _spec_from_form

    furniture = db.get(Location, loc_id)
    f = await request.form()
    spec = _spec_from_form(f)
    if furniture is None or not spec.controller_id or db.get(WledController, spec.controller_id) is None:
        flash(request, "Möbel oder Controller nicht gefunden.", "err")
        return redirect(f"/orte/{loc_id}")
    spec.rows, spec.cols = furniture.grid_rows or spec.rows, furniture.grid_cols or spec.cols
    cells = [c for c in furniture.children if c.grid_row]
    spec.merges = [(c.grid_row, c.grid_col, c.grid_rowspan, c.grid_colspan) for c in cells if c.grid_rowspan > 1 or c.grid_colspan > 1]
    mapping = locsvc.led_mapping(spec, [{"row": c.grid_row, "col": c.grid_col, "rowspan": c.grid_rowspan, "colspan": c.grid_colspan} for c in cells])
    n = 0
    for c in cells:
        for lt in list(c.lights):
            if lt.controller_id == spec.controller_id:
                db.delete(lt)
        if (c.grid_row, c.grid_col) in mapping:
            s, cnt = mapping[(c.grid_row, c.grid_col)]
            db.add(LocationLight(location_id=c.id, controller_id=spec.controller_id, led_start=s, led_count=cnt))
            n += 1
    furniture.light_mode = f.get("light_mode", furniture.light_mode)
    furniture.led_spec = {k: getattr(spec, k) for k in ("leds_per_cell", "led_gap", "led_row_gap", "serpentine", "led_start_corner", "led_direction")}
    audit.log(db, ctx.user_id, "update", "location", furniture.id, f"LED-Mapping für {n} Fächer neu berechnet")
    db.commit()
    flash(request, f"LED-Zuordnung für {n} Fächer gesetzt. Tipp: einzelne Fächer im Raster antippen und mit +/− justieren.")
    return redirect(f"/orte/{loc_id}")
