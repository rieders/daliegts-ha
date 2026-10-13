"""Weitere 3D-Druckerfamilien (v0.23): Bambu Lab, Prusa (PrusaLink), Klipper (Moonraker), OctoPrint.

Jede Anbindung übersetzt den Druckerstand in dasselbe Format wie beim Creality K1, damit Abbuchen, Druckmodelle,
„Reicht die Spule?“ und „Noch nicht abgebucht“ für alle gleich funktionieren:
  {"state": 1 druckt | 0 bereitet vor | 5 pausiert | 4 abgebrochen, "printFileName", "printProgress" (0–100),
   "usedMaterialLength" (mm, bisher verbraucht), "usageUnknown" (True = Drucker meldet keinen Verbrauch)}
Dateien mit geplantem Verbrauch: {"path", "name", "consumables" (mm), "filamentWeight" (g)}.

Klipper meldet den tatsächlichen Verbrauch. Prusa, OctoPrint und Bambu melden nur den Fortschritt – dort wird
Fortschritt × geplanter Verbrauch der Datei gerechnet (bei abgebrochenen Drucken also anteilig).
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import ssl
import time
import zipfile

from .printer3d import BaseMonitor, _num, base

# ------------------------------------------------------------------ Modelle je Familie
KINDS = {
    "creality": ("Creality (Original-Firmware)", 9999),
    "bambu": ("Bambu Lab (LAN)", 8883),
    "prusalink": ("Prusa (PrusaLink)", 80),
    "moonraker": ("Klipper (Moonraker / Fluidd / Mainsail)", 7125),
    "octoprint": ("OctoPrint", 80),
}
MODEL_GROUPS: list[tuple[str, list[str]]] = [
    ("creality", ["K1", "K1C", "K1 SE", "K1 Max", "K2", "K2 Plus", "Hi", "Ender-3 V3 KE", "andere"]),
    ("bambu", ["A1 mini", "A1", "P1P", "P1S", "X1 Carbon", "X1E", "H2D", "anderer Bambu"]),
    ("prusalink", ["MINI+", "MK4", "MK4S", "Core One", "XL", "anderer Prusa"]),
    ("moonraker", ["Voron", "Sovol SV06 / SV07 / SV08", "Elegoo Neptune 4", "Qidi X-Plus 3 / Q1 Pro / Plus4",
                   "Creality mit Klipper (Root)", "anderer Klipper-Drucker"]),
    ("octoprint", ["Ender-3 (V2 / S1 / Pro)", "Prusa MK3S+", "Anycubic i3 Mega / Kobra", "Artillery Sidewinder",
                   "anderer Drucker mit OctoPrint"]),
]
MODEL_KIND = {m: k for k, models in MODEL_GROUPS for m in models}
MULTI_SPOOL = {"creality": "CFS", "bambu": "AMS", "prusalink": "MMU", "moonraker": "Mehrfach-Spulensystem", "octoprint": "MMU"}


def kind_of(model: str) -> str:
    return MODEL_KIND.get(model, "creality")


# ------------------------------------------------------------------ Übersetzungen (rein, gut testbar)
def moonraker_state(ps: dict, vsd: dict) -> dict:
    """Klipper/Moonraker: print_stats + virtual_sdcard -> Creality-Format (Verbrauch kommt direkt vom Drucker)."""
    state = (ps or {}).get("state") or ""
    file = (ps or {}).get("filename") or ""
    d = {"printFileName": file if state in ("printing", "paused", "cancelled", "complete", "error") else "",
         "usedMaterialLength": _num((ps or {}).get("filament_used"))}
    if state == "complete":
        d["printProgress"], d["state"] = 100, 1
    else:
        d["printProgress"] = round((_num((vsd or {}).get("progress")) or 0) * 100, 1)
        d["state"] = {"printing": 1, "paused": 5, "cancelled": 4}.get(state)
    if state == "error":
        d["err"] = {"errcode": 1}
    return d


def moonraker_entry(path: str, meta: dict) -> dict:
    return {"path": path, "name": base(path), "consumables": _num(meta.get("filament_total")),
            "filamentWeight": _num(meta.get("filament_weight_total"))}


def _progress_used(pct: float | None, planned_mm: float | None) -> float | None:
    return pct / 100.0 * planned_mm if pct is not None and planned_mm else None


def octoprint_state(j: dict) -> tuple[dict, list]:
    """OctoPrint /api/job -> Creality-Format + Datei-Eintrag (geplante Länge aus der G-Code-Analyse)."""
    job = j.get("job") or {}
    f = job.get("file") or {}
    path = f.get("path") or f.get("name") or ""
    tools = (job.get("filament") or {}) or {}
    planned = sum(_num((t or {}).get("length")) or 0 for t in tools.values()) if isinstance(tools, dict) else 0
    pct = _num((j.get("progress") or {}).get("completion"))
    st = (j.get("state") or "").lower()
    d = {"printFileName": path, "printProgress": pct or 0, "usageUnknown": not planned}
    if st.startswith("printing") or st in ("finishing", "starting"):
        d["state"] = 1
    elif st.startswith("paus"):
        d["state"] = 5
    elif st.startswith("cancel"):
        d["state"] = 4
    elif path and pct is not None and pct >= 99.9:
        d["state"], d["printProgress"] = 1, 100
    elif path and pct:
        d["state"] = 4  # bereit, Druck nicht fertig -> abgebrochen
    else:
        d["printFileName"] = ""
    if st.startswith("error") or st.startswith("offline"):
        d["err"] = {"errcode": 1}
    d["usedMaterialLength"] = _progress_used(d["printProgress"], planned) if d.get("printFileName") else None
    entries = [{"path": path, "name": f.get("name") or base(path), "consumables": planned or None}] if path else []
    return d, entries


def octoprint_files(tree: dict) -> list[dict]:
    out = []

    def walk(items):
        for it in items or []:
            if it.get("type") == "folder":
                walk(it.get("children"))
            elif it.get("type") == "machinecode":
                tools = ((it.get("gcodeAnalysis") or {}).get("filament") or {})
                mm = sum(_num((t or {}).get("length")) or 0 for t in tools.values()) if isinstance(tools, dict) else 0
                out.append({"path": it.get("path") or it.get("name"), "name": it.get("name"), "consumables": mm or None})

    walk(tree.get("files"))
    return out


def prusalink_state(status: dict, job: dict | None) -> tuple[dict, list]:
    """PrusaLink v1: /api/v1/status + /api/v1/job -> Creality-Format (Verbrauch = Fortschritt × geplanter Verbrauch)."""
    st = ((status.get("printer") or {}).get("state") or "").upper()
    job = job or {}
    f = job.get("file") or {}
    meta = f.get("meta") or {}
    path = f.get("path") or f.get("name") or ""
    if path and f.get("name") and not path.endswith(f["name"]):
        path = path.rstrip("/") + "/" + f["name"]
    name = f.get("display_name") or f.get("name") or base(path)
    planned_mm = _num(meta.get("filament used [mm]"))
    planned_g = _num(meta.get("filament used [g]"))
    pct = _num(job.get("progress") if job.get("progress") is not None else (status.get("job") or {}).get("progress"))
    d = {"printFileName": path, "printProgress": pct or 0, "usageUnknown": not planned_mm}
    if st == "PRINTING":
        d["state"] = 1
    elif st == "PAUSED":
        d["state"] = 5
    elif st == "STOPPED":
        d["state"] = 4
    elif st == "FINISHED":
        d["state"], d["printProgress"] = 1, 100
    else:
        d["printFileName"] = ""
    if st in ("ERROR", "ATTENTION"):
        d["err"] = {"errcode": 1}
    d["usedMaterialLength"] = _progress_used(d["printProgress"], planned_mm) if d.get("printFileName") else None
    entries = [{"path": path, "name": name, "consumables": planned_mm, "filamentWeight": planned_g}] if path else []
    return d, entries


def bambu_state(pr: dict, planned_mm: float | None) -> dict:
    """Bambu Lab (MQTT „print“-Bericht) -> Creality-Format."""
    gs = (pr.get("gcode_state") or "").upper()
    file = pr.get("subtask_name") or base(pr.get("gcode_file") or "")
    pct = _num(pr.get("mc_percent"))
    d = {"printFileName": file, "printProgress": pct or 0, "usageUnknown": not planned_mm}
    if gs == "RUNNING":
        d["state"] = 1
    elif gs in ("PREPARE", "SLICING"):
        d["state"] = 0
    elif gs == "PAUSE":
        d["state"] = 5
    elif gs == "FAILED":
        d["state"] = 4
    elif gs == "FINISH":
        d["state"], d["printProgress"] = 1, 100
    else:
        d["printFileName"] = ""
    d["usedMaterialLength"] = _progress_used(d["printProgress"], planned_mm) if d.get("printFileName") else None
    return d


def slice_info_usage(data: bytes, plate: int | None = None) -> tuple[float | None, float | None]:
    """Geplanter Verbrauch (g, mm) aus einer Bambu-.3mf (Metadata/slice_info.config), alle Farben zusammen."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile):
        return None, None
    plates = re.findall(r"<plate>(.*?)</plate>", xml, re.S) or [xml]
    chosen = plates[0]
    if plate:
        for pl in plates:
            m = re.search(r'key="index"\s+value="(\d+)"', pl)
            if m and int(m.group(1)) == plate:
                chosen = pl
    grams = sum(float(g) for g in re.findall(r'used_g="([\d.]+)"', chosen)) or None
    meters = sum(float(m) for m in re.findall(r'used_m="([\d.]+)"', chosen)) or None
    return grams, meters * 1000 if meters else None


def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


# ------------------------------------------------------------------ HTTP-Anbindungen (Abfrage alle 10 s)
class _PollMonitor(BaseMonitor):
    interval = 10

    def _client_kwargs(self) -> dict:
        return {}

    def base_url(self) -> str:
        return f"http://{self.host}" + (f":{self.port}" if self.port and self.port != 80 else "")

    async def poll(self, c) -> tuple[dict, list]:  # pragma: no cover - je Anbindung
        raise NotImplementedError

    async def run(self) -> None:
        import httpx

        delay, n = 5, 0
        async with httpx.AsyncClient(base_url=self.base_url(), timeout=10, verify=False, **self._client_kwargs()) as c:
            self.client = c
            while True:
                try:
                    d, entries = await self.poll(c)
                    self._record("in", json.dumps(d, ensure_ascii=False))
                    if entries:
                        await self.files(entries, full=False)
                    actions = await self.handle(d)
                    if n % 360 == 0 or "file_info" in actions:  # stündlich bzw. bei neuem Druck: Dateiliste
                        try:
                            await self.request_files()
                        except Exception:  # noqa: BLE001 - Dateiliste ist Zusatz
                            pass
                    n, delay = n + 1, 5
                    await asyncio.sleep(self.interval)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await self.mark_offline(exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120)


class MoonrakerMonitor(_PollMonitor):
    kind = "moonraker"
    _meta_for = ""

    def _client_kwargs(self) -> dict:
        return {"headers": {"X-Api-Key": self.access_code}} if self.access_code else {}

    async def _meta(self, c, path: str) -> dict:
        r = await c.get("/server/files/metadata", params={"filename": path})
        return (r.json().get("result") or {}) if r.status_code == 200 else {}

    async def poll(self, c) -> tuple[dict, list]:
        r = await c.get("/printer/objects/query?print_stats&virtual_sdcard")
        r.raise_for_status()
        status = (r.json().get("result") or {}).get("status") or {}
        d = moonraker_state(status.get("print_stats") or {}, status.get("virtual_sdcard") or {})
        entries = []
        file = d.get("printFileName")
        if file and file != self._meta_for:
            self._meta_for = file
            entries = [moonraker_entry(file, await self._meta(c, file))]
        return d, entries

    async def request_files(self) -> None:
        c = getattr(self, "client", None)
        if c is None:
            return
        r = await c.get("/server/files/list", params={"root": "gcodes"})
        files = sorted(r.json().get("result") or [], key=lambda f: -(f.get("modified") or 0))[:60]
        entries = [moonraker_entry(f["path"], await self._meta(c, f["path"])) for f in files if f.get("path")]
        if entries:
            await self.files(entries)


class OctoPrintMonitor(_PollMonitor):
    kind = "octoprint"

    def _client_kwargs(self) -> dict:
        return {"headers": {"X-Api-Key": self.access_code}} if self.access_code else {}

    async def poll(self, c) -> tuple[dict, list]:
        r = await c.get("/api/job")
        r.raise_for_status()
        return octoprint_state(r.json())

    async def request_files(self) -> None:
        c = getattr(self, "client", None)
        if c is None:
            return
        r = await c.get("/api/files", params={"recursive": "true"})
        if r.status_code == 200:
            entries = octoprint_files(r.json())
            if entries:
                await self.files(entries)


class PrusaLinkMonitor(_PollMonitor):
    kind = "prusalink"
    _job_id = None
    _job: dict | None = None

    def _client_kwargs(self) -> dict:
        import httpx

        return {"auth": httpx.DigestAuth(self.serial or "maker", self.access_code)} if self.access_code else {}

    async def poll(self, c) -> tuple[dict, list]:
        r = await c.get("/api/v1/status")
        r.raise_for_status()
        status = r.json()
        jid = (status.get("job") or {}).get("id")
        if jid is not None:
            rj = await c.get("/api/v1/job")
            if rj.status_code == 200:
                self._job, self._job_id = rj.json(), jid
        elif ((status.get("printer") or {}).get("state") or "").upper() not in ("FINISHED", "STOPPED"):
            self._job, self._job_id = None, None
        return prusalink_state(status, self._job)


# ------------------------------------------------------------------ Bambu Lab: MQTT über TLS (Port 8883), Benutzer „bblp“
def _mqtt_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _mqtt_str(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def mqtt_connect(client_id: str, user: str, password: str, keepalive: int = 60) -> bytes:
    var = _mqtt_str("MQTT") + bytes([4, 0xC2]) + keepalive.to_bytes(2, "big")
    payload = _mqtt_str(client_id) + _mqtt_str(user) + _mqtt_str(password)
    return bytes([0x10]) + _mqtt_len(len(var) + len(payload)) + var + payload


def mqtt_subscribe(topic: str, pid: int = 1) -> bytes:
    body = pid.to_bytes(2, "big") + _mqtt_str(topic) + b"\x00"
    return bytes([0x82]) + _mqtt_len(len(body)) + body


def mqtt_publish(topic: str, payload: bytes) -> bytes:
    body = _mqtt_str(topic) + payload
    return bytes([0x30]) + _mqtt_len(len(body)) + body


async def mqtt_read(reader) -> tuple[int, int, bytes]:
    head = (await reader.readexactly(1))[0]
    mult, length = 1, 0
    while True:
        b = (await reader.readexactly(1))[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return head >> 4, head & 0x0F, await reader.readexactly(length) if length else b""


def mqtt_parse_publish(flags: int, body: bytes) -> tuple[str, bytes]:
    tlen = int.from_bytes(body[:2], "big")
    topic = body[2:2 + tlen].decode("utf-8", "replace")
    rest = body[2 + tlen:]
    if (flags >> 1) & 0x03:  # QoS 1/2: Paket-ID überspringen
        rest = rest[2:]
    return topic, rest


def _bambu_ftp_get(host: str, code: str, paths: list[str]) -> bytes | None:
    """Datei per FTPS (implizit, Port 990) vom Bambu-Drucker holen – erste vorhandene der Pfade."""
    import ftplib

    class ImplicitFTPS(ftplib.FTP_TLS):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._sock = None

        @property
        def sock(self):
            return self._sock

        @sock.setter
        def sock(self, value):
            if value is not None and not isinstance(value, ssl.SSLSocket):
                value = self.context.wrap_socket(value, server_hostname=self.host)
            self._sock = value

        def ntransfercmd(self, cmd, rest=None):
            conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
            if self._prot_p:
                conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
            return conn, size

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ImplicitFTPS(context=ctx, timeout=20)
    try:
        ftp.connect(host, 990)
        ftp.login("bblp", code)
        ftp.prot_p()
        for p in paths:
            buf = io.BytesIO()
            try:
                ftp.retrbinary("RETR " + p, buf.write)
                if buf.tell():
                    return buf.getvalue()
            except ftplib.all_errors:
                continue
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            pass
    return None


class BambuMonitor(BaseMonitor):
    """Bambu Lab im LAN-Modus (Entwicklermodus): Zugangscode und Seriennummer stehen am Drucker unter Einstellungen › Netzwerk/Gerät."""
    kind = "bambu"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.report: dict = {}
        self.planned: dict[str, tuple[float | None, float | None]] = {}

    async def _planned_for(self, pr: dict) -> tuple[float | None, float | None]:
        name = pr.get("subtask_name") or ""
        gfile = pr.get("gcode_file") or ""
        key = name or gfile
        if key in self.planned:
            return self.planned[key]
        self.planned[key] = (None, None)
        cands = [p for p in (gfile if gfile.endswith(".3mf") else "", f"/cache/{name}.gcode.3mf", f"/cache/{name}.3mf",
                             f"/{name}.gcode.3mf", f"/{name}.3mf") if p and not p.endswith("/.3mf")]
        m = re.search(r"plate_(\d+)", gfile)
        try:
            data = await asyncio.to_thread(_bambu_ftp_get, self.host, self.access_code, cands)
            if data:
                self.planned[key] = slice_info_usage(data, int(m.group(1)) if m else None)
        except Exception:  # noqa: BLE001 - ohne Plan wird nach Abschluss nachgefragt
            pass
        return self.planned[key]

    async def run(self) -> None:
        delay = 5
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        while True:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port or 8883, ssl=ctx), 15)
                writer.write(mqtt_connect(f"daliegts-{self.printer_id}-{int(time.time())}", "bblp", self.access_code))
                await writer.drain()
                ptype, _, body = await asyncio.wait_for(mqtt_read(reader), 15)
                if ptype != 2 or len(body) < 2 or body[1] != 0:
                    raise ConnectionError("Zugangscode oder Seriennummer falsch (MQTT verweigert)" if ptype == 2 else "keine MQTT-Antwort")
                writer.write(mqtt_subscribe(f"device/{self.serial}/report"))
                await self._pushall(writer)
                self.online, delay = True, 5
                last_ping = last_push = time.monotonic()
                while True:
                    try:
                        ptype, flags, body = await asyncio.wait_for(mqtt_read(reader), 30)
                    except asyncio.TimeoutError:
                        ptype = None
                    now = time.monotonic()
                    if now - last_ping > 30:
                        writer.write(b"\xc0\x00")
                        last_ping = now
                    if now - last_push > 300:  # P1/A1 schicken nur Änderungen – regelmäßig alles anfordern
                        await self._pushall(writer)
                        last_push = now
                    await writer.drain()
                    if ptype == 3:
                        _, payload = mqtt_parse_publish(flags, body)
                        await self._on_report(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self.mark_offline(exc)
            finally:
                if writer is not None:
                    writer.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def _pushall(self, writer) -> None:
        msg = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode()
        writer.write(mqtt_publish(f"device/{self.serial}/request", msg))
        await writer.drain()

    async def _on_report(self, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace")
        self._record("in", text)
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("print"), dict):
            return
        pr = deep_merge(self.report, msg["print"])
        planned_g, planned_mm = (None, None)
        if pr.get("subtask_name") or pr.get("gcode_file"):
            planned_g, planned_mm = await self._planned_for(pr)
        d = bambu_state(pr, planned_mm)
        if d.get("printFileName") and (planned_g or planned_mm):
            await self.files([{"path": d["printFileName"], "name": d["printFileName"], "consumables": planned_mm, "filamentWeight": planned_g}],
                             full=False)
        await self.handle(d)



# ------------------------------------------------------------------ Einrichtungshilfe je Familie (für Formular und Druckerkarte)
# Menünamen können je nach Firmware leicht abweichen; „Zahnrad“ = Einstellungen am Touchdisplay.
SETUP_GUIDES: dict[str, list[str]] = {
    "creality": [
        "Drucker ins WLAN/LAN bringen (am Display: Einstellungen › Netzwerk).",
        "IP-Adresse am Display unter Einstellungen › Netzwerk ablesen – am besten im Router fest vergeben.",
        "Hier Modell und IP eintragen, Port bleibt <b>9999</b>. Kein Root, kein Passwort nötig.",
    ],
    "bambu": [
        "<b>Firmware aktualisieren</b> – der Entwicklermodus braucht mindestens: X1-Serie 01.08.03, P1-Serie 01.08.02, A1-Serie 01.05.00, H2D 01.01.00.",
        "<b>LAN-Modus einschalten</b> am Display (Zahnrad): X1/X1E/H2D unter <i>Einstellungen › LAN-Modus</i>, "
        "P1P/P1S unter <i>Einstellungen › WLAN</i>, A1/A1 mini unter <i>Einstellungen › WLAN › Nur LAN</i>.",
        "Im selben Menü den <b>Entwicklermodus</b> einschalten – erst dann lässt der Drucker fremde Programme wie DaLiegt’s zu.",
        "Dort steht auch der <b>Zugangscode</b> (8 Zeichen). Zeigt der X1 nur Nullen: auf das Aktualisieren-Symbol daneben tippen.",
        "<b>IP-Adresse</b> im WLAN-/Netzwerk-Menü ablesen (im Router fest vergeben), <b>Seriennummer</b> unter Einstellungen › Gerät/Allgemein "
        "oder in Bambu Studio unter <i>Gerät</i> (beginnt z. B. mit 01P…, 03W…, 00M…).",
        "Hier eintragen: Modell, IP, Port <b>8883</b>, Seriennummer, Zugangscode.",
        "Gut zu wissen: Im LAN-Modus funktioniert die Bambu-Handy-App nicht mehr; Bambu Studio druckt weiter im LAN. "
        "Firmware-Updates dann per microSD-Karte.",
    ],
    "prusalink": [
        "Am Drucker <b>Einstellungen › Netzwerk</b>: mit WLAN/LAN verbinden und <b>PrusaLink</b> einschalten.",
        "Dort stehen <b>IP-Adresse</b>, <b>Benutzer</b> (meist „maker“) und <b>Passwort</b> für PrusaLink.",
        "Hier eintragen: Modell, IP, Port 80, Benutzer und Passwort.",
    ],
    "moonraker": [
        "Fluidd oder Mainsail im Browser öffnen – läuft es, ist Moonraker schon da.",
        "IP-Adresse des Druckers (bzw. des Raspberry Pi mit Klipper) eintragen, Port <b>7125</b>.",
        "API-Schlüssel nur nötig, wenn in <i>moonraker.conf</i> unter [authorization] das Heimnetz nicht als vertrauenswürdig eingetragen ist.",
    ],
    "octoprint": [
        "OctoPrint öffnen › Schraubenschlüssel (Einstellungen) › <b>Anwendungsschlüssel</b> bzw. API › neuen Schlüssel „DaLiegt’s“ erzeugen.",
        "Hier IP des OctoPrint-Rechners, Port 80 (bzw. 5000 ohne Webserver) und den Schlüssel eintragen.",
        "Damit der Verbrauch bekannt ist: in OctoPrint die G-Code-Analyse eingeschaltet lassen (Standard).",
    ],
}
