"""Bauteil-Erkennung per Foto (v0.4): Claude-Vision über den eigenen API-Schlüssel.

Die KI liefert einen Vorschlag (Typ, Wert, Gehäuse, Aufdruck, Hersteller-Nr. …) mit Sicherheitswerten.
Lagerwerk prüft Farbringe selbst nach, zeigt unsichere Felder markiert an und fragt bei fehlenden Angaben nach.
Nichts wird ohne Bestätigung angelegt.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_SIDE = 1568


class VisionError(Exception):
    pass


def load_config(db) -> dict:
    from ..security import get_setting

    conf = {"enabled": False, "model": DEFAULT_MODEL, **(get_setting(db, "vision", {}) or {})}
    conf.update(get_setting(db, "secret.vision", {}) or {})
    env_key = os.environ.get("LAGERWERK_VISION_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        conf["api_key"] = env_key
    return conf


def active(db) -> bool:
    c = load_config(db)
    return bool(c.get("enabled") and c.get("api_key"))


# ------------------------------------------------------------------ Bilder
def prepare_image(data: bytes) -> str:
    """Dreht nach EXIF, verkleinert auf max. 1568 px und liefert JPEG als Base64 (spart Kosten und Zeit)."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"Bild kann nicht gelesen werden ({type(e).__name__}).")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ Farbringe (eigene Prüfung)
COLORS = {"schwarz": 0, "braun": 1, "rot": 2, "orange": 3, "gelb": 4, "grün": 5, "gruen": 5, "blau": 6, "violett": 7, "lila": 7,
          "grau": 8, "weiß": 9, "weiss": 9, "gold": -1, "silber": -2}
TOLERANCE = {"braun": 1, "rot": 2, "grün": 0.5, "gruen": 0.5, "blau": 0.25, "violett": 0.1, "grau": 0.05, "gold": 5, "silber": 10}


def format_ohm(v: float) -> str:
    for div, suffix in ((1e6, "M"), (1e3, "k"), (1, "")):
        if v >= div:
            x = v / div
            s = f"{x:.3g}"
            return (s.replace(".", suffix) if "." in s and suffix else s + suffix) if suffix else s.replace(".", ",")
    return f"{v:g}".replace(".", ",")


def decode_bands(bands: list[str]) -> tuple[float, float | None] | None:
    """4 oder 5 Ringe -> (Ohm, Toleranz %)."""
    b = [str(x).strip().lower() for x in bands or [] if str(x).strip()]
    if len(b) not in (4, 5) or any(x not in COLORS for x in b):
        return None
    digits, mult, tol = (b[:2], b[2], b[3]) if len(b) == 4 else (b[:3], b[3], b[4])
    if any(COLORS[d] < 0 for d in digits):
        return None
    value = int("".join(str(COLORS[d]) for d in digits)) * (10 ** COLORS[mult])
    return float(value), TOLERANCE.get(tol)


# ------------------------------------------------------------------ Anfrage
def build_prompt(hint: str, categories: list[str], params: list[str]) -> str:
    cats = "\n".join(f"- {c}" for c in categories[:120])
    return f"""Du bist Experte für elektronische Bauteile (auch alte DDR-/Osteuropa-Typen) und Werkzeug.
Analysiere die Fotos eines Teils für eine Lagerverwaltung. Lies Aufdrucke, Farbringe, Gehäuseform und Beschriftungen.
Rate nicht: Was du nicht sicher erkennst, lässt du leer und stellst dazu eine kurze Rückfrage.
{('Hinweis des Benutzers: ' + hint) if hint else ''}

Antworte NUR mit einem JSON-Objekt:
{{
 "art": "bauteil|werkzeug|geraet|verbrauch|sonstiges",
 "typ": "z. B. Widerstand, Keramikkondensator, Elko, IC, Transistor, Diode, LED, Spannungsregler, Relais, Stecker",
 "name": "kurzer deutscher Artikelname, z. B. 'Widerstand 4,7 kΩ 0,25 W 1 %' oder 'NE555 Timer DIP-8'",
 "beschreibung": "1–2 Sätze",
 "kategorie": "passender Pfad aus der Liste unten (mit / getrennt) oder neuer Vorschlag",
 "hersteller": "", "mpn": "Hersteller-Teilenummer, nur wenn lesbar/eindeutig", "aufdruck": "gelesener Text wörtlich",
 "gehaeuse": "z. B. TO-92, TO-220, DIP-8, SOIC-8, 0805, axial, radial",
 "farbringe": ["nur bei Widerständen, von links, deutsche Farbnamen"],
 "parameter": [{{"name": "…", "wert": "…", "einheit": "…"}}],
 "herkunft": "DDR|UdSSR|West|… nur wenn erkennbar",
 "sicherheit": {{"typ": 0.0, "wert": 0.0, "mpn": 0.0, "gehaeuse": 0.0, "hersteller": 0.0}},
 "rueckfragen": ["was fehlt oder unsicher ist, als Frage an den Benutzer"],
 "suchbegriff": "beste Suchanfrage für Elektronik-Shops (MPN, sonst Wert + Gehäuse)"
}}
Parameter-Namen möglichst aus dieser Liste: {", ".join(params)}.
Werte ohne Einheit im Feld "wert" (z. B. "4,7k", "100n", "25"), Einheit separat (Ω, F, V, W, %, A, Hz).

Vorhandene Kategorien:
{cats}"""


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise VisionError("Die KI hat kein verwertbares Ergebnis geliefert.")
    try:
        return json.loads(m.group(0))
    except ValueError:
        raise VisionError("Die KI-Antwort war nicht lesbar – bitte erneut versuchen.")


async def analyze(images: list[bytes], conf: dict, hint: str = "", categories: list[str] | None = None, params: list[str] | None = None,
                  client: httpx.AsyncClient | None = None) -> dict:
    if not conf.get("api_key"):
        raise VisionError("Kein API-Schlüssel für die Bilderkennung hinterlegt (Verwaltung › Info-Provider).")
    if not images:
        raise VisionError("Bitte mindestens ein Foto aufnehmen.")
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": prepare_image(b)}} for b in images[:3]]
    content.append({"type": "text", "text": build_prompt(hint, categories or [], params or [])})
    body = {"model": conf.get("model") or DEFAULT_MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": conf["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=90)
    try:
        r = await client.post(API_URL, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise VisionError(f"KI nicht erreichbar ({type(e).__name__}) – hat der Server Internet?")
    finally:
        if own:
            await client.aclose()
    if r.status_code == 401:
        raise VisionError("API-Schlüssel ungültig.")
    if r.status_code == 404:
        raise VisionError(f"Modell „{body['model']}“ nicht gefunden – unter Verwaltung › Info-Provider ein anderes eintragen.")
    if r.status_code in (429, 529):
        raise VisionError("KI gerade ausgelastet – bitte gleich noch einmal versuchen.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise VisionError(f"KI-Fehler {r.status_code}: {msg}"[:300])
    text = "".join(part.get("text", "") for part in r.json().get("content", []) if part.get("type") == "text")
    return normalize_result(extract_json(text))


def normalize_result(d: dict) -> dict:
    """Ergänzt/prüft den KI-Vorschlag: Farbringe selbst dekodieren, Sicherheit und Rückfragen vereinheitlichen."""
    out = {
        "art": str(d.get("art") or "bauteil").lower(), "typ": str(d.get("typ") or ""), "name": str(d.get("name") or "").strip(),
        "beschreibung": str(d.get("beschreibung") or ""), "kategorie": str(d.get("kategorie") or ""), "hersteller": str(d.get("hersteller") or ""),
        "mpn": str(d.get("mpn") or "").strip(), "aufdruck": str(d.get("aufdruck") or ""), "gehaeuse": str(d.get("gehaeuse") or ""),
        "farbringe": [str(x) for x in (d.get("farbringe") or []) if x], "herkunft": str(d.get("herkunft") or ""),
        "parameter": [p for p in (d.get("parameter") or []) if isinstance(p, dict) and p.get("name") and str(p.get("wert", "")).strip()],
        "sicherheit": {k: float(v) for k, v in (d.get("sicherheit") or {}).items() if isinstance(v, (int, float))},
        "rueckfragen": [str(q) for q in (d.get("rueckfragen") or []) if q][:6], "suchbegriff": str(d.get("suchbegriff") or ""),
        "hinweise": [],
    }
    if out["art"] not in ("bauteil", "werkzeug", "geraet", "verbrauch", "sonstiges", "filament"):
        out["art"] = "bauteil"
    dec = decode_bands(out["farbringe"])
    if dec:
        ohm, tol = dec
        wert = format_ohm(ohm)
        existing = next((p for p in out["parameter"] if p["name"].lower() == "widerstand"), None)
        if existing is None:
            out["parameter"].insert(0, {"name": "Widerstand", "wert": wert, "einheit": "Ω"})
        elif not _same_value(existing.get("wert"), ohm):
            out["hinweise"].append(f"Farbringe ergeben {wert} Ω, die KI schrieb {existing.get('wert')} – bitte prüfen.")
            out["sicherheit"]["wert"] = min(out["sicherheit"].get("wert", 0.5), 0.4)
        if tol is not None and not any(p["name"].lower() == "toleranz" for p in out["parameter"]):
            out["parameter"].append({"name": "Toleranz", "wert": f"{tol:g}".replace(".", ","), "einheit": "%"})
    if not out["name"]:
        out["name"] = " ".join(x for x in (out["typ"], out["mpn"] or out["aufdruck"][:30]) if x) or "Unbekanntes Teil"
    if not out["mpn"] and out["art"] == "bauteil" and not any(p["name"].lower() in ("widerstand", "kapazität") for p in out["parameter"]):
        out["rueckfragen"].append("Welche Hersteller-Nr. steht auf dem Teil oder der Verpackung?")
    out["suchbegriff"] = out["suchbegriff"] or out["mpn"] or " ".join(
        [str(p.get("wert", "")) + str(p.get("einheit", "")) for p in out["parameter"][:2]] + [out["gehaeuse"]]).strip()
    return out


def _same_value(text, ohm: float) -> bool:
    from .params import parse_value

    v = parse_value(str(text or "").replace("Ω", "").replace("Ohm", "").strip())
    return v is not None and abs(v - ohm) <= ohm * 0.001


def unsure_fields(result: dict, threshold: float = 0.6) -> set[str]:
    return {k for k, v in (result.get("sicherheit") or {}).items() if v < threshold}
