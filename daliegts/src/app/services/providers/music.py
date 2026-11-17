"""Tonträger online: MusicBrainz (Suche, Release mit Titelliste), Cover Art Archive (Hüllen) und Discogs
(Suche, Release, Marktwert). Alle Fehler kommen als ProviderError mit verständlicher Meldung zurück.

* MusicBrainz: höchstens 1 Anfrage pro Sekunde, eigener User-Agent mit Kontakt (Pflicht laut MusicBrainz).
* Discogs: Suche und Preisvorschläge nur mit persönlichem Token; Marktstatistik (niedrigstes Angebot) auch ohne.
  Der Header X-Discogs-Ratelimit-Remaining wird ausgewertet – wird es knapp, wartet der Abruf kurz.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field

import httpx

from . import ProviderError

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
DISCOGS_BASE = "https://api.discogs.com"

# Discogs-Bezeichnungen der Zustandsstufen in /marketplace/price_suggestions
DISCOGS_GRADES = {"M": "Mint (M)", "NM": "Near Mint (NM or M-)", "VG+": "Very Good Plus (VG+)", "VG": "Very Good (VG)",
                  "G+": "Good Plus (G+)", "G": "Good (G)", "F": "Fair (F)", "P": "Poor (P)"}


@dataclass
class RecordHit:
    source: str  # musicbrainz | discogs
    id: str
    artist: str = ""
    album: str = ""
    year: int | None = None
    country: str = ""
    label: str = ""
    catalog_no: str = ""
    medium: str = ""
    format: str = ""
    barcode: str = ""
    genre: str = ""
    musicbrainz_id: str = ""
    discogs_id: str = ""
    thumb: str = ""
    cover_front: str = ""
    cover_back: str = ""
    tracks: list = field(default_factory=list)  # [{side, position, title, artist, length_seconds}]
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _year(v) -> int | None:
    m = re.match(r"\s*(\d{4})", str(v or ""))
    return int(m.group(1)) if m else None


def medium_from_format(fmt: str, track_count: int | None = None) -> str:
    f = (fmt or "").lower()
    if "cassette" in f or "kassette" in f:
        return "Kassette"
    if "cd" in f:
        return "CD"
    if "vinyl" in f or '"' in f or "lp" in f:
        if '7"' in f or "single" in f:
            return "Single"
        if "maxi" in f or ('12"' in f and track_count is not None and track_count <= 4):
            return "Maxi"
        return "LP"
    return "Sonstiges" if f else ""


def split_position(pos: str, disc: int = 1, discs: int = 1) -> tuple[str, int | None]:
    """„A1“ -> ("A", 1); „B“ -> ("B", 1); „3“ -> ("1", 3) bzw. CD-Nummer als Seite."""
    p = (pos or "").strip().upper()
    m = re.fullmatch(r"([A-Z]{1,2})\s*(\d*)", p)
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else 1
    m = re.fullmatch(r"(\d+)[-.](\d+)", p)  # „2-05“ (CD 2, Titel 5)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"\d+", p)
    return (str(disc) if discs > 1 or disc > 1 else "1"), (int(p) if m else None)


def parse_duration(v) -> int | None:
    """„3:25“ -> 205; Millisekunden (int) -> Sekunden."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(round(v / 1000))
    parts = [x for x in str(v).split(":") if x.strip().isdigit()]
    if not parts:
        return None
    sec = 0
    for x in parts:
        sec = sec * 60 + int(x)
    return sec


def clean_artist(name: str) -> str:
    """Discogs hängt bei Namensgleichheit „ (2)“ an und markiert Variationen mit „*“."""
    return re.sub(r"\s*\(\d+\)$", "", (name or "").strip()).rstrip("*").strip()


def credit_name(credits: list | None) -> str:
    """MusicBrainz artist-credit -> „Simon & Garfunkel“."""
    out = ""
    for c in credits or []:
        if isinstance(c, dict):
            out += (c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "")
    return out.strip()


class _Client:
    def __init__(self, conf: dict, client: httpx.AsyncClient | None = None):
        from .sammlung import user_agent

        self.conf = conf or {}
        self.client = client
        self.ua = user_agent(self.conf)

    async def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=20, follow_redirects=True)
        return self.client


# ------------------------------------------------------------------ MusicBrainz + Cover Art Archive
class MusicBrainz(_Client):
    name = "MusicBrainz"
    min_interval = 1.0  # Sekunden zwischen zwei Anfragen (Vorgabe von MusicBrainz)
    _lock: asyncio.Lock | None = None
    _last = 0.0

    async def _get(self, path: str, params: dict) -> dict:
        cls = type(self)
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        http = await self._http()
        async with cls._lock:
            wait = cls._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await http.get(f"{MB_BASE}{path}", params={**params, "fmt": "json"},
                                   headers={"User-Agent": self.ua, "Accept": "application/json"})
            except httpx.HTTPError as e:
                raise ProviderError(f"MusicBrainz nicht erreichbar: {e.__class__.__name__}") from e
            finally:
                cls._last = time.monotonic()
        if r.status_code == 404:
            raise ProviderError("MusicBrainz: Eintrag nicht gefunden.")
        if r.status_code in (429, 503):
            raise ProviderError("MusicBrainz: zu viele Anfragen – bitte kurz warten.")
        if r.status_code >= 400:
            raise ProviderError(f"MusicBrainz: Fehler {r.status_code}.")
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError("MusicBrainz: unerwartete Antwort.") from e

    @staticmethod
    def _q(v: str) -> str:
        return '"' + re.sub(r'["\\]', " ", v.strip()) + '"'

    def _hit(self, rel: dict) -> RecordHit:
        li = (rel.get("label-info") or [{}])
        li0 = li[0] if li else {}
        media = rel.get("media") or []
        fmt = " + ".join(m.get("format") or "" for m in media if m.get("format"))
        tracks = sum(int(m.get("track-count") or 0) for m in media) or None
        return RecordHit("musicbrainz", rel.get("id", ""), artist=credit_name(rel.get("artist-credit")), album=rel.get("title") or "",
                         year=_year(rel.get("date")), country=rel.get("country") or "", label=((li0 or {}).get("label") or {}).get("name") or "",
                         catalog_no=(li0 or {}).get("catalog-number") or "", format=fmt, medium=medium_from_format(fmt, tracks),
                         barcode=rel.get("barcode") or "", musicbrainz_id=rel.get("id", ""),
                         url=f"https://musicbrainz.org/release/{rel.get('id', '')}")

    async def search(self, artist: str = "", album: str = "", barcode: str = "", catno: str = "", limit: int = 10) -> list[RecordHit]:
        parts = []
        if barcode:
            parts.append(f"barcode:{re.sub(r'[^0-9]', '', barcode)}")
        if catno:
            parts.append(f"catno:{self._q(catno)}")
        if artist:
            parts.append(f"artist:{self._q(artist)}")
        if album:
            parts.append(f"release:{self._q(album)}")
        if not parts:
            return []
        data = await self._get("/release", {"query": " AND ".join(parts), "limit": limit})
        return [self._hit(r) for r in data.get("releases") or []]

    async def release(self, mbid: str) -> RecordHit:
        if not re.fullmatch(r"[0-9a-f-]{36}", mbid or ""):
            raise ProviderError("Ungültige MusicBrainz-ID.")
        rel = await self._get(f"/release/{mbid}", {"inc": "recordings+artist-credits+labels+release-groups+genres+url-rels"})
        hit = self._hit(rel)
        album_artist = hit.artist
        media = rel.get("media") or []
        vinyl_like = any("vinyl" in (m.get("format") or "").lower() or "cassette" in (m.get("format") or "").lower() for m in media)
        tracks = []
        for m in media:
            disc = int(m.get("position") or 1)
            for t in m.get("tracks") or []:
                side, pos = split_position(str(t.get("number") or t.get("position") or ""), disc, len(media))
                if not vinyl_like and not re.fullmatch(r"\d+", side):
                    side = str(disc)
                ta = credit_name(t.get("artist-credit"))
                tracks.append({"side": side, "position": pos or int(t.get("position") or 0) or len(tracks) + 1,
                               "title": t.get("title") or (t.get("recording") or {}).get("title") or "",
                               "artist": "" if ta == album_artist else ta,
                               "length_seconds": parse_duration(t.get("length") or (t.get("recording") or {}).get("length"))})
        hit.tracks = tracks
        genres = sorted(rel.get("genres") or (rel.get("release-group") or {}).get("genres") or [], key=lambda g: -(g.get("count") or 0))
        hit.genre = ", ".join(g["name"].title() for g in genres[:2] if g.get("name"))
        if hit.year is None:
            hit.year = _year((rel.get("release-group") or {}).get("first-release-date"))
        for rl in rel.get("relations") or []:
            m = re.search(r"discogs\.com/release/(\d+)", ((rl.get("url") or {}).get("resource") or ""))
            if m:
                hit.discogs_id = m.group(1)
        hit.cover_front, hit.cover_back = await CoverArtArchive(self.conf, self.client).covers(mbid)
        hit.thumb = hit.cover_front
        return hit


class CoverArtArchive(_Client):
    async def covers(self, mbid: str) -> tuple[str, str]:
        """(front-500, back-500) – leer, wenn nicht vorhanden. Fehler werden ignoriert (Hülle ist optional)."""
        http = await self._http()
        try:
            r = await http.get(f"{CAA_BASE}/release/{mbid}", headers={"User-Agent": self.ua, "Accept": "application/json"})
        except httpx.HTTPError:
            return "", ""
        if r.status_code != 200:
            return "", ""
        try:
            imgs = r.json().get("images") or []
        except ValueError:
            return "", ""
        front = f"{CAA_BASE}/release/{mbid}/front-500" if any(i.get("front") for i in imgs) else ""
        back = f"{CAA_BASE}/release/{mbid}/back-500" if any(i.get("back") for i in imgs) else ""
        return front, back


# ------------------------------------------------------------------ Discogs
class Discogs(_Client):
    name = "Discogs"
    low_watermark = 2  # ab so wenigen verbleibenden Anfragen kurz warten
    pause = 5.0  # Sekunden
    remaining: int | None = None  # zuletzt gemeldetes Restkontingent (Anzeige in der Verwaltung)

    @property
    def token(self) -> str:
        return (self.conf.get("discogs_token") or "").strip()

    async def _get(self, path: str, params: dict | None = None, need_token: bool = False) -> dict | None:
        if need_token and not self.token:
            raise ProviderError("Discogs: dafür wird ein persönliches Token benötigt (Verwaltung › Sammlung).")
        cls = type(self)
        if cls.remaining is not None and cls.remaining <= self.low_watermark:
            await asyncio.sleep(self.pause)
        headers = {"User-Agent": self.ua, "Accept": "application/vnd.discogs.v2.discogs+json"}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"
        http = await self._http()
        try:
            r = await http.get(f"{DISCOGS_BASE}{path}", params=params or {}, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"Discogs nicht erreichbar: {e.__class__.__name__}") from e
        rem = r.headers.get("X-Discogs-Ratelimit-Remaining")
        if rem is not None and rem.strip().isdigit():
            cls.remaining = int(rem)
        if r.status_code == 404:
            return None
        if r.status_code == 401:
            raise ProviderError("Discogs: Token ungültig oder fehlt." if self.token else "Discogs: dafür wird ein Token benötigt.")
        if r.status_code == 403:
            raise ProviderError("Discogs: Zugriff verweigert (403).")
        if r.status_code == 429:
            raise ProviderError("Discogs: Anfragelimit erreicht – in einer Minute erneut versuchen.")
        if r.status_code >= 400:
            raise ProviderError(f"Discogs: Fehler {r.status_code}.")
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError("Discogs: unerwartete Antwort.") from e

    async def search(self, artist: str = "", album: str = "", barcode: str = "", catno: str = "", fmt: str = "",
                     limit: int = 10) -> list[RecordHit]:
        params = {"type": "release", "per_page": limit}
        for k, v in (("artist", artist), ("release_title", album), ("barcode", re.sub(r"[^0-9]", "", barcode)), ("catno", catno), ("format", fmt)):
            if v:
                params[k] = v.strip()
        if len(params) == 2:
            return []
        data = await self._get("/database/search", params, need_token=True) or {}
        out = []
        for r in data.get("results") or []:
            title = r.get("title") or ""
            a, _, t = title.partition(" - ")
            fmts = r.get("format") or []
            out.append(RecordHit("discogs", str(r.get("id") or ""), artist=clean_artist(a), album=t.strip() or title, year=_year(r.get("year")),
                                 country=r.get("country") or "", label=(r.get("label") or [""])[0], catalog_no=r.get("catno") or "",
                                 format=", ".join(fmts), medium=medium_from_format(" ".join(fmts)), barcode=(r.get("barcode") or [""])[0],
                                 genre=", ".join((r.get("style") or r.get("genre") or [])[:2]), discogs_id=str(r.get("id") or ""),
                                 thumb=r.get("thumb") or "", cover_front=r.get("cover_image") or "",
                                 url=f"https://www.discogs.com/release/{r.get('id')}"))
        return out

    async def release(self, rid: str) -> RecordHit:
        if not str(rid).isdigit():
            raise ProviderError("Ungültige Discogs-ID.")
        r = await self._get(f"/releases/{rid}", {"curr_abbr": "EUR"})
        if not r:
            raise ProviderError("Discogs: Release nicht gefunden.")
        artist = ""
        for a in r.get("artists") or []:
            artist += clean_artist(a.get("anv") or a.get("name") or "")
            j = (a.get("join") or "").strip()
            if j:
                artist += ", " if j == "," else f" {j} "
        artist = artist.strip().rstrip(",").strip()
        fmts = r.get("formats") or []
        fmt = ", ".join(" ".join([f.get("name") or ""] + (f.get("descriptions") or [])) for f in fmts)
        tracklist = [t for t in r.get("tracklist") or [] if (t.get("type_") or "track") == "track"]
        tracks = []
        for i, t in enumerate(tracklist, 1):
            side, pos = split_position(t.get("position") or "")
            ta = ", ".join(clean_artist(a.get("name") or "") for a in t.get("artists") or [])
            tracks.append({"side": side, "position": pos or i, "title": t.get("title") or "", "artist": "" if ta == artist else ta,
                           "length_seconds": parse_duration(t.get("duration"))})
        imgs = r.get("images") or []
        primary = next((i for i in imgs if i.get("type") == "primary"), imgs[0] if imgs else {})
        secondary = next((i for i in imgs if i is not primary), {})
        labels = r.get("labels") or [{}]
        barcode = next((i.get("value") for i in r.get("identifiers") or [] if (i.get("type") or "").lower() == "barcode"), "")
        return RecordHit("discogs", str(r.get("id")), artist=artist, album=r.get("title") or "", year=_year(r.get("year") or r.get("released")),
                         country=r.get("country") or "", label=clean_artist(labels[0].get("name") or ""), catalog_no=labels[0].get("catno") or "",
                         format=fmt, medium=medium_from_format(fmt, len(tracks)), barcode=re.sub(r"[^0-9]", "", barcode or ""),
                         genre=", ".join((r.get("styles") or r.get("genres") or [])[:2]), discogs_id=str(r.get("id")),
                         thumb=primary.get("uri150") or "", cover_front=primary.get("uri") or "", cover_back=secondary.get("uri") or "",
                         tracks=tracks, url=r.get("uri") or f"https://www.discogs.com/release/{r.get('id')}")

    async def market(self, rid: str, condition: str = "") -> dict:
        """Marktwert: niedrigstes Angebot (ohne Token) und Preisvorschlag zum Zustand (mit Token).

        Liefert {"lowest", "suggested", "currency", "for_sale", "notes": [...]}; wirft ProviderError nur, wenn gar nichts ging.
        """
        if not str(rid or "").isdigit():
            raise ProviderError("Keine Discogs-ID – den Tonträger zuerst mit Discogs verknüpfen.")
        notes: list[str] = []
        stats = await self._get(f"/marketplace/stats/{rid}", {"curr_abbr": "EUR"})
        if stats is None:
            raise ProviderError("Discogs: Release nicht gefunden.")
        lp = stats.get("lowest_price") or {}
        lowest = float(lp["value"]) if isinstance(lp, dict) and lp.get("value") is not None else None
        currency = (lp.get("currency") if isinstance(lp, dict) else None) or "EUR"
        for_sale = stats.get("num_for_sale")
        if stats.get("blocked_from_sale"):
            notes.append("Bei Discogs für den Verkauf gesperrt.")
        suggested = None
        if not self.token:
            notes.append("Preisvorschlag braucht ein Discogs-Token.")
        elif not condition:
            notes.append("Für einen Preisvorschlag den Zustand der Platte eintragen.")
        else:
            try:
                sug = await self._get(f"/marketplace/price_suggestions/{rid}", need_token=True) or {}
                v = sug.get(DISCOGS_GRADES.get(condition, ""))
                if isinstance(v, dict) and v.get("value") is not None:
                    scur = v.get("currency") or currency
                    suggested = round(float(v["value"]), 2)
                    if scur != currency:
                        notes.append(f"Preisvorschlag in {scur} (Verkäufer-Währung bei Discogs); niedrigstes Angebot ausgeblendet.")
                        lowest, currency = None, scur
                else:
                    notes.append(f"Kein Preisvorschlag für Zustand {condition}.")
            except ProviderError as e:
                notes.append(str(e))
        return {"lowest": round(lowest, 2) if lowest is not None else None, "suggested": suggested, "currency": currency[:3],
                "for_sale": int(for_sale) if isinstance(for_sale, (int, float)) else None, "notes": notes}


# ------------------------------------------------------------------ gemeinsame Suche
async def search_all(conf: dict, client: httpx.AsyncClient | None = None, **kw) -> tuple[list[dict], list[str]]:
    """Suche bei allen eingeschalteten Quellen. Rückgabe: (Treffer als dicts, Hinweise/Fehler)."""
    hits, notes = [], []
    if conf.get("musicbrainz", True):
        try:
            hits += [h.to_dict() for h in await MusicBrainz(conf, client).search(**{k: v for k, v in kw.items() if k != "fmt"})]
        except ProviderError as e:
            notes.append(str(e))
    if conf.get("discogs", True):
        if (conf.get("discogs_token") or "").strip():
            try:
                hits += [h.to_dict() for h in await Discogs(conf, client).search(**kw)]
            except ProviderError as e:
                notes.append(str(e))
        else:
            notes.append("Discogs-Suche ist aus: kein Token eingetragen (Verwaltung › Sammlung).")
    return hits, notes


async def release_details(conf: dict, source: str, rid: str, client: httpx.AsyncClient | None = None) -> dict:
    if source == "musicbrainz":
        hit = await MusicBrainz(conf, client).release(rid)
    elif source == "discogs":
        hit = await Discogs(conf, client).release(rid)
    else:
        raise ProviderError("Unbekannte Quelle.")
    return hit.to_dict()
