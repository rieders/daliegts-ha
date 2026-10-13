"""Parser für Werte mit Einheiten und SI-Präfixen: "4k7", "100n", "2,2µF", "1R5", "10 kΩ", "2–6"."""
from __future__ import annotations

import re

PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0, "r": 1.0,
}
UNITS = ["Ohm", "ohm", "Ω", "F", "H", "Hz", "V", "A", "W", "s", "°C", "%", "mm", "m", "g"]
_num = r"\d+(?:[.,]\d+)?"
RKM_RE = re.compile(r"^(?P<a>\d+)(?P<p>[pnuµμmkKMGRr])(?P<b>\d+)$")
VALUE_RE = re.compile(rf"^(?P<n>[-+]?{_num})\s*(?P<p>[pnuµμmkKMG]?)\s*(?P<u>.*)$")


def _strip_unit(s: str) -> tuple[str, str]:
    s = s.strip()
    for u in sorted(UNITS, key=len, reverse=True):
        if s.endswith(u) and len(s) > len(u):
            head = s[: -len(u)].strip()
            # "m" allein ist Präfix, nicht Meter, wenn davor eine Zahl+Präfix steht – vereinfachte Regel:
            if u in ("m",) and re.search(r"\d$", head) is None:
                continue
            return head, "Ω" if u.lower() == "ohm" else u
    return s, ""


def parse_value(text: str) -> float | None:
    """Wandelt einen Wert in eine Zahl in Basiseinheit um. Gibt None zurück, wenn nicht numerisch."""
    if text is None:
        return None
    s = str(text).strip().replace(" ", "")
    if not s:
        return None
    s, _unit = _strip_unit(s)
    m = RKM_RE.match(s)
    if m:
        mult = PREFIX[m.group("p")]
        return float(f"{m.group('a')}.{m.group('b')}") * mult
    m = VALUE_RE.match(s)
    if not m:
        return None
    rest = m.group("u").strip()
    if rest and not re.fullmatch(r"[A-Za-zΩ°%/]*", rest):
        return None
    try:
        n = float(m.group("n").replace(",", "."))
    except ValueError:
        return None
    return n * PREFIX.get(m.group("p"), 1.0)


def parse_range(text: str) -> tuple[float | None, float | None, float | None]:
    """ "2–6" -> (min, typ, max). Einzelwert -> (v, v, v)."""
    if text is None:
        return None, None, None
    parts = re.split(r"\s*(?:–|—|\.\.\.|…|bis)\s*|(?<=\d)\s*-\s*(?=\d)", str(text).strip())
    if len(parts) == 2:
        a, b = parse_value(parts[0]), parse_value(parts[1])
        if a is not None and b is not None:
            return min(a, b), None, max(a, b)
    v = parse_value(text)
    return v, v, v


def format_si(value: float | None, unit: str = "") -> str:
    if value is None:
        return ""
    if value == 0:
        return f"0 {unit}".strip()
    for p, f in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(value) >= f:
            v = value / f
            s = f"{v:.3g}".replace(".", ",")
            return f"{s} {p}{unit}".strip()
    return f"{value:g} {unit}".strip()
