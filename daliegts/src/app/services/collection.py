"""Sammlung (v0.33): Tonträger und Bücher als Artikel mit Zusatzdaten.

Jeder Tonträger/jedes Buch ist ein normaler Artikel (Item, kind „tontraeger“ bzw. „buch“) mit genau einem
Bestand am Lagerort; die Fachdaten liegen 1:1 in media_records/book_records. Hüllen- und Coverbilder sind
Anhänge (owner_type „item“, kind „cover“, title = Rolle front/back/inlay/label); das Front-Bild ist zugleich
Item.image. Barcode bzw. ISBN liegen zusätzlich als Artikel-Code, damit der normale Scanner sie findet.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (BOOK_BINDINGS, COVER_ROLES, GRADES, MEDIA_MEDIUMS, Attachment, BookRecord, Category, Item, ItemCode, MediaRecord,
                      MediaTrack, StockLot)
from . import audit
from . import stock as stocksvc
from .codes import add_item_code, normalize
from .items import create_item, ensure_category_path, image_url, reindex, store_upload

ARTICLES = ("the ", "die ", "der ", "das ")
CATEGORY_RECORDS = "Sammlung/Tonträger"
CATEGORY_BOOKS = "Sammlung/Bücher"


# ------------------------------------------------------------------ Sortierung
def artist_sort(name: str) -> str:
    """„The Beatles“ -> „Beatles“, „Die Ärzte“ -> „Ärzte“ (Sortierschlüssel ohne führenden Artikel)."""
    s = (name or "").strip()
    low = s.lower()
    for a in ARTICLES:
        if low.startswith(a) and len(s) > len(a):
            return s[len(a):].strip()
    return s


def split_authors(authors: str | list | None) -> list[str]:
    if isinstance(authors, list):
        parts = authors
    else:
        parts = re.split(r"\s*;\s*|\s+&\s+|\s+und\s+", authors or "")
    return [p.strip() for p in parts if p and p.strip()]


def person_sort(name: str) -> str:
    """„Johann Wolfgang von Goethe“ -> „Goethe, Johann Wolfgang von“; „Goethe, J. W.“ bleibt."""
    n = (name or "").strip()
    if not n or "," in n:
        return n
    words = n.split()
    if len(words) == 1:
        return n
    last = words.pop()  # Namenszusätze („von“) bleiben beim Vornamen – wie in Bibliothekskatalogen
    return f"{last}, {' '.join(words)}"


def author_sort(authors: str | list | None) -> str:
    names = split_authors(authors)
    return person_sort(names[0]) if names else ""


def sort_letter(key: str) -> str:
    """A–Z-Leiste: Umlaute zum Grundbuchstaben, Ziffern/Sonderzeichen unter „#“."""
    c = (key or "#").strip()[:1].upper()
    c = {"Ä": "A", "Ö": "O", "Ü": "U"}.get(c, c)
    return c if "A" <= c <= "Z" else "#"


def sort_key(s: str) -> str:
    s = (s or "").lower()
    return s.translate(str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"}))


def series_no_key(v: str) -> tuple:
    m = re.match(r"\s*(\d+)(?:[.,](\d+))?", v or "")
    return (int(m.group(1)), int(m.group(2) or 0), v) if m else (10**6, 0, v or "")


# ------------------------------------------------------------------ ISBN
def isbn_digits(raw: str | None) -> str:
    return re.sub(r"[^0-9X]", "", (raw or "").upper())


def isbn13_valid(s: str) -> bool:
    if not re.fullmatch(r"\d{13}", s or ""):
        return False
    return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s)) % 10 == 0


def isbn10_valid(s: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", s or ""):
        return False
    return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s)) % 11 == 0


def isbn10_to_13(s: str) -> str:
    core = "978" + s[:9]
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def isbn13_to_10(s: str) -> str | None:
    if not s.startswith("978"):
        return None
    core = s[3:12]
    check = (11 - sum((10 - i) * int(c) for i, c in enumerate(core)) % 11) % 11
    return core + ("X" if check == 10 else str(check))


def normalize_isbn(raw: str | None) -> tuple[str | None, str | None]:
    """Beliebige Schreibweise -> (ISBN-13, ISBN-10). Ungültig -> (None, None)."""
    d = isbn_digits(raw)
    if len(d) == 10 and isbn10_valid(d):
        return isbn10_to_13(d), d
    if len(d) == 13 and isbn13_valid(d):
        return d, isbn13_to_10(d)
    return None, None


def looks_like_isbn(raw: str | None) -> bool:
    """Buchland-EAN (978/979) oder gültige ISBN-10."""
    d = isbn_digits(raw)
    return normalize_isbn(d)[0] is not None and (len(d) == 10 or d[:3] in ("978", "979"))


# ------------------------------------------------------------------ Eingaben säubern
def _str(v, n: int = 255) -> str:
    return str(v if v is not None else "").strip()[:n]


def _int(v) -> int | None:
    if v in (None, ""):
        return None
    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def _float(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "on", "ja", "yes", "y", "x")


def _grade(v) -> str:
    v = _str(v, 4).upper()
    return v if v in GRADES else ""


def parse_tracks(raw) -> list[dict]:
    """JSON-Liste oder Liste von dicts -> bereinigte Titel."""
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise ValueError("tracks ist kein gültiges JSON.") from e
    if not isinstance(raw, list):
        raise ValueError("tracks muss eine Liste sein.")
    out = []
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict) or not _str(t.get("title")):
            continue
        out.append({"side": _str(t.get("side") or "A", 4).upper() or "A", "position": _int(t.get("position")) or i,
                    "title": _str(t.get("title")), "artist": _str(t.get("artist")), "length_seconds": _int(t.get("length_seconds"))})
    return out


# ------------------------------------------------------------------ Bilder
def covers_of(db: Session, item_id: int) -> dict[str, str]:
    out = {}
    for a in db.scalars(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == item_id,
                                                 Attachment.kind == "cover").order_by(Attachment.id)):
        if a.title in COVER_ROLES and a.filename:
            out[a.title] = image_url(a.filename)
    return out


def set_cover(db: Session, item: Item, role: str, path: str) -> None:
    """Bild (Pfad unter media/) als Hüllen-/Coverbild mit Rolle speichern; Vorderseite = Artikelbild."""
    if role not in COVER_ROLES or not path:
        return
    old = db.scalar(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == item.id,
                                             Attachment.kind == "cover", Attachment.title == role))
    if old is None:
        db.add(Attachment(owner_type="item", owner_id=item.id, kind="cover", title=role, filename=path))
    else:
        if old.filename and old.filename != path:  # altes Bild nach dem Commit wegräumen (cleanup_replaced)
            db.info.setdefault("cover_replaced", set()).add(old.filename)
        old.filename = path
    if role == "front":
        item.image = path


def store_cover_bytes(data: bytes, filename: str) -> str | None:
    if not data:
        return None
    path = store_upload(data, filename or "cover.jpg")
    return path if path.endswith(".webp") or path.rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "gif") else None


# ------------------------------------------------------------------ Lagerort
def place(db: Session, item: Item, location_id: int | None, user_id: int | None) -> None:
    """Ein Exemplar am Lagerort: anlegen oder umlagern (nie doppelt)."""
    lots = [lot for lot in item.lots if lot.status not in ("lost", "used")]
    if not lots:
        stocksvc.add_stock(db, item, location_id, 1, user_id, note="Sammlung")
    elif location_id is not None and lots[0].location_id != location_id:
        stocksvc.move_stock(db, lots[0], location_id, None, user_id, note="Sammlung")


def location_of(item: Item):
    lot = next((lot for lot in item.lots if lot.status not in ("lost", "used")), None)
    return lot.location if lot else None


# ------------------------------------------------------------------ Tonträger
RECORD_FIELDS = ("artist", "album", "year", "genre", "label", "catalog_no", "medium", "country", "musicbrainz_id", "discogs_id",
                 "media_condition", "sleeve_condition", "digitized", "digitized_path")


def barcode_variants(raw: str | None) -> list[str]:
    """UPC-A (12 Stellen) und EAN-13 mit führender 0 sind derselbe Code – beide Schreibweisen.

    „075678164224“ -> [„075678164224“, „0075678164224“]; „0075678164224“ -> [„0075678164224“, „075678164224“].
    """
    d = re.sub(r"[^0-9]", "", raw or "")
    if not d:
        return []
    out = [d]
    if len(d) == 12:
        out.append("0" + d)
    elif len(d) == 13 and d.startswith("0"):
        out.append(d[1:])
    return out


def _barcode_list(v) -> list[str]:
    return [str(b) for b in (v if isinstance(v, (list, tuple)) else [v]) if b]


def find_record_by_ids(db: Session, barcodes: list[str] | tuple = (), mbid: str = "", discogs_id: str = "") -> MediaRecord | None:
    """Gleiche Platte über MusicBrainz-ID, Discogs-ID oder Barcode (alle Schreibweisen)."""
    if mbid:
        r = db.scalar(select(MediaRecord).where(MediaRecord.musicbrainz_id == mbid).limit(1))
        if r:
            return r
    if discogs_id:
        r = db.scalar(select(MediaRecord).where(MediaRecord.discogs_id == str(discogs_id)).limit(1))
        if r:
            return r
    codes = {normalize(v) for b in barcodes for v in barcode_variants(b)}
    if codes:
        r = db.scalar(select(MediaRecord).join(ItemCode, ItemCode.item_id == MediaRecord.item_id)
                      .join(Item, Item.id == MediaRecord.item_id)
                      .where(ItemCode.code.in_(codes), Item.kind == "tontraeger").limit(1))
        if r:
            return r
    return None


def find_handmade_twin(db: Session, data: dict) -> MediaRecord | None:
    """Platte ohne MusicBrainz- und Discogs-ID (von Hand erfasst) mit gleichem Interpret, Album und Jahr.

    Hat die vorhandene Platte eine ID, ist eine Platte mit gleichem Titel eine andere Pressung (= neue Platte).
    """
    artist, album = _str(data.get("artist")).lower(), _str(data.get("album")).lower()
    if not artist or not album:
        return None
    q = select(MediaRecord).where(func.lower(MediaRecord.artist) == artist, func.lower(MediaRecord.album) == album,
                                  func.coalesce(MediaRecord.musicbrainz_id, "") == "", func.coalesce(MediaRecord.discogs_id, "") == "")
    year = _int(data.get("year"))
    q = q.where(MediaRecord.year == year) if year is not None else q.where(MediaRecord.year.is_(None))
    return db.scalar(q.limit(1))


def find_record_duplicate(db: Session, data: dict) -> MediaRecord | None:
    """Dubletten-Regel für alle Wege (Formular, REST-API, Platten-Scan, Vinyl2MP3):
    gleiche MusicBrainz-ID, Discogs-ID oder Barcode = dieselbe Platte; Interpret + Album + Jahr nur, wenn die
    vorhandene Platte weder MusicBrainz- noch Discogs-ID hat. Andere Pressung = neue Platte."""
    return (find_record_by_ids(db, _barcode_list(data.get("barcode")), _str(data.get("musicbrainz_id"), 36), _str(data.get("discogs_id"), 20))
            or find_handmade_twin(db, data))


def detail_url(db: Session, item: Item | None) -> str | None:
    """Detailseite der Sammlung für einen Tonträger/ein Buch, sonst None (Scanner, Code-Sprung)."""
    if item is None or item.kind not in ("tontraeger", "buch"):
        return None
    if item.kind == "tontraeger":
        rec = record_of_item(db, item.id)
        return f"/sammlung/tontraeger/{rec.id}" if rec else None
    book = book_of_item(db, item.id)
    return f"/sammlung/buecher/{book.id}" if book else None


def record_name(rec: MediaRecord) -> str:
    return " – ".join(p for p in (rec.artist, rec.album) if p) or "Tonträger"


def upsert_record(db: Session, data: dict, user_id: int | None, location_id: int | None = None, covers: dict | None = None,
                  record_id: int | None = None, category_id: int | None = None) -> tuple[MediaRecord, bool]:
    """Tonträger anlegen oder – Dublette nach find_record_duplicate bzw. record_id – aktualisieren.

    data: Feldwerte als Strings/Zahlen (nur übergebene Felder werden geändert), optional „tracks“ (Liste/JSON)
    und „barcode“ (Text oder Liste von Schreibweisen). covers: {Rolle: Pfad unter media/}.
    """
    rec = db.get(MediaRecord, record_id) if record_id else find_record_duplicate(db, data)
    if record_id and rec is None:
        raise LookupError("Tonträger nicht gefunden")
    created = rec is None
    if created:
        if not _str(data.get("artist")) and not _str(data.get("album")):
            raise ValueError("Interpret oder Album angeben.")
        cat = db.get(Category, category_id) if category_id else ensure_category_path(db, CATEGORY_RECORDS)
        item = create_item(db, "Tonträger", user_id, kind="tontraeger", category_id=cat.id if cat else None, unit="Stk")
        rec = MediaRecord(item_id=item.id)
        db.add(rec)
        before = {}
    else:
        item = rec.item
        before = audit.snapshot(rec)
        if category_id:
            item.category_id = category_id
    for f in RECORD_FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f == "year":
            rec.year = _int(v)
        elif f in ("media_condition", "sleeve_condition"):
            setattr(rec, f, _grade(v))
        elif f == "medium":
            m = _str(v, 20)
            rec.medium = next((k for k in MEDIA_MEDIUMS if k.lower() == m.lower()), "Sonstiges" if m else "LP")
        elif f == "digitized":
            rec.digitized = _bool(v)
        elif f in ("musicbrainz_id", "discogs_id"):
            setattr(rec, f, _str(v, 36 if f == "musicbrainz_id" else 20) or None)
        else:
            setattr(rec, f, _str(v, 500 if f == "digitized_path" else 255))
    rec.artist_sort = artist_sort(rec.artist)
    if "tracks" in data:
        rec.tracks.clear()
        db.flush()
        for t in parse_tracks(data["tracks"]):
            rec.tracks.append(MediaTrack(**t))
    item.name = record_name(rec)[:255]
    item.description = " · ".join(str(p) for p in (rec.medium, rec.year, rec.label, rec.catalog_no) if p)
    item.tags = ", ".join(p for p in ("Sammlung", "Tonträger", rec.genre) if p)
    bcs = data.get("barcode")
    for bc in _barcode_list(bcs):
        if bc:
            add_item_code(db, item.id, str(bc), "barcode", "sammlung")
    for role, path in (covers or {}).items():
        set_cover(db, item, role, path)
    db.flush()
    db.refresh(item)
    place(db, item, location_id, user_id)
    if rec.market_value is not None:
        item.collector_value = rec.market_value
    db.flush()
    reindex(db, item)
    if created:
        audit.log(db, user_id, "create", "media_record", rec.id, f"Tonträger {item.name}")
    else:
        ch = audit.diff(before, audit.snapshot(rec))
        audit.log(db, user_id, "update", "media_record", rec.id, f"Tonträger {item.name} geändert", ch)
    return rec, created


def apply_market(rec: MediaRecord, lowest: float | None, suggested: float | None, currency: str, for_sale: int | None,
                 error: str = "") -> None:
    rec.market_lowest, rec.market_suggested = lowest, suggested
    rec.market_currency = (currency or "EUR")[:3]
    rec.market_for_sale = for_sale
    rec.market_checked_at = datetime.now().replace(microsecond=0)
    rec.market_error = (error or "")[:300]
    rec.item.collector_value = rec.market_value


def record_json(db: Session, rec: MediaRecord, tc=None) -> dict:
    loc = location_of(rec.item)
    return {
        "id": rec.id, "item_id": rec.item_id, "code": rec.item.code, "artist": rec.artist, "artist_sort": rec.artist_sort,
        "album": rec.album, "year": rec.year, "genre": rec.genre, "label": rec.label, "catalog_no": rec.catalog_no, "medium": rec.medium,
        "country": rec.country, "musicbrainz_id": rec.musicbrainz_id, "discogs_id": rec.discogs_id,
        "media_condition": rec.media_condition, "sleeve_condition": rec.sleeve_condition, "digitized": rec.digitized,
        "digitized_path": rec.digitized_path,
        "barcodes": [c.code for c in rec.item.codes],
        "market": {"lowest": rec.market_lowest, "suggested": rec.market_suggested, "currency": rec.market_currency,
                   "for_sale": rec.market_for_sale, "checked_at": rec.market_checked_at.isoformat() if rec.market_checked_at else None,
                   "value": rec.market_value, "error": rec.market_error or None},
        "tracks": [{"side": t.side, "position": t.position, "title": t.title, "artist": t.artist, "length_seconds": t.length_seconds}
                   for t in rec.tracks],
        "covers": covers_of(db, rec.item_id),
        "location_id": loc.id if loc else None, "location": (tc.path(loc.id) if tc else loc.name) if loc else None,
        "url": f"/sammlung/tontraeger/{rec.id}",
    }


# ------------------------------------------------------------------ Bücher
BOOK_FIELDS = ("title", "subtitle", "authors", "publisher", "year", "edition", "language", "pages", "series", "series_number", "genre",
               "binding", "condition", "signed", "read", "market_value", "market_currency")


def find_book_duplicate(db: Session, isbn13: str | None) -> BookRecord | None:
    return db.scalar(select(BookRecord).where(BookRecord.isbn13 == isbn13).limit(1)) if isbn13 else None


def upsert_book(db: Session, data: dict, user_id: int | None, location_id: int | None = None, covers: dict | None = None,
                book_id: int | None = None, category_id: int | None = None) -> tuple[BookRecord, bool]:
    """Buch anlegen oder aktualisieren (gleiche ISBN-13 bzw. book_id). Nur übergebene Felder werden geändert."""
    isbn_raw = data.get("isbn13") or data.get("isbn") or data.get("isbn10")
    isbn13, isbn10 = normalize_isbn(isbn_raw)
    if isbn_raw and not isbn13:
        raise ValueError(f"„{isbn_raw}“ ist keine gültige ISBN.")
    book = db.get(BookRecord, book_id) if book_id else find_book_duplicate(db, isbn13)
    if book_id and book is None:
        raise LookupError("Buch nicht gefunden")
    created = book is None
    if created:
        if not _str(data.get("title")) and not isbn13:
            raise ValueError("Titel oder ISBN angeben.")
        cat = db.get(Category, category_id) if category_id else ensure_category_path(db, CATEGORY_BOOKS)
        item = create_item(db, "Buch", user_id, kind="buch", category_id=cat.id if cat else None, unit="Stk")
        book = BookRecord(item_id=item.id)
        db.add(book)
        db.flush()  # Spalten-Defaults ("") setzen, bevor unten damit gerechnet wird
        before = {}
    else:
        item = book.item
        before = audit.snapshot(book)
        if category_id:
            item.category_id = category_id
    for f in BOOK_FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f in ("year", "pages"):
            setattr(book, f, _int(v))
        elif f in ("signed", "read"):
            setattr(book, f, _bool(v))
        elif f == "market_value":
            book.market_value = _float(v)
            book.market_checked_at = datetime.now().replace(microsecond=0) if book.market_value is not None else None
        elif f == "market_currency":
            book.market_currency = (_str(v, 3) or "EUR").upper()
        elif f == "binding":
            b = _str(v, 20)
            book.binding = next((k for k in BOOK_BINDINGS if k.lower() == b.lower()), "Sonstiges" if b else "")
        elif f == "authors":
            book.authors = "; ".join(split_authors(v))[:500]
        else:
            setattr(book, f, _str(v, 300))
    if isbn13:
        book.isbn13, book.isbn10 = isbn13, isbn10
    book.author_sort = author_sort(book.authors)
    item.name = (book.title or (f"ISBN {book.isbn13}" if book.isbn13 else "Buch"))[:255]
    item.description = " · ".join(p for p in (book.authors.replace("; ", ", "), book.publisher, str(book.year or "")) if p)
    item.tags = ", ".join(p for p in ("Sammlung", "Buch", book.genre, book.series) if p)
    item.condition = book.condition[:40]
    item.collector_value = book.market_value
    if book.isbn13:
        add_item_code(db, item.id, book.isbn13, "barcode", "isbn")
    for role, path in (covers or {}).items():
        set_cover(db, item, role, path)
    db.flush()
    db.refresh(item)
    place(db, item, location_id, user_id)
    db.flush()
    reindex(db, item)
    if created:
        audit.log(db, user_id, "create", "book_record", book.id, f"Buch {item.name}")
    else:
        audit.log(db, user_id, "update", "book_record", book.id, f"Buch {item.name} geändert", audit.diff(before, audit.snapshot(book)))
    return book, created


def book_json(db: Session, b: BookRecord, tc=None) -> dict:
    loc = location_of(b.item)
    return {
        "id": b.id, "item_id": b.item_id, "code": b.item.code, "title": b.title, "subtitle": b.subtitle, "authors": split_authors(b.authors),
        "author_sort": b.author_sort, "publisher": b.publisher, "year": b.year, "edition": b.edition, "isbn13": b.isbn13, "isbn10": b.isbn10,
        "language": b.language, "pages": b.pages, "series": b.series, "series_number": b.series_number, "genre": b.genre,
        "binding": b.binding, "condition": b.condition, "signed": b.signed, "read": b.read,
        "market": {"value": b.market_value, "currency": b.market_currency,
                   "checked_at": b.market_checked_at.isoformat() if b.market_checked_at else None},
        "covers": covers_of(db, b.item_id),
        "location_id": loc.id if loc else None, "location": (tc.path(loc.id) if tc else loc.name) if loc else None,
        "url": f"/sammlung/buecher/{b.id}",
    }


# ------------------------------------------------------------------ Listen & Summen
@dataclass
class Filters:
    q: str = ""
    artist: str = ""
    author: str = ""
    medium: str = ""
    genre: str = ""
    year: int | None = None
    digitized: str = ""  # "", "ja", "nein"
    series: str = ""
    language: str = ""
    read: str = ""  # "", "ja", "nein"
    location_id: int | None = None


def _in_location(db: Session, location_id: int | None):
    if not location_id:
        return None
    from .locations import TreeCache

    ids = [location_id, *TreeCache(db).descendants(location_id)]
    return select(StockLot.item_id).where(StockLot.location_id.in_(ids))


def _like(col, text: str):
    return func.lower(col).like(f"%{text.lower()}%")


def query_records(db: Session, f: Filters) -> list[MediaRecord]:
    q = select(MediaRecord)
    if f.q:
        for w in f.q.split():
            sub = select(ItemCode.item_id).where(ItemCode.code == normalize(w))
            q = q.where(_like(MediaRecord.artist, w) | _like(MediaRecord.album, w) | _like(MediaRecord.label, w)
                        | _like(MediaRecord.catalog_no, w) | MediaRecord.item_id.in_(sub)
                        | MediaRecord.id.in_(select(MediaTrack.record_id).where(_like(MediaTrack.title, w))))
    if f.artist:
        q = q.where(_like(MediaRecord.artist, f.artist))
    if f.medium:
        q = q.where(MediaRecord.medium == f.medium)
    if f.genre:
        q = q.where(_like(MediaRecord.genre, f.genre))
    if f.year:
        q = q.where(MediaRecord.year == f.year)
    if f.digitized in ("ja", "nein"):
        q = q.where(MediaRecord.digitized.is_(f.digitized == "ja"))
    sub = _in_location(db, f.location_id)
    if sub is not None:
        q = q.where(MediaRecord.item_id.in_(sub))
    rows = list(db.scalars(q).unique())
    rows.sort(key=lambda r: (sort_key(r.artist_sort), r.year or 9999, sort_key(r.album)))
    return rows


def query_books(db: Session, f: Filters) -> list[BookRecord]:
    q = select(BookRecord)
    if f.q:
        for w in f.q.split():
            isbn13, _ = normalize_isbn(w)
            cond = (_like(BookRecord.title, w) | _like(BookRecord.subtitle, w) | _like(BookRecord.authors, w) | _like(BookRecord.series, w)
                    | _like(BookRecord.publisher, w))
            if isbn13:
                cond = cond | (BookRecord.isbn13 == isbn13)
            q = q.where(cond)
    if f.author:
        q = q.where(_like(BookRecord.authors, f.author) | _like(BookRecord.author_sort, f.author))
    if f.genre:
        q = q.where(_like(BookRecord.genre, f.genre))
    if f.series:
        q = q.where(BookRecord.series == f.series)
    if f.language:
        q = q.where(BookRecord.language == f.language)
    if f.read in ("ja", "nein"):
        q = q.where(BookRecord.read.is_(f.read == "ja"))
    sub = _in_location(db, f.location_id)
    if sub is not None:
        q = q.where(BookRecord.item_id.in_(sub))
    rows = list(db.scalars(q).unique())
    rows.sort(key=book_order)
    return rows


def book_order(b: BookRecord) -> tuple:
    """Nach Autor, Reihen zusammenhängend in Bandreihenfolge, sonst nach Jahr/Titel."""
    if b.series:
        return (sort_key(b.author_sort), 0, sort_key(b.series), series_no_key(b.series_number), b.year or 9999, sort_key(b.title))
    return (sort_key(b.author_sort), 1, "", (0, 0, ""), b.year or 9999, sort_key(b.title))


def group_records(rows: list[MediaRecord]) -> list[tuple[str, list[tuple[str, list[MediaRecord]]]]]:
    """[(Buchstabe, [(Interpret, [Alben nach Jahr])])]"""
    letters: dict[str, dict[str, list]] = {}
    for r in rows:
        letters.setdefault(sort_letter(r.artist_sort), {}).setdefault(r.artist or "Unbekannt", []).append(r)
    return [(k, list(v.items())) for k, v in sorted(letters.items(), key=lambda kv: (kv[0] == "#", kv[0]))]


def group_books(rows: list[BookRecord]) -> list[tuple[str, list[tuple[str, list[BookRecord]]]]]:
    letters: dict[str, dict[str, list]] = {}
    for b in rows:
        letters.setdefault(sort_letter(b.author_sort), {}).setdefault(b.author_sort or "Ohne Autor", []).append(b)
    return [(k, list(v.items())) for k, v in sorted(letters.items(), key=lambda kv: (kv[0] == "#", kv[0]))]


def totals(db: Session) -> dict:
    recs = list(db.scalars(select(MediaRecord)).unique())
    books = list(db.scalars(select(BookRecord)).unique())
    rcur = {r.market_currency for r in recs if r.market_value is not None} or {"EUR"}
    return {
        "records": {"count": len(recs), "value": round(sum(r.market_value or 0 for r in recs), 2),
                    "currency": rcur.pop() if len(rcur) == 1 else "gemischt", "with_value": sum(1 for r in recs if r.market_value is not None)},
        "books": {"count": len(books), "value": round(sum(b.market_value or 0 for b in books), 2),
                  "with_value": sum(1 for b in books if b.market_value is not None)},
    }


def record_of_item(db: Session, item_id: int) -> MediaRecord | None:
    return db.scalar(select(MediaRecord).where(MediaRecord.item_id == item_id))


def book_of_item(db: Session, item_id: int) -> BookRecord | None:
    return db.scalar(select(BookRecord).where(BookRecord.item_id == item_id))


def search_text(db: Session, item: Item) -> list[str]:
    """Zusatztext für die Volltextsuche (Interpret, Titel, Label, Autoren, Reihe …)."""
    if item.kind == "tontraeger":
        r = record_of_item(db, item.id)
        if r:
            return [r.artist, r.album, r.label, r.catalog_no, r.genre, str(r.year or ""), r.medium] + [t.title for t in r.tracks]
    if item.kind == "buch":
        b = book_of_item(db, item.id)
        if b:
            return [b.title, b.subtitle, b.authors, b.publisher, b.series, b.genre, b.isbn13 or "", b.isbn10 or "", str(b.year or "")]
    return []


# ------------------------------------------------------------------ Löschen (v0.34.4)
def _local_media(path: str | None) -> bool:
    """Nur eigene Dateien unter media/ – keine Katalog-Bilder („catalog:…“) oder Web-Adressen."""
    return bool(path) and ":" not in path and not path.startswith(("/", "\\")) and ".." not in path


def _file_in_use(db: Session, path: str) -> bool:
    """Dateien sind inhaltsadressiert (gleiches Bild = gleicher Name) – erst löschen, wenn nichts mehr darauf zeigt."""
    return bool(db.scalar(select(func.count()).select_from(Item).where(Item.image == path))
                or db.scalar(select(func.count()).select_from(Attachment).where(Attachment.filename == path)))


def delete_entry(db: Session, item: Item, user_id: int | None) -> list[str]:
    """Tonträger/Buch samt Artikel, Bestand, Barcodes/ISBN (ItemCode), Titelliste und Bild-Anhängen löschen.

    Liefert die Dateien (Pfade unter media/), die danach von keinem anderen Datensatz mehr benutzt werden; die
    löscht remove_files() erst nach dem Commit (schlägt der Commit fehl, bleiben die Bilder erhalten).
    """
    from .search import remove_from_index

    rec, book = record_of_item(db, item.id), book_of_item(db, item.id)
    if item.kind == "tontraeger" and rec is not None:
        entity, oid, label = "media_record", rec.id, f"Tonträger {record_name(rec)}"
    else:
        entity, oid, label = "book_record", book.id if book else None, f"Buch {item.name}"
    atts = list(db.scalars(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == item.id)))
    files = {p for p in [item.image, *(a.filename for a in atts)] if _local_media(p)}
    audit.log(db, user_id, "delete", entity, oid, f"{label} gelöscht",
              {"snapshot": [audit.snapshot(rec or book) if (rec or book) else None, None]})
    remove_from_index(db, item.id)
    for v in item.variants:
        v.variant_of_id = None
    for a in atts:
        db.delete(a)
    if rec is not None:
        db.delete(rec)  # Titel über cascade
    if book is not None:
        db.delete(book)
    db.delete(item)  # Bestände, Codes (Barcode/ISBN) und Parameter über cascade
    db.flush()
    return sorted(p for p in files if not _file_in_use(db, p))


def cleanup_replaced(db: Session) -> int:
    """Nach dem Commit: durch ein neues Cover ersetzte Bilddateien löschen, wenn nichts anderes sie mehr benutzt."""
    paths = db.info.pop("cover_replaced", set())
    return remove_files(sorted(p for p in paths if _local_media(p) and not _file_in_use(db, p)))


def remove_files(paths: list[str]) -> int:
    """Bilddateien (und ihr Vorschaubild *.thumb.webp) unter media/ löschen; liefert die Zahl gelöschter Dateien."""
    from ..config import settings

    root = settings.media_dir.resolve()
    n = 0
    for p in paths:
        if not _local_media(p):
            continue
        targets = [root / p]
        if p.endswith(".webp"):
            targets.append(root / (p[:-5] + ".thumb.webp"))
        for t in targets:
            try:
                t = t.resolve()
                if t.is_relative_to(root) and t.is_file():
                    t.unlink()
                    n += 1
            except OSError:
                pass
    return n
