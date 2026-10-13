"""Webshops ohne offizielle Schnittstelle (Reichelt, Pollin): Auslesen der Shopseiten.

Achtung: Kein offizielles API – wenn ein Shop seine Seiten umbaut, liefert der Provider eine Fehlermeldung,
bis der Parser angepasst ist. Abrufe erfolgen sparsam (nur auf Knopfdruck bzw. langsam im Nachtlauf).
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from . import Hit, Provider, ProviderError, currency_code, normalize_mpn, parse_int, parse_price


def _meta(soup, **attrs) -> str:
    el = soup.find("meta", attrs=attrs)
    return (el.get("content") or "").strip() if el else ""


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def json_ld_products(soup) -> list[dict]:
    """schema.org-Product-Blöcke (JSON-LD) – viele Shops liefern sie zusätzlich aus."""
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                if d.get("@type") in ("Product", ["Product"]):
                    out.append(d)
                stack.extend(v for k, v in d.items() if k == "@graph")
    return out


def apply_json_ld(hit: Hit, p: dict) -> None:
    hit.name = hit.name or str(p.get("name") or "")
    hit.sku = hit.sku or str(p.get("sku") or "")
    hit.mpn = hit.mpn or str(p.get("mpn") or "")
    brand = p.get("brand")
    hit.manufacturer = hit.manufacturer or (brand.get("name", "") if isinstance(brand, dict) else str(brand or ""))
    img = p.get("image")
    hit.image = hit.image or (img[0] if isinstance(img, list) and img else img if isinstance(img, str) else "")
    hit.description = hit.description or str(p.get("description") or "")[:500]
    offers = p.get("offers")
    offers = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offers, dict) and not hit.prices:
        price = parse_price(offers.get("price") or offers.get("lowPrice"))
        if price is not None:
            hit.prices = [(1.0, price)]
            hit.currency = currency_code(offers.get("priceCurrency"))


class ReicheltProvider(Provider):
    key = "reichelt"
    name = "Reichelt"
    supplier_name = "Reichelt"
    kind = "webshop"
    delay = 5.0
    base = "https://www.reichelt.com/de/de"
    note = "Liest die Shopseiten aus (kein offizielles API)."

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        http = await self._http()
        r = await http.get(f"{self.base}/shop/search/{quote(query.strip())}")
        self.check(r, "Reichelt-Suche")
        hits = self.parse_search(r.text, str(r.url))
        if not hits and "/shop/product/" in str(r.url):  # direkte Weiterleitung auf ein Produkt
            h = self.parse_product(r.text, str(r.url))
            return [h] if h else []
        details = []
        for h in hits[: min(limit, 4)]:  # Preise stehen erst auf der Produktseite – nur die ersten laden
            if h.url:
                try:
                    d = await self._product(h.url)
                    details.append(d or h)
                    continue
                except ProviderError:
                    pass
            details.append(h)
        return details + hits[len(details):limit]

    async def _product(self, url: str) -> Hit | None:
        http = await self._http()
        r = await http.get(url)
        self.check(r, "Reichelt-Produktseite")
        return self.parse_product(r.text, str(r.url))

    async def get(self, sku: str, url: str = "") -> Hit | None:
        if url and "reichelt." in url:
            h = await self._product(url)
            if h:
                return h
        return await super().get(sku)

    def parse_search(self, html: str, page_url: str = "") -> list[Hit]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for box in soup.select("div.al_gallery_article"):
            sku = _meta(box, itemprop="sku")
            link = box.select_one("a.al_artinfo_link")
            img = box.select_one("div.al_artlogo img")
            if not sku and not link:
                continue
            out.append(Hit(self.key, sku=sku, name=_meta(box, itemprop="name") or _text(link),
                           url=urljoin(page_url or self.base, link.get("href", "")) if link else "",
                           image=(img.get("data-original") or img.get("src") or "") if img else ""))
        return out

    def parse_product(self, html: str, page_url: str = "") -> Hit | None:
        soup = BeautifulSoup(html, "html.parser")
        h = Hit(self.key, sku=_meta(soup, itemprop="sku"), url=page_url)
        h.name = _meta(soup, itemprop="name") or _meta(soup, property="og:title")
        h.description = _text(soup.select_one('p[itemprop="description"]'))[:500]
        h.image = _meta(soup, property="og:image")
        price = parse_price(_meta(soup, itemprop="price"))
        if price is not None:
            h.prices = [(1.0, price)]
        h.currency = currency_code(_meta(soup, itemprop="priceCurrency") or "EUR")
        breaks = []
        for a in soup.select("div.discountValue a.inline-block"):
            q = parse_int(_text(a.select_one("span.block")))
            p = parse_price(_text(a.select_one("p.productPrice")))
            if q and p is not None:
                breaks.append((float(q), p))
        if breaks:
            h.prices = sorted({*h.prices, *breaks})
        mpn_el = soup.select_one('meta[itemprop="url"] + small')
        if mpn_el:
            h.mpn = re.sub(r"^(Herst\.?-?Nr\.?|MPN)[:\s]*", "", _text(mpn_el), flags=re.I)
        ds = soup.select_one("div.articleDatasheet a")
        h.datasheet = urljoin(page_url, ds.get("href", "")) if ds else ""
        for p in json_ld_products(soup):
            apply_json_ld(h, p)
        if not h.sku:
            m = re.search(r"Art\.?-?Nr\.?:?\s*</?[^>]*>?\s*([A-Z0-9][\w .\-/]+)", html)
            h.sku = m.group(1).strip() if m else ""
        return h if (h.sku or h.name) else None


class PollinProvider(Provider):
    key = "pollin"
    name = "Pollin"
    supplier_name = "Pollin"
    kind = "webshop"
    delay = 5.0
    base = "https://www.pollin.de"
    note = "Liest die Shopseiten aus (kein offizielles API)."

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        http = await self._http()
        r = await http.get(f"{self.base}/search", params={"search": query.strip()})
        self.check(r, "Pollin-Suche")
        hits = self.parse_search(r.text, str(r.url))
        if not hits and "/p/" in str(r.url):
            h = self.parse_product(r.text, str(r.url))
            return [h] if h else []
        details = []
        for h in hits[: min(limit, 4)]:
            try:
                d = await self._product(h.url) if h.url else None
            except ProviderError:
                d = None
            details.append(d or h)
        return details + hits[len(details):limit]

    async def _product(self, url: str) -> Hit | None:
        http = await self._http()
        r = await http.get(url)
        self.check(r, "Pollin-Produktseite")
        return self.parse_product(r.text, str(r.url))

    async def get(self, sku: str, url: str = "") -> Hit | None:
        if url and "pollin." in url:
            h = await self._product(url)
            if h:
                return h
        hits = await self.search(sku, 6)
        want = normalize_mpn(sku)
        return next((h for h in hits if normalize_mpn(h.sku) == want), None)

    def parse_search(self, html: str, page_url: str = "") -> list[Hit]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for box in soup.select("div.product-box"):
            link = box.select_one("a.product-name")
            if not link:
                continue
            img = box.select_one("img.product-image")
            m = re.search(r"-(\d{6})(?:/|$|\?)", link.get("href", ""))
            out.append(Hit(self.key, sku=m.group(1) if m else _meta(box, itemprop="productID"), name=_text(link),
                           url=urljoin(page_url or self.base, link.get("href", "")), image=img.get("src", "") if img else ""))
        return out

    def parse_product(self, html: str, page_url: str = "") -> Hit | None:
        soup = BeautifulSoup(html, "html.parser")
        h = Hit(self.key, sku=_text(soup.select_one('span[itemprop="sku"]')), url=_meta(soup, property="product:product_link") or page_url)
        h.name = _meta(soup, property="og:title")
        h.description = _meta(soup, property="og:description")[:500]
        h.manufacturer = _meta(soup, property="product:brand")
        h.image = _meta(soup, property="og:image")
        price = parse_price(_meta(soup, itemprop="highPrice") or _meta(soup, property="product:price:amount"))
        if price is not None:
            h.prices = [(1.0, price)]
        h.currency = currency_code(_meta(soup, property="product:price:currency") or "EUR")
        ds = soup.select_one("div.pol-product-detail-download-files a")
        h.datasheet = urljoin(page_url, ds.get("href", "")) if ds else ""
        for row in soup.select("table.product-detail-properties-table tr.properties-row"):
            label, value = _text(row.select_one("th.properties-label")).lower(), _text(row.select_one("td.properties-value"))
            if "hersteller" in label and ("nummer" in label or "teil" in label or "bezeichnung" in label):
                h.mpn = value
        for p in json_ld_products(soup):
            apply_json_ld(h, p)
        if not h.sku:
            m = re.search(r"-(\d{6})(?:/|$|\?)", page_url)
            h.sku = m.group(1) if m else ""
        return h if (h.sku or h.name) else None
