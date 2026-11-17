"""Sammlung (v0.33): Seite /sammlung mit Umschalter „Tonträger | Bücher“, Detail- und Erfassungsseiten, Einstellungen.

Gespeichert wird über die API (/api/v1/collection/…), damit Handy-Seite, Formulare und eigene Skripte denselben Weg nehmen.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import BOOK_BINDINGS, COVER_ROLES, GRADES, MEDIA_MEDIUMS, BookRecord, MediaRecord, StockLot
from ..security import get_setting, require, set_setting
from ..services import audit
from ..services import collection as col
from ..services import collection_market as market
from ..services.locations import TreeCache
from ..services.providers import sammlung as cfg
from ..services.providers.books import BOOK_MARKET_PROVIDERS
from ..services.providers.music import Discogs
from ..web import flash, redirect, render

router = APIRouter()

VIEWS = {"gruppen": "nach Interpret/Autor", "kacheln": "Cover-Kacheln", "tabelle": "Tabelle"}


def _int(v) -> int | None:
    return int(v) if str(v or "").strip().isdigit() else None


def _location_options(db: Session, item_ids: list[int], tc: TreeCache) -> list[tuple[int, str]]:
    """Lagerorte, an denen Sammlungsstücke liegen (mit ihren Elternorten) – für den Filter."""
    ids = set()
    if item_ids:
        for lid in db.scalars(select(StockLot.location_id).where(StockLot.item_id.in_(item_ids), StockLot.location_id.is_not(None))):
            while lid is not None and lid not in ids:
                ids.add(lid)
                node = tc.nodes.get(lid)
                lid = node.parent_id if node is not None else None
    return sorted(((i, tc.path(i)) for i in ids), key=lambda t: t[1].lower())


@router.get("/sammlung")
def collection_page(request: Request, art: str = "tontraeger", ansicht: str = "gruppen", q: str = "", medium: str = "", genre: str = "",
                    jahr: str = "", digital: str = "", ort: str = "", reihe: str = "", sprache: str = "", gelesen: str = "", job: str = "",
                    db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    art = "buecher" if art == "buecher" else "tontraeger"
    ansicht = ansicht if ansicht in VIEWS else "gruppen"
    tc = TreeCache(db)
    f = col.Filters(q=q.strip(), medium=medium, genre=genre, year=_int(jahr), digitized=digital, series=reihe, language=sprache,
                    read=gelesen, location_id=_int(ort))
    tot = col.totals(db)
    c = {"art": art, "ansicht": ansicht, "views": VIEWS, "q": q, "filters": {"medium": medium, "genre": genre, "jahr": jahr, "digital": digital,
         "ort": ort, "reihe": reihe, "sprache": sprache, "gelesen": gelesen}, "totals": tot, "tc": tc, "job": job,
         "MEDIA_MEDIUMS": MEDIA_MEDIUMS, "GRADES": GRADES}
    if art == "tontraeger":
        all_rows = col.query_records(db, col.Filters())
        rows = col.query_records(db, f) if any((f.q, f.medium, f.genre, f.year, f.digitized, f.location_id)) else all_rows
        c.update(rows=rows, groups=col.group_records(rows),
                 genres=sorted({g.strip() for r in all_rows for g in (r.genre or "").split(",") if g.strip()}),
                 years=sorted({r.year for r in all_rows if r.year}, reverse=True),
                 locations=_location_options(db, [r.item_id for r in all_rows], tc),
                 shown_value=round(sum(r.market_value or 0 for r in rows), 2))
    else:
        all_rows = col.query_books(db, col.Filters())
        rows = col.query_books(db, f) if any((f.q, f.genre, f.series, f.language, f.read, f.location_id)) else all_rows
        c.update(rows=rows, groups=col.group_books(rows),
                 genres=sorted({g.strip() for b in all_rows for g in (b.genre or "").split(",") if g.strip()}),
                 series=sorted({b.series for b in all_rows if b.series}, key=str.lower),
                 languages=sorted({b.language for b in all_rows if b.language}),
                 locations=_location_options(db, [b.item_id for b in all_rows], tc),
                 shown_value=round(sum(b.market_value or 0 for b in rows), 2))
    c["covers"] = {}  # Front-Bild steht in item.image
    return render(request, "collection.html", c)


# ------------------------------------------------------------------ Tonträger
def _record(db: Session, record_id: int) -> MediaRecord:
    rec = db.get(MediaRecord, record_id)
    if rec is None:
        raise HTTPException(404, "Tonträger nicht gefunden")
    return rec


@router.get("/sammlung/tontraeger/neu")
def record_new(request: Request, barcode: str = "", ort: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    conf = cfg.load(db)
    return render(request, "record_form.html", {"rec": None, "data": {"barcode": barcode, "medium": "LP", "tracks": []}, "preset_location": ort,
                                                "MEDIA_MEDIUMS": MEDIA_MEDIUMS, "GRADES": GRADES, "COVER_ROLES": COVER_ROLES,
                                                "has_discogs_token": bool(conf.get("discogs_token")), "covers": {}})


@router.get("/sammlung/tontraeger/{record_id}")
def record_detail(record_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    rec = _record(db, record_id)
    tc = TreeCache(db)
    sides: dict[str, list] = {}
    for t in rec.tracks:
        sides.setdefault(t.side or "A", []).append(t)
    loc = col.location_of(rec.item)
    from ..services.wled import resolve_targets

    return render(request, "record_detail.html", {"rec": rec, "item": rec.item, "sides": sides, "covers": col.covers_of(db, rec.item_id),
                                                  "loc": loc, "loc_path": tc.path(loc.id) if loc else "", "GRADES": GRADES, "COVER_ROLES": COVER_ROLES,
                                                  "has_lights": bool(loc and resolve_targets(db, loc)),
                                                  "has_token": bool(cfg.load(db).get("discogs_token"))})


@router.get("/sammlung/tontraeger/{record_id}/bearbeiten")
def record_edit(record_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    rec = _record(db, record_id)
    data = col.record_json(db, rec, TreeCache(db))
    data["barcode"] = (data.get("barcodes") or [""])[0]
    loc = col.location_of(rec.item)
    return render(request, "record_form.html", {"rec": rec, "data": data, "preset_location": loc.code if loc else "",
                                                "MEDIA_MEDIUMS": MEDIA_MEDIUMS, "GRADES": GRADES, "COVER_ROLES": COVER_ROLES,
                                                "has_discogs_token": bool(cfg.load(db).get("discogs_token")), "covers": data["covers"]})


@router.post("/sammlung/tontraeger/{record_id}/marktwert")
async def record_market(record_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    rec = _record(db, record_id)
    async with market.new_client() as client:
        res = await market.refresh_record(db, rec, client=client)
    db.commit()
    flash(request, res["message"], "ok" if res["ok"] else "warn")
    for n in res.get("notes") or []:
        flash(request, n, "warn")
    return redirect(f"/sammlung/tontraeger/{rec.id}#markt")


@router.post("/sammlung/tontraeger/marktwerte")
def records_market_all(request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    ids = market.due_records(db, 10_000)
    if not ids:
        flash(request, "Kein Tonträger mit Discogs-ID vorhanden – Marktwerte kommen von Discogs.", "warn")
        return redirect("/sammlung")
    job = market.start_job(ids)
    flash(request, f"Marktwerte für {len(ids)} Tonträger werden im Hintergrund aktualisiert.")
    return redirect(f"/sammlung?job={job}")


# ------------------------------------------------------------------ Bücher
def _book(db: Session, book_id: int) -> BookRecord:
    b = db.get(BookRecord, book_id)
    if b is None:
        raise HTTPException(404, "Buch nicht gefunden")
    return b


@router.get("/sammlung/buecher/neu")
def book_new(request: Request, isbn: str = "", ort: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    return render(request, "book_form.html", {"book": None, "data": {"isbn13": col.normalize_isbn(isbn)[0] or "", "authors": []},
                                              "preset_location": ort, "BOOK_BINDINGS": BOOK_BINDINGS, "covers": {}})


@router.get("/sammlung/buecher/{book_id}")
def book_detail(book_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    b = _book(db, book_id)
    tc = TreeCache(db)
    loc = col.location_of(b.item)
    from ..services.wled import resolve_targets

    same_series = []
    if b.series:
        same_series = sorted(db.scalars(select(BookRecord).where(BookRecord.series == b.series)).unique(), key=col.book_order)
    return render(request, "book_detail.html", {"b": b, "item": b.item, "covers": col.covers_of(db, b.item_id), "loc": loc,
                                                "loc_path": tc.path(loc.id) if loc else "", "same_series": same_series,
                                                "has_lights": bool(loc and resolve_targets(db, loc)),
                                                "market_provider": bool(cfg.load(db).get("book_market"))})


@router.get("/sammlung/buecher/{book_id}/bearbeiten")
def book_edit(book_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    b = _book(db, book_id)
    data = col.book_json(db, b, TreeCache(db))
    loc = col.location_of(b.item)
    return render(request, "book_form.html", {"book": b, "data": data, "preset_location": loc.code if loc else "",
                                              "BOOK_BINDINGS": BOOK_BINDINGS, "covers": data["covers"]})


@router.post("/sammlung/buecher/{book_id}/gelesen")
def book_read(book_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    b = _book(db, book_id)
    b.read = not b.read
    audit.log(db, ctx.user_id, "update", "book_record", b.id, f"Buch {b.title}: {'gelesen' if b.read else 'ungelesen'}", {"read": [not b.read, b.read]})
    db.commit()
    return redirect(f"/sammlung/buecher/{b.id}")


# ------------------------------------------------------------------ Einstellungen
@router.get("/admin/sammlung")
def settings_page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    conf = cfg.load(db)
    return render(request, "admin_collection.html", {
        "conf": conf, "has_token": bool(conf.get("discogs_token")), "has_google": bool(conf.get("google_key")),
        "sources": cfg.BOOK_SOURCES, "market_providers": BOOK_MARKET_PROVIDERS, "last_run": get_setting(db, "collection.last_run"),
        "ratelimit": Discogs.remaining,
    })


@router.post("/admin/sammlung")
async def settings_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 2))):
    f = await request.form()
    order = [k for k in str(f.get("book_order") or "").split(",") if k in cfg.BOOK_SOURCES]
    try:
        budget = max(1, min(int(f.get("market_budget") or 25), 500))
    except ValueError:
        flash(request, "Bitte eine Zahl für „Tonträger pro Nacht“ eingeben.", "err")
        return redirect("/admin/sammlung")
    contact = str(f.get("contact") or "").strip()[:120] or cfg.DEFAULTS["contact"]
    bm = str(f.get("book_market") or "")
    public = {"musicbrainz": bool(f.get("musicbrainz")), "discogs": bool(f.get("discogs")), "contact": contact,
              "book_order": order or cfg.DEFAULTS["book_order"], "book_sources": {k: bool(f.get(f"src.{k}")) for k in cfg.BOOK_SOURCES},
              "nightly_market": bool(f.get("nightly_market")), "market_budget": budget,
              "book_market": bm if bm in BOOK_MARKET_PROVIDERS else ""}
    secret = dict(get_setting(db, "secret.collection", {}) or {})
    for key in cfg.SECRET_FIELDS:
        val = str(f.get(key) or "").strip()
        if val:
            secret[key] = val[:300]
        if f.get(f"{key}.clear"):
            secret.pop(key, None)
    if public["book_market"]:
        mconf = dict(secret.get("book_market_conf") or {})
        for fld, _ in BOOK_MARKET_PROVIDERS[public["book_market"]].fields:
            val = str(f.get(f"bm.{fld}") or "").strip()
            if val:
                mconf[fld] = val[:300]
        secret["book_market_conf"] = mconf
    set_setting(db, "collection", public)
    set_setting(db, "secret.collection", secret)
    audit.log(db, ctx.user_id, "update", "settings", None, "Sammlungs-Einstellungen geändert")
    db.commit()
    flash(request, "Gespeichert.")
    return redirect("/admin/sammlung")


@router.post("/admin/sammlung/test")
async def settings_test(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    """Kurzer Verbindungstest: MusicBrainz, Discogs (mit Token) und die erste Buchquelle."""
    from ..services.providers import ProviderError
    from ..services.providers import books as bookprov
    from ..services.providers.music import MusicBrainz

    conf = cfg.load(db)
    msgs = []
    async with market.new_client() as client:
        try:
            n = len(await MusicBrainz(conf, client).search(artist="Kraftwerk", album="Autobahn", limit=3))
            msgs.append(("ok", f"MusicBrainz: {n} Treffer."))
        except ProviderError as e:
            msgs.append(("err", str(e)))
        if conf.get("discogs_token"):
            try:
                n = len(await Discogs(conf, client).search(artist="Kraftwerk", album="Autobahn", limit=3))
                msgs.append(("ok", f"Discogs: {n} Treffer (Restkontingent {Discogs.remaining if Discogs.remaining is not None else '?'})."))
            except ProviderError as e:
                msgs.append(("err", str(e)))
        else:
            msgs.append(("warn", "Discogs: kein Token – Suche und Preisvorschläge sind aus, niedrigstes Angebot geht trotzdem."))
        try:
            data, notes = await bookprov.lookup_isbn(conf, "9783423214124", client)
            msgs.append(("ok" if data else "warn", f"Bücher: {'gefunden über ' + ', '.join(data['sources']) if data else 'nichts gefunden'}."))
            msgs += [("warn", n) for n in notes]
        except ProviderError as e:
            msgs.append(("err", str(e)))
    for kind, m in msgs:
        flash(request, m, kind)
    return redirect("/admin/sammlung")
