"""WLED-Ansteuerung: Fächer beleuchten, Blinkcodes für Abteile, Auto-Timeout, Ruhemodus.

Protokoll: WLED JSON-API, POST http://<host>/json/state mit Einzel-LED-Steuerung über seg.i:
    {"on": true, "bri": 180, "seg": {"id": 0, "i": [0, 60, "000000", 12, 15, "00FF00"]}}
Ranges sind [start, stop) – stop exklusiv.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Location, LocationLight, WledController

log = logging.getLogger("lagerwerk.wled")

BLINK_ON = 0.25
BLINK_SLOT = 0.5
BLINK_PAUSE = 1.0
TICK_HZ = 4


@dataclass
class LightTarget:
    controller_id: int
    start: int
    count: int
    color: str
    blink: int = 0  # 0 = dauerhaft, n = Blinkcode (n Blinks, dann Pause)
    pulse: bool = False
    location_id: int | None = None
    inherited: bool = False
    note: str = ""
    t0: float = 0.0  # Startzeit der Anzeige – Blinkphase wird relativ dazu berechnet

    @property
    def animated(self) -> bool:
        return self.blink > 0 or self.pulse


# ------------------------------------------------------------------ Auflösung Lagerort -> LEDs
def _furniture_of(loc: Location) -> Location | None:
    cur = loc.parent
    while cur is not None:
        if cur.grid_rows:
            return cur
        cur = cur.parent
    return None


def resolve_targets(db: Session, loc: Location, color: str | None = None) -> list[LightTarget]:
    """Ermittelt die LEDs für einen Lagerort inkl. Vererbung und Abteil-Darstellung."""
    own = [lt for lt in loc.lights if lt.controller and lt.controller.enabled]
    if own:
        return [LightTarget(lt.controller_id, lt.led_start, lt.led_count, (color or lt.color or lt.controller.color).upper(), location_id=loc.id) for lt in own]
    # Vorfahren mit LEDs suchen
    anc, depth = loc.parent, 0
    while anc is not None and not [lt for lt in anc.lights if lt.controller and lt.controller.enabled]:
        anc, depth = anc.parent, depth + 1
    if anc is None:
        return []
    lights = [lt for lt in anc.lights if lt.controller and lt.controller.enabled]
    targets: list[LightTarget] = []
    is_compartment = loc.type == "abteil" and loc.compartment_index and loc.compartment_count and depth == 0
    if not is_compartment:
        for lt in lights:
            targets.append(LightTarget(lt.controller_id, lt.led_start, lt.led_count, (color or lt.color or lt.controller.color).upper(),
                                       location_id=loc.id, inherited=True, note=f"LEDs von {anc.name}"))
        return targets
    furniture = _furniture_of(loc)
    mode = furniture.light_mode if furniture else "both"
    idx, cnt = loc.compartment_index, loc.compartment_count
    for lt in lights:
        col = (color or lt.color or lt.controller.color).upper()
        can_segment = loc.compartment_layout in ("row", "grid", None) and lt.led_count >= cnt and mode in ("segment", "both")
        if can_segment:
            s, c = split_range(lt.led_start, lt.led_count, cnt, idx)
            targets.append(LightTarget(lt.controller_id, s, c, col, blink=idx if mode == "both" else 0, pulse=mode == "segment",
                                       location_id=loc.id, note=f"Abteil {idx} von {cnt}"))
        else:
            targets.append(LightTarget(lt.controller_id, lt.led_start, lt.led_count, col, blink=idx,
                                       location_id=loc.id, note=f"Abteil {idx} von {cnt} – blinkt {idx}×"))
    return targets


def split_range(start: int, count: int, parts: int, index: int) -> tuple[int, int]:
    """Teilt count LEDs anteilig auf parts Abteile auf; index 1-basiert. Jedes Abteil bekommt mind. 1 LED."""
    edges = [start + round(count * i / parts) for i in range(parts + 1)]
    s, e = edges[index - 1], edges[index]
    if e <= s:
        e = s + 1
    return s, e - s


# ------------------------------------------------------------------ Frame-Berechnung (rein, testbar)
def blink_on(n: int, t: float) -> bool:
    period = n * BLINK_SLOT + BLINK_PAUSE
    x = t % period
    if x >= n * BLINK_SLOT:
        return False
    return (x % BLINK_SLOT) < BLINK_ON


HEX_RE = __import__("re").compile(r"^[0-9A-Fa-f]{6}$")


def clean_color(value: str | None, default: str | None = None) -> str | None:
    v = (value or "").strip().lstrip("#")
    return v.upper() if HEX_RE.match(v) else default


def clamp_duration(value: float | None) -> float | None:
    if value is None:
        return None
    return max(1.0, min(float(value), 600.0))


def scale_color(hex_color: str, f: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return "".join(f"{max(0, min(255, round(c * f))):02X}" for c in (r, g, b))


def frame(targets: list[LightTarget], t: float) -> list[tuple[int, int, str]]:
    out = []
    for tg in targets:
        if tg.blink and not blink_on(tg.blink, t - tg.t0 + 0.01):
            continue
        col = tg.color
        if tg.pulse:
            col = scale_color(col, 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.6)))
        out.append((tg.start, tg.start + tg.count, col))
    return out


def payload(ctrl: WledController | "CtrlInfo", ranges: list[tuple[int, int, str]], brightness: int | None = None) -> dict:
    arr: list = [0, ctrl.led_count, "000000"]
    for s, e, c in sorted(ranges):
        s = max(0, min(s, ctrl.led_count))
        e = max(s, min(e, ctrl.led_count))
        if e > s:
            arr += [s, e, c]
    return {"on": True, "bri": brightness or ctrl.brightness, "seg": {"id": ctrl.segment_id, "i": arr}}


# ------------------------------------------------------------------ Manager (asynchron)
@dataclass
class CtrlInfo:
    id: int
    name: str
    host: str
    port: int
    led_count: int
    segment_id: int
    brightness: int
    restore_mode: str
    restore_preset: int | None
    timeout_s: int
    idle_mode: str
    idle_preset: int | None
    idle_after_min: int
    idle_brightness: int
    idle_from: str
    idle_to: str

    @classmethod
    def of(cls, c: WledController) -> "CtrlInfo":
        return cls(c.id, c.name, c.host, c.port, c.led_count, c.segment_id, c.brightness, c.restore_mode, c.restore_preset,
                   c.timeout_s, c.idle_mode, c.idle_preset, c.idle_after_min, c.idle_brightness, c.idle_from, c.idle_to)

    @property
    def url(self) -> str:
        host = self.host if "://" in self.host else f"http://{self.host}"
        return f"{host}:{self.port}" if self.port and self.port != 80 and host.count(":") < 2 else host


@dataclass
class Highlight:
    targets: list[LightTarget]
    expires: float
    token: int


@dataclass
class CtrlState:
    info: CtrlInfo
    highlights: dict[int, Highlight] = field(default_factory=dict)
    saved_state: dict | None = None
    task: asyncio.Task | None = None
    last_error: str = ""
    last_ok: float | None = None
    idle_active: bool = False
    capturing: bool = False
    manual_off: bool = False  # „Alle LEDs aus“: kein Ruhemodus bis zur nächsten echten Anzeige
    last_activity: float = field(default_factory=time.monotonic)


class WledManager:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.states: dict[int, CtrlState] = {}
        self._token = 0
        self.lowstock_provider = None  # Callable[[int], list[LightTarget]] für Ruhemodus "Bestandsampel"
        self.paused_idle = False
        self.presence: bool | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.wled_timeout, transport=self._transport)
        return self._client

    async def close(self) -> None:
        for st in self.states.values():
            if st.task:
                st.task.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------- HTTP
    async def post_state(self, info: CtrlInfo, data: dict) -> bool:
        st = self.states.setdefault(info.id, CtrlState(info))
        try:
            r = await self.client.post(f"{info.url}/json/state", json=data)
            r.raise_for_status()
            st.last_error, st.last_ok = "", time.time()
            return True
        except Exception as exc:  # noqa: BLE001
            st.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("WLED %s nicht erreichbar: %s", info.name, st.last_error)
            return False

    async def get_json(self, info: CtrlInfo, path: str) -> dict | None:
        st = self.states.setdefault(info.id, CtrlState(info))
        try:
            r = await self.client.get(f"{info.url}{path}")
            r.raise_for_status()
            st.last_error, st.last_ok = "", time.time()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            st.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return None

    async def test(self, info: CtrlInfo) -> dict:
        data = await self.get_json(info, "/json/info")
        return {"ok": data is not None, "info": data, "error": self.states[info.id].last_error}

    async def presets(self, info: CtrlInfo) -> dict:
        data = await self.get_json(info, "/presets.json") or {}
        return {k: v.get("n", f"Preset {k}") for k, v in data.items() if k != "0" and isinstance(v, dict)}

    # ---------------------------------------------------------- Anzeige
    async def show(self, infos: dict[int, CtrlInfo], targets: list[LightTarget], duration: float | None = None, exclusive: bool = True) -> int:
        """Zeigt Ziele an. exclusive=True ersetzt bisherige Anzeigen auf den betroffenen Controllern."""
        self._token += 1
        token = self._token
        by_ctrl: dict[int, list[LightTarget]] = {}
        for t in targets:
            by_ctrl.setdefault(t.controller_id, []).append(t)
        await asyncio.gather(*(self._show_on(infos[cid], tgs, duration, token, exclusive) for cid, tgs in by_ctrl.items() if cid in infos))
        return token

    async def _show_on(self, info: CtrlInfo, targets: list[LightTarget], duration: float | None, token: int, exclusive: bool) -> None:
        st = self.states.get(info.id)
        if st is None:
            st = self.states[info.id] = CtrlState(info)
        st.info = info
        if not st.highlights and info.restore_mode == "previous" and not st.idle_active and st.saved_state is None and not st.capturing:
            st.capturing = True
            try:
                state = await self.get_json(info, "/json/state")
                if st.saved_state is None:
                    st.saved_state = state
            finally:
                st.capturing = False
        if exclusive:
            st.highlights.clear()
        st.idle_active = False
        st.manual_off = False
        st.last_activity = time.monotonic()
        dur = duration if duration is not None else info.timeout_s
        start = time.monotonic()
        for tg in targets:
            tg.t0 = start
        st.highlights[token] = Highlight(targets, start + dur, token)
        await self._render(st)
        if st.task is None or st.task.done():
            st.task = asyncio.create_task(self._loop(st))

    async def _render(self, st: CtrlState) -> None:
        t = time.monotonic()
        tg = [x for h in st.highlights.values() for x in h.targets]
        await self.post_state(st.info, payload(st.info, frame(tg, t)))

    async def _loop(self, st: CtrlState) -> None:
        try:
            tick0, k = time.monotonic(), 0
            while st.highlights:
                k += 1  # feste Taktung relativ zum Start -> Blinkphasen werden nicht verschluckt
                await asyncio.sleep(max(0.0, tick0 + k / TICK_HZ - time.monotonic()))
                now = time.monotonic()
                expired = [k for k, h in st.highlights.items() if h.expires <= now]
                for k in expired:
                    del st.highlights[k]
                if not st.highlights:
                    await self._restore(st)
                    break
                animated = any(t.animated for h in st.highlights.values() for t in h.targets)
                if animated or expired:
                    await self._render(st)
        except asyncio.CancelledError:
            pass

    async def _restore(self, st: CtrlState) -> None:
        info = st.info
        st.last_activity = time.monotonic()
        # Effektwand erst nach der eingestellten Wartezeit (idle_tick), nur bei 0 Minuten sofort
        if info.idle_after_min <= 0 and not st.manual_off and await self._maybe_idle(st):
            return
        if info.restore_mode == "preset" and info.restore_preset:
            await self.post_state(info, {"ps": info.restore_preset})
        elif info.restore_mode == "previous" and st.saved_state:
            state = dict(st.saved_state)
            state.pop("seg", None)
            await self.post_state(info, {"seg": {"id": info.segment_id, "frz": False}})
            await self.post_state(info, state)
        else:
            await self.post_state(info, {"on": False})
        st.saved_state = None

    def idle_allowed(self, info: CtrlInfo, now: datetime | None = None) -> bool:
        if info.idle_mode == "off" or self.paused_idle or self.presence is False:
            return False
        now = now or datetime.now()
        hm = now.strftime("%H:%M")
        a, b = info.idle_from, info.idle_to
        return (a <= hm < b) if a <= b else (hm >= a or hm < b)

    async def _maybe_idle(self, st: CtrlState) -> bool:
        info = st.info
        if not self.idle_allowed(info):
            return False
        if info.idle_mode == "preset" and info.idle_preset:
            st.idle_active = await self.post_state(info, {"ps": info.idle_preset, "bri": info.idle_brightness})
            return st.idle_active
        if info.idle_mode == "lowstock" and self.lowstock_provider:
            targets = await asyncio.to_thread(self.lowstock_provider, info.id)
            st.idle_active = await self.post_state(info, payload(info, frame(targets, 0), brightness=info.idle_brightness))
            return st.idle_active
        return False

    async def idle_tick(self, infos: dict[int, CtrlInfo]) -> None:
        """Periodisch aufgerufen: Ruhemodus nach Inaktivität starten bzw. außerhalb des Zeitfensters beenden."""
        now = time.monotonic()
        for info in infos.values():
            st = self.states.setdefault(info.id, CtrlState(info))
            st.info = info
            if st.highlights or st.manual_off:
                continue
            allowed = self.idle_allowed(info)
            if st.idle_active and not allowed:
                st.idle_active = False
                await self.post_state(info, {"on": False})
            elif not st.idle_active and allowed and now - st.last_activity >= info.idle_after_min * 60:
                await self._maybe_idle(st)

    async def set_paused(self, infos: dict[int, CtrlInfo], paused: bool) -> None:
        """Ruhemodus pausieren: laufenden Effekt sofort ausschalten."""
        self.paused_idle = paused
        if paused:
            for info in infos.values():
                st = self.states.get(info.id)
                if st is not None and st.idle_active and not st.highlights:
                    st.idle_active = False
                    await self.post_state(info, {"on": False})

    async def clear(self, infos: dict[int, CtrlInfo]) -> None:
        async def _one(info: CtrlInfo):
            st = self.states.setdefault(info.id, CtrlState(info))
            st.info = info
            st.highlights.clear()
            if st.task and not st.task.done():
                st.task.cancel()
            # „Aus“ heißt aus: kein Effekt, kein Wiederherstellen – bis wieder etwas angezeigt wird
            st.manual_off = True
            st.idle_active = False
            st.saved_state = None
            st.last_activity = time.monotonic()
            await self.post_state(info, {"on": False})

        await asyncio.gather(*(_one(i) for i in infos.values()))

    async def count_through(self, info: CtrlInfo, step: int = 10) -> None:
        """Lauflicht: jede step-te LED weiß, Rest aus – hilft beim Zählen/Zuordnen."""
        ranges = [(i, i + 1, "FFFFFF" if i % step == 0 else "202020") for i in range(0, info.led_count)]
        await self.post_state(info, payload(info, ranges))

    def status(self) -> dict[int, dict]:
        return {cid: {"error": st.last_error, "last_ok": st.last_ok, "active": len(st.highlights), "idle": st.idle_active}
                for cid, st in self.states.items()}


manager = WledManager()


def controller_infos(db: Session) -> dict[int, CtrlInfo]:
    return {c.id: CtrlInfo.of(c) for c in db.scalars(select(WledController).where(WledController.enabled.is_(True)))}


def targets_for_locations(db: Session, locations: list[Location], palette: list[str] | None = None) -> list[LightTarget]:
    out: list[LightTarget] = []
    for i, loc in enumerate(locations):
        color = palette[i % len(palette)] if palette else None
        out += resolve_targets(db, loc, color)
    return out


MULTI_PALETTE = ["00FF40", "0080FF", "FF8000", "FF00C0", "FFFF00", "00FFFF", "FF2020", "A040FF"]


def lowstock_targets(db: Session, controller_id: int) -> list[LightTarget]:
    from ..models import Item, StockLot

    out = []
    lots = db.scalars(select(StockLot).join(Item).where(StockLot.location_id.is_not(None))).all()
    seen = set()
    for lot in lots:
        if lot.item.below_min and lot.location_id not in seen:
            seen.add(lot.location_id)
            for t in resolve_targets(db, lot.location, "FF0000"):
                if t.controller_id == controller_id:
                    t.blink, t.pulse = 0, False
                    out.append(t)
    return out


def lights_for_furniture(db: Session, furniture: Location) -> list[LocationLight]:
    ids = [c.id for c in furniture.children]
    return list(db.scalars(select(LocationLight).where(LocationLight.location_id.in_(ids))))
