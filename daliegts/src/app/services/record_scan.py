"""Platten scannen (v0.34): Serien-Erfassung von Tonträgern per Barcode – wie „Bücher scannen“.

Ablauf je Scan (siehe /api/v1/collection/records/scan):
1. Schon in der Sammlung (gleicher Barcode in allen Schreibweisen)? -> „duplicate“, ohne Online-Anfrage.
   Die Eingabe wird vorher gesäubert (clean_code: Steuerzeichen weg, fast nur Ziffern -> nur die Ziffern).
2. Suche – mit Token – zuerst bei Discogs (je Schreibweise, bis eine trifft), dann bei MusicBrainz (alle Schreibweisen
   in einer Anfrage). Schreibweisen: search_variants (UPC-A, EAN-13, GTIN-14, ohne Zusatzcode).
   Treffer beider Quellen zur selben Pressung werden zusammengelegt.
3. Genau ein Treffer + „automatisch speichern“: Release-Daten holen (Titelliste, Cover, Label, Kat.-Nr.,
   Discogs-ID), noch einmal auf Dublette prüfen (MusicBrainz-/Discogs-ID, Barcode) und mit den Serien-
   Einstellungen (Lagerort, Medium, Zustand) anlegen. Mehrere Treffer -> Auswahl; keiner -> Hinweis.
"""
from __future__ import annotations

import re

import httpx
from sqlalchemy.orm import Session

from ..models import GRADES, MEDIA_MEDIUMS, MediaRecord
from . import collection as col
from .items import image_url
from .providers import ProviderError
from .providers.music import CAA_BASE, Discogs, MusicBrainz

BARCODE_LENGTHS = (8, 11, 12, 13, 14, 15, 17, 18)  # EAN-8, UPC-A (ohne/mit führender 0), EAN-13, GTIN-14, mit 2/5er-Zusatz
ADDON_CORE = {15: 13, 17: 12, 18: 13}  # EAN-13+2, UPC-A+5, EAN-13+5 -> Länge des eigentlichen Codes
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ------------------------------------------------------------------ Eingabe säubern
def clean_code(raw: str | None) -> str:
    """Steuerzeichen (CR/LF/ESC … vom Handscanner) entfernen. Besteht der Rest fast nur aus Ziffern (≥ 8 Ziffern,
    höchstens 3 andere Zeichen, keine zwei Buchstaben hintereinander), nur die Ziffern: „ 0 88106-11712 01“ -> „0881061171201“.
    Alles andere (Katalognummer, Lagerort-Etikett) bleibt wie eingegeben, nur ohne Steuerzeichen."""
    s = strip_ctrl(raw)
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) >= 8 and len(s) - len(digits) <= 3 and not re.search(r"[^\W\d_]{2}", s):
        return digits
    return s


def strip_ctrl(raw: str | None) -> str:
    """Nur Steuerzeichen entfernen – für Katalognummern („7567-81642-1“ behält die Bindestriche)."""
    return _CTRL.sub("", raw or "").strip()


def is_barcode(raw: str | None) -> bool:
    d = clean_code(raw)
    return d.isdigit() and len(d) in BARCODE_LENGTHS


def search_variants(raw: str | None) -> list[str]:
    """Alle Schreibweisen eines Barcodes für die Suche: wie gescannt, führende Nullen weg und auf 12 (UPC-A), 13 (EAN-13)
    und 14 Stellen (GTIN-14) aufgefüllt. Mit 2-/5-stelligem Zusatzcode (15, 17, 18 Ziffern) zuerst der eigentliche Code
    (die ersten 13 bzw. 12 Ziffern). Reihenfolge = Suchreihenfolge bei Discogs (wahrscheinlichste zuerst)."""
    d = clean_code(raw)
    if not d.isdigit():
        return []
    core = d[:ADDON_CORE[len(d)]] if len(d) in ADDON_CORE else d
    s = core.lstrip("0") or "0"
    out = [core] + [s.zfill(n) for n in (13, 12, 14) if len(s) <= n] + [d]  # vollständiger Code mit Zusatz zuletzt
    return list(dict.fromkeys(out))


def storage_codes(raw: str | None) -> list[str]:
    """Codes, die am Artikel gespeichert werden: wie gescannt plus UPC-A/EAN-13-Paar des eigentlichen Codes
    (ohne Zusatzcode, ohne GTIN-14-Füllnullen) – der normale Scanner findet die Platte dann mit jeder Schreibweise."""
    d = clean_code(raw)
    if not d.isdigit():
        return []
    core = d[:ADDON_CORE[len(d)]] if len(d) in ADDON_CORE else d
    out = [d]
    if 11 <= len(core) <= 14:
        g = core.zfill(14)
        out += col.barcode_variants(g[1:]) if g[0] == "0" else [core]
    else:
        out += col.barcode_variants(core)
    return list(dict.fromkeys(out))


barcode_variants = col.barcode_variants


def catno_variants(raw: str) -> list[str]:
    """Katalognummer wie eingegeben und ohne Leerzeichen: „8 55 497“ -> [„8 55 497“, „855497“]."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return []
    out = [s]
    compact = s.replace(" ", "")
    if compact != s:
        out.append(compact)
    return out


def _bc_key(v: str) -> str:
    return re.sub(r"[^0-9]", "", v or "").lstrip("0")


def _cat_key(v: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (v or "").upper())


# ------------------------------------------------------------------ Dubletten
# Eine Regel für alle Wege (siehe collection.find_record_duplicate).
find_existing = col.find_record_by_ids
find_handmade_twin = col.find_handmade_twin


# ------------------------------------------------------------------ Suche
def merge_hits(hits: list[dict]) -> list[dict]:
    """Doppelte Treffer (gleiche Quelle/ID) entfernen und Discogs-Treffer zur gleichen Pressung in den
    MusicBrainz-Treffer übernehmen (gleicher Barcode bzw. Kat.-Nr., Jahr und Kat.-Nr. widersprechen sich nicht)."""
    seen, uniq = set(), []
    for h in hits:
        key = (h.get("source"), str(h.get("id")))
        if key not in seen:
            seen.add(key)
            uniq.append({**h, "sources": [h.get("source")]})
    mb = [h for h in uniq if h["source"] == "musicbrainz"]
    out = list(mb)
    for d in (h for h in uniq if h["source"] != "musicbrainz"):
        target = None
        for m in mb:
            if m.get("year") and d.get("year") and m["year"] != d["year"]:
                continue
            mc, dc = _cat_key(m.get("catalog_no")), _cat_key(d.get("catalog_no"))
            if mc and dc and mc != dc:
                continue
            mbc, dbc = _bc_key(m.get("barcode")), _bc_key(d.get("barcode"))
            if (mbc and mbc == dbc) or (mc and mc == dc):
                target = m
                break
        if target is None:
            out.append(d)
            continue
        if "discogs" not in target["sources"]:
            target["sources"].append("discogs")
            target["discogs_id"] = target.get("discogs_id") or d.get("discogs_id") or ""
        for k in ("thumb", "cover_front", "genre", "label"):
            if not target.get(k) and d.get(k):
                target[k] = d[k]
    for h in out:
        if h["source"] == "musicbrainz" and not h.get("thumb") and h.get("musicbrainz_id"):
            h["thumb"] = f"{CAA_BASE}/release/{h['musicbrainz_id']}/front-250"  # fehlt das Bild, blendet die Seite es aus
    return out


def _token(conf: dict) -> bool:
    return bool(conf.get("discogs", True) and (conf.get("discogs_token") or "").strip())


async def search(conf: dict, client: httpx.AsyncClient | None, barcode: str = "", catno: str = "", artist: str = "",
                 album: str = "", fmt: str = "") -> tuple[list[dict], list[str]]:
    """Suche über alle Schreibweisen bei Discogs (mit Token, zuerst – dort sind Vinyl-Barcodes besser gepflegt) und
    MusicBrainz. Rückgabe: (zusammengelegte Treffer, Hinweise)."""
    bcs, cns = search_variants(barcode), catno_variants(catno)
    if not (bcs or cns or artist.strip() or album.strip()):
        return [], []
    hits, notes = [], []
    if conf.get("discogs", True):
        if _token(conf):
            dg = Discogs(conf, client)
            # Discogs kennt kein ODER: je Schreibweise eine Anfrage (Barcode bzw. Kat.-Nr.; sonst Interpret/Album),
            # bis eine etwas findet – schont das Anfragelimit (60/min).
            # Formatfilter nur ohne Barcode (Discogs.search lässt ihn bei Barcode ohnehin weg)
            queries = [{"barcode": b} for b in bcs] or [{"catno": c, "artist": artist, "album": album, "fmt": fmt} for c in cns] \
                or [{"artist": artist, "album": album, "fmt": fmt}]
            for q in queries:
                try:
                    found = [h.to_dict() for h in await dg.search(**q)]
                except ProviderError as e:
                    notes.append(str(e))
                    break
                if found:
                    hits += found
                    break
        else:
            notes.append("Discogs-Suche ist aus: kein Token eingetragen (Verwaltung › Sammlung).")
    if conf.get("musicbrainz", True):
        try:  # alle Schreibweisen in einer Anfrage, ohne Formatfilter
            res = await MusicBrainz(conf, client).search(artist=artist.strip(), album=album.strip(), barcodes=bcs, catnos=cns)
            hits += [h.to_dict() for h in res]
        except ProviderError as e:
            notes.append(str(e))
    return merge_hits(hits), list(dict.fromkeys(notes))


def none_message(conf: dict, barcode: str = "", catno: str = "") -> str:
    """Klare Meldung, wenn nichts gefunden wurde: gesuchte Zahl mit Ziffernanzahl, was man stattdessen versuchen kann."""
    if barcode:
        msg = f"Nichts gefunden für Barcode {barcode} ({len(barcode)} Ziffern). Katalognummer oder Interpret/Album versuchen."
    elif catno:
        msg = f"Nichts gefunden für Katalognummer „{catno}“. Andere Schreibweise oder Interpret/Album versuchen."
    else:
        msg = "Nichts gefunden. Katalognummer oder Interpret/Album versuchen."
    if conf.get("discogs", True) and not _token(conf):
        msg += " Tipp: Discogs kennt viel mehr Platten-Barcodes – dafür ein Token unter Verwaltung › Sammlung eintragen."
    return msg


# ------------------------------------------------------------------ gemeinsamer Weg: Platten scannen + „Online suchen“
def parse_query(code: str = "", catno: str = "") -> tuple[str, str, str]:
    """Eingabe säubern. code = Scan-/Barcode-Feld (Barcode oder Katalognummer), catno = Feld Katalognummer.
    Rückgabe: (roh ohne Steuerzeichen, Barcode in Ziffern oder "", Katalognummer)."""
    raw = strip_ctrl(code)
    cleaned = clean_code(raw)
    barcode = cleaned if is_barcode(cleaned) else ""
    return raw, barcode, strip_ctrl(catno) or ("" if barcode else raw)


async def lookup(conf: dict, client: httpx.AsyncClient | None, barcode: str = "", catno: str = "", artist: str = "",
                 album: str = "", fmt: str = "") -> dict:
    """Online-Suche für beide Wege. Fällt eine Quelle aus, kommen die Treffer der anderen trotzdem; der Ausfall steht
    nur in „notes“. Ohne Treffer: „message“ mit gesuchter Zahl/Ziffernanzahl (und ggf. Token-Hinweis).
    Rückgabe: {hits, notes, query, message}."""
    hits, notes = await search(conf, client, barcode=barcode, catno=catno, artist=artist, album=album, fmt=fmt)
    query = {"barcode": barcode, "digits": len(barcode), "catno": catno,
             "variants": search_variants(barcode) or catno_variants(catno)}
    message = ""
    if not hits:
        message = none_message(conf, barcode, "" if barcode else catno)
        if "Token" in message:  # Token-Hinweis steht schon in der Meldung
            notes = [n for n in notes if "kein Token" not in n]
    return {"hits": hits, "notes": notes, "query": query, "message": message}


async def release(conf: dict, client: httpx.AsyncClient | None, mbid: str = "", discogs_id: str = "") -> tuple[dict, list[str]]:
    """Vollständige Daten (Titelliste, Cover, Label, Kat.-Nr., Discogs-ID). MusicBrainz zuerst; fehlende Felder
    (z. B. Cover, Titelliste) ergänzt Discogs, wenn ein Token da ist."""
    notes: list[str] = []
    data: dict = {}
    if mbid:
        data = (await MusicBrainz(conf, client).release(mbid)).to_dict()
        discogs_id = data.get("discogs_id") or discogs_id
    if discogs_id and (_token(conf) or not data):
        try:
            dg = (await Discogs(conf, client).release(str(discogs_id))).to_dict()
        except ProviderError as e:
            if not data:
                raise
            notes.append(str(e))
            dg = {}
        if not data:
            data = dg
        else:
            for k, v in dg.items():
                if k in ("source", "id", "url"):
                    continue
                if v and not data.get(k):
                    data[k] = v
    if not data:
        raise ProviderError("Keine MusicBrainz- oder Discogs-ID angegeben.")
    data["discogs_id"] = str(discogs_id or data.get("discogs_id") or "")
    return data, notes


# ------------------------------------------------------------------ Speichern
def series_settings(raw: dict) -> dict:
    """Einstellungen der Serie bereinigen: Medium (leer = aus dem Treffer), Zustand Platte/Hülle."""
    medium = str(raw.get("medium") or "")
    return {"medium": medium if medium in MEDIA_MEDIUMS else "",
            "media_condition": str(raw.get("media_condition") or "") if raw.get("media_condition") in GRADES else "",
            "sleeve_condition": str(raw.get("sleeve_condition") or "") if raw.get("sleeve_condition") in GRADES else ""}


def record_data(details: dict, settings: dict, scanned: list[str]) -> dict:
    """Release-Daten + Serien-Einstellungen -> Felder für collection.upsert_record."""
    data = {k: details.get(k) or "" for k in ("artist", "album", "genre", "label", "catalog_no", "country")}
    data["year"] = details.get("year") or ""
    data["musicbrainz_id"] = details.get("musicbrainz_id") or ""
    data["discogs_id"] = details.get("discogs_id") or ""
    data["medium"] = settings.get("medium") or details.get("medium") or "LP"
    data["media_condition"] = settings.get("media_condition") or ""
    data["sleeve_condition"] = settings.get("sleeve_condition") or ""
    data["tracks"] = details.get("tracks") or []
    codes: list[str] = []
    for b in [*scanned, details.get("barcode") or ""]:
        for v in storage_codes(b):  # wie gescannt + UPC-A/EAN-13: der Scanner findet die Platte dann mit jeder Schreibweise
            if v not in codes:
                codes.append(v)
    data["barcode"] = codes
    return data


def record_summary(db: Session, rec: MediaRecord, tc=None) -> dict:
    """Kurzform für die Liste „in dieser Serie erfasst“."""
    loc = col.location_of(rec.item)
    return {"id": rec.id, "artist": rec.artist, "album": rec.album, "year": rec.year, "medium": rec.medium, "label": rec.label,
            "catalog_no": rec.catalog_no, "url": f"/sammlung/tontraeger/{rec.id}", "thumb": image_url(rec.item.image) if rec.item.image else "",
            "location": (tc.path(loc.id) if tc else loc.name) if loc else None}
