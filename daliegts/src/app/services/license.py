"""Lizenz: kostenlose Grundversion und DaLiegt’s Pro (v0.22).

Ein Lizenzschlüssel ist signiert (Ed25519) und wird offline geprüft – kein „Nach-Hause-Telefonieren“.
Aufbau:  DLS1.<Daten base64url>.<Signatur base64url>
Daten (JSON): {"v":1, "id":"…", "n":"Name", "m":"E-Mail", "e":"pro", "iat":"2026-09-30", "upd":"2027-09-30" | null}

„upd“ = Updates bis: Pro gilt für alle Programmversionen, die bis zu diesem Tag erschienen sind (ältere laufen
für immer weiter). Ohne „upd“ gelten alle künftigen Versionen.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache

from sqlalchemy.orm import Session

from ..config import APP_RELEASE_DATE
from . import ed25519

SETTING = "secret.license_key"  # „secret.*“: wird nicht exportiert

# Öffentliche Schlüssel, mit denen Lizenzen signiert sein dürfen (neuer Schlüssel -> hier ergänzen, alte bleiben gültig)
PUBLIC_KEYS: list[str] = ["vvGctLeQvhSnuTRNBLkzLKmLQSaIsYLaOWVvjtx7J4E="]

# Pro-Funktionen (Schlüssel -> Anzeigename). Grundversion: alles andere.
FEATURES = {
    "labelprint": "Direktdruck auf Etikettendrucker (Brother, Zebra, TSPL, CUPS)",
    "printer3d": "3D-Drucker & Filament-Verbrauch (Creality K1/K1C, CFS)",
    "orderplan": "Bestellplaner – Teile bei möglichst wenigen Shops bündeln",
    "projects": "Projekte & Stücklisten (KiCad)",
    "homeassistant": "Home Assistant: Sprachsuche „Wo ist …?“ mit Aufleuchten",
    "wled_multi": "Mehr als ein WLED-Controller",
}
FREE_WLED_CONTROLLERS = 1


@dataclass(frozen=True)
class License:
    ok: bool
    edition: str = "free"
    name: str = ""
    email: str = ""
    lic_id: str = ""
    issued: str = ""
    updates_until: str | None = None
    error: str = ""
    covers_version: bool = True

    @property
    def issued_de(self) -> str:
        return _de(self.issued)

    @property
    def updates_de(self) -> str:
        return _de(self.updates_until)

    @property
    def pro(self) -> bool:
        return self.ok and self.edition == "pro" and self.covers_version


FREE = License(ok=False)


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def normalize(key: str) -> str:
    return "".join((key or "").split())  # Zeilenumbrüche/Leerzeichen beim Kopieren entfernen


@lru_cache(maxsize=32)
def parse(key: str, release: str = APP_RELEASE_DATE) -> License:
    key = normalize(key)
    if not key:
        return FREE
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != "DLS1":
        return License(ok=False, error="Das ist kein DaLiegt’s-Lizenzschlüssel (er beginnt mit „DLS1.“).")
    try:
        data, sig = _b64d(parts[1]), _b64d(parts[2])
    except (ValueError, TypeError):
        return License(ok=False, error="Lizenzschlüssel ist unvollständig – bitte komplett kopieren.")
    if not any(ed25519.verify(base64.b64decode(pk), data, sig) for pk in PUBLIC_KEYS):
        return License(ok=False, error="Lizenzschlüssel ist ungültig (Signatur passt nicht). Bitte komplett und unverändert einfügen.")
    try:
        d = json.loads(data)
    except ValueError:
        return License(ok=False, error="Lizenzschlüssel ist beschädigt.")
    upd = d.get("upd") or None
    covers = upd is None or release <= upd
    lic = License(ok=True, edition=d.get("e", "pro"), name=d.get("n", ""), email=d.get("m", ""), lic_id=d.get("id", ""),
                  issued=d.get("iat", ""), updates_until=upd, covers_version=covers)
    if not covers:
        return replace(lic, error=f"Die Lizenz enthält Updates bis {_de(upd)}. Diese Version ist vom {_de(release)} – "
                                                     "bitte Update-Zeitraum verlängern oder die vorherige Version weiter nutzen.")
    return lic


def _de(iso: str | None) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(iso or "")


def make_key(secret: bytes, name: str, email: str = "", lic_id: str = "", issued: str | None = None, updates_until: str | None = None,
             edition: str = "pro") -> str:
    """Neuen Lizenzschlüssel signieren (nur mit dem privaten Schlüssel – der liegt nicht im Programm)."""
    data = {"v": 1, "id": lic_id, "n": name, "m": email, "e": edition, "iat": issued or date.today().isoformat(), "upd": updates_until}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return f"DLS1.{_b64e(raw)}.{_b64e(ed25519.sign(secret, raw))}"


def current(db: Session) -> License:
    from ..security import get_setting

    return parse(normalize(get_setting(db, SETTING, "") or ""))


def is_pro(db: Session) -> bool:
    return current(db).pro


def has(db: Session, feature: str) -> bool:
    return feature not in FEATURES or is_pro(db)


class ProRequired(Exception):
    def __init__(self, feature: str):
        self.feature = feature


def check(db: Session, feature: str) -> None:
    if not has(db, feature):
        raise ProRequired(feature)


def require_pro(feature: str):
    """FastAPI-Abhängigkeit: Route nur mit Pro-Lizenz."""
    from fastapi import Depends

    from ..db import get_db

    def dep(db: Session = Depends(get_db)) -> None:
        check(db, feature)

    return dep
