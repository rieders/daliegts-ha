"""Stromversorgung der WLED-Controller über eine WiFi-Steckdose.

Die Steckdose geht an, sobald das Lagersystem benutzt wird (LED-Anzeige, optional schon beim Öffnen von Suche/Orten), und nach
einer einstellbaren Zeit ohne Nutzung wieder aus. Unterstützt werden Shelly (Gen1 und Gen2+), Tasmota, Home Assistant
(beliebige Steckdose als switch/light/input_boolean) sowie freie Webhook-URLs für alles andere.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from ..security import get_setting, set_setting

log = logging.getLogger("lagerwerk.power")

SETTING_KEY = "power.config"
KINDS = {
    "shelly1": "Shelly Gen1 (HTTP)",
    "shelly2": "Shelly Gen2/Gen3/Plus (RPC)",
    "tasmota": "Tasmota",
    "homeassistant": "Home Assistant (Schalter)",
    "webhook": "Webhook / freie URLs",
}
DEFAULTS: dict = {
    "enabled": False, "kind": "shelly2", "host": "", "channel": 0, "user": "", "password": "",
    "on_url": "", "off_url": "", "method": "GET",
    "ha_url": "", "ha_token": "", "entity_id": "",
    "boot_wait_s": 10, "idle_min": 15, "wake_on_pages": False,
}
INT_LIMITS = {"channel": (0, 15), "boot_wait_s": (0, 120), "idle_min": (1, 1440)}
SECRET_KEYS = ("password", "ha_token")
WAKE_PREFIXES = ("/suche", "/orte", "/wand", "/scan")


def clean(raw: dict | None) -> dict:
    """Prüft und ergänzt eine Konfiguration (unbekannte Schlüssel fliegen raus, Zahlen werden begrenzt)."""
    cfg = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        if raw is None or k not in raw:
            continue
        v = raw[k]
        if isinstance(default, bool):
            cfg[k] = v in (True, "1", 1, "on", "true", "ja")
        elif isinstance(default, int):
            lo, hi = INT_LIMITS[k]
            try:
                cfg[k] = max(lo, min(int(v), hi))
            except (TypeError, ValueError):
                cfg[k] = default
        else:
            cfg[k] = str(v or "").strip()
    if cfg["kind"] not in KINDS:
        cfg["kind"] = DEFAULTS["kind"]
    cfg["method"] = "POST" if cfg["method"].upper() == "POST" else "GET"
    cfg["host"] = cfg["host"].replace("http://", "").replace("https://", "").strip("/ ")
    return cfg


def load(db: Session) -> dict:
    return clean(get_setting(db, SETTING_KEY, None))


def save(db: Session, raw: dict, keep_secrets_from: dict | None = None) -> dict:
    """Speichert die Konfiguration. Leere Geheimnisfelder behalten den bisherigen Wert."""
    cfg = clean(raw)
    old = keep_secrets_from if keep_secrets_from is not None else load(db)
    for k in SECRET_KEYS:
        if not cfg[k] and not raw.get(f"clear_{k}"):
            cfg[k] = old.get(k, "")
    set_setting(db, SETTING_KEY, cfg)
    return cfg


def public(cfg: dict) -> dict:
    """Konfiguration für die Oberfläche ohne Geheimnisse."""
    out = dict(cfg)
    for k in SECRET_KEYS:
        out[f"has_{k}"] = bool(out.pop(k, ""))
    return out


# ------------------------------------------------------------------ Schalten
def build_request(cfg: dict, on: bool) -> tuple[str, str, dict, dict | None]:
    """(Methode, URL, Header, JSON-Körper) für das Ein-/Ausschalten. Wirft ValueError bei unvollständiger Konfiguration."""
    kind, host, ch = cfg["kind"], cfg["host"], cfg["channel"]
    if kind == "shelly1":
        _need(host, "Adresse der Steckdose")
        return "GET", f"http://{host}/relay/{ch}?turn={'on' if on else 'off'}", {}, None
    if kind == "shelly2":
        _need(host, "Adresse der Steckdose")
        return "GET", f"http://{host}/rpc/Switch.Set?id={ch}&on={'true' if on else 'false'}", {}, None
    if kind == "tasmota":
        _need(host, "Adresse der Steckdose")
        power = "Power" if ch == 0 else f"Power{ch}"
        url = f"http://{host}/cm?cmnd={quote(power + (' On' if on else ' Off'))}"
        if cfg["user"]:
            url += f"&user={quote(cfg['user'])}&password={quote(cfg['password'])}"
        return "GET", url, {}, None
    if kind == "homeassistant":
        entity = _need(cfg["entity_id"], "Entität (z. B. switch.lagersteckdose)")
        if "." not in entity:
            raise ValueError("Die Entität muss so aussehen: switch.name")
        token = cfg["ha_token"] or os.environ.get("SUPERVISOR_TOKEN", "")
        base = (cfg["ha_url"] or ("http://supervisor/core" if os.environ.get("SUPERVISOR_TOKEN") else "")).rstrip("/")
        _need(base, "Home-Assistant-Adresse (z. B. http://homeassistant.local:8123)")
        _need(token, "Zugriffstoken (langlebig) von Home Assistant")
        domain = entity.split(".")[0]
        base = base[:-4] if base.endswith("/api") else base
        return "POST", f"{base}/api/services/{domain}/{'turn_on' if on else 'turn_off'}", {"Authorization": f"Bearer {token}"}, {"entity_id": entity}
    url = _need(cfg["on_url"] if on else cfg["off_url"], "Adresse zum Einschalten" if on else "Adresse zum Ausschalten")
    return cfg["method"], url, {}, None


def _need(v: str, what: str) -> str:
    if not v:
        raise ValueError(f"{what} fehlt")
    return v


class PowerManager:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self.cfg: dict = clean(None)
        self.is_on: bool | None = None  # None = unbekannt
        self.last_activity = time.monotonic()
        self.last_error = ""
        self.last_change: float | None = None
        self._lock = asyncio.Lock()
        self.sleep = asyncio.sleep  # in Tests ersetzbar

    def configure(self, cfg: dict) -> None:
        self.cfg = cfg
        self.last_activity = time.monotonic()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def _send(self, on: bool) -> tuple[bool, str]:
        try:
            method, url, headers, body = build_request(self.cfg, on)
        except ValueError as exc:
            return False, str(exc)
        auth = None
        if self.cfg["kind"] in ("shelly1", "shelly2") and self.cfg["user"]:
            auth = httpx.BasicAuth(self.cfg["user"], self.cfg["password"]) if self.cfg["kind"] == "shelly1" else httpx.DigestAuth(self.cfg["user"], self.cfg["password"])
        try:
            async with httpx.AsyncClient(timeout=6.0, transport=self._transport, auth=auth) as client:
                r = await client.request(method, url, headers=headers, json=body)
                r.raise_for_status()
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:300]

    async def switch(self, on: bool) -> tuple[bool, str]:
        """Schaltet die Steckdose. Rückgabe: (ok, Fehlertext)."""
        async with self._lock:
            ok, err = await self._send(on)
            self.last_error = err
            if ok:
                self.is_on = on
                self.last_change = time.time()
                self.touch()
            else:
                log.warning("Steckdose schalten fehlgeschlagen: %s", err)
            return ok, err

    async def ensure_on(self, wait_for=None) -> bool:
        """Sorgt dafür, dass die Steckdose an ist. Wartet nach dem Einschalten, bis WLED hochgefahren ist.

        wait_for: optionale async-Funktion, die True liefert, sobald der Controller antwortet (wird bis boot_wait_s wiederholt).
        """
        if not self.enabled:
            return True
        self.touch()
        if self.is_on:
            return True
        ok, _ = await self.switch(True)
        if not ok:
            return False
        waited, limit = 0.0, float(self.cfg["boot_wait_s"])
        if wait_for is None:
            await self.sleep(limit)
            return True
        while waited < limit:
            await self.sleep(1.0)
            waited += 1.0
            try:
                if await wait_for():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return True

    async def idle_tick(self, busy: bool = False) -> bool:
        """Periodisch: nach idle_min Minuten ohne Nutzung ausschalten. Rückgabe: True, wenn ausgeschaltet wurde."""
        if not self.enabled or self.is_on is False or busy:
            return False
        if time.monotonic() - self.last_activity < self.cfg["idle_min"] * 60:
            return False
        ok, _ = await self.switch(False)
        return ok

    def status(self) -> dict:
        idle_left = None
        if self.enabled and self.is_on is not False:
            idle_left = max(0, int(self.cfg["idle_min"] * 60 - (time.monotonic() - self.last_activity)))
        return {"enabled": self.enabled, "on": self.is_on, "error": self.last_error, "last_change": self.last_change, "idle_left_s": idle_left}


power = PowerManager()
