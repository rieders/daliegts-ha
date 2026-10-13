"""Info-Provider: Preise, Bestellnummern und Artikeldaten online abrufen (v0.3).

Jeder Provider liefert `Hit`-Objekte (ein Angebot eines Shops). Welche Provider aktiv sind und die
API-Schlüssel stehen in den Einstellungen; Schlüssel liegen getrennt unter `secret.providers` und werden
nicht exportiert. Umgebungsvariablen (`LAGERWERK_PROVIDER_MOUSER_API_KEY` …) haben Vorrang.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

import httpx

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36 DaLiegts-Heimlager")


class ProviderError(Exception):
    """Verständliche Fehlermeldung eines Providers (wird dem Benutzer angezeigt)."""


@dataclass
class Hit:
    provider: str
    sku: str
    mpn: str = ""
    manufacturer: str = ""
    name: str = ""
    description: str = ""
    url: str = ""
    image: str = ""
    datasheet: str = ""
    prices: list = field(default_factory=list)  # [(ab Menge, Preis je Einheit)]
    currency: str = "EUR"
    stock: int | None = None
    pack_qty: float = 1
    footprint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["prices"] = [list(p) for p in self.prices]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Hit":
        allowed = cls.__dataclass_fields__
        h = cls(**{k: v for k, v in d.items() if k in allowed})
        h.prices = [(float(q), float(p)) for q, p in (h.prices or [])]
        return h

    def total_for(self, qty: float) -> float | None:
        """Gesamtpreis inkl. Mindestmenge und Verpackungseinheit."""
        import math

        tiers = sorted(self.prices)
        if not tiers:
            return None
        pack = self.pack_qty if self.pack_qty and self.pack_qty > 0 else 1
        order = max(qty, tiers[0][0])
        order = math.ceil(order / pack - 1e-9) * pack
        return round((self.price_for(order) or 0) * order, 4)

    def price_for(self, qty: float) -> float | None:
        tiers = sorted(self.prices)
        if not tiers:
            return None
        usable = [p for q, p in tiers if q <= max(qty, tiers[0][0])]
        return usable[-1] if usable else tiers[0][1]


def parse_price(v) -> float | None:
    """„0,12 €“, „1.234,56“, „0.12“, 0.12 -> float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.,]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d.\s]*", str(v))
    return int(re.sub(r"\D", "", m.group(0))) if m else None


def currency_code(v: str | None) -> str:
    v = (v or "").strip()
    return {"€": "EUR", "US$": "USD", "$": "USD", "£": "GBP"}.get(v, v.upper()[:3] or "EUR")


def normalize_mpn(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


class Provider:
    key = ""
    name = ""
    supplier_name = ""
    fields: tuple[tuple[str, str], ...] = ()  # (Schlüssel, Beschriftung) der benötigten Zugangsdaten
    signup_url = ""
    kind = "api"  # api | webshop
    delay = 1.0  # Pause zwischen Abrufen im Nachtlauf (Sekunden)
    note = ""

    def __init__(self, conf: dict | None = None, client: httpx.AsyncClient | None = None):
        self.conf = conf or {}
        self.client = client

    def configured(self) -> bool:
        return all(self.conf.get(k) for k, _ in self.fields)

    async def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT,
                                            "Accept-Language": "de-DE,de;q=0.9"})
        return self.client

    async def search(self, query: str, limit: int = 8) -> list[Hit]:
        raise NotImplementedError

    async def get(self, sku: str, url: str = "") -> Hit | None:
        """Einzelnes Angebot per Bestellnummer (bzw. gespeichertem Produkt-Link) – für Preis-Aktualisierung."""
        hits = await self.search(sku, 10)
        want = normalize_mpn(sku)
        return next((h for h in hits if normalize_mpn(h.sku) == want), None)

    @staticmethod
    def check(r: httpx.Response, what: str) -> None:
        if r.status_code in (401, 403):
            raise ProviderError(f"{what}: Zugriff verweigert ({r.status_code}) – Schlüssel prüfen oder Shop blockiert Abrufe.")
        if r.status_code == 429:
            raise ProviderError(f"{what}: zu viele Anfragen – später erneut versuchen.")
        if r.status_code >= 400:
            raise ProviderError(f"{what}: Fehler {r.status_code}.")


# ------------------------------------------------------------------ Registry & Konfiguration
EXTRA_PROVIDERS: dict[str, type[Provider]] = {}  # für Tests/Erweiterungen


def registry() -> dict[str, type[Provider]]:
    from .apis import DigiKeyProvider, LcscProvider, MouserProvider, TmeProvider
    from .shops import PollinProvider, ReicheltProvider

    reg = {p.key: p for p in (ReicheltProvider, PollinProvider, LcscProvider, MouserProvider, DigiKeyProvider, TmeProvider)}
    reg.update(EXTRA_PROVIDERS)
    return reg


DEFAULT_CONFIG = {"reichelt": {"enabled": False}, "pollin": {"enabled": False}, "lcsc": {"enabled": False},
                  "mouser": {"enabled": False}, "digikey": {"enabled": False}, "tme": {"enabled": False}}
DEFAULT_OPTIONS = {"nightly": False, "budget": 150, "cross_check": True, "cross_check_days": 30, "usd_eur": 0.9, "auto_on_create": True}


def load_config(db) -> tuple[dict, dict]:
    """(Provider-Konfiguration inkl. Schlüssel, allgemeine Optionen)."""
    from ...security import get_setting

    conf = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    for k, v in (get_setting(db, "providers", {}) or {}).items():
        conf.setdefault(k, {}).update(v)
    for k, v in (get_setting(db, "secret.providers", {}) or {}).items():
        conf.setdefault(k, {}).update(v)
    for env, val in os.environ.items():  # LAGERWERK_PROVIDER_MOUSER_API_KEY=…
        m = re.fullmatch(r"LAGERWERK_PROVIDER_([A-Z]+)_([A-Z_]+)", env)
        if m and val:
            conf.setdefault(m.group(1).lower(), {})[m.group(2).lower()] = val
    opts = {**DEFAULT_OPTIONS, **(get_setting(db, "providers.options", {}) or {})}
    return conf, opts


def active_providers(db, client: httpx.AsyncClient | None = None, only: list[str] | None = None) -> list[Provider]:
    conf, _ = load_config(db)
    out = []
    for key, cls in registry().items():
        c = conf.get(key, {})
        if only is not None and key not in only:
            continue
        p = cls(c, client)
        if c.get("enabled") and p.configured():
            out.append(p)
    return out


def provider_for_supplier_name(name: str) -> str | None:
    n = (name or "").lower()
    for key, cls in registry().items():
        if n == cls.supplier_name.lower() or re.match(rf"{re.escape(cls.supplier_name.lower())}\b", n):
            return key
    return None
