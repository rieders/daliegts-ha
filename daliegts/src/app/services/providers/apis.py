"""Distributoren mit Schnittstelle: Mouser, DigiKey, TME (kostenloser Schlüssel) und LCSC (inoffiziell, ohne Schlüssel)."""
from __future__ import annotations

import base64
import time
from urllib.parse import quote

from . import Hit, Provider, ProviderError, currency_code, normalize_mpn, parse_int, parse_price


def _find_lists(obj, key: str) -> list[dict]:
    """Sucht rekursiv nach Listen von Objekten, die `key` enthalten (robust gegen kleine Strukturänderungen)."""
    out, stack = [], [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            if cur and all(isinstance(x, dict) for x in cur) and any(key in x for x in cur):
                out.extend(x for x in cur if key in x)
            else:
                stack.extend(cur)
        elif isinstance(cur, dict):
            if key in cur and isinstance(cur.get(key), (str, int, float, dict)):
                out.append(cur)
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
    seen, uniq = set(), []
    for d in out:
        if id(d) not in seen:
            seen.add(id(d))
            uniq.append(d)
    return uniq


# ------------------------------------------------------------------ LCSC (inoffiziell)
class LcscProvider(Provider):
    key = "lcsc"
    name = "LCSC"
    supplier_name = "LCSC"
    delay = 2.0
    base = "https://wmsc.lcsc.com/ftps/wm"
    note = "Inoffizielle Schnittstelle, ohne Schlüssel; kann sich ändern."

    async def _json(self, path: str, params: dict) -> dict:
        http = await self._http()
        r = await http.get(f"{self.base}{path}", params=params, cookies={"currencyCode": "EUR"})
        self.check(r, "LCSC")
        try:
            return r.json()
        except ValueError:
            raise ProviderError("LCSC: unerwartete Antwort (kein JSON).")

    def to_hit(self, p: dict) -> Hit:
        prices = [(float(x.get("ladder") or 1), float(x.get("productPrice") or x.get("usdPrice") or 0))
                  for x in p.get("productPriceList") or [] if x.get("productPrice") is not None or x.get("usdPrice") is not None]
        cur = next((x.get("currencySymbol") for x in p.get("productPriceList") or [] if x.get("currencySymbol")), "US$")
        images = p.get("productImages") or []
        code = str(p.get("productCode") or "")
        return Hit(self.key, sku=code, mpn=str(p.get("productModel") or ""), manufacturer=str(p.get("brandNameEn") or ""),
                   name=str(p.get("productModel") or code), description=str(p.get("productIntroEn") or p.get("productDescEn") or "")[:500],
                   url=f"https://www.lcsc.com/product-detail/{code}.html" if code else "",
                   image=str(p.get("productImageUrl") or (images[0] if images else "") or ""), datasheet=str(p.get("pdfUrl") or ""),
                   prices=sorted(prices), currency=currency_code(cur), stock=parse_int(p.get("stockNumber")),
                   footprint=str(p.get("encapStandard") or ""))

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        data = await self._json("/search/v3/global", {"keyword": query.strip()})
        products = _find_lists(data.get("result") or {}, "productCode")
        if not products:
            data = await self._json("/search/third", {"keyword": query.strip()})
            products = _find_lists(data.get("result") or {}, "productCode")
        hits = [self.to_hit(p) for p in products if p.get("productModel") or p.get("productPriceList")]
        return hits[:limit]

    async def get(self, sku: str, url: str = "") -> Hit | None:
        data = await self._json("/product/detail", {"productCode": sku.strip()})
        p = data.get("result")
        return self.to_hit(p) if isinstance(p, dict) and p.get("productCode") else None


# ------------------------------------------------------------------ Mouser
class MouserProvider(Provider):
    key = "mouser"
    name = "Mouser"
    supplier_name = "Mouser"
    fields = (("api_key", "Search-API-Schlüssel"),)
    signup_url = "https://www.mouser.de/api-search/"
    base = "https://api.mouser.com/api/v2/search"

    async def _post(self, path: str, body: dict) -> dict:
        http = await self._http()
        r = await http.post(f"{self.base}/{path}", params={"apiKey": self.conf.get("api_key")}, json=body)
        self.check(r, "Mouser")
        data = r.json()
        errs = data.get("Errors") or []
        if errs:
            raise ProviderError("Mouser: " + "; ".join(str(e.get("Message") or e) for e in errs)[:200])
        return data

    def to_hit(self, p: dict) -> Hit:
        prices, cur = [], "EUR"
        for b in p.get("PriceBreaks") or []:
            q, pr = parse_int(b.get("Quantity")), parse_price(b.get("Price"))
            if q and pr is not None:
                prices.append((float(q), pr))
                cur = currency_code(b.get("Currency") or cur)
        return Hit(self.key, sku=str(p.get("MouserPartNumber") or ""), mpn=str(p.get("ManufacturerPartNumber") or ""),
                   manufacturer=str(p.get("Manufacturer") or ""), name=str(p.get("ManufacturerPartNumber") or ""),
                   description=str(p.get("Description") or "")[:500], url=str(p.get("ProductDetailUrl") or ""),
                   image=str(p.get("ImagePath") or ""), datasheet=str(p.get("DataSheetUrl") or ""), prices=sorted(prices), currency=cur,
                   stock=parse_int(p.get("AvailabilityInStock")), pack_qty=float(parse_int(p.get("Mult")) or 1))

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        data = await self._post("keyword", {"SearchByKeywordRequest": {"keyword": query.strip(), "records": limit, "startingRecord": 0,
                                                                     "searchOptions": "", "searchWithYourSignUpLanguage": "true"}})
        parts = (data.get("SearchResults") or {}).get("Parts") or []
        return [self.to_hit(p) for p in parts if p.get("MouserPartNumber") and p.get("MouserPartNumber") != "N/A"][:limit]

    async def get(self, sku: str, url: str = "") -> Hit | None:
        data = await self._post("partnumber", {"SearchByPartRequest": {"mouserPartNumber": sku.strip(), "partSearchOptions": "Exact"}})
        parts = (data.get("SearchResults") or {}).get("Parts") or []
        want = normalize_mpn(sku)
        p = next((x for x in parts if normalize_mpn(x.get("MouserPartNumber")) == want), parts[0] if len(parts) == 1 else None)
        return self.to_hit(p) if p else None


# ------------------------------------------------------------------ DigiKey (Product Information API v4, Client-Credentials)
class DigiKeyProvider(Provider):
    key = "digikey"
    name = "DigiKey"
    supplier_name = "DigiKey"
    fields = (("client_id", "Client-ID"), ("client_secret", "Client-Secret"))
    signup_url = "https://developer.digikey.com/"
    base = "https://api.digikey.com"
    _token: tuple[str, float] | None = None

    async def _headers(self) -> dict:
        tok = type(self)._token
        if not tok or tok[1] < time.time() + 30:
            http = await self._http()
            r = await http.post(f"{self.base}/v1/oauth2/token", data={"client_id": self.conf.get("client_id"),
                                                                     "client_secret": self.conf.get("client_secret"),
                                                                     "grant_type": "client_credentials"})
            self.check(r, "DigiKey-Anmeldung")
            d = r.json()
            tok = (d["access_token"], time.time() + float(d.get("expires_in") or 600))
            type(self)._token = tok
        return {"Authorization": f"Bearer {tok[0]}", "X-DIGIKEY-Client-Id": self.conf.get("client_id", ""),
                "X-DIGIKEY-Locale-Site": "DE", "X-DIGIKEY-Locale-Language": "de", "X-DIGIKEY-Locale-Currency": "EUR"}

    def to_hits(self, p: dict, only_sku: str = "") -> list[Hit]:
        desc = p.get("Description") or {}
        base = dict(mpn=str(p.get("ManufacturerProductNumber") or ""), manufacturer=str((p.get("Manufacturer") or {}).get("Name") or ""),
                    name=str(p.get("ManufacturerProductNumber") or ""),
                    description=str(desc.get("DetailedDescription") or desc.get("ProductDescription") or "")[:500],
                    url=str(p.get("ProductUrl") or ""), image=str(p.get("PhotoUrl") or ""), datasheet=str(p.get("DatasheetUrl") or ""),
                    stock=parse_int(p.get("QuantityAvailable")))
        out = []
        for v in p.get("ProductVariations") or []:
            sku = str(v.get("DigiKeyProductNumber") or "")
            if not sku or (only_sku and normalize_mpn(sku) != normalize_mpn(only_sku)):
                continue
            prices = sorted((float(b.get("BreakQuantity") or 1), float(b.get("UnitPrice") or 0)) for b in v.get("StandardPricing") or [])
            if not prices and not only_sku:
                continue  # z. B. nur als Rolle erhältlich
            out.append(Hit(self.key, sku=sku, prices=prices, currency="EUR",
                           pack_qty=float(v.get("MinimumOrderQuantity") or 1) if prices and prices[0][0] > 1 else 1, **base))
        # günstigste Einstiegsvariante (meist Gurtabschnitt) zuerst
        return sorted(out, key=lambda h: (h.prices[0][0] if h.prices else 1e9))

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        http = await self._http()
        r = await http.post(f"{self.base}/products/v4/search/keyword", headers=await self._headers(),
                            json={"Keywords": query.strip(), "Limit": limit, "Offset": 0,
                                  "FilterOptionsRequest": {"MarketPlaceFilter": "ExcludeMarketPlace"}})
        self.check(r, "DigiKey")
        data = r.json()
        products = (data.get("ExactMatches") or []) + (data.get("Products") or [])
        hits, seen = [], set()
        for p in products:
            for h in self.to_hits(p)[:1]:
                if h.sku not in seen:
                    seen.add(h.sku)
                    hits.append(h)
        return hits[:limit]

    async def get(self, sku: str, url: str = "") -> Hit | None:
        http = await self._http()
        r = await http.get(f"{self.base}/products/v4/search/{quote(sku.strip(), safe='')}/productdetails", headers=await self._headers())
        if r.status_code == 404:
            return None
        self.check(r, "DigiKey")
        p = r.json().get("Product") or {}
        hits = self.to_hits(p, only_sku=sku)
        return hits[0] if hits else None


# ------------------------------------------------------------------ TME (neue REST-API mit OAuth)
class TmeProvider(Provider):
    key = "tme"
    name = "TME"
    supplier_name = "TME"
    fields = (("token", "Anwendungs-Token"), ("secret", "Anwendungs-Secret"))
    signup_url = "https://developers.tme.eu/"
    base = "https://api.tme.eu"
    _token: tuple[str, float] | None = None

    async def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        http = await self._http()
        tok = type(self)._token
        if not tok or tok[1] < time.time() + 30:
            basic = base64.b64encode(f"{self.conf.get('token')}:{self.conf.get('secret')}".encode()).decode()
            r = await http.post(f"{self.base}/auth/token", headers={"Authorization": f"Basic {basic}"}, data={"grant_type": "client_credentials"})
            self.check(r, "TME-Anmeldung")
            d = r.json()
            tok = (d["access_token"], time.time() + float(d.get("expires_in") or 600))
            type(self)._token = tok
        r = await http.get(f"{self.base}/{path}", params=params, headers={"Authorization": f"Bearer {tok[0]}", "Accept-Language": "de"})
        self.check(r, "TME")
        return r.json()

    @staticmethod
    def _prices(el: dict) -> list[tuple[float, float]]:
        rows = _find_lists(el, "price") + _find_lists(el, "price_value")
        out = set()
        for p in rows:
            q = parse_int(p.get("amount") or p.get("quantity"))
            v = parse_price(p.get("price") if not isinstance(p.get("price"), dict) else p["price"].get("value"))
            v = v if v is not None else parse_price(p.get("price_value"))
            if q and v is not None:
                out.add((float(q), v))
        return sorted(out)

    def to_hit(self, el: dict, data: dict | None = None) -> Hit:
        sym = str(el.get("symbol") or "")
        mans = el.get("manufacturer_symbols") or el.get("original_symbol") or ""
        mpn = mans[0] if isinstance(mans, list) and mans else str(mans or "")
        photo = ((el.get("assets") or {}).get("primary_photo") or {}).get("prime") or el.get("photo") or ""
        h = Hit(self.key, sku=sym, mpn=mpn, manufacturer=str((el.get("manufacturer") or {}).get("name") or el.get("producer") or ""),
                name=mpn or sym, description=str(el.get("description") or "")[:500],
                url=f"https://www.tme.eu/de/details/{quote(sym.lower(), safe='')}/" if sym else "",
                image=("https:" + photo) if str(photo).startswith("//") else str(photo))
        if data:
            h.prices = self._prices(data)
            stock = data.get("stock_quantity")
            if stock is None and isinstance(data.get("stock"), dict):
                stock = data["stock"].get("quantity") or data["stock"].get("amount")
            h.stock = parse_int(stock)
        return h

    async def _data(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        params = [("country", "DE"), ("currency", "EUR"), ("scope[]", "prices"), ("scope[]", "stock")] + [("symbols[]", s) for s in symbols]
        data = await self._get("products/data", params)
        return {str(e.get("symbol")): e for e in _find_lists(data, "symbol")}

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        data = await self._get("products/search", [("country", "DE"), ("phrase", query.strip()), ("sort[property]", "ACCURACY"),
                                                    ("sort[direction]", "desc"), ("scope[]", "products")])
        els = [e for e in _find_lists(data, "symbol") if e.get("description") is not None or e.get("manufacturer")][:limit]
        extra = await self._data([str(e["symbol"]) for e in els])
        return [self.to_hit(e, extra.get(str(e["symbol"]))) for e in els]

    async def get(self, sku: str, url: str = "") -> Hit | None:
        data = await self._get("products", [("country", "DE"), ("symbols[]", sku.strip())])
        els = [e for e in _find_lists(data, "symbol") if normalize_mpn(e.get("symbol")) == normalize_mpn(sku)]
        if not els:
            return None
        extra = await self._data([str(els[0]["symbol"])])
        return self.to_hit(els[0], extra.get(str(els[0]["symbol"])))
