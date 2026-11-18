"""Platten scannen (v0.34): Serien-Erfassung von Tonträgern per Barcode – wie „Bücher scannen“.

Ablauf je Scan (siehe /api/v1/collection/records/scan):
1. Schon in der Sammlung (gleicher Barcode in allen Schreibweisen)? -> „duplicate“, ohne Online-Anfrage.
2. Suche bei MusicBrainz (alle Schreibweisen in einer Anfrage) und – mit Token – Discogs (je Schreibweise).
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

BARCODE_LENGTHS = (8, 12, 13, 14)  # EAN-8, UPC-A, EAN-13, GTIN-14


# ------------------------------------------------------------------ Schreibweisen
def is_barcode(raw: str) -> bool:
    d = re.sub(r"[\s-]", "", raw or "")
    return d.isdigit() and len(d) in BARCODE_LENGTHS


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
                 album: str = "") -> tuple[list[dict], list[str]]:
    """Suche über alle Schreibweisen bei MusicBrainz und (mit Token) Discogs. Rückgabe: (zusammengelegte Treffer, Hinweise)."""
    bcs, cns = barcode_variants(barcode), catno_variants(catno)
    if not (bcs or cns or artist.strip() or album.strip()):
        return [], []
    hits, notes = [], []
    if conf.get("musicbrainz", True):
        try:
            res = await MusicBrainz(conf, client).search(artist=artist.strip(), album=album.strip(), barcodes=bcs, catnos=cns)
            hits += [h.to_dict() for h in res]
        except ProviderError as e:
            notes.append(str(e))
    if conf.get("discogs", True):
        if _token(conf):
            dg = Discogs(conf, client)
            # Discogs kennt kein ODER: je Schreibweise eine Anfrage (Barcode bzw. Kat.-Nr.; sonst Interpret/Album)
            queries = [{"barcode": b} for b in bcs] or [{"catno": c, "artist": artist, "album": album} for c in cns] \
                or [{"artist": artist, "album": album}]
            for q in queries:
                try:
                    hits += [h.to_dict() for h in await dg.search(**q)]
                except ProviderError as e:
                    notes.append(str(e))
                    break
        else:
            notes.append("Discogs-Suche ist aus: kein Token eingetragen (Verwaltung › Sammlung).")
    return merge_hits(hits), list(dict.fromkeys(notes))


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
        for v in barcode_variants(b):  # beide Schreibweisen speichern: der Scanner findet die Platte dann mit jeder
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
