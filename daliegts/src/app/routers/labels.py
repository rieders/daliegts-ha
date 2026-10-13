"""Etikettendruck für Lagerorte, Artikel und Bestände – mit eigenen Vorlagen (v0.12)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, LabelPrinter, LabelTemplate, Location, StockLot, now
from ..security import require
from ..services import labeldesign as ld
from ..services.labels import PRESETS, sheet_pages
from ..services.license import has as lic_has
from ..services.license import require_pro
from ..services.locations import TreeCache, natural_key
from ..web import flash, redirect, render, templates

router = APIRouter()
MAX_LABELS = 2000


def _grid_sort_key(tc: TreeCache, db: Session, loc_id: int):
    loc = db.get(Location, loc_id)
    return (natural_key(tc.path(loc.parent_id) if loc.parent_id else ""), loc.grid_row or 0, loc.grid_col or 0, loc.sort_order, natural_key(loc.name))


def _ids(v: str) -> list[int]:
    return [int(x) for x in (v or "").replace(";", ",").split(",") if x.strip().isdigit()][:MAX_LABELS]


@router.get("/etiketten")
def labels(request: Request, art: str = "", ort: str = "", ort_code: str = "", unterorte: int = 0, nur: str = "alle", artikel: str = "",
           bestand: str = "", artikel_ort: str = "", vorlage: str = "", start: str = "1", anzahl: str = "1", preset: str = "",
           db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    data = _build(request, db, dict(request.query_params))
    pro = lic_has(db, "labelprint")
    printers = list(db.scalars(select(LabelPrinter).where(LabelPrinter.enabled.is_(True)).order_by(LabelPrinter.is_default.desc(), LabelPrinter.name))) if pro else []
    return render(request, "labels.html", {**data, "printers": printers, "query": str(request.query_params), "labelprint_pro": pro})


def _build(request: Request, db: Session, q: dict) -> dict:
    """Etiketten für die Parameter der Etikettenseite zusammenstellen (auch für den Direktdruck)."""
    from .items import try_resolve_location

    def _s(k: str, d: str = "") -> str:
        v = q.get(k, d)
        return str(v[-1] if isinstance(v, list) else v)

    art, ort, ort_code, nur, artikel, bestand = _s("art"), _s("ort"), _s("ort_code"), _s("nur", "alle"), _s("artikel"), _s("bestand")
    artikel_ort, vorlage, start, anzahl, preset = _s("artikel_ort"), _s("vorlage"), _s("start", "1"), _s("anzahl", "1"), _s("preset")
    unterorte = 1 if _s("unterorte") == "1" or "f" not in q else 0
    ld.ensure_default_templates(db)
    db.commit()
    # Etikettenart: aus Parametern ableiten (alte Links: ?ort=…, ?artikel=…)
    if art not in ld.KINDS:
        art = "item" if artikel and not (ort or ort_code) else ("lot" if bestand else "location")
    tc = TreeCache(db)
    tpls = ld.templates_for(db, art)
    t = db.get(LabelTemplate, int(vorlage)) if vorlage.isdigit() else None
    if t is None and preset:  # alte Links mit ?preset=…: Vorlage mit diesem Format
        t = next((x for x in tpls if x.media == preset), None)
    if t is None or t.kind != art:
        t = tpls[0] if tpls else None
    if t is None:
        raise HTTPException(500, "Keine Etikettenvorlage vorhanden.")
    spec = ld.spec_of(t)
    try:
        start_n = max(1, int(start or 1))
        copies = max(1, min(int(anzahl or 1), 50))
    except ValueError:
        start_n, copies = 1, 1

    recs: list[dict] = []
    loc = None
    if art == "location":
        raw = (ort_code or ort or "").strip()
        ort_id, err = try_resolve_location(db, raw) if raw else (None, "")
        if err:
            flash(request, err, "err")
        loc = db.get(Location, ort_id) if ort_id else None
        if loc is not None:
            ids = [loc.id] + (tc.descendants(loc.id) if unterorte else [])
            ids.sort(key=lambda i: _grid_sort_key(tc, db, i))
            for lid in ids:
                lo = db.get(Location, lid)
                if (nur == "faecher" and lo.type != "fach") or (nur == "abteile" and lo.type != "abteil") or (nur == "ungedruckt" and lo.label_printed_at):
                    continue
                content = ", ".join(sorted({lt.item.name for lt in db.scalars(select(StockLot).where(StockLot.location_id == lid))}))
                recs.append(ld.location_record(lo, tc, content))
    else:
        from ..services.stats import unit_values

        prices = unit_values(db) if any(ln.get("f") == "price" for ln in spec.lines) else None
        item_ids = _ids(artikel)
        lot_ids = _ids(bestand)
        if artikel_ort.strip():  # alle Artikel (bzw. Bestände) in einem Lagerort
            lid, err = try_resolve_location(db, artikel_ort)
            if err:
                flash(request, err, "err")
            if lid:
                sub = [lid, *tc.descendants(lid)]
                lots_in = list(db.scalars(select(StockLot).where(StockLot.location_id.in_(sub), StockLot.status.notin_(("lost", "used")))))
                lots_in.sort(key=lambda lt: natural_key(tc.path(lt.location_id)))
                if art == "lot":
                    lot_ids += [lt.id for lt in lots_in if lt.id not in lot_ids]
                else:
                    item_ids += [lt.item_id for lt in lots_in if lt.item_id not in item_ids]
        if art == "item":
            for iid in dict.fromkeys(item_ids):
                it = db.get(Item, iid)
                if it is not None:
                    recs.append(ld.item_record(db, it, tc, prices))
        else:
            for iid in _ids(artikel):  # Artikel gewählt -> alle seine Bestände
                lot_ids += [lt.id for lt in db.scalars(select(StockLot).where(StockLot.item_id == iid)) if lt.id not in lot_ids]
            for lid_ in dict.fromkeys(lot_ids):
                lt = db.get(StockLot, lid_)
                if lt is not None:
                    recs.append(ld.lot_record(db, lt, tc, prices))
    recs = [ld.finish(r, spec) for r in recs for _ in range(copies)][:MAX_LABELS]
    sample = None if recs else ld.finish(ld.sample_record(db, art), spec)
    start_pos = max(1, min(start_n, spec.cols * spec.rows))
    pages = sheet_pages(recs, spec, start_pos) if spec.sheet and recs else None
    chosen_items = [db.get(Item, i) for i in dict.fromkeys(_ids(artikel))]
    return {
        "art": art, "kinds": ld.KINDS, "tpls": tpls, "t": t, "s": spec, "labels": recs, "pages": pages, "sample": sample,
        "loc": loc, "unterorte": unterorte, "nur": nur, "artikel": artikel, "bestand": bestand, "artikel_ort": artikel_ort,
        "chosen": [i for i in chosen_items if i is not None], "start": start_pos, "anzahl": copies,
        "loc_ids": [r["id"] for r in recs if r["kind"] == "location"],
    }


@router.post("/etiketten/gedruckt")
def mark_printed(request: Request, ids: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    n = 0
    for x in ids.split(","):
        if x.strip().isdigit():
            loc = db.get(Location, int(x))
            if loc:
                loc.label_printed_at = now()
                n += 1
    db.commit()
    flash(request, f"{n} Etiketten als gedruckt markiert.")
    return redirect(request.headers.get("referer") or "/etiketten")


# ------------------------------------------------------------------ Vorlagen verwalten
@router.get("/etiketten/vorlagen")
def template_list(request: Request, art: str = "location", id: int | None = None, neu: int = 0, db: Session = Depends(get_db),
                  ctx=Depends(require("labels", 1))):
    ld.ensure_default_templates(db)
    db.commit()
    art = art if art in ld.KINDS else "location"
    t = db.get(LabelTemplate, id) if id else None
    if t is not None:
        art = t.kind
    if t is None and not neu:
        t = ld.default_template(db, art)
    edit = {"id": t.id if t else None, "name": t.name if t else "Neue Vorlage", "kind": art, "media": t.media if t else "eigen",
            "width_mm": t.width_mm if t else 55, "height_mm": t.height_mm if t else 16, "symbology": t.symbology if t else "qr",
            "code_pos": t.code_pos if t else "left",
            "lines": (t.lines if t else [{"f": "name", "size": "l", "bold": True, "text": ""}, {"f": "code", "size": "s", "bold": False, "text": ""}]),
            "is_default": bool(t and t.is_default)}
    media = {"eigen": "Eigene Größe (Endlosband / Einzeletikett)", **{k: p.name for k, p in PRESETS.items()}}
    return render(request, "label_templates.html", {
        "art": art, "kinds": ld.KINDS, "all": {k: ld.templates_for(db, k) for k in ld.KINDS}, "edit": edit,
        "fields": {k: ld.field_choices(k) for k in ld.KINDS}, "sizes": ld.SIZES, "media": media,
        "media_sizes": {k: [p.width_mm, p.height_mm] for k, p in PRESETS.items()},
        "printers": list(db.scalars(select(LabelPrinter).where(LabelPrinter.enabled.is_(True)).order_by(LabelPrinter.is_default.desc(), LabelPrinter.name))),
    })


def _spec_from_body(body: dict) -> ld.Spec:
    kind = body.get("kind") if body.get("kind") in ld.KINDS else "location"
    t = LabelTemplate(id=None, name=str(body.get("name") or "Vorlage")[:120], kind=kind, media=str(body.get("media") or "eigen"),
                      symbology=body.get("symbology") if body.get("symbology") in ("qr", "code128", "qr+code128", "none") else "qr",
                      code_pos="right" if body.get("code_pos") == "right" else "left", lines=ld.clean_lines(body.get("lines")))
    try:
        t.width_mm = max(10.0, min(float(str(body.get("width_mm") or 55).replace(",", ".")), 200.0))
        t.height_mm = max(6.0, min(float(str(body.get("height_mm") or 16).replace(",", ".")), 150.0))
    except ValueError:
        t.width_mm, t.height_mm = 55.0, 16.0
    if t.media != "eigen" and t.media not in PRESETS:
        t.media = "eigen"
    return ld.spec_of(t)


@router.post("/etiketten/vorlagen/vorschau")
async def template_preview(request: Request, db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    body = await request.json()
    spec = _spec_from_body(body if isinstance(body, dict) else {})
    rec = ld.finish(ld.sample_record(db, spec.kind), spec)
    tpl = templates.env.from_string('{% from "partials/label.html" import label, label_css %}<style>{{ label_css(s) }}</style>{{ label(l, s) }}')
    return HTMLResponse(tpl.render(s=spec, l=rec))


@router.post("/etiketten/vorlagen/speichern")
async def template_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("labels", 2))):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON erwartet")
    spec = _spec_from_body(body)
    t = db.get(LabelTemplate, int(body["id"])) if str(body.get("id") or "").isdigit() else None
    if t is None:
        t = LabelTemplate(kind=spec.kind)
        db.add(t)
    t.name = spec.name.strip() or "Vorlage"
    t.kind = spec.kind
    t.media, t.symbology, t.code_pos, t.lines = spec.media, spec.symbology, spec.code_pos, spec.lines
    t.width_mm, t.height_mm = spec.width_mm, spec.height_mm
    if body.get("is_default"):
        for o in db.scalars(select(LabelTemplate).where(LabelTemplate.kind == t.kind)):
            o.is_default = False
        t.is_default = True
    db.commit()
    return {"ok": True, "id": t.id, "message": f"Vorlage „{t.name}“ gespeichert."}


@router.post("/etiketten/vorlagen/{tid}/loeschen")
def template_delete(tid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("labels", 2))):
    t = db.get(LabelTemplate, tid)
    if t is None:
        raise HTTPException(404, "Vorlage nicht gefunden")
    kind = t.kind
    if len(ld.templates_for(db, kind)) <= 1:
        flash(request, "Die letzte Vorlage dieser Art kann nicht gelöscht werden.", "err")
        return redirect(f"/etiketten/vorlagen?id={tid}")
    was_default = t.is_default
    db.delete(t)
    db.flush()
    if was_default:
        first = ld.default_template(db, kind)
        if first:
            first.is_default = True
    db.commit()
    flash(request, "Vorlage gelöscht.")
    return redirect(f"/etiketten/vorlagen?art={kind}")


# ------------------------------------------------------------------ Etikettendrucker / Direktdruck (v0.13)
def _printer(db: Session, pid) -> LabelPrinter:
    p = db.get(LabelPrinter, int(pid)) if str(pid or "").isdigit() else None
    if p is None:
        raise HTTPException(404, "Drucker nicht gefunden")
    return p


def _print_error(p: LabelPrinter, exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}"[:300]
    if isinstance(exc, OSError):
        msg = f"Drucker {p.host}:{p.port} nicht erreichbar ({msg}). Ist er eingeschaltet und im Netzwerk?"
    return msg


def print_now(db: Session, p: LabelPrinter, images) -> tuple[bool, str]:
    """Synchron drucken (für normale, nicht-async Routen – laufen ohnehin im Threadpool)."""
    from ..services import printing as pr

    try:
        pr.print_images(p, images)
        p.last_error, p.last_ok = None, now()
        db.commit()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = _print_error(p, exc)
        p.last_error = msg[:300]
        db.commit()
        return False, msg


async def _print(db: Session, p: LabelPrinter, images) -> tuple[bool, str]:
    import asyncio

    from ..services import printing as pr

    try:
        await asyncio.to_thread(pr.print_images, p, images)
        p.last_error, p.last_ok = None, now()
        db.commit()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = _print_error(p, exc)
        p.last_error = msg[:300]
        db.commit()
        return False, msg


def default_printer(db: Session) -> LabelPrinter | None:
    return db.scalar(select(LabelPrinter).where(LabelPrinter.enabled.is_(True)).order_by(LabelPrinter.is_default.desc(), LabelPrinter.id).limit(1))


def label_record(db: Session, art: str, oid: int, tc: TreeCache | None = None) -> dict | None:
    """Datensatz für genau ein Etikett (gleicher Code wie beim ersten Druck – alte Etiketten bleiben gültig)."""
    tc = tc or TreeCache(db)
    if art == "location":
        lo = db.get(Location, oid)
        if lo is None:
            return None
        content = ", ".join(sorted({lt.item.name for lt in db.scalars(select(StockLot).where(StockLot.location_id == lo.id))}))
        return ld.location_record(lo, tc, content)
    if art == "item":
        it = db.get(Item, oid)
        return ld.item_record(db, it, tc) if it else None
    if art == "lot":
        lt = db.get(StockLot, oid)
        return ld.lot_record(db, lt, tc) if lt else None
    return None


def browser_url(art: str, oid: int, tid: int | None = None, copies: int = 1) -> str:
    """Etikettenseite (Browserdruck) mit genau diesem einen Etikett."""
    key = {"location": "ort", "item": "artikel", "lot": "bestand"}[art]
    url = f"/etiketten?art={art}&{key}={oid}&f=1&unterorte=0&anzahl={copies}"
    return url + (f"&vorlage={tid}" if tid else "")


def _safe_back(url: str, fallback: str) -> str:
    url = (url or "").strip()
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else fallback


@router.post("/etiketten/nachdruck")
def reprint(request: Request, art: str = Form(...), id: int = Form(...), vorlage: str = Form(""), drucker: str = Form(""),
            anzahl: str = Form("1"), zurueck: str = Form(""), browser: str = Form(""), db: Session = Depends(get_db),
            ctx=Depends(require("labels", 1))):
    """Ein einzelnes Etikett nachdrucken (zerstört/unleserlich): Standardvorlage + Standarddrucker, ohne Umweg über die Etikettenseite.
    Ohne Etikettendrucker (oder bei A4-Vorlage) geht es auf die Etikettenseite zum Browserdruck."""
    if art not in ld.KINDS:
        raise HTTPException(400, "Unbekannte Etikettenart")
    t = db.get(LabelTemplate, int(vorlage)) if vorlage.isdigit() else None
    if t is None or t.kind != art:
        t = ld.default_template(db, art)
    if t is None:
        raise HTTPException(500, "Keine Etikettenvorlage vorhanden.")
    try:
        copies = max(1, min(int(anzahl or 1), 20))
    except ValueError:
        copies = 1
    rec = label_record(db, art, id)
    if rec is None:
        raise HTTPException(404, "Nicht gefunden")
    back_url = _safe_back(zurueck, browser_url(art, id, t.id))
    p = None
    if lic_has(db, "labelprint"):  # Direktdruck ist Pro – sonst Etikettenseite zum Drucken im Browser
        p = _printer(db, drucker) if drucker.isdigit() else default_printer(db)
    spec = ld.spec_of(t)
    if browser or p is None or spec.sheet:
        db.commit()
        return redirect(browser_url(art, id, t.id, copies))
    from ..services import printing as pr

    fin = ld.finish(rec, spec)
    images = [pr.render_label(fin, spec, pr.dpi_of(p)) for _ in range(copies)]
    ok, err = print_now(db, p, images)
    if ok and art == "location":
        lo = db.get(Location, id)
        lo.label_printed_at = now()
        db.commit()
    flash(request, f"Etikett {rec['code']} ({t.name}) an „{p.name}“ gesendet." if ok else f"Nachdruck fehlgeschlagen: {err}", "ok" if ok else "err")
    return redirect(back_url)


@router.get("/etiketten/code")
def reprint_by_code(request: Request, c: str = "", db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    """Code vom unleserlichen Etikett abtippen (L-…, A-…, S-… oder InvenTree-Code) -> passendes Etikett zum Nachdrucken."""
    from ..services import codes

    r = codes.resolve(db, c) if c.strip() else None
    if r is None or r.kind not in ("location", "item", "lot") or not r.id:
        flash(request, f"Code „{c.strip()}“ nicht gefunden." if c.strip() else "Bitte einen Code eingeben.", "err")
        return redirect("/etiketten")
    return redirect(browser_url(r.kind, r.id))


@router.get("/etiketten/drucker", dependencies=[Depends(require_pro("labelprint"))])
def printer_list(request: Request, id: int | None = None, db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    from ..services import printing as pr

    printers = list(db.scalars(select(LabelPrinter).order_by(LabelPrinter.is_default.desc(), LabelPrinter.name)))
    edit = db.get(LabelPrinter, id) if id else None
    return render(request, "label_printers.html", {"printers": printers, "edit": edit, "models": pr.MODELS, "media": pr.media_choices(), "drivers": pr.DRIVERS})


@router.post("/etiketten/drucker/speichern", dependencies=[Depends(require_pro("labelprint"))])
def printer_save(request: Request, id: str = Form(""), name: str = Form(...), model: str = Form("QL-820NWB"), host: str = Form(...),
                 port: str = Form(""), media: str = Form("29"), cut: str = Form(""), hq: str = Form(""), threshold: str = Form("70"),
                 rotate: str = Form(""), offset_mm: str = Form("0"), is_default: str = Form(""), enabled: str = Form(""),
                 driver: str = Form("brother_ql"), dpi: str = Form("203"), media_w_mm: str = Form(""), media_h_mm: str = Form(""),
                 gap_mm: str = Form(""), invert: str = Form(""),
                 db: Session = Depends(get_db), ctx=Depends(require("labels", 2))):
    from ..services import printing as pr

    p = _printer(db, id) if id else LabelPrinter()
    if driver not in pr.DRIVERS:
        raise HTTPException(400, "Unbekannte Druckersprache")
    if driver == "brother_ql" and model not in pr.MODELS:
        raise HTTPException(400, "Unbekanntes Druckermodell")
    if driver == "brother_ql" and media not in pr.media_choices():
        raise HTTPException(400, "Unbekanntes Etikettenformat")
    host = host.strip().removeprefix("http://").removeprefix("https://").strip("/")
    if not host:
        raise HTTPException(400, "Adresse fehlt (IP, usb:/dev/usb/lp0 oder CUPS-Warteschlange)")

    def num(v: str, lo: float, hi: float) -> float | None:
        v = (v or "").strip().replace(",", ".")
        return max(lo, min(float(v), hi)) if v else None

    try:
        p.port = max(1, min(int(port), 65535)) if str(port).strip() else pr.DEFAULT_PORT[driver] or 9100
        p.threshold = max(10, min(int(threshold or 70), 95))
        p.offset_mm = num(offset_mm, -10, 10) or 0.0
        p.dpi = 300 if driver == "brother_ql" else (int(dpi) if dpi in ("203", "300", "600") else 203)
        p.media_w_mm, p.media_h_mm, p.gap_mm = num(media_w_mm, 5, 300), num(media_h_mm, 5, 500), num(gap_mm, 0, 20)
    except ValueError:
        raise HTTPException(400, "Zahl erwartet (Port, Schwelle, Versatz, Größe)")
    p.driver = driver
    p.name, p.host = name.strip()[:120] or "Etikettendrucker", host[:200]
    p.model = model if driver == "brother_ql" else (p.model or "QL-820NWB")
    p.media = media if driver == "brother_ql" else (p.media or "29")
    p.cut, p.hq, p.enabled, p.invert = bool(cut), bool(hq), bool(enabled) or not id, bool(invert)
    if driver == "brother_ql":
        p.rotate = 270 if rotate == "270" else 90
    else:
        p.rotate = int(rotate) if rotate in ("0", "90", "180", "270") else 0
    if p.id is None:
        db.add(p)
    others = [o for o in db.scalars(select(LabelPrinter)) if o is not p]
    if is_default or not any(o.is_default for o in others):
        for o in others:
            o.is_default = False
        p.is_default = True
    db.commit()
    flash(request, f"Drucker „{p.name}“ gespeichert. Mit „Testdruck“ prüfen, ob alles passt.")
    return redirect(f"/etiketten/drucker?id={p.id}")


@router.post("/etiketten/drucker/{pid}/loeschen", dependencies=[Depends(require_pro("labelprint"))])
def printer_delete(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("labels", 2))):
    p = _printer(db, pid)
    was_default = p.is_default
    db.delete(p)
    db.flush()
    if was_default:
        first = db.scalars(select(LabelPrinter).order_by(LabelPrinter.name)).first()
        if first:
            first.is_default = True
    db.commit()
    flash(request, "Drucker gelöscht.")
    return redirect("/etiketten/drucker")


@router.post("/etiketten/drucker/{pid}/test", dependencies=[Depends(require_pro("labelprint"))])
async def printer_test(pid: int, request: Request, art: str = Form("muster"), db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    """Testdruck: Messmuster (Rahmen/Messstriche) oder ein Beispiel-Etikett mit der Standardvorlage."""
    from ..services import printing as pr

    p = _printer(db, pid)
    if art in ld.KINDS:
        spec = ld.spec_of(ld.default_template(db, art))
        img = pr.render_label(ld.finish(ld.sample_record(db, art), spec), spec, pr.dpi_of(p))
    else:
        img = pr.test_pattern(p)
    ok, err = await _print(db, p, [img])
    flash(request, f"Testdruck an „{p.name}“ gesendet." if ok else f"Testdruck fehlgeschlagen: {err}", "ok" if ok else "err")
    return redirect(f"/etiketten/drucker?id={p.id}")


@router.post("/etiketten/drucker/{pid}/verbindung", dependencies=[Depends(require_pro("labelprint"))])
async def printer_ping(pid: int, db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    import asyncio

    from ..services import printing as pr

    p = _printer(db, pid)
    ok, err = await asyncio.to_thread(pr.reachable, p)
    return {"ok": ok, "error": err}


@router.get("/etiketten/drucker/{pid}/vorschau.png", dependencies=[Depends(require_pro("labelprint"))])
def printer_preview(pid: int, art: str = "muster", db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    """So sieht der Drucker das Bild (nach Drehen/Einpassen auf das Band) – zum Kontrollieren ohne Papier."""
    import io

    from fastapi.responses import Response

    from ..services import printing as pr

    p = _printer(db, pid)
    if art in ld.KINDS:
        spec = ld.spec_of(ld.default_template(db, art))
        img = pr.render_label(ld.finish(ld.sample_record(db, art), spec), spec, pr.dpi_of(p))
    else:
        img = pr.test_pattern(p)
    out = pr.place_on_media(img, p)
    if p.driver == "brother_ql":
        out = out.rotate(-90 if p.rotate != 270 else 90, expand=True)  # zum Ansehen wieder quer
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/etiketten/direktdruck", dependencies=[Depends(require_pro("labelprint"))])
async def direct_print(request: Request, drucker: str = Form(""), query: str = Form(""), markieren: str = Form(""), db: Session = Depends(get_db),
                       ctx=Depends(require("labels", 1))):
    from urllib.parse import parse_qs

    from ..services import printing as pr

    p = _printer(db, drucker)
    q = parse_qs(query, keep_blank_values=True)
    data = _build(request, db, q)
    back_url = "/etiketten?" + query
    spec, recs = data["s"], data["labels"]
    if spec.sheet:
        flash(request, "A4-Bögen können nicht direkt auf den Etikettendrucker gedruckt werden – bitte eine Band-/Etikettenvorlage wählen.", "err")
        return redirect(back_url)
    if not recs:
        flash(request, "Keine Etiketten ausgewählt.", "err")
        return redirect(back_url)
    import asyncio

    images = await asyncio.to_thread(lambda: [pr.render_label(r, spec, pr.dpi_of(p)) for r in recs])
    ok, err = await _print(db, p, images)
    if ok and data["art"] == "location" and markieren:
        for lid in data["loc_ids"]:
            lo = db.get(Location, lid)
            if lo:
                lo.label_printed_at = now()
        db.commit()
    flash(request, f"{len(images)} Etikett{'en' if len(images) != 1 else ''} an „{p.name}“ gesendet." if ok else f"Druck fehlgeschlagen: {err}",
          "ok" if ok else "err")
    return redirect(back_url)


@router.post("/etiketten/vorlagen/testdruck", dependencies=[Depends(require_pro("labelprint"))])
async def template_testprint(request: Request, db: Session = Depends(get_db), ctx=Depends(require("labels", 1))):
    """Designer: aktuelle (auch ungespeicherte) Vorlage mit Beispieldaten auf dem Drucker ausgeben."""
    from ..services import printing as pr

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON erwartet")
    p = _printer(db, body.get("drucker"))
    spec = _spec_from_body(body)
    img = pr.render_label(ld.finish(ld.sample_record(db, spec.kind), spec), spec, pr.dpi_of(p))
    ok, err = await _print(db, p, [img])
    return {"ok": ok, "message": f"Testdruck an „{p.name}“ gesendet." if ok else f"Testdruck fehlgeschlagen: {err}"}
