"""Bücher online: Deutsche Nationalbibliothek (SRU, MARC21), Open Library und Google Books.

`lookup_isbn` fragt die Quellen in der eingestellten Reihenfolge und ergänzt fehlende Felder aus der jeweils
nächsten Quelle. `search` sucht nach Titel + Autor (z. B. ältere Ausgaben/DDR-Bücher ohne ISBN).

Marktwert: nur als Schnittstelle vorbereitet (`BookMarketProvider`). Es ist bewusst keine feste Quelle eingebaut;
eigene Provider registriert man in `BOOK_MARKET_PROVIDERS` und wählt sie in der Verwaltung aus.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, fields

import httpx

from . import ProviderError

OL_BASE = "https://openlibrary.org"
OL_COVERS = "https://covers.openlibrary.org"
GB_BASE = "https://www.googleapis.com/books/v1/volumes"
DNB_SRU = "https://services.dnb.de/sru/dnb"
DNB_COVER = "https://portal.dnb.de/opac/mvb/cover"
MARC = "{http://www.loc.gov/MARC21/slim}"
LANGS = {"ger": "de", "deu": "de", "eng": "en", "fre": "fr", "fra": "fr", "rus": "ru", "ita": "it", "spa": "es", "pol": "pl",
         "cze": "cs", "ces": "cs", "dut": "nl", "nld": "nl", "lat": "la", "swe": "sv", "dan": "da", "nor": "no", "hun": "hu"}


@dataclass
class BookHit:
    source: str
    isbn13: str = ""
    isbn10: str = ""
    title: str = ""
    subtitle: str = ""
    authors: list = field(default_factory=list)
    publisher: str = ""
    year: int | None = None
    edition: str = ""
    language: str = ""
    pages: int | None = None
    series: str = ""
    series_number: str = ""
    genre: str = ""
    binding: str = ""
    cover_url: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


MERGE_FIELDS = [f.name for f in fields(BookHit) if f.name not in ("source", "url")]


def merge(hits: list[BookHit]) -> dict | None:
    """Erster Treffer gewinnt, leere Felder werden aus den folgenden ergänzt. „sources“ nennt die beteiligten Quellen."""
    hits = [h for h in hits if h and (h.title or h.isbn13)]
    if not hits:
        return None
    out = hits[0].to_dict()
    used = {hits[0].source}
    for h in hits[1:]:
        for k in MERGE_FIELDS:
            v = getattr(h, k)
            if v and not out.get(k):
                out[k] = v
                used.add(h.source)
    out["sources"] = [h.source for h in hits if h.source in used]
    out.pop("source", None)
    return out


def _year(v) -> int | None:
    m = re.search(r"(1[4-9]\d\d|20\d\d)", str(v or ""))
    return int(m.group(1)) if m else None


def _pages(v) -> int | None:
    if isinstance(v, int):
        return v or None
    m = re.search(r"(\d+)\s*(?:S\.|Seiten|p\.|pages)?", str(v or ""))
    return int(m.group(1)) if m else None


def _isbns(values) -> tuple[str, str]:
    from ..collection import normalize_isbn

    for v in values or []:
        i13, i10 = normalize_isbn(v)
        if i13:
            return i13, i10 or ""
    return "", ""


def _binding(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ("gb.", "geb.", "hardcover", "gebunden", "hardback", "pappband", "leinen")):
        return "Hardcover"
    if any(w in t for w in ("kt.", "kart.", "taschenbuch", "paperback", "broschiert", "softcover", "br.")):
        return "Taschenbuch"
    if "heft" in t:
        return "Heft"
    return ""


def invert_name(n: str) -> str:
    """„Goethe, Johann Wolfgang von“ -> „Johann Wolfgang von Goethe“."""
    n = re.sub(r"\s+", " ", (n or "").strip().rstrip(",."))
    if "," in n:
        last, _, first = n.partition(",")
        return f"{first.strip()} {last.strip()}".strip()
    return n


class _Source:
    key = ""
    name = ""

    def __init__(self, conf: dict | None = None, client: httpx.AsyncClient | None = None):
        from .sammlung import user_agent

        self.conf = conf or {}
        self.client = client
        self.ua = user_agent(self.conf)

    async def _get(self, url: str, params: dict) -> httpx.Response:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=20, follow_redirects=True)
        try:
            r = await self.client.get(url, params=params, headers={"User-Agent": self.ua})
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name} nicht erreichbar: {e.__class__.__name__}") from e
        if r.status_code == 429:
            raise ProviderError(f"{self.name}: zu viele Anfragen – später erneut versuchen.")
        if r.status_code in (401, 403):
            raise ProviderError(f"{self.name}: Zugriff verweigert ({r.status_code}) – Schlüssel prüfen.")
        if r.status_code >= 400 and r.status_code != 404:
            raise ProviderError(f"{self.name}: Fehler {r.status_code}.")
        return r

    async def isbn(self, isbn13: str) -> BookHit | None:
        raise NotImplementedError

    async def search(self, title: str = "", author: str = "", limit: int = 10) -> list[BookHit]:
        raise NotImplementedError


# ------------------------------------------------------------------ Open Library
class OpenLibrary(_Source):
    key = "openlibrary"
    name = "Open Library"

    async def isbn(self, isbn13: str) -> BookHit | None:
        r = await self._get(f"{OL_BASE}/api/books", {"bibkeys": f"ISBN:{isbn13}", "format": "json", "jscmd": "data"})
        if r.status_code == 404:
            return None
        d = (r.json() or {}).get(f"ISBN:{isbn13}")
        if not d:
            return None
        ids = d.get("identifiers") or {}
        i13, i10 = _isbns((ids.get("isbn_13") or []) + (ids.get("isbn_10") or []) + [isbn13])
        cover = (d.get("cover") or {})
        return BookHit(self.key, isbn13=i13, isbn10=i10, title=d.get("title") or "", subtitle=d.get("subtitle") or "",
                       authors=[a.get("name") for a in d.get("authors") or [] if a.get("name")],
                       publisher=((d.get("publishers") or [{}])[0] or {}).get("name") or "", year=_year(d.get("publish_date")),
                       pages=_pages(d.get("number_of_pages")), genre=", ".join(s.get("name") for s in (d.get("subjects") or [])[:2] if s.get("name")),
                       cover_url=cover.get("large") or cover.get("medium") or "", url=d.get("url") or "")

    async def search(self, title: str = "", author: str = "", limit: int = 10) -> list[BookHit]:
        params = {"limit": limit, "fields": "key,title,subtitle,author_name,first_publish_year,isbn,publisher,language,cover_i,number_of_pages_median"}
        if title:
            params["title"] = title
        if author:
            params["author"] = author
        r = await self._get(f"{OL_BASE}/search.json", params)
        if r.status_code == 404:
            return []
        out = []
        for d in (r.json() or {}).get("docs") or []:
            i13, i10 = _isbns(d.get("isbn"))
            lang = (d.get("language") or [""])[0]
            out.append(BookHit(self.key, isbn13=i13, isbn10=i10, title=d.get("title") or "", subtitle=d.get("subtitle") or "",
                               authors=d.get("author_name") or [], publisher=(d.get("publisher") or [""])[0], year=d.get("first_publish_year"),
                               language=LANGS.get(lang, lang), pages=d.get("number_of_pages_median"),
                               cover_url=f"{OL_COVERS}/b/id/{d['cover_i']}-L.jpg" if d.get("cover_i") else "",
                               url=f"{OL_BASE}{d.get('key', '')}"))
        return out


# ------------------------------------------------------------------ Google Books
class GoogleBooks(_Source):
    key = "google"
    name = "Google Books"

    def _params(self, q: str, limit: int) -> dict:
        p = {"q": q, "maxResults": min(limit, 20), "printType": "books"}
        if self.conf.get("google_key"):
            p["key"] = self.conf["google_key"]
        return p

    def _hit(self, it: dict) -> BookHit:
        v = it.get("volumeInfo") or {}
        ids = {i.get("type"): i.get("identifier") for i in v.get("industryIdentifiers") or []}
        i13, i10 = _isbns([ids.get("ISBN_13"), ids.get("ISBN_10")])
        img = (v.get("imageLinks") or {})
        cover = img.get("thumbnail") or img.get("smallThumbnail") or ""
        if cover:
            cover = cover.replace("http://", "https://").replace("&edge=curl", "")
        return BookHit(self.key, isbn13=i13, isbn10=i10, title=v.get("title") or "", subtitle=v.get("subtitle") or "",
                       authors=v.get("authors") or [], publisher=v.get("publisher") or "", year=_year(v.get("publishedDate")),
                       language=(v.get("language") or "")[:5], pages=v.get("pageCount") or None,
                       genre=", ".join((v.get("categories") or [])[:2]), cover_url=cover, url=v.get("infoLink") or "")

    async def isbn(self, isbn13: str) -> BookHit | None:
        r = await self._get(GB_BASE, self._params(f"isbn:{isbn13}", 5))
        items = (r.json() or {}).get("items") or [] if r.status_code == 200 else []
        return self._hit(items[0]) if items else None

    async def search(self, title: str = "", author: str = "", limit: int = 10) -> list[BookHit]:
        q = " ".join(p for p in ((f'intitle:"{title}"' if title else ""), (f'inauthor:"{author}"' if author else "")) if p)
        if not q:
            return []
        r = await self._get(GB_BASE, self._params(q, limit))
        return [self._hit(it) for it in (r.json() or {}).get("items") or []] if r.status_code == 200 else []


# ------------------------------------------------------------------ Deutsche Nationalbibliothek (SRU, MARC21-xml)
class Dnb(_Source):
    key = "dnb"
    name = "Deutsche Nationalbibliothek"

    @staticmethod
    def _sub(rec, tag: str, code: str) -> list[str]:
        out = []
        for df in rec.findall(f"{MARC}datafield[@tag='{tag}']"):
            for sf in df.findall(f"{MARC}subfield[@code='{code}']"):
                if sf.text and sf.text.strip():
                    out.append(sf.text.strip())
        return out

    def parse(self, xml_text: str) -> list[BookHit]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise ProviderError("Deutsche Nationalbibliothek: unerwartete Antwort.") from e
        out = []
        for rec in root.iter(f"{MARC}record"):
            s = lambda tag, code: (self._sub(rec, tag, code) or [""])[0]  # noqa: E731
            isbn_raw = self._sub(rec, "020", "a") + self._sub(rec, "020", "9")
            i13, i10 = _isbns([re.split(r"\s", x)[0] for x in isbn_raw])
            title = s("245", "a").rstrip(" /:;")
            part = " ".join(p for p in (s("245", "n"), s("245", "p")) if p)
            if part:
                title = f"{title}. {part}".strip()
            authors = [invert_name(a) for a in self._sub(rec, "100", "a")]
            for df in rec.findall(f"{MARC}datafield[@tag='700']"):
                roles = [sf.text for sf in df.findall(f"{MARC}subfield[@code='4']")]
                if not roles or "aut" in roles:
                    n = df.find(f"{MARC}subfield[@code='a']")
                    if n is not None and n.text:
                        authors.append(invert_name(n.text))
            series, number = s("490", "a"), s("490", "v")
            if not series:
                series, number = s("830", "a"), s("830", "v")
            m = re.search(r"(\d+(?:[.,]\d+)?)", number or "")
            lang = s("041", "a")
            extent = s("300", "a")
            out.append(BookHit(self.key, isbn13=i13, isbn10=i10, title=title, subtitle=s("245", "b").rstrip(" /:;"), authors=authors,
                               publisher=s("264", "b") or s("260", "b"), year=_year(s("264", "c") or s("260", "c")),
                               edition=s("250", "a"), language=LANGS.get(lang, lang), pages=_pages(extent), series=series.rstrip(" ;"),
                               series_number=m.group(1) if m else number, binding=_binding(" ".join(self._sub(rec, "020", "c") + [extent])),
                               genre=s("655", "a") or s("689", "a")))
        return out

    async def _sru(self, query: str, limit: int) -> list[BookHit]:
        r = await self._get(DNB_SRU, {"version": "1.1", "operation": "searchRetrieve", "query": query, "recordSchema": "MARC21-xml",
                                      "maximumRecords": limit})
        return self.parse(r.text) if r.status_code == 200 else []

    async def isbn(self, isbn13: str) -> BookHit | None:
        hits = await self._sru(f"num={isbn13}", 3)
        if not hits:
            return None
        h = hits[0]
        if not h.cover_url and h.isbn13:
            h.cover_url = f"{DNB_COVER}?isbn={h.isbn13}"  # Cover der MVB (nicht für jedes Buch vorhanden)
        return h

    async def search(self, title: str = "", author: str = "", limit: int = 10) -> list[BookHit]:
        clean = lambda v: re.sub(r'["()=<>]', " ", v).strip()  # noqa: E731
        parts = [f'tit="{clean(title)}"' if title else "", f'per="{clean(author)}"' if author else ""]
        q = " and ".join(p for p in parts if p)
        return await self._sru(q, limit) if q else []


SOURCES: dict[str, type[_Source]] = {"dnb": Dnb, "openlibrary": OpenLibrary, "google": GoogleBooks}


def active_sources(conf: dict, client: httpx.AsyncClient | None = None) -> list[_Source]:
    return [SOURCES[k](conf, client) for k in conf.get("book_order") or list(SOURCES)
            if k in SOURCES and (conf.get("book_sources") or {}).get(k, True)]


async def lookup_isbn(conf: dict, isbn: str, client: httpx.AsyncClient | None = None) -> tuple[dict | None, list[str]]:
    """Vorschlagsdaten zur ISBN (zusammengeführt) und Hinweise. Speichert nichts."""
    from ..collection import normalize_isbn

    i13, i10 = normalize_isbn(isbn)
    if not i13:
        raise ProviderError(f"„{isbn}“ ist keine gültige ISBN.")
    hits, notes = [], []
    for src in active_sources(conf, client):
        try:
            h = await src.isbn(i13)
        except ProviderError as e:
            notes.append(str(e))
            continue
        if h:
            h.isbn13, h.isbn10 = h.isbn13 or i13, h.isbn10 or (i10 or "")
            hits.append(h)
    data = merge(hits)
    if data:
        data["isbn13"], data["isbn10"] = i13, i10 or data.get("isbn10") or ""
    return data, notes


async def search(conf: dict, title: str = "", author: str = "", client: httpx.AsyncClient | None = None,
                 limit: int = 10) -> tuple[list[dict], list[str]]:
    hits, notes = [], []
    for src in active_sources(conf, client):
        try:
            hits += [h.to_dict() for h in await src.search(title, author, limit)]
        except ProviderError as e:
            notes.append(str(e))
    return hits, notes


# ------------------------------------------------------------------ Marktwert (nur Schnittstelle)
class BookMarketProvider:
    """Optionaler Marktwert-Provider für Bücher. Eigene Klassen in BOOK_MARKET_PROVIDERS eintragen."""
    key = ""
    name = ""
    fields: tuple[tuple[str, str], ...] = ()  # (Schlüssel, Beschriftung) der Zugangsdaten
    note = ""

    def __init__(self, conf: dict | None = None, client: httpx.AsyncClient | None = None):
        self.conf = conf or {}
        self.client = client

    def configured(self) -> bool:
        return all(self.conf.get(k) for k, _ in self.fields)

    async def quote(self, book: dict) -> tuple[float, str] | None:
        """(Wert, Währung) für ein Buch (Felder wie book_json) oder None, wenn nichts gefunden."""
        raise NotImplementedError


BOOK_MARKET_PROVIDERS: dict[str, type[BookMarketProvider]] = {}


def book_market_provider(conf: dict, client: httpx.AsyncClient | None = None) -> BookMarketProvider | None:
    cls = BOOK_MARKET_PROVIDERS.get(conf.get("book_market") or "")
    if cls is None:
        return None
    p = cls(conf.get("book_market_conf") or {}, client)
    return p if p.configured() else None
