"""REST-API der Sammlung (v0.33): /api/v1/collection/records (Tonträger) und /api/v1/collection/books (Bücher).

Authentifizierung wie die übrige API (Sitzung oder „Authorization: Bearer <token>“). Lesen: Artikel lesen;
Anlegen: Artikel anlegen; Aktualisieren/Marktwert: Artikel bearbeiten.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import BookRecord, Location, MediaRecord
from ..security import check, require
from ..services import collection as col
from ..services import collection_market as market
from ..services import record_scan as rscan
from ..services.codes import resolve
from ..services.locations import TreeCache
from ..services.providers import ProviderError
from ..services.providers import books as bookprov
from ..services.providers import music
from ..services.providers import sammlung as cfg
from .items import resolve_location

router = APIRouter(prefix="/api/v1/collection", tags=["Sammlung"])


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 500)), max(0, offset)


def _location_id(db: Session, value) -> int | None:
    try:
        return resolve_location(db, value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _filter_location(db: Session, value: str) -> int | None:
    return _location_id(db, value) if value else None


async def _covers(uploads: dict[str, UploadFile | None], urls: dict[str, str | None]) -> dict[str, str]:
    """Hochgeladene Dateien haben Vorrang vor Bild-Adressen aus der Online-Suche."""
    from .items import _download_image

    out = {}
    for role, up in uploads.items():
        if up is not None and getattr(up, "filename", ""):
            path = col.store_cover_bytes(await up.read(), up.filename)
            if path:
                out[role] = path
    for role, url in urls.items():
        if url and role not in out:
            path = await asyncio.to_thread(_download_image, url)
            if path:
                out[role] = path
    return out


# ------------------------------------------------------------------ Tonträger
@router.get("/records")
def records(q: str = "", artist: str = "", medium: str = "", genre: str = "", year: int | None = None, digitized: str = "",
            location: str = "", limit: int = 50, offset: int = 0, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    limit, offset = _page(limit, offset)
    rows = col.query_records(db, col.Filters(q=q, artist=artist, medium=medium, genre=genre, year=year, digitized=digitized,
                                             location_id=_filter_location(db, location)))
    tc = TreeCache(db)
    return {"total": len(rows), "limit": limit, "offset": offset, "records": [col.record_json(db, r, tc) for r in rows[offset:offset + limit]]}


@router.get("/records/search")
async def records_search(artist: str = "", album: str = "", barcode: str = "", catno: str = "", format: str = "",
                         db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Online-Suche (MusicBrainz, Discogs) nach Interpret + Album oder Barcode/Katalognummer – speichert nichts."""
    if not any((artist, album, barcode, catno)):
        raise HTTPException(400, "Interpret/Album, Barcode oder Katalognummer angeben.")
    conf = cfg.load(db)
    async with market.new_client() as client:
        hits, notes = await music.search_all(conf, client, artist=artist, album=album, barcode=barcode, catno=catno, fmt=format)
    return {"hits": hits, "notes": notes}


@router.get("/records/release")
async def records_release(source: str, id: str, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Vollständige Daten eines Treffers (inkl. Titelliste und Cover-Adressen) – speichert nichts."""
    conf = cfg.load(db)
    try:
        async with market.new_client() as client:
            return {"hit": await music.release_details(conf, source, id, client)}
    except ProviderError as e:
        raise HTTPException(502, str(e)) from e


class RecordScanIn(BaseModel):
    """Ein Scan der Seite „Platten scannen“. Suche: code (Barcode oder Kat.-Nr.), catno, artist/album.
    Auswahl übernehmen: mbid und/oder discogs_id. scanned = ursprünglich gescannter Barcode (wird mitgespeichert,
    auch wenn danach über die Katalognummer gesucht wurde). Dazu die Serien-Einstellungen."""
    code: str = ""
    scanned: str = ""
    catno: str = ""
    artist: str = ""
    album: str = ""
    mbid: str = ""
    discogs_id: str = ""
    location: str = ""
    medium: str = ""
    media_condition: str = ""
    sleeve_condition: str = ""
    auto_save: bool = True


def _scan_dup(db: Session, rec: MediaRecord, tc: TreeCache, hint: str) -> dict:
    return {"status": "duplicate", "record": rscan.record_summary(db, rec, tc), "message": hint, "hits": [], "notes": []}


async def _scan_save(db: Session, ctx, body: RecordScanIn, client, tc: TreeCache) -> dict:
    """Release-Daten holen, auf Dublette prüfen, mit den Serien-Einstellungen anlegen."""
    check(ctx, "items", 3)
    scanned = list(dict.fromkeys(rscan.clean_code(c) for c in (body.code, body.scanned) if rscan.is_barcode(c)))
    try:
        details, notes = await rscan.release(cfg.load(db), client, mbid=body.mbid.strip(), discogs_id=body.discogs_id.strip())
    except ProviderError as e:
        raise HTTPException(502, str(e)) from e
    dup = col.find_record_duplicate(db, {**details, "barcode": [v for b in [*scanned, details.get("barcode") or ""]
                                                                for v in rscan.storage_codes(b)]})
    if dup is not None:
        return {**_scan_dup(db, dup, tc, "Diese Platte ist schon in der Sammlung."), "notes": notes}
    loc_id = _location_id(db, body.location)
    data = rscan.record_data(details, rscan.series_settings(body.model_dump()), scanned)
    covers = await _covers({}, {"front": details.get("cover_front"), "back": details.get("cover_back")})
    rec, created = col.upsert_record(db, data, ctx.user_id, loc_id, covers)
    db.commit()
    return {"status": "saved", "created": created, "record": rscan.record_summary(db, rec, tc), "hits": [], "notes": notes}


@router.post("/records/scan")
async def records_scan(body: RecordScanIn, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Platten scannen (Serien-Erfassung). Antwort „status“:
    saved (angelegt) · duplicate (schon vorhanden, mit Link) · choose (Treffer zur Auswahl) · none (nichts gefunden)
    · location (Lagerort-Etikett gescannt: gilt ab jetzt für die Serie)."""
    tc = TreeCache(db)
    _location_id(db, body.location)  # falschen Lagerort sofort melden, nicht erst nach der Online-Suche
    async with market.new_client() as client:
        if body.mbid or body.discogs_id:
            return await _scan_save(db, ctx, body, client, tc)
        raw = rscan.strip_ctrl(body.code)
        code = rscan.clean_code(raw)  # fast nur Ziffern -> nur die Ziffern
        barcode = code if rscan.is_barcode(code) else ""
        scanned = rscan.clean_code(body.scanned) if rscan.is_barcode(body.scanned) else ""
        if raw and (not barcode or re.search(r"[A-Za-z]", raw)):  # „L-00012“ ist ein Lagerort, kein Barcode
            r = resolve(db, raw)
            if r.kind == "location":  # Lagerort-Etikett gescannt -> Lagerort der Serie
                loc = db.get(Location, r.id)
                return {"status": "location", "location": loc.code, "path": tc.path(loc.id), "hits": [], "notes": []}
        catno = rscan.strip_ctrl(body.catno) or ("" if barcode else raw)
        if not (barcode or catno or body.artist.strip() or body.album.strip()):
            raise HTTPException(400, "Barcode, Katalognummer oder Interpret/Album angeben.")
        if barcode or scanned:
            dup = rscan.find_existing(db, [v for b in (barcode, scanned) for v in rscan.storage_codes(b)])
            if dup is not None:
                return _scan_dup(db, dup, tc, "Diese Platte ist schon in der Sammlung.")
        hits, notes = await rscan.search(cfg.load(db), client, barcode=barcode, catno=catno, artist=body.artist, album=body.album)
        query = {"barcode": barcode, "digits": len(barcode), "catno": catno,
                 "variants": rscan.search_variants(barcode) or rscan.catno_variants(catno)}
        if not hits:
            conf = cfg.load(db)
            msg = rscan.none_message(conf, barcode, "" if barcode else catno)
            if "Token" in msg:  # Token-Hinweis steht schon in der Meldung
                notes = [n for n in notes if "kein Token" not in n]
            return {"status": "none", "hits": [], "notes": notes, "query": query, "message": msg}
        if len(hits) == 1 and body.auto_save:
            h = hits[0]
            pick = body.model_copy(update={"mbid": h.get("musicbrainz_id") or "", "discogs_id": h.get("discogs_id") or "", "code": barcode,
                                           "scanned": scanned})
            res = await _scan_save(db, ctx, pick, client, tc)
            return {**res, "notes": list(dict.fromkeys(notes + res["notes"])), "query": query}
        return {"status": "choose", "hits": hits, "notes": notes, "query": query}


@router.post("/records/market")
def records_market_all(db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    """Marktwert für die ganze Sammlung im Hintergrund aktualisieren (ältester Stand zuerst)."""
    ids = market.due_records(db, 10_000)
    if not ids:
        return {"ok": False, "job": None, "message": "Kein Tonträger mit Discogs-ID vorhanden."}
    return {"ok": True, "job": market.start_job(ids), "total": len(ids)}


@router.get("/records/market/{job_id}")
def records_market_job(job_id: str, ctx=Depends(require("items", 1))):
    st = market.JOBS.get(job_id)
    if st is None:
        raise HTTPException(404, "Auftrag nicht gefunden")
    return st


@router.get("/records/{record_id}")
def record(record_id: int, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    rec = db.get(MediaRecord, record_id)
    if rec is None:
        raise HTTPException(404, "Tonträger nicht gefunden")
    return col.record_json(db, rec, TreeCache(db))


@router.post("/records")
async def record_save(
    id: int | None = Form(None), artist: str | None = Form(None), album: str | None = Form(None), year: str | None = Form(None),
    genre: str | None = Form(None), label: str | None = Form(None), catalog_no: str | None = Form(None), medium: str | None = Form(None),
    country: str | None = Form(None), musicbrainz_id: str | None = Form(None), discogs_id: str | None = Form(None),
    media_condition: str | None = Form(None), sleeve_condition: str | None = Form(None), digitized: str | None = Form(None),
    digitized_path: str | None = Form(None), barcode: str | None = Form(None), location: str | None = Form(None),
    category_id: int | None = Form(None), tracks: str | None = Form(None),
    cover_front: UploadFile | None = File(None), cover_back: UploadFile | None = File(None),
    cover_inlay: UploadFile | None = File(None), cover_label: UploadFile | None = File(None),
    cover_front_url: str | None = Form(None), cover_back_url: str | None = Form(None),
    db: Session = Depends(get_db), ctx=Depends(require("items", 2)),
):
    """Tonträger anlegen oder – bei Dublette (oder id) – aktualisieren. Dublette: gleiche MusicBrainz-ID, Discogs-ID oder
    Barcode (12/13 Stellen); Interpret + Album + Jahr nur, wenn die vorhandene Platte keine dieser IDs hat."""
    data = {k: v for k, v in dict(artist=artist, album=album, year=year, genre=genre, label=label, catalog_no=catalog_no, medium=medium,
                                   country=country, musicbrainz_id=musicbrainz_id, discogs_id=discogs_id, media_condition=media_condition,
                                   sleeve_condition=sleeve_condition, digitized=digitized, digitized_path=digitized_path, barcode=barcode,
                                   tracks=tracks).items() if v is not None}
    if id is None and col.find_record_duplicate(db, data) is None:
        check(ctx, "items", 3)
    try:
        col.parse_tracks(data.get("tracks"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    loc_id = _location_id(db, location)
    covers = await _covers({"front": cover_front, "back": cover_back, "inlay": cover_inlay, "label": cover_label},
                           {"front": cover_front_url, "back": cover_back_url})
    try:
        rec, created = col.upsert_record(db, data, ctx.user_id, loc_id, covers, record_id=id, category_id=category_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    db.commit()
    return {"ok": True, "created": created, "record": col.record_json(db, rec, TreeCache(db))}


@router.post("/records/{record_id}/market")
async def record_market(record_id: int, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    rec = db.get(MediaRecord, record_id)
    if rec is None:
        raise HTTPException(404, "Tonträger nicht gefunden")
    async with market.new_client() as client:
        res = await market.refresh_record(db, rec, client=client)
    db.commit()
    return {**res, "record": col.record_json(db, rec, TreeCache(db))}


# ------------------------------------------------------------------ Bücher
@router.get("/books")
def books(q: str = "", author: str = "", genre: str = "", series: str = "", language: str = "", read: str = "", location: str = "",
          limit: int = 50, offset: int = 0, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    limit, offset = _page(limit, offset)
    rows = col.query_books(db, col.Filters(q=q, author=author, genre=genre, series=series, language=language, read=read,
                                           location_id=_filter_location(db, location)))
    tc = TreeCache(db)
    return {"total": len(rows), "limit": limit, "offset": offset, "books": [col.book_json(db, b, tc) for b in rows[offset:offset + limit]]}


@router.post("/books/lookup")
async def books_lookup(isbn: str, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Vorschlagsdaten zur ISBN (Quellen in eingestellter Reihenfolge, fehlende Felder ergänzt) – speichert nichts."""
    conf = cfg.load(db)
    i13, _ = col.normalize_isbn(isbn)
    if not i13:
        raise HTTPException(400, f"„{isbn}“ ist keine gültige ISBN.")
    async with market.new_client() as client:
        data, notes = await bookprov.lookup_isbn(conf, i13, client)
    existing = col.find_book_duplicate(db, i13)
    return {"found": data is not None, "book": data, "notes": notes,
            "existing": col.book_json(db, existing, TreeCache(db)) if existing else None}


@router.get("/books/search")
async def books_search(title: str = "", author: str = "", db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Suche nach Titel + Autor (z. B. ältere Ausgaben ohne ISBN) – speichert nichts."""
    if not title.strip() and not author.strip():
        raise HTTPException(400, "Titel oder Autor angeben.")
    async with market.new_client() as client:
        hits, notes = await bookprov.search(cfg.load(db), title.strip(), author.strip(), client)
    return {"hits": hits, "notes": notes}


@router.get("/books/{book_id}")
def book(book_id: int, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    b = db.get(BookRecord, book_id)
    if b is None:
        raise HTTPException(404, "Buch nicht gefunden")
    return col.book_json(db, b, TreeCache(db))


@router.post("/books")
async def book_save(
    id: int | None = Form(None), title: str | None = Form(None), subtitle: str | None = Form(None), authors: str | None = Form(None),
    publisher: str | None = Form(None), year: str | None = Form(None), edition: str | None = Form(None), isbn13: str | None = Form(None),
    isbn10: str | None = Form(None), isbn: str | None = Form(None), language: str | None = Form(None), pages: str | None = Form(None),
    series: str | None = Form(None), series_number: str | None = Form(None), genre: str | None = Form(None), binding: str | None = Form(None),
    condition: str | None = Form(None), signed: str | None = Form(None), read: str | None = Form(None),
    market_value: str | None = Form(None), market_currency: str | None = Form(None), location: str | None = Form(None),
    category_id: int | None = Form(None), cover_front: UploadFile | None = File(None), cover_back: UploadFile | None = File(None),
    cover_front_url: str | None = Form(None), cover_back_url: str | None = Form(None),
    db: Session = Depends(get_db), ctx=Depends(require("items", 2)),
):
    """Buch anlegen oder – bei gleicher ISBN-13 (oder id) – aktualisieren."""
    data = {k: v for k, v in dict(title=title, subtitle=subtitle, authors=authors, publisher=publisher, year=year, edition=edition,
                                   isbn13=isbn13, isbn10=isbn10, isbn=isbn, language=language, pages=pages, series=series,
                                   series_number=series_number, genre=genre, binding=binding, condition=condition, signed=signed, read=read,
                                   market_value=market_value, market_currency=market_currency).items() if v is not None}
    i13, _ = col.normalize_isbn(isbn13 or isbn or isbn10)
    if (isbn13 or isbn or isbn10) and not i13:
        raise HTTPException(400, f"„{isbn13 or isbn or isbn10}“ ist keine gültige ISBN.")
    if id is None and col.find_book_duplicate(db, i13) is None:
        check(ctx, "items", 3)
    loc_id = _location_id(db, location)
    covers = await _covers({"front": cover_front, "back": cover_back}, {"front": cover_front_url, "back": cover_back_url})
    try:
        b, created = col.upsert_book(db, data, ctx.user_id, loc_id, covers, book_id=id, category_id=category_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    db.commit()
    return {"ok": True, "created": created, "book": col.book_json(db, b, TreeCache(db))}


@router.post("/books/{book_id}/market")
async def book_market(book_id: int, db: Session = Depends(get_db), ctx=Depends(require("items", 2))):
    b = db.get(BookRecord, book_id)
    if b is None:
        raise HTTPException(404, "Buch nicht gefunden")
    async with market.new_client() as client:
        res = await market.refresh_book(db, b, client=client)
    db.commit()
    return {**res, "book": col.book_json(db, b, TreeCache(db))}
