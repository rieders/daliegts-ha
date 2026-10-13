"""Info-Provider: Einstellungen, Online-Suche am Artikel, Preise aktualisieren, Entscheidungen (nachfragen)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CANDIDATE_REASONS, Item, ProviderCandidate, SupplierPart
from ..security import check, get_setting, require, set_setting
from ..services import audit, infosync
from ..services import purchasing as pur
from ..services.items import reindex, safe_url
from ..services.providers import DEFAULT_OPTIONS, Hit, active_providers, load_config, normalize_mpn, registry
from ..web import back, flash, redirect, render

router = APIRouter()


def _item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Artikel nicht gefunden")
    return item


# ------------------------------------------------------------------ Einstellungen
@router.get("/admin/info-provider")
def settings_page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    conf, opts = load_config(db)
    rows = []
    for key, cls in registry().items():
        c = conf.get(key, {})
        rows.append({"key": key, "cls": cls, "enabled": bool(c.get("enabled")),
                     "have": {f: bool(c.get(f)) for f, _ in cls.fields}, "configured": cls(c).configured()})
    from ..services import vision

    vconf = vision.load_config(db)
    from ..services import datasheets as dsh

    ds = {"todo": len(dsh.candidates(db, force=True)), "offline": bool(get_setting(db, "datasheets.offline", True)),
          "job": request.query_params.get("job", "")}
    return render(request, "admin_providers.html", {"ds": ds, "rows": rows, "opts": opts, "last_run": get_setting(db, "providers.last_run"),
                                                    "vision": {"enabled": vconf.get("enabled"), "model": vconf.get("model"),
                                                               "api_key": bool(vconf.get("api_key"))}})


@router.post("/admin/info-provider")
async def settings_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 2))):
    f = await request.form()
    public = get_setting(db, "providers", {}) or {}
    secret = dict(get_setting(db, "secret.providers", {}) or {})
    for key, cls in registry().items():
        public[key] = {"enabled": bool(f.get(f"{key}.enabled"))}
        sec = dict(secret.get(key, {}))
        for field, _ in cls.fields:
            val = (f.get(f"{key}.{field}") or "").strip()
            if val:
                sec[field] = val[:300]
            if f.get(f"{key}.clear"):
                sec.pop(field, None)
        secret[key] = sec
    opts = dict(DEFAULT_OPTIONS)
    opts["nightly"] = bool(f.get("nightly"))
    opts["cross_check"] = bool(f.get("cross_check"))
    opts["auto_on_create"] = bool(f.get("auto_on_create"))
    try:
        opts["budget"] = max(10, min(int(f.get("budget") or 150), 2000))
        opts["cross_check_days"] = max(1, min(int(f.get("cross_check_days") or 30), 365))
        opts["usd_eur"] = max(0.1, min(float(str(f.get("usd_eur") or 0.9).replace(",", ".")), 10))
    except ValueError:
        flash(request, "Bitte Zahlen eingeben.", "err")
        return redirect("/admin/info-provider")
    set_setting(db, "providers", public)
    set_setting(db, "secret.providers", secret)
    from ..services.vision import DEFAULT_MODEL

    set_setting(db, "vision", {"enabled": bool(f.get("vision.enabled")), "model": (f.get("vision.model") or DEFAULT_MODEL).strip()[:80]})
    vsec = dict(get_setting(db, "secret.vision", {}) or {})
    if (f.get("vision.api_key") or "").strip():
        vsec["api_key"] = f["vision.api_key"].strip()[:300]
    if f.get("vision.clear"):
        vsec.pop("api_key", None)
    set_setting(db, "secret.vision", vsec)
    set_setting(db, "providers.options", opts)
    audit.log(db, ctx.user_id, "update", "settings", None, "Info-Provider-Einstellungen geändert")
    db.commit()
    flash(request, "Gespeichert.")
    return redirect("/admin/info-provider")


@router.post("/admin/info-provider/vision/test")
async def vision_test(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    """Schickt ein kleines Testbild (Widerstand-Farbringe) an die KI."""
    from PIL import Image, ImageDraw

    from ..services import vision

    img = Image.new("RGB", (600, 200), "#f0e6d2")
    d = ImageDraw.Draw(img)
    d.rectangle([150, 70, 450, 130], fill="#d9c7a0")
    for x, col in ((190, "#e6c200"), (240, "#7b1fa2"), (290, "#d32f2f"), (400, "#c9a227")):
        d.rectangle([x, 70, x + 22, 130], fill=col)
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        res = await vision.analyze([buf.getvalue()], vision.load_config(db), "Testbild: gezeichneter Widerstand")
        wert = next((p["wert"] for p in res["parameter"] if p["name"].lower() == "widerstand"), "?")
        flash(request, f"Bilderkennung funktioniert: „{res['name']}“ (Widerstand {wert} Ω).")
    except vision.VisionError as e:
        flash(request, str(e), "err")
    return redirect("/admin/info-provider#vision")


@router.post("/admin/info-provider/{key}/test")
async def settings_test(key: str, request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    cls = registry().get(key)
    if cls is None:
        raise HTTPException(404, "Unbekannter Provider")
    conf, _ = load_config(db)
    p = cls(conf.get(key, {}), infosync.new_client())
    if not p.configured():
        flash(request, f"{cls.name}: Zugangsdaten fehlen.", "err")
        return redirect("/admin/info-provider")
    res = (await infosync.search_all([p], "NE555", 3))[key]
    await p.client.aclose()
    if isinstance(res, str):
        flash(request, res, "err")
    elif not res:
        flash(request, f"{cls.name}: verbunden, aber keine Treffer für „NE555“ – Parser prüfen.", "warn")
    else:
        h = res[0]
        price = f", ab {h.prices[0][0]:g}: {h.prices[0][1]:g} {h.currency}" if h.prices else ", ohne Preis"
        flash(request, f"{cls.name} funktioniert: {len(res)} Treffer, z. B. {h.sku} ({h.mpn or h.name}{price}).")
    return redirect("/admin/info-provider")


# ------------------------------------------------------------------ Online-Suche am Artikel
@router.get("/artikel/{item_id}/online")
def online_page(item_id: int, request: Request, q: str = "", nur: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    item = _item(db, item_id)
    provs = active_providers(db)
    return render(request, "online.html", {"item": item, "q": q or infosync.default_query(item), "providers": provs, "nur": nur,
                                           "problems": infosync.query_problems(q or infosync.default_query(item))})


@router.get("/artikel/{item_id}/online/ergebnisse")
async def online_results(item_id: int, request: Request, q: str = "", nur: str = "", db: Session = Depends(get_db),
                         ctx=Depends(require("items", 1))):
    item = _item(db, item_id)
    problems = infosync.query_problems(q)
    if problems:
        return render(request, "partials/online_results.html", {"item": item, "problems": problems, "found": {}, "q": q})
    async with infosync.new_client() as client:
        provs = active_providers(db, client, only=[nur] if nur else None)
        found = await infosync.search_all(provs, q, 8)
    need = pur.need_qty(item)
    have = {(infosync.provider_key_of(sp), normalize_mpn(sp.sku)) for sp in db.scalars(select(SupplierPart).where(SupplierPart.item_id == item.id))}
    rows = {}
    for key, hits in found.items():
        if isinstance(hits, str):
            rows[key] = hits
            continue
        exact, unsure = infosync.exact_matches(item, hits)
        exact_ids = {id(h) for h in exact}
        rows[key] = [{"hit": h, "json": json.dumps(h.to_dict(), ensure_ascii=False), "exact": id(h) in exact_ids and not unsure,
                      "price": h.price_for(need), "total": h.total_for(need),
                      "linked": (key, normalize_mpn(h.sku)) in have} for h in hits]
    return render(request, "partials/online_results.html", {"item": item, "found": rows, "need": need, "q": q, "problems": [],
                                                            "names": {k: c.name for k, c in registry().items()}})


def _clean_hit(raw: str) -> Hit | None:
    try:
        h = Hit.from_dict(json.loads(raw))
    except (ValueError, TypeError):
        return None
    if h.provider not in registry() or not h.sku:
        return None
    h.url, h.image, h.datasheet = safe_url(h.url), safe_url(h.image), safe_url(h.datasheet)
    h.prices = [(q, p) for q, p in h.prices if q > 0 and p >= 0][:20]
    return h


@router.post("/artikel/{item_id}/online/uebernehmen")
async def online_apply(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    item = _item(db, item_id)
    f = await request.form()
    fill = {k for k in ("mpn", "manufacturer", "description", "datasheet", "image") if f.get(f"fill_{k}")}
    _, opts = load_config(db)
    n = 0
    for raw in f.getlist("hit"):
        h = _clean_hit(raw)
        if h is not None:
            infosync.apply_hit(db, item, h, origin="manuell", fill=fill, rate=opts["usd_eur"])
            n += 1
    if not n:
        flash(request, "Nichts ausgewählt.", "warn")
        return back(request, f"/artikel/{item.id}")
    for c in db.scalars(select(ProviderCandidate).where(ProviderCandidate.item_id == item.id, ProviderCandidate.status == "open")):
        c.status = "done"
    reindex(db, item)
    audit.log(db, ctx.user_id, "update", "item", item.id, f"{n} Bezugsquelle(n) online übernommen")
    db.commit()
    flash(request, f"{n} Bezugsquelle(n) übernommen – Preise und Bestellnummern sind eingetragen.")
    return redirect(f"/artikel/{item.id}#bezug")


@router.post("/artikel/{item_id}/preise-aktualisieren")
async def refresh(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    item = _item(db, item_id)
    _, opts = load_config(db)
    async with infosync.new_client() as client:
        provs = {p.key: p for p in active_providers(db, client)}
        if not provs:
            flash(request, "Kein Info-Provider aktiv – unter Verwaltung › Info-Provider einschalten.", "warn")
            return redirect(f"/artikel/{item.id}#bezug")
        msgs = await infosync.refresh_item(db, item, provs, opts["usd_eur"])
        res = await infosync.cross_check(db, item, provs, opts["usd_eur"])
    db.commit()
    parts = [f"{sum(1 for m in msgs if 'aktualisiert' in m)} Preis(e) aktualisiert"]
    if res["linked"]:
        parts.append("neu gefunden bei " + ", ".join(registry()[k].name for k in res["linked"]))
    if res["asked"]:
        parts.append("bitte entscheiden (siehe unten)")
    problems = [m for m in msgs if "aktualisiert" not in m] + res["errors"]
    flash(request, " · ".join(parts) + ".")
    if problems:
        flash(request, " · ".join(problems[:4]), "warn")
    return redirect(f"/artikel/{item.id}#bezug")


# ------------------------------------------------------------------ Einkaufsliste: alles aktualisieren (Hintergrund)
@router.post("/einkauf/aktualisieren")
async def shopping_refresh(request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    ids = infosync.shopping_item_ids(db)
    if not ids:
        flash(request, "Keine Artikel auf der Einkaufsliste.", "warn")
        return redirect("/einkauf")
    job = infosync.start_job(ids, cross=True, label="Einkaufsliste")
    return redirect(f"/einkauf?job={job}")


@router.get("/jobs/{job_id}")
def job(job_id: str, ctx=Depends(require("items", 1))):
    st = infosync.job_status(job_id)
    if st is None:
        raise HTTPException(404, "Auftrag nicht gefunden")
    return st


# ------------------------------------------------------------------ Entscheidungen
@router.get("/entscheidungen")
def candidates(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    rows = list(db.scalars(select(ProviderCandidate).where(ProviderCandidate.status == "open").order_by(ProviderCandidate.item_id,
                                                                                                         ProviderCandidate.id)))
    return render(request, "candidates.html", {"rows": rows, "names": {k: c.name for k, c in registry().items()},
                                               "REASONS": CANDIDATE_REASONS, "hits": {c.id: [Hit.from_dict(d) for d in (c.data or [])] for c in rows},
                                               "need": {c.id: pur.need_qty(c.item) for c in rows}})


def _cand(db: Session, cid: int) -> ProviderCandidate:
    c = db.get(ProviderCandidate, cid)
    if c is None:
        raise HTTPException(404, "Nicht gefunden")
    return c


@router.post("/entscheidungen/{cid}/uebernehmen/{idx}")
def candidate_take(cid: int, idx: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    c = _cand(db, cid)
    data = c.data or []
    if not 0 <= idx < len(data):
        raise HTTPException(404, "Treffer nicht gefunden")
    h = _clean_hit(json.dumps(data[idx]))
    if h is None:
        raise HTTPException(400, "Ungültiger Treffer")
    _, opts = load_config(db)
    infosync.apply_hit(db, c.item, h, origin="manuell", fill={"mpn", "manufacturer", "datasheet"}, rate=opts["usd_eur"])
    c.status = "done"
    audit.log(db, ctx.user_id, "update", "item", c.item_id, f"Bezugsquelle {h.sku} bei {registry()[h.provider].name} bestätigt")
    db.commit()
    flash(request, f"„{c.item.name}“: {h.sku} übernommen.")
    return redirect("/entscheidungen")


@router.post("/entscheidungen/{cid}/verwerfen")
def candidate_dismiss(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    c = _cand(db, cid)
    c.status = "dismissed"
    db.commit()
    return redirect("/entscheidungen")


@router.post("/entscheidungen/{cid}/angaben")
async def candidate_details(cid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    """Fehlende Angaben nachtragen (Hersteller-Nr.) und sofort erneut suchen."""
    c = _cand(db, cid)
    f = await request.form()
    mpn = (f.get("mpn") or "").strip()[:120]
    if not mpn:
        flash(request, "Bitte eine Hersteller-Nr. eintragen.", "err")
        return redirect("/entscheidungen")
    item = c.item
    item.mpn = mpn
    c.status = "done"
    reindex(db, item)
    _, opts = load_config(db)
    async with infosync.new_client() as client:
        provs = {p.key: p for p in active_providers(db, client)}
        res = await infosync.cross_check(db, item, provs, opts["usd_eur"])
    db.commit()
    flash(request, f"„{item.name}“: Hersteller-Nr. gespeichert. " + (f"Eingetragen bei {', '.join(registry()[k].name for k in res['linked'])}. " if res["linked"] else "")
          + ("Weitere Rückfragen stehen in der Liste." if res["asked"] else ""))
    return redirect("/entscheidungen")


def schedule_after_save(db: Session, item: Item, ctx) -> None:
    """Nach Anlegen/Ändern mit Hersteller-Nr.: im Hintergrund Bestellnummern und Preise suchen."""
    _, opts = load_config(db)
    if item.mpn and opts.get("auto_on_create", True) and active_providers(db):
        try:
            check(ctx, "items", 2)
            infosync.start_job([item.id], cross=True, label=item.name)
        except Exception:  # noqa: BLE001 – ohne Recht oder ohne laufende Schleife einfach nichts tun
            pass


# ------------------------------------------------------------------ Datenblätter offline (v0.16)
@router.post("/admin/datenblaetter")
async def datasheets_all(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 2))):
    from ..services import datasheets as dsh

    f = await request.form()
    if "offline" in f:
        set_setting(db, "datasheets.offline", f.get("offline") == "1")
        db.commit()
        flash(request, "Gespeichert.")
        return redirect("/admin/info-provider")
    ids = dsh.candidates(db, force=True)
    if not ids:
        flash(request, "Alle verlinkten Datenblätter sind schon offline gespeichert.")
        return redirect("/admin/info-provider")
    return redirect(f"/admin/info-provider?job={dsh.start_job(ids)}#datenblaetter")


@router.post("/artikel/{item_id}/datenblaetter")
async def datasheets_item(item_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("attachments", 3))):
    from ..services import datasheets as dsh

    ids = dsh.candidates(db, [item_id], force=True)
    if not ids:
        flash(request, "Keine verlinkten Datenblätter zum Herunterladen.")
        return redirect(f"/artikel/{item_id}#anhaenge")
    res = await dsh.download_many(ids)
    msg = f"{res['ok']} Datenblatt/-blätter offline gespeichert." + (f" {res['failed']} nicht möglich: " + "; ".join(res["messages"][:3]) if res["failed"] else "")
    flash(request, msg, "ok" if res["ok"] or not res["failed"] else "err")
    return redirect(f"/artikel/{item_id}#anhaenge")
