"""Neue Bauteile erkennen: Tüten-Etikett und/oder Foto -> Vorschlag für die Neuanlage (v0.4).

Der Vorschlag wird nie automatisch gespeichert; unsichere Felder und Rückfragen werden markiert.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Category, Item, ParameterTemplate
from . import infosync, vision
from .items import catalog_search, category_path
from .providers import Hit, active_providers, normalize_mpn, provider_for_supplier_name, registry

STORED_RE = re.compile(r"^images/[0-9a-f]{16}\.(webp|jpg|jpeg|png|gif|bmp)$")


def stored_image_bytes(name: str) -> bytes | None:
    if not STORED_RE.match(name or ""):
        return None
    path = (settings.media_dir / name).resolve()
    if not path.is_relative_to(settings.media_dir.resolve()) or not path.exists():
        return None
    return path.read_bytes()


def empty_suggestion() -> dict:
    return {"name": "", "beschreibung": "", "art": "bauteil", "kategorie": "", "hersteller": "", "mpn": "", "gehaeuse": "", "herkunft": "",
            "parameter": [], "qty": None, "images": [], "unsure": set(), "rueckfragen": [], "hinweise": [], "label": None,
            "rows": {}, "query": "", "existing": [], "catalog": [], "aufdruck": "", "source": []}


def category_choices(db: Session) -> list[str]:
    cats = list(db.scalars(select(Category)))
    return sorted({category_path(c, "/") for c in cats})


def param_names(db: Session) -> list[str]:
    return [p.name for p in db.scalars(select(ParameterTemplate).order_by(ParameterTemplate.name))]


def apply_label(sug: dict, lab: dict) -> None:
    sug["label"] = lab
    sug["source"].append(f"Etikett {lab.get('supplier') or lab.get('source')}")
    sug["mpn"] = sug["mpn"] or lab.get("mpn", "")
    sug["hersteller"] = sug["hersteller"] or lab.get("manufacturer", "")
    if lab.get("qty"):
        sug["qty"] = lab["qty"]
    if not sug["name"] and sug["mpn"]:
        sug["name"] = sug["mpn"]


def apply_vision(sug: dict, res: dict) -> None:
    sug["source"].append("Foto-KI")
    for key in ("name", "beschreibung", "art", "kategorie", "hersteller", "mpn", "gehaeuse", "herkunft", "aufdruck"):
        if res.get(key) and not sug.get(key):
            sug[key] = res[key]
    sug["parameter"] = sug["parameter"] or res.get("parameter", [])
    sug["unsure"] |= vision.unsure_fields(res)
    sug["rueckfragen"] += res.get("rueckfragen", [])
    sug["hinweise"] += res.get("hinweise", [])
    sug["query"] = sug["query"] or res.get("suchbegriff", "")


async def add_offers(db: Session, sug: dict) -> None:
    """Shops fragen: per Bestellnummer vom Etikett (exakt) und per Hersteller-Nr./Suchbegriff bei den übrigen."""
    lab = sug.get("label") or {}
    query = sug["mpn"] or sug["query"]
    fake_item = SimpleNamespace(mpn=sug["mpn"], manufacturer=SimpleNamespace(name=sug["hersteller"]) if sug["hersteller"] else None)
    need = float(sug["qty"] or 10)
    async with infosync.new_client() as client:
        provs = {p.key: p for p in active_providers(db, client)}
        if not provs:
            return
        found: dict[str, list[Hit] | str] = {}
        label_key = provider_for_supplier_name(lab.get("supplier", "")) if lab.get("sku") else None
        if label_key and label_key in provs:
            try:
                h = await provs[label_key].get(lab["sku"])
                found[label_key] = [h] if h else []
            except Exception as e:  # noqa: BLE001
                found[label_key] = f"{provs[label_key].name}: {e}"
        others = [p for k, p in provs.items() if k not in found]
        if query and not infosync.query_problems(query):
            found.update(await infosync.search_all(others, query, 5))
        elif query:
            sug["rueckfragen"].append("Für die Shop-Suche fehlt die Hersteller-Nr. oder ein genauer Wert mit Gehäuse.")
    rows = {}
    for key, hits in found.items():
        if isinstance(hits, str):
            rows[key] = hits
            continue
        exact, unsure = infosync.exact_matches(fake_item, hits) if sug["mpn"] else ([], False)
        ids = {id(h) for h in exact}
        rows[key] = [{"hit": h, "json": json.dumps(h.to_dict(), ensure_ascii=False),
                      "exact": (key == label_key) or (id(h) in ids and not unsure), "total": h.total_for(need)} for h in hits]
    sug["rows"] = rows
    sug["need"] = need
    # eindeutiger Treffer ergänzt leere Felder (Hersteller, Beschreibung, Gehäuse, Hersteller-Nr.)
    best = next((r["hit"] for rs in rows.values() if not isinstance(rs, str) for r in rs if r["exact"]), None)
    if best:
        sug["mpn"] = sug["mpn"] or best.mpn
        sug["hersteller"] = sug["hersteller"] or best.manufacturer
        sug["beschreibung"] = sug["beschreibung"] or best.description
        sug["gehaeuse"] = sug["gehaeuse"] or best.footprint
        if not sug["name"] or sug["name"] == lab.get("mpn"):
            sug["name"] = (best.mpn + (" – " + best.description[:60] if best.description else "")) if best.mpn else sug["name"]
        sug["unsure"] -= {"mpn", "hersteller"}


def add_existing(db: Session, sug: dict) -> None:
    if sug["mpn"]:
        want = normalize_mpn(sug["mpn"])
        sug["existing"] = [i for i in db.scalars(select(Item).where(Item.mpn != "")) if normalize_mpn(i.mpn) == want][:5]
    q = sug["mpn"] or sug["name"]
    if q and sug["art"] == "bauteil":
        sug["catalog"] = catalog_search(q, 3)


def names_for_rows() -> dict[str, str]:
    return {k: c.name for k, c in registry().items()}
