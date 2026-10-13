"""Teilen (v0.28): eigene Vorlagen und Bauteile freiwillig der Community vorschlagen – und Bauteile aus der Community holen.

Es wird nie automatisch etwas gesendet. Die Seite „Teilen“ zeigt genau, was weitergegeben würde, prüft die Qualität und erzeugt
einen vorausgefüllten GitHub-Vorschlag (Issue), den der Nutzer selbst abschickt. Aufgenommen wird ein Vorschlag erst, wenn der
Betreiber des Katalog-Repositories ihn geprüft hat. Beim Import wählt jeder selbst aus, was er übernimmt; alle Werte werden bereinigt.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Item, StorageTemplate
from . import locations as locsvc
from . import template_catalog as tc
from .items import category_path, create_item, ensure_category_path, ensure_footprint, ensure_manufacturer, reindex, set_parameter

REPO = "https://github.com/rieders/daliegts-vorlagen"
PARTS_URL = "https://raw.githubusercontent.com/rieders/daliegts-vorlagen/main/index-bauteile.json"
PARTS_SETTING = "parts_catalog_url"
MAX_URL = 7000  # darüber nehmen Browser/GitHub die Adresse nicht mehr an → JSON zum Kopieren
_JUNK = re.compile(r"^(test|asdf|qwer|xxx+|aaa+|abc|foo|bar|blabla|nix|hallo|egal|\d+|\W+)\d*$", re.I)


def _junk_name(name: str) -> bool:
    n = name.strip()
    letters = [c for c in n if c.isalpha()]
    return bool(_JUNK.match(n)) or len(letters) < 3 or (len(set(n.lower())) <= 2 and len(n) > 3)


# ------------------------------------------------------------------ Vorlagen
def template_contribution(t: StorageTemplate, author: str = "") -> dict:
    """Bereinigte Vorlage im Katalogformat (ohne LED-Verkabelung, ohne Daten aus der Installation)."""
    raw = {"name": t.name, "kind": t.kind, "definition": dict(t.definition or {}), "notes": t.notes or ""}
    c = tc.clean_template(raw)
    if author.strip():
        c["author"] = author.strip()[:60]
    return {k: c[k] for k in ("name", "kind", "group", "author", "notes", "definition") if c.get(k)}


def template_problems(db: Session, c: dict) -> tuple[list[str], list[str]]:
    """(Fehler, Hinweise): Fehler verhindern den Vorschlag, Hinweise helfen beim Verbessern."""
    errs, warns = [], []
    if _junk_name(c["name"]):
        errs.append("Der Name sagt nichts aus – nenne Hersteller, Modell und Fächerzahl, z. B. „Parkside Kleinteilemagazin 33 Schubladen (30,5×41,5 cm)“.")
    d = c["definition"]
    cells = len(locsvc._cells(locsvc.FurnitureSpec(name="x", **{k: v for k, v in d.items() if k in locsvc.FurnitureSpec.__dataclass_fields__})))
    if cells < 2:
        errs.append("Eine Vorlage mit nur einem Fach ist als Vorlage nicht nützlich.")
    if db.scalar(select(StorageTemplate).where(StorageTemplate.builtin.is_(True), StorageTemplate.name == c["name"])):
        errs.append("Diese Vorlage gehört schon zum Lieferumfang von DaLiegt’s.")
    if len(c.get("notes", "")) < 15:
        errs.append("Bitte die Quelle der Maße angeben (Herstellerangabe, Händler oder selbst gemessen) – mindestens ein Satz.")
    if not (d.get("width_cm") and d.get("height_cm")):
        warns.append("Ohne Außenmaße (Breite × Höhe) wird die Vorlage weniger nützlich.")
    if "Parkside" in c["name"] and not c.get("group"):
        warns.append("Rubrik fehlt – für Sortimentskästen „Sortimentskästen“ eintragen.")
    return errs, warns


# ------------------------------------------------------------------ Bauteile
_PART_STR = {"name": 120, "category": 120, "description": 400, "footprint": 40, "manufacturer": 80, "mpn": 80, "unit": 10}


def clean_part(data: dict) -> dict:
    """Bereinigt ein geteiltes Bauteil. Wirft ValueError, wenn es unbrauchbar ist."""
    if not isinstance(data, dict):
        raise ValueError("Kein Bauteil.")
    out = {k: re.sub(r"\s+", " ", str(data.get(k) or "")).strip()[:n] for k, n in _PART_STR.items()}
    if not out["name"]:
        raise ValueError("Bauteil ohne Namen.")
    out["unit"] = out["unit"] or "Stk"
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [x for x in tags.split(",")]
    out["tags"] = [str(x).strip()[:30] for x in tags if str(x).strip()][:15]
    params = []
    for p in (data.get("params") or [])[:30]:
        if isinstance(p, dict) and str(p.get("name") or "").strip() and str(p.get("value") or "").strip():
            params.append({"name": str(p["name"]).strip()[:80], "value": str(p["value"]).strip()[:100], "unit": str(p.get("unit") or "").strip()[:20]})
    out["params"] = params
    out["author"] = str(data.get("author") or "")[:60]
    return out


def part_contribution(db: Session, item: Item, author: str = "") -> dict:
    """Nur Stammdaten – keine Bestände, Preise, Lagerorte, Notizen, Bilder oder Codes."""
    c = {
        "name": item.name, "category": category_path(item.category, " / ").replace(" / ", "/"), "description": item.description,
        "footprint": item.footprint.name if item.footprint else "", "manufacturer": item.manufacturer.name if item.manufacturer else "",
        "mpn": item.mpn, "unit": item.unit, "tags": item.tags,
        "params": [{"name": p.name, "value": p.value_text, "unit": p.unit} for p in item.parameters],
    }
    c = clean_part(c)
    c["author"] = author.strip()[:60]
    return {k: v for k, v in c.items() if v not in ("", [])}


def part_problems(db: Session, c: dict) -> tuple[list[str], list[str]]:
    from .items import load_catalog

    errs, warns = [], []
    if _junk_name(c["name"]):
        errs.append("Der Name sagt nichts aus – Typbezeichnung oder gängige Bezeichnung verwenden (z. B. „BC547B“, „Elko 100 µF 25 V“).")
    if not c.get("category"):
        errs.append("Bitte eine Kategorie angeben (z. B. „Halbleiter/Transistoren“).")
    if not (c.get("description") or c.get("mpn") or c.get("params")):
        errs.append("Es fehlen Angaben: mindestens Beschreibung, Typnummer oder ein Parameter.")
    low = c["name"].lower()
    if any(p["name"].lower() == low for p in load_catalog()[0]):
        errs.append("Dieses Bauteil steht schon im mitgelieferten Katalog.")
    if not c.get("params"):
        warns.append("Ohne Parameter (z. B. Spannung, Wert) ist das Bauteil schlecht auffindbar.")
    if not c.get("footprint"):
        warns.append("Gehäuse/Bauform fehlt.")
    return errs, warns


def import_parts(db: Session, entries: list[dict], user_id: int | None = None) -> list[str]:
    """Legt Bauteile ohne Bestand an; vorhandene (gleicher Name oder gleiche Typnummer) werden übersprungen."""
    done = []
    for e in entries:
        if db.scalar(select(Item.id).where(func.lower(Item.name) == e["name"].lower())):
            continue
        if e.get("mpn") and db.scalar(select(Item.id).where(func.lower(Item.mpn) == e["mpn"].lower())):
            continue
        cat = ensure_category_path(db, e["category"] or "Sonstiges")
        fp = ensure_footprint(db, e["footprint"])
        man = ensure_manufacturer(db, e["manufacturer"])
        item = create_item(db, e["name"], user_id, description=e["description"], kind="bauteil", category_id=cat.id if cat else None,
                           footprint_id=fp.id if fp else None, manufacturer_id=man.id if man else None, mpn=e["mpn"],
                           tags=", ".join(e["tags"]), unit=e["unit"], origin="")
        for p in e["params"]:
            set_parameter(db, item, p["name"], p["value"], p["unit"])
        reindex(db, item)
        done.append(e["name"])
    return done


def parts_url(db: Session) -> str:
    from ..security import get_setting

    return (get_setting(db, PARTS_SETTING, "") or "").strip() or PARTS_URL


def fetch_parts(url: str, client=None) -> list[dict]:
    try:
        raw = tc.download_json(url, client)["parts"]
    except KeyError as e:
        raise tc.CatalogError("Der Katalog hat nicht das erwartete Format.") from e
    out, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        try:
            p = clean_part(entry)
        except (ValueError, TypeError, AttributeError):
            continue
        if p["name"].lower() not in seen and p["category"]:
            seen.add(p["name"].lower())
            out.append(p)
    return out


# ------------------------------------------------------------------ GitHub-Vorschlag
def issue_url(kind: str, contribution: dict) -> tuple[str, bool]:
    """(Adresse, vollständig?) – bei zu langen Daten ohne Text, dann muss das JSON eingefügt werden."""
    label = "Vorlage" if kind == "vorlage" else "Bauteil"
    title = f"{label}: {contribution['name']}"[:120]
    body = ("Ich schlage dies für den Katalog vor und stimme zu, dass die Daten frei (CC0) genutzt werden dürfen.\n\n"
            "```json\n" + json.dumps(contribution, ensure_ascii=False, indent=1) + "\n```\n")
    url = f"{REPO}/issues/new?labels={quote(kind)}&title={quote(title)}&body={quote(body)}"
    if len(url) > MAX_URL:
        return f"{REPO}/issues/new?labels={quote(kind)}&title={quote(title)}", False
    return url, True
