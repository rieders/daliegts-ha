"""Einstellungen der Sammlungs-Provider (Tonträger und Bücher, v0.33).

Öffentliche Einstellungen unter `collection`, Zugangsdaten getrennt unter `secret.collection` (werden wie alle
`secret.*`-Einstellungen nicht exportiert). Umgebungsvariablen haben Vorrang:
LAGERWERK_DISCOGS_TOKEN, LAGERWERK_GOOGLE_BOOKS_KEY.
"""
from __future__ import annotations

import os

from ...config import APP_VERSION

BOOK_SOURCES = {"dnb": "Deutsche Nationalbibliothek", "openlibrary": "Open Library", "google": "Google Books"}

DEFAULTS = {
    "musicbrainz": True,  # Suche/Titellisten über MusicBrainz + Cover Art Archive
    "discogs": True,  # Discogs (Suche nur mit Token; Marktstatistik auch ohne)
    "contact": "https://daliegts.de",  # Kontakt im User-Agent (MusicBrainz verlangt eine Kontaktmöglichkeit)
    "book_order": ["dnb", "openlibrary", "google"],
    "book_sources": {"dnb": True, "openlibrary": True, "google": True},
    "nightly_market": False,  # Marktwerte nachts aktualisieren (ältester Stand zuerst)
    "market_budget": 25,  # Tonträger pro Nacht
    "book_market": "",  # optionaler Marktwert-Provider für Bücher (Schlüssel aus books.BOOK_MARKET_PROVIDERS)
}
SECRET_FIELDS = ("discogs_token", "google_key")


def user_agent(conf: dict) -> str:
    contact = (conf.get("contact") or DEFAULTS["contact"]).strip()[:120]
    return f"DaLiegts/{APP_VERSION} ( {contact} )"


def load(db) -> dict:
    """Zusammengeführte Konfiguration inkl. Zugangsdaten (nur serverseitig verwenden)."""
    from ...security import get_setting

    conf = {**DEFAULTS, **(get_setting(db, "collection", {}) or {})}
    conf["book_sources"] = {**DEFAULTS["book_sources"], **(conf.get("book_sources") or {})}
    order = [k for k in conf.get("book_order") or [] if k in BOOK_SOURCES]
    conf["book_order"] = order + [k for k in BOOK_SOURCES if k not in order]
    secret = get_setting(db, "secret.collection", {}) or {}
    for k in SECRET_FIELDS:
        conf[k] = secret.get(k, "")
    conf["book_market_conf"] = secret.get("book_market_conf", {}) or {}
    if os.environ.get("LAGERWERK_DISCOGS_TOKEN"):
        conf["discogs_token"] = os.environ["LAGERWERK_DISCOGS_TOKEN"]
    if os.environ.get("LAGERWERK_GOOGLE_BOOKS_KEY"):
        conf["google_key"] = os.environ["LAGERWERK_GOOGLE_BOOKS_KEY"]
    return conf
