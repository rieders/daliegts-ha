"""3D-Drucker: Filamentverbrauch automatisch abbuchen (v0.16).

Creality K1/K1C/K1 Max (auch K2/Hi) mit Original-Firmware melden sich im Heimnetz per WebSocket auf Port 9999.
Die Telemetrie kommt als JSON-Teilstände (nur geänderte Felder), u. a.:
  state (0 vorbereiten, 1 druckt, 4 abgebrochen, 5 pausiert), printFileName, printProgress, usedMaterialLength (mm).
Der Drucker schickt Herzschläge ({"ModeCode": "heart_beat"}), auf die mit "ok" geantwortet wird. Mit
{"method": "get", "params": {"reqGcodeFile": 1}} liefert er die Dateiliste mit geplantem Verbrauch
(`consumables` in mm, `filamentWeight` in g) – daraus die Warnung „Spule reicht nicht“ und die Umrechnung in Gramm.

Die Schnittstelle ist von Creality nicht offiziell dokumentiert; fehlt ein Feld, bucht Lagerwerk nichts und
merkt sich den Druck unter „noch nicht abgebucht“.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PrintModel, Printer3D, StockLot

log = logging.getLogger("lagerwerk.printer3d")

DENSITY = [  # g/cm³, Reihenfolge wichtig (PETG vor PET, ASA vor ABS …)
    (r"\bPETG\b", "PETG", 1.27), (r"\bASA\b", "ASA", 1.07), (r"\bABS\b", "ABS", 1.05), (r"\bTPU\b|\bTPE\b", "TPU", 1.21),
    (r"\bPC\b|POLYCARBONAT", "PC", 1.20), (r"\bPA\d*\b|NYLON", "PA", 1.14), (r"\bPLA\b", "PLA", 1.24),
]
DEFAULT_DENSITY = ("PLA", 1.24)
ACTIVE = ("printing", "paused", "processing")
STATE_LABELS = {"printing": "druckt", "paused": "pausiert", "processing": "bereitet vor", "completed": "fertig", "stopped": "abgebrochen",
                "idle": "bereit", "error": "Fehler", "offline": "nicht erreichbar", "": "–"}
FILE_INFO_REQUEST = {"method": "get", "params": {"reqGcodeFile": 1}}
STATUS_REQUEST = {"method": "get", "params": {"ReqPrinterPara": 1}}


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def material_of(text: str) -> tuple[str, float]:
    t = (text or "").upper()
    for pat, name, dens in DENSITY:
        if re.search(pat, t):
            return name, dens
    return DEFAULT_DENSITY


def classify(d: dict) -> str:
    """Druckerzustand aus dem (zusammengeführten) Telemetrie-Stand."""
    err = d.get("err")
    code = _num(err.get("errcode") if isinstance(err, dict) else err)
    file = d.get("printFileName") or ""
    prog = _num(d.get("printProgress") if d.get("printProgress") is not None else d.get("dProgress"))
    st = _num(d.get("state"))
    if file:
        if prog is not None and prog >= 100:
            return "completed"
        if st == 5:
            return "paused"
        if st == 4:
            return "stopped"
        if st == 1:
            return "printing"
        if st == 0:
            return "processing"
    if code:
        return "error"
    return "idle"


def base(file: str) -> str:
    return (file or "").rsplit("/", 1)[-1]


def grams_for(used_mm: float, job: dict, diameter: float, density: float) -> float:
    """Verbrauch in g. Kennt der Drucker Länge und Gewicht der Datei, nutzt er sein eigenes Verhältnis (Material/Dichte des Slicers)."""
    exp_g, exp_mm = _num(job.get("expected_g")), _num(job.get("expected_mm"))
    if exp_g and exp_mm and exp_mm > 0:
        return used_mm / exp_mm * exp_g
    vol_cm3 = math.pi * (diameter / 2) ** 2 * used_mm / 1000.0
    return vol_cm3 * density


def spool_info(lot: StockLot | None) -> dict | None:
    """Rest der Spule in g (wenn die Einheit g/kg ist)."""
    if lot is None or lot.status in ("lost", "used"):
        return None
    it = lot.item
    unit = (it.unit or "").lower()
    mat, dens = material_of(" ".join([it.name, it.tags or "", it.description or ""]))
    grams = None
    if it.stock_mode == "exact":
        if unit in ("g", "gramm"):
            grams = lot.quantity or 0
        elif unit in ("kg",):
            grams = (lot.quantity or 0) * 1000
    return {"lot": lot, "item": it, "material": mat, "density": dens, "grams": grams, "unit": unit}


def _amount_in_unit(unit: str, grams: float, used_mm: float) -> float | None:
    if unit in ("g", "gramm"):
        return round(grams, 1)
    if unit == "kg":
        return round(grams / 1000, 4)
    if unit in ("m", "meter"):
        return round(used_mm / 1000, 2)
    return None


CFS_SLOTS = 4


def slot_lots(db: Session, p: Printer3D) -> list[StockLot | None]:
    """CFS: die 4 Plätze (Bestand oder None)."""
    ids = list(p.cfs_slots or [])[:CFS_SLOTS] + [None] * CFS_SLOTS
    return [db.get(StockLot, i) if i else None for i in ids[:CFS_SLOTS]]


def loaded(db: Session, p: Printer3D) -> list[tuple[str, dict]]:
    """Eingelegte Spulen mit Beschriftung: [(„Platz 2“, spool_info)] bzw. [(„Spule“, …)]."""
    if p.has_cfs:
        return [(f"Platz {i + 1}", si) for i, lt in enumerate(slot_lots(db, p)) if (si := spool_info(lt))]
    si = spool_info(p.spool_lot)
    return [("Spule", si)] if si else []


def _target(db: Session, p: Printer3D) -> tuple[dict | None, str]:
    """Von welcher Spule wird abgebucht? CFS mit mehreren Spulen: noch unbekannt -> nachfragen."""
    spools = loaded(db, p)
    if not spools:
        return None, "keine Spule eingesetzt" if not p.has_cfs else "keine Spule im CFS eingetragen"
    if len(spools) > 1:
        return None, "CFS mit mehreren Spulen – bitte die verwendete Spule wählen"
    return spools[0][1], ""


def model_for(db: Session, p: Printer3D, file: str) -> PrintModel | None:
    return db.scalar(select(PrintModel).where(PrintModel.printer_id == p.id, PrintModel.path == file)) or db.scalar(
        select(PrintModel).where(PrintModel.printer_id == p.id, PrintModel.name == base(file)))


def _record_print(db: Session, p: Printer3D, file: str, grams: float, stopped: bool) -> None:
    m = model_for(db, p, file)
    if m is None:
        m = PrintModel(printer_id=p.id, path=file[:500], name=base(file)[:255], prints=0, on_printer=True, notes="")
        db.add(m)
    m.last_printed = datetime.now().replace(microsecond=0)
    if not stopped:
        m.prints = (m.prints or 0) + 1
        m.last_g = round(grams, 1)


def book(db: Session, p: Printer3D, file: str, used_mm: float, job: dict, stopped: bool = False) -> str:
    """Verbrauch eines beendeten Drucks von der eingesetzten Spule abbuchen (oder als offen merken)."""
    from . import stock as stocksvc

    if not used_mm or used_mm <= 0:
        if job.get("usage_unknown"):  # Drucker meldet keinen Verbrauch und die Datei kennt ihn nicht
            p.warning = f"Für „{base(file)}“ hat der Drucker keinen Filamentverbrauch gemeldet – bitte beim Artikel von Hand abbuchen."
        return "kein Verbrauch gemeldet"
    si, why = _target(db, p)
    density = si["density"] if si else (loaded(db, p)[0][1]["density"] if loaded(db, p) else DEFAULT_DENSITY[1])
    grams = grams_for(used_mm, job, p.diameter_mm or 1.75, density)
    meters = used_mm / 1000
    _record_print(db, p, file, grams, stopped)
    label = f"3D-Druck „{base(file)}“{' (abgebrochen)' if stopped else ''}: {meters:.2f} m ≈ {grams:.1f} g"
    amount = _amount_in_unit(si["unit"], grams, used_mm) if si and si["item"].stock_mode == "exact" else None
    if si is not None and amount is None:
        why = f"Einheit „{si['item'].unit}“ – bitte g, kg oder m"
    if si is None or amount is None:
        pend = list(p.pending or [])
        pend.append({"file": base(file), "grams": round(grams, 1), "meters": round(meters, 2), "stopped": stopped,
                     "ts": datetime.now().replace(microsecond=0).isoformat(), "why": why})
        p.pending = pend[-30:]
        return "gemerkt (" + why + ")"
    lot = si["lot"]
    take = min(amount, lot.quantity or 0)
    if take > 0:
        stocksvc.remove_stock(db, lot, take, None, note=label, ref=f"3d:{p.id}")
    if amount > take + 1e-6:
        p.warning = f"Spule „{si['item'].name}“ ist rechnerisch leer – Restmenge prüfen (Druck brauchte {amount:g} {si['item'].unit})."
    return label


def fits(db: Session, p: Printer3D, need_g: float | None) -> list[tuple[str, dict, bool | None]]:
    """Für jede eingelegte Spule: reicht sie für need_g? (None = unbekannt)"""
    out = []
    for label, si in loaded(db, p):
        ok = None if (need_g is None or si["grams"] is None) else si["grams"] >= need_g
        out.append((label, si, ok))
    return out


def spool_warning(p: Printer3D, job: dict, db: Session | None = None) -> str | None:
    from sqlalchemy.orm import object_session

    db = db or object_session(p)
    spools = loaded(db, p) if db is not None else []
    need = _num(job.get("expected_g"))
    name = base(job.get("file", ""))
    if not spools:
        return "Keine Spule eingesetzt – der Verbrauch wird gemerkt und kann später abgebucht werden." if not p.has_cfs else \
            "Im CFS ist keine Spule eingetragen – der Verbrauch wird gemerkt."
    if not need:
        return None
    known = [(lbl, si) for lbl, si in spools if si["grams"] is not None]
    if not known:
        return None
    if p.has_cfs:
        if any(si["grams"] >= need for _, si in known):
            return None
        best = max(known, key=lambda t: t[1]["grams"])
        return f"Keine CFS-Spule hat genug für „{name}“ (~{need:.0f} g) – am meisten hat {best[0]} ({best[1]['item'].name}): ~{best[1]['grams']:.0f} g."
    si = known[0][1]
    if si["grams"] < need:
        return f"Spule „{si['item'].name}“ hat noch ~{si['grams']:.0f} g, der Druck „{name}“ braucht ~{need:.0f} g."
    return None


def _progress_mm(db: Session, p: Printer3D, d: dict, job: dict) -> float | None:
    """Drucker meldet nur den Fortschritt, die Datei aber keinen Verbrauch: geplante Gramm (z. B. von Hand im Modell)
    über Dichte und Durchmesser in mm umrechnen. job["expected_mm"] wird gesetzt, damit die Gramm später exakt stimmen."""
    prog = _num(d.get("printProgress"))
    if not d.get("usageUnknown") or prog is None or not job.get("expected_g"):
        return None
    if not job.get("expected_mm"):
        spools = loaded(db, p)
        dens = spools[0][1]["density"] if spools else DEFAULT_DENSITY[1]
        area = math.pi * ((p.diameter_mm or 1.75) / 2) ** 2
        job["expected_mm"] = round(job["expected_g"] / dens / area * 1000, 1)
    return prog / 100.0 * job["expected_mm"]


def process(db: Session, p: Printer3D, d: dict) -> list[str]:
    """Zustand auswerten, Druckbeginn/-ende erkennen. Rückgabe: Aktionen für den Client (z. B. „file_info“)."""
    actions: list[str] = []
    st = classify(d)
    file = d.get("printFileName") or ""
    used = _num(d.get("usedMaterialLength"))
    p.last_seen = datetime.now().replace(microsecond=0)
    p.last_state = st
    p.last_error = None
    job = dict(p.job) if p.job else None
    if st in ACTIVE and file:
        if job is None or job.get("file") != file:
            if job is not None:  # vorheriger Druck nie sauber beendet gesehen -> mit letztem Stand abbuchen
                book(db, p, job["file"], job.get("used_mm") or 0, job)
            job = {"file": file, "used_mm": 0, "seen": p.last_seen.isoformat()}
            m = model_for(db, p, file)  # schon einmal gedruckt/bekannt -> Bedarf sofort prüfen
            if m is not None:
                job["expected_g"], job["expected_mm"] = m.need_g, m.expected_mm if not m.last_g else None
            p.warning = spool_warning(p, job, db) if job.get("expected_g") else None
            actions.append("file_info")
        if used is None:
            used = _progress_mm(db, p, d, job)
        if used is not None and used >= (job.get("used_mm") or 0):
            job["used_mm"] = used
        job["usage_unknown"] = bool(d.get("usageUnknown")) and not used
        p.job = job
    elif job is not None and st in ("completed", "stopped", "idle"):
        if used is None and file == job.get("file"):
            used = _progress_mm(db, p, d, job)
        final = used if (used is not None and file == job.get("file") and st != "idle") else job.get("used_mm") or 0
        final = max(final, job.get("used_mm") or 0)
        book(db, p, job["file"], final, job, stopped=(st == "stopped"))
        p.job = None
    db.flush()
    return actions


def upsert_models(db: Session, p: Printer3D, entries: list, full: bool = True) -> int:
    """Dateiliste des Druckers in die Modell-Bibliothek übernehmen (geplanter Verbrauch aus dem Slicer)."""
    seen = set()
    n = 0
    for e in entries:
        if not isinstance(e, dict) or not (e.get("path") or e.get("name")):
            continue
        path = str(e.get("path") or e.get("name"))[:500]
        seen.add(path)
        m = db.scalar(select(PrintModel).where(PrintModel.printer_id == p.id, PrintModel.path == path))
        if m is None:
            m = PrintModel(printer_id=p.id, path=path, name=str(e.get("name") or base(path))[:255], prints=0, notes="")
            db.add(m)
            n += 1
        m.expected_g = _num(e.get("filamentWeight")) or m.expected_g
        m.expected_mm = _num(e.get("consumables")) or m.expected_mm
        m.on_printer = True
        m.seen_at = datetime.now().replace(microsecond=0)
    for m in (db.scalars(select(PrintModel).where(PrintModel.printer_id == p.id)) if full else []):  # nur bei kompletter Dateiliste
        if m.path not in seen:
            m.on_printer = False  # bleibt dauerhaft in der Bibliothek (Archiv) – nur von Hand löschbar
    db.flush()
    return n


def apply_file_info(db: Session, p: Printer3D, entries, full: bool = True) -> None:
    """Dateiliste: Bibliothek aktualisieren und geplanten Verbrauch des laufenden Drucks übernehmen."""
    if not isinstance(entries, list):
        return
    upsert_models(db, p, entries, full)
    if not p.job:
        return
    file = p.job.get("file", "")
    hit = next((e for e in entries if isinstance(e, dict) and e.get("path") == file), None) or next(
        (e for e in entries if isinstance(e, dict) and e.get("name") == base(file)), None)
    if not hit:
        return
    job = dict(p.job)
    job["expected_g"] = _num(hit.get("filamentWeight"))
    job["expected_mm"] = _num(hit.get("consumables"))
    p.job = job
    p.warning = spool_warning(p, job, db)
    db.flush()


def settle_pending(db: Session, p: Printer3D, idx: int, lot: StockLot, user_id: int | None) -> str:
    """Einen gemerkten Druck nachträglich von einer Spule abbuchen."""
    from . import stock as stocksvc

    pend = list(p.pending or [])
    if not 0 <= idx < len(pend):
        raise ValueError("Eintrag nicht gefunden")
    e = pend[idx]
    si = spool_info(lot)
    amount = _amount_in_unit(si["unit"], e["grams"], e["meters"] * 1000) if si else None
    if amount is None:
        raise ValueError("Diese Spule hat keine Einheit g, kg oder m.")
    take = min(amount, lot.quantity or 0)
    if take > 0:
        stocksvc.remove_stock(db, lot, take, user_id, note=f"3D-Druck „{e['file']}“ (nachgetragen): {e['meters']:.2f} m ≈ {e['grams']:.1f} g", ref=f"3d:{p.id}")
    pend.pop(idx)
    p.pending = pend
    db.flush()
    return f"{take:g} {lot.item.unit} von „{lot.item.name}“ abgebucht."


# ------------------------------------------------------------------ Mitschnitt
MAX_RECORDING = 50 * 1024 * 1024


def recording_path(pid: int):
    from ..config import settings

    d = settings.data_dir / "mitschnitt"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"drucker{pid}.jsonl"


# ------------------------------------------------------------------ Verbindung (Hintergrund)
class BaseMonitor:
    """Gemeinsames Gerüst aller Druckeranbindungen: Daten werden in das Creality-Format übersetzt und mit process() ausgewertet."""
    kind = "base"

    def __init__(self, printer_id: int, host: str, port: int, access_code: str = "", serial: str = ""):
        self.printer_id, self.host, self.port = printer_id, host, port
        self.access_code, self.serial = access_code or "", serial or ""
        self.data: dict = {}
        self.task: asyncio.Task | None = None
        self.ws = None
        self.online = False
        self.record_until: datetime | None = None  # Mitschnitt aktiv bis

    @property
    def key(self) -> tuple:
        return (self.kind, self.host, self.port, self.access_code, self.serial)

    @property
    def connected(self) -> bool:
        return self.ws is not None or self.online

    def _record(self, direction: str, msg: str) -> None:
        if not self.record_until:
            return
        if datetime.now() > self.record_until:
            self.record_until = None
            return
        path = recording_path(self.printer_id)
        try:
            if path.exists() and path.stat().st_size > MAX_RECORDING:
                self.record_until = None
                return
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": datetime.now().isoformat(timespec="milliseconds"), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("Mitschnitt")

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self.run(), name=f"3d-{self.printer_id}")

    def stop(self) -> None:
        if self.task:
            self.task.cancel()

    def _save(self, fn):
        from .. import db as dbmod

        with dbmod.session_scope() as db:
            p = db.get(Printer3D, self.printer_id)
            if p is None:
                return None
            return fn(db, p)

    async def mark_offline(self, exc: Exception) -> None:
        self.online = False
        err = f"{type(exc).__name__}: {exc}"[:300]

        def mark(db, p, err=err):
            p.last_error, p.last_state = err, "offline"

        await asyncio.to_thread(self._save, mark)

    async def handle(self, snapshot: dict) -> list[str]:
        """Übersetzten Stand auswerten (Druckbeginn/-ende, Abbuchen)."""
        self.online = True
        return await asyncio.to_thread(self._save, lambda db, p: process(db, p, snapshot)) or []

    async def files(self, entries: list, full: bool = True) -> None:
        """full=False: nur Angaben zur laufenden Datei (keine komplette Liste -> andere Modelle bleiben, wie sie sind)."""
        await asyncio.to_thread(self._save, lambda db, p: apply_file_info(db, p, entries, full))

    async def request_files(self) -> None:  # Dateiliste holen (Modell-Bibliothek)
        return None

    async def run(self) -> None:  # pragma: no cover - je Anbindung
        raise NotImplementedError


class CrealityMonitor(BaseMonitor):
    kind = "creality"

    async def send(self, ws, obj) -> None:
        text = obj if isinstance(obj, str) else json.dumps(obj)
        self._record("out", text)
        await ws.send(text)

    async def request_files(self) -> None:
        if self.ws is not None:
            await self.send(self.ws, FILE_INFO_REQUEST)

    async def run(self) -> None:
        import websockets

        delay = 5
        while True:
            try:
                async with websockets.connect(f"ws://{self.host}:{self.port}", open_timeout=10, ping_interval=None, max_size=4 * 1024 * 1024) as ws:
                    self.ws = ws
                    delay = 5
                    await self.send(ws, STATUS_REQUEST)
                    await self.send(ws, FILE_INFO_REQUEST)  # Dateiliste -> Modell-Bibliothek
                    poll = asyncio.create_task(self._poll(ws))
                    try:
                        async for msg in ws:
                            await self._on_message(ws, msg)
                    finally:
                        poll.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self.mark_offline(exc)
            self.ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def _poll(self, ws) -> None:
        n = 0
        while True:
            await asyncio.sleep(10)
            n += 1
            await self.send(ws, STATUS_REQUEST)
            if n % 360 == 0:  # stündlich Dateiliste
                await self.send(ws, FILE_INFO_REQUEST)

    async def _on_message(self, ws, msg) -> None:
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        self._record("in", msg)
        if msg.strip() == "ok":
            return
        try:
            payload = json.loads(msg)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("ModeCode") == "heart_beat":
            await self.send(ws, "ok")
            return
        if "retGcodeFileInfo2" in payload:
            await self.files(payload.pop("retGcodeFileInfo2"))
        if not payload:
            return
        self.data.update(payload)
        actions = await self.handle(dict(self.data))
        if "file_info" in actions:
            await self.send(ws, FILE_INFO_REQUEST)


MONITORS: dict[int, BaseMonitor] = {}


def make_monitor(p) -> BaseMonitor:
    from . import printer3d_drivers as drv

    cls = {"creality": CrealityMonitor, "moonraker": drv.MoonrakerMonitor, "octoprint": drv.OctoPrintMonitor,
           "prusalink": drv.PrusaLinkMonitor, "bambu": drv.BambuMonitor}.get(p.kind or "creality", CrealityMonitor)
    return cls(p.id, p.host, p.port, p.access_code or "", p.serial or "")


def sync_monitors() -> None:
    """Verbindungen passend zu den eingetragenen Druckern starten/stoppen (im laufenden Event-Loop aufrufen)."""
    from .. import db as dbmod

    from . import license as lic

    with dbmod.session_scope() as db:
        rows = list(db.scalars(select(Printer3D).where(Printer3D.enabled.is_(True)))) if lic.has(db, "printer3d") else []
        want = {p.id: make_monitor(p) for p in rows}
        rec = {p.id: p.recording_until for p in rows}
    for pid, mon in list(MONITORS.items()):
        if pid not in want or want[pid].key != mon.key:
            mon.stop()
            MONITORS.pop(pid)
    for pid, mon in want.items():
        if pid not in MONITORS:
            mon.start()
            MONITORS[pid] = mon
    for pid, mon in MONITORS.items():
        mon.record_until = rec.get(pid)


async def supervise() -> None:
    while True:
        try:
            sync_monitors()
        except Exception:  # noqa: BLE001
            log.exception("3D-Drucker-Überwachung")
        await asyncio.sleep(60)
