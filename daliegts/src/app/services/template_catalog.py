"""Vorlagen-Katalog (v0.27): Möbel-/Magazinvorlagen aus einem öffentlichen GitHub-Repository holen und teilen.

Der Katalog ist eine einzige JSON-Datei (index.json) mit {"version": 1, "templates": [...]}. Jede Vorlage hat dasselbe
Format wie der JSON-Export: {"name", "kind", "group", "definition", "notes", "author"}. Es werden nur Daten gelesen,
nie Code ausgeführt; alle Werte werden begrenzt und bereinigt.
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import StorageTemplate
from . import locations as locsvc

DEFAULT_URL = "https://raw.githubusercontent.com/rieders/daliegts-vorlagen/main/index.json"
SETTING = "template_catalog_url"
MAX_BYTES = 1_000_000
MAX_CELLS = 400
_ALLOWED = set(locsvc.FurnitureSpec.__dataclass_fields__) - {"name", "controller_id", "led_start"}  # keine LED-Verkabelung teilen


class CatalogError(Exception):
    pass


def clean_template(data: dict) -> dict:
    """Prüft und bereinigt eine Vorlage aus Import-Datei oder Katalog. Wirft ValueError bei ungültigen Daten."""
    if not isinstance(data, dict) or not isinstance(data.get("definition"), dict):
        raise ValueError("Vorlage braucht einen Namen und eine „definition“.")
    name = str(data.get("name") or "").strip()[:100]
    if not name:
        raise ValueError("Vorlage ohne Namen.")
    raw = data["definition"]
    definition = {k: v for k, v in raw.items() if k in _ALLOWED}
    for k in ("width_cm", "height_cm"):
        if raw.get(k) not in (None, ""):
            v = float(raw[k])
            if not 0 < v <= 1000:
                raise ValueError(f"{k} außerhalb des erlaubten Bereichs.")
            definition[k] = round(v, 1)
    group = str(data.get("group") or raw.get("group") or "").strip()[:40]
    if group:
        definition["group"] = group
    spec = locsvc.FurnitureSpec(name="x", **{k: v for k, v in definition.items() if k in locsvc.FurnitureSpec.__dataclass_fields__})
    cells = len(locsvc._cells(spec))
    if not 0 < cells <= MAX_CELLS:
        raise ValueError(f"{cells} Fächer – erlaubt sind 1 bis {MAX_CELLS}.")
    kind = str(data.get("kind") or spec.type)[:30]
    if kind not in locsvc.LOCATION_TYPES:
        kind = "magazin"
    return {"name": name, "kind": kind, "group": group, "definition": definition, "notes": str(data.get("notes") or "")[:500],
            "author": str(data.get("author") or "")[:60], "cells": cells}


def catalog_url(db: Session) -> str:
    from ..security import get_setting

    return (get_setting(db, SETTING, "") or "").strip() or DEFAULT_URL


def download_json(url: str, client: httpx.Client | None = None) -> dict:
    """Lädt eine JSON-Datei. Nur https, begrenzte Größe."""
    if not url.lower().startswith("https://"):
        raise CatalogError("Die Katalog-Adresse muss mit https:// beginnen.")
    own = client is None
    client = client or httpx.Client(timeout=8.0, follow_redirects=True)
    try:
        with client.stream("GET", url) as r:
            if r.status_code == 404:
                raise CatalogError("Katalog nicht gefunden (404). Gibt es das Repository schon und ist es öffentlich?")
            if r.status_code != 200:
                raise CatalogError(f"Katalog nicht erreichbar (HTTP {r.status_code}).")
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise CatalogError("Der Katalog ist zu groß.")
    except httpx.HTTPError as e:
        raise CatalogError(f"Katalog nicht erreichbar: {type(e).__name__}") from e
    finally:
        if own:
            client.close()
    try:
        data = json.loads(buf)
    except ValueError as e:
        raise CatalogError("Der Katalog hat nicht das erwartete Format.") from e
    if not isinstance(data, dict):
        raise CatalogError("Der Katalog hat nicht das erwartete Format.")
    return data


def fetch_index(url: str, client: httpx.Client | None = None) -> list[dict]:
    """Lädt den Katalog. Ungültige Einträge werden übersprungen."""
    try:
        raw = download_json(url, client)["templates"]
    except KeyError as e:
        raise CatalogError("Der Katalog hat nicht das erwartete Format.") from e
    out, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        try:
            t = clean_template(entry)
        except (ValueError, TypeError):
            continue
        if t["name"] not in seen:
            seen.add(t["name"])
            out.append(t)
    return out


def import_templates(db: Session, entries: list[dict]) -> list[str]:
    """Legt Vorlagen an, die es noch nicht (nach Namen) gibt. Gibt die angelegten Namen zurück."""
    done = []
    for t in entries:
        if db.scalar(select(StorageTemplate).where(StorageTemplate.name == t["name"])):
            continue
        db.add(StorageTemplate(name=t["name"], kind=t["kind"], definition=t["definition"], notes=t["notes"], builtin=False))
        done.append(t["name"])
    db.flush()
    return done
