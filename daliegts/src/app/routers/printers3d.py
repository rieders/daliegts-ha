"""3D-Drucker: Spule einsetzen, Verbrauch sehen, gemerkte Drucke nachträglich abbuchen (v0.16)."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, PrintModel, Printer3D, StockLot, StockMovement
from ..security import require
from ..services import audit
from ..services import printer3d as p3
from ..services import printer3d_drivers as drv
from ..services.codes import resolve
from ..services.locations import TreeCache
from ..services.license import require_pro
from ..web import flash, redirect, render

router = APIRouter(dependencies=[Depends(require_pro("printer3d"))])


def _printer(db: Session, pid) -> Printer3D:
    p = db.get(Printer3D, int(pid)) if str(pid or "").isdigit() else None
    if p is None:
        raise HTTPException(404, "Drucker nicht gefunden")
    return p


MODELS3D = list(drv.MODEL_KIND)  # alle Modelle aller Familien (v0.23)


def _lot_from(db: Session, request: Request, code: str, lot_id: str) -> tuple[StockLot | None, bool]:
    """Spule aus Scan oder Liste. Rückgabe (Bestand, ok)."""
    if code.strip():
        r = resolve(db, code)
        if r.kind == "lot":
            return db.get(StockLot, r.id), True
        if r.kind == "item":
            lots = [lt for lt in db.get(Item, r.id).lots if lt.status not in ("lost", "used")]
            if len(lots) == 1:
                return lots[0], True
            flash(request, "Dieser Artikel hat mehrere Spulen – bitte die Spule selbst scannen oder aus der Liste wählen.", "err")
            return None, False
        flash(request, f"„{code}“ ist keine bekannte Spule.", "err")
        return None, False
    return (db.get(StockLot, int(lot_id)) if lot_id.isdigit() else None), True


def _filament_category_ids(db: Session) -> set[int]:
    """Kategorie „Filament“ und alles darunter (PLA, PETG …)."""
    from ..models import Category

    cats = list(db.scalars(select(Category)))
    roots = {c.id for c in cats if c.name.strip().lower() == "filament"}
    ids, changed = set(roots), True
    while changed:
        changed = False
        for c in cats:
            if c.parent_id in ids and c.id not in ids:
                ids.add(c.id)
                changed = True
    return ids


def _spools(db: Session) -> list[StockLot]:
    """Filament-Spulen: Bestände von Artikeln der Art „Filament“, aus der Kategorie Filament oder mit Einheit g/kg."""
    cat_ids = _filament_category_ids(db)
    cond = (Item.kind == "filament") | Item.unit.in_(("g", "kg"))
    if cat_ids:
        cond = cond | Item.category_id.in_(cat_ids)
    lots = db.scalars(select(StockLot).join(Item).where(StockLot.status.notin_(("lost", "used")), cond)).unique()
    return sorted(lots, key=lambda lt: (lt.item.kind != "filament", lt.item.name.lower()))


@router.get("/drucker3d")
def page(request: Request, id: int | None = None, neu: int | None = None, db: Session = Depends(get_db), ctx=Depends(require("stock", 1))):
    printers = list(db.scalars(select(Printer3D).order_by(Printer3D.name)))
    history = {p.id: list(db.scalars(select(StockMovement).where(StockMovement.ref == f"3d:{p.id}").order_by(StockMovement.id.desc()).limit(10)))
               for p in printers}
    models, fit = {}, {}
    for p in printers:
        rows = list(db.scalars(select(PrintModel).where(PrintModel.printer_id == p.id)))
        rows.sort(key=lambda m: (m.last_printed is None, -(m.last_printed.timestamp() if m.last_printed else 0), m.name.lower()))
        models[p.id] = rows
        for m in rows:
            fit[m.id] = p3.fits(db, p, m.need_g)
    import os

    rec = {p.id: (os.path.getsize(f) if (f := p3.recording_path(p.id)).exists() else 0) for p in printers}
    from ..models import LabelPrinter
    from ..services.labeldesign import spool_templates

    spool_tpls = spool_templates(db)
    db.commit()
    label_printers = list(db.scalars(select(LabelPrinter).where(LabelPrinter.enabled.is_(True)).order_by(LabelPrinter.is_default.desc(), LabelPrinter.name)))
    new_lot = db.get(StockLot, neu) if neu else None
    return render(request, "printers3d.html", {
        "spool_tpls": spool_tpls, "label_printers": label_printers, "new_lot": new_lot,
        "printers": printers, "edit": db.get(Printer3D, id) if id else None, "spools": _spools(db), "tc": TreeCache(db),
        "labels": p3.STATE_LABELS, "info": {p.id: p3.spool_info(p.spool_lot) for p in printers}, "history": history,
        "slots": {p.id: p3.slot_lots(db, p) for p in printers if p.has_cfs}, "slot_info": p3.spool_info,
        "models": models, "fit": fit, "rec": rec, "now": datetime.now(), "MODELS3D": MODELS3D, "MATERIALS": MATERIALS,
        "MODEL_GROUPS": drv.MODEL_GROUPS, "GUIDES": drv.SETUP_GUIDES, "KINDS": drv.KINDS, "MODEL_KIND": drv.MODEL_KIND, "MULTI": drv.MULTI_SPOOL,
    })


@router.post("/drucker3d/speichern")
async def save(request: Request, id: str = Form(""), name: str = Form(...), host: str = Form(...), port: str = Form("9999"),
         diameter_mm: str = Form("1.75"), enabled: str = Form(""), model: str = Form("K1C"), has_cfs: str = Form(""),
         access_code: str = Form(""), serial: str = Form(""), db: Session = Depends(get_db), ctx=Depends(require("stock", 3))):
    p = _printer(db, id) if id else Printer3D(kind="creality", last_state="")
    model = model if model in MODELS3D else "andere"
    kind = drv.kind_of(model)
    host = host.strip().removeprefix("http://").removeprefix("https://").strip("/")
    if not host:
        raise HTTPException(400, "IP-Adresse fehlt")
    try:
        p.port = max(1, min(int(port or drv.KINDS[kind][1]), 65535))
        p.diameter_mm = max(1.0, min(float(str(diameter_mm or 1.75).replace(",", ".")), 3.0))
    except ValueError:
        raise HTTPException(400, "Zahl erwartet")
    p.name, p.host, p.enabled = name.strip()[:120] or "3D-Drucker", host[:200], bool(enabled) or not id
    if p.kind != kind and str(port or "") == str(drv.KINDS.get(p.kind or "creality", ("", 0))[1]):
        p.port = drv.KINDS[kind][1]  # Familie gewechselt, Port war Standard -> Standard der neuen Familie
    p.model, p.kind = model, kind
    p.has_cfs = bool(has_cfs)
    if access_code.strip():  # leeres Feld beim Bearbeiten = gespeicherten Code/Schlüssel behalten
        p.access_code = access_code.strip()[:200]
    elif not id:
        p.access_code = ""
    p.serial = serial.strip()[:64]
    if kind == "bambu" and not (p.access_code and p.serial):
        raise HTTPException(400, "Bei Bambu Lab werden Zugangscode und Seriennummer gebraucht (am Drucker unter Einstellungen).")
    if p.id is None:
        db.add(p)
    db.commit()
    _resync(request)
    flash(request, f"„{p.name}“ gespeichert – DaLiegt’s verbindet sich in wenigen Sekunden.")
    return redirect("/drucker3d")


def _resync(request: Request) -> None:
    """Verbindungen sofort anpassen (sonst spätestens nach 60 s durch die Überwachung)."""
    if getattr(request.app.state, "background_jobs", False):
        try:
            p3.sync_monitors()
        except RuntimeError:  # kein laufender Event-Loop
            pass


@router.post("/drucker3d/{pid}/loeschen")
async def delete(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 3))):
    db.delete(_printer(db, pid))
    db.commit()
    _resync(request)
    flash(request, "Drucker entfernt.")
    return redirect("/drucker3d")


@router.post("/drucker3d/{pid}/spule")
def set_spool(pid: int, request: Request, lot_id: str = Form(""), code: str = Form(""), db: Session = Depends(get_db),
              ctx=Depends(require("stock", 2))):
    """Spule einsetzen: aus der Liste wählen oder Etikett scannen (Bestand, Artikel mit genau einer Spule)."""
    p = _printer(db, pid)
    lot, ok = _lot_from(db, request, code, lot_id)
    if not ok:
        return redirect("/drucker3d")
    p.spool_lot_id = lot.id if lot else None
    db.flush()
    db.refresh(p)
    p.warning = p3.spool_warning(p, p.job) if p.job else None
    audit.log(db, ctx.user_id, "update", "printer3d", p.id, f"{p.name}: Spule {lot.item.name if lot else '–'} eingesetzt")
    db.commit()
    flash(request, f"„{lot.item.name}“ ({lot.code}) eingesetzt." if lot else "Spule entnommen.")
    return redirect("/drucker3d")


@router.post("/drucker3d/{pid}/offen/{idx}")
def settle(pid: int, idx: int, request: Request, lot_id: str = Form(""), verwerfen: str = Form(""), db: Session = Depends(get_db),
           ctx=Depends(require("stock", 2))):
    p = _printer(db, pid)
    if verwerfen:
        pend = list(p.pending or [])
        if 0 <= idx < len(pend):
            pend.pop(idx)
        p.pending = pend
        db.commit()
        return redirect("/drucker3d")
    lot = db.get(StockLot, int(lot_id)) if lot_id.isdigit() else None
    if lot is None:
        flash(request, "Spule wählen.", "err")
        return redirect("/drucker3d")
    try:
        msg = p3.settle_pending(db, p, idx, lot, ctx.user_id)
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect("/drucker3d")
    db.commit()
    flash(request, msg)
    return redirect("/drucker3d")


@router.post("/drucker3d/{pid}/hinweis-weg")
def clear_warning(pid: int, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    p = _printer(db, pid)
    p.warning = None
    db.commit()
    return redirect("/drucker3d")


@router.post("/drucker3d/{pid}/platz/{slot}")
def set_slot(pid: int, slot: int, request: Request, lot_id: str = Form(""), code: str = Form(""), db: Session = Depends(get_db),
             ctx=Depends(require("stock", 2))):
    """CFS: Spule in Platz 1–4 eintragen (scannen oder wählen), leer = Platz frei."""
    p = _printer(db, pid)
    if not 1 <= slot <= p3.CFS_SLOTS:
        raise HTTPException(404, "Platz gibt es nicht")
    lot, ok = _lot_from(db, request, code, lot_id)
    if not ok:
        return redirect("/drucker3d")
    slots = (list(p.cfs_slots or []) + [None] * p3.CFS_SLOTS)[:p3.CFS_SLOTS]
    if lot is not None:
        slots = [None if s == lot.id else s for s in slots]  # eine Spule steckt nur in einem Platz
    slots[slot - 1] = lot.id if lot else None
    p.cfs_slots = slots
    db.flush()
    db.refresh(p)
    p.warning = p3.spool_warning(p, p.job, db) if p.job else None
    audit.log(db, ctx.user_id, "update", "printer3d", p.id, f"{p.name}: CFS-Platz {slot}: {lot.item.name if lot else 'leer'}")
    db.commit()
    flash(request, f"Platz {slot}: „{lot.item.name}“" if lot else f"Platz {slot} ist jetzt leer.")
    return redirect("/drucker3d")


@router.post("/drucker3d/{pid}/mitschnitt")
def recording(pid: int, request: Request, stunden: str = Form("0"), loeschen: str = Form(""), db: Session = Depends(get_db),
              ctx=Depends(require("stock", 3))):
    """Mitschnitt der Drucker-Telemetrie starten/stoppen – zum Einbauen neuer Funktionen (z. B. CFS)."""
    p = _printer(db, pid)
    path = p3.recording_path(p.id)
    if loeschen and path.exists():
        path.unlink()
    h = int(stunden) if stunden.isdigit() else 0
    p.recording_until = datetime.now() + timedelta(hours=min(h, 72)) if h > 0 else None
    db.commit()
    mon = p3.MONITORS.get(p.id)
    if mon is not None:
        mon.record_until = p.recording_until
    flash(request, f"Mitschnitt läuft bis {p.recording_until:%d.%m. %H:%M} Uhr." if p.recording_until else ("Mitschnitt gelöscht." if loeschen else "Mitschnitt gestoppt."))
    return redirect("/drucker3d")


@router.get("/drucker3d/{pid}/mitschnitt.jsonl")
def recording_download(pid: int, db: Session = Depends(get_db), ctx=Depends(require("stock", 1))):
    from fastapi.responses import FileResponse

    p = _printer(db, pid)
    path = p3.recording_path(p.id)
    if not path.exists():
        raise HTTPException(404, "Noch kein Mitschnitt vorhanden")
    return FileResponse(path, media_type="application/x-ndjson", filename=f"lagerwerk-mitschnitt-{p.model.replace(' ', '')}-{datetime.now():%Y%m%d-%H%M}.jsonl")


@router.post("/drucker3d/{pid}/dateien")
async def sync_files(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    """Dateiliste jetzt vom Drucker holen (Modell-Bibliothek)."""
    import asyncio

    p = _printer(db, pid)
    mon = p3.MONITORS.get(p.id)
    if mon is None or not mon.connected:
        flash(request, "Der Drucker ist gerade nicht verbunden – die Liste wird automatisch geholt, sobald er erreichbar ist.", "err")
        return redirect("/drucker3d")
    await mon.request_files()
    await asyncio.sleep(3)
    flash(request, "Dateiliste vom Drucker geholt.")
    return redirect("/drucker3d#modelle")


@router.post("/drucker3d/modell/{mid}")
def model_notes(mid: int, request: Request, notes: str = Form(""), gramm: str = Form(None), loeschen: str = Form(""),
                db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    m = db.get(PrintModel, mid)
    if m is None:
        raise HTTPException(404, "Modell nicht gefunden")
    if loeschen:
        db.delete(m)
        flash(request, f"Modell „{m.name}“ gelöscht.")
    else:
        m.notes = notes.strip()[:2000]
        if gramm is not None:
            g = p3._num(gramm.replace(",", ".")) if gramm.strip() else None
            m.expected_g = round(g, 1) if g and g > 0 else None
    db.commit()
    return redirect("/drucker3d#modelle")


@router.post("/drucker3d/{pid}/modell-neu")
def model_add(pid: int, request: Request, name: str = Form(...), gramm: str = Form(""), notes: str = Form(""),
              db: Session = Depends(get_db), ctx=Depends(require("stock", 2))):
    """Modell von Hand in die Bibliothek (z. B. Bambu: Druck aus Bambu Studio, Datei nie auf dem Drucker gespeichert)."""
    p = _printer(db, pid)
    name = name.strip()[:255]
    if not name:
        raise HTTPException(400, "Name fehlt")
    g = p3._num(gramm.replace(",", ".")) if gramm.strip() else None
    m = p3.model_for(db, p, name)
    if m is None:
        m = PrintModel(printer_id=p.id, path=name, name=name, prints=0, on_printer=False, notes="")
        db.add(m)
    if g and g > 0:
        m.expected_g = round(g, 1)
    if notes.strip():
        m.notes = notes.strip()[:2000]
    db.commit()
    flash(request, f"Modell „{name}“ gespeichert.")
    return redirect("/drucker3d#modelle")


MATERIALS = ["PLA", "PLA+", "PETG", "ABS", "ASA", "TPU", "PC", "PA (Nylon)", "anderes"]


@router.post("/drucker3d/spule-neu")
def new_spool(request: Request, material: str = Form("PLA"), farbe: str = Form(""), marke: str = Form(""), gramm: str = Form("1000"),
              ort: str = Form(""), einsetzen: str = Form(""), etiketten: str = Form(""), etikettendrucker: str = Form(""),
              db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    """Neue Filament-Spule in einem Schritt: Artikel (Art Filament, Einheit g) + Bestand, auf Wunsch gleich einsetzen."""
    from ..models import Category
    from ..services.items import create_item, reindex
    from ..services.stock import _move, new_lot
    from .items import try_resolve_location

    material = material if material in MATERIALS else "PLA"
    try:
        grams = max(0.0, min(float((gramm or "0").replace(",", ".")), 20000.0))
    except ValueError:
        raise HTTPException(400, "Gewicht als Zahl eingeben")
    name = " ".join(x for x in (material if material != "anderes" else "Filament", farbe.strip(), marke.strip()) if x)[:200]
    # passende Kategorie: „PLA“ unter „Filament“, sonst „Filament“
    cat_ids = _filament_category_ids(db)
    cats = [c for c in db.scalars(select(Category)) if c.id in cat_ids]
    key = material.split(" ")[0].replace("+", "").lower()
    cat = next((c for c in cats if c.name.strip().lower() == key), None) or next((c for c in cats if c.name.strip().lower() == "filament"), None)
    it = db.scalar(select(Item).where(Item.name == name, Item.kind == "filament"))
    if it is None:
        it = create_item(db, name, ctx.user_id, kind="filament", unit="g", category_id=cat.id if cat else None,
                         description=f"{material}-Filament 1,75 mm" + (f", {marke.strip()}" if marke.strip() else ""))
        reindex(db, it)
    loc_id = None
    if ort.strip():
        loc_id, err = try_resolve_location(db, ort)
        if err:
            flash(request, err + " – Spule ohne Lagerort angelegt.", "err")
    lot = new_lot(db, it, loc_id, grams)  # jede Spule ein eigener Bestand (nicht mit anderen zusammenlegen)
    _move(db, it, lot, "created", grams, ctx.user_id, to=loc_id, note="Neue Spule")
    msg = f"Spule „{it.name}“ ({lot.code}) mit {grams:g} g angelegt."
    if einsetzen.isdigit():
        p = _printer(db, einsetzen)
        if p.has_cfs:
            slots = (list(p.cfs_slots or []) + [None] * p3.CFS_SLOTS)[:p3.CFS_SLOTS]
            free = next((i for i, x in enumerate(slots) if not x), None)
            if free is not None:
                slots[free] = lot.id
                p.cfs_slots = slots
                msg += f" In Platz {free + 1} eingesetzt."
        else:
            p.spool_lot_id = lot.id
            msg += f" In „{p.name}“ eingesetzt."
    audit.log(db, ctx.user_id, "create", "item", it.id, f"Filament-Spule {it.name} {grams:g} g")
    db.commit()
    # Etiketten: eins für die Spule, eins für Karton/Beutel – beide mit dem Code dieses Bestands
    roles = {"beide": ("spool", "pack"), "spule": ("spool",), "verpackung": ("pack",)}.get(etiketten, ())
    level = "ok"
    if roles and ctx.can("labels", 1):
        from ..models import LabelPrinter
        from ..services import labeldesign as ld
        from ..services import printing as pr
        from .labels import default_printer, label_record, print_now

        lp = db.get(LabelPrinter, int(etikettendrucker)) if etikettendrucker.isdigit() else default_printer(db)
        tpls = ld.spool_templates(db)
        db.commit()
        rec = label_record(db, "lot", lot.id)
        if lp is not None and lp.enabled:
            imgs = []
            for role in roles:
                spec = ld.spec_of(tpls[role])
                if not spec.sheet:
                    imgs.append(pr.render_label(ld.finish(rec, spec), spec, pr.dpi_of(lp)))
            ok, err = print_now(db, lp, imgs) if imgs else (False, "A4-Vorlage kann nicht direkt gedruckt werden")
            if ok:
                msg += f" {len(imgs)} Etikett{'en' if len(imgs) != 1 else ''} an „{lp.name}“ gesendet."
            else:
                msg += f" Etikettendruck fehlgeschlagen: {err}"
                level = "err"
    flash(request, msg, level)
    return redirect(f"/drucker3d?neu={lot.id}#neu")
