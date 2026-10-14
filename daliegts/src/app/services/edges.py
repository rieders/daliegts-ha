"""Kantenleisten: eine LED-Leiste über die ganze Wand (senkrecht an der Seite, waagerecht oben/unten).

Die LEDs sitzen an festen Positionen (cm) entlang der Wandkante. Ein Fach leuchtet über die LEDs, deren Position in seiner
Höhe (senkrechte Leiste) bzw. Breite (waagerechte Leiste) liegt. Fach = Kreuzungspunkt aus senkrechter und waagerechter Leiste.
Die Positionen kommen aus den Fächern selbst („eine LED je Fach“), aus einem festen Abstand (LED-Streifen) oder aus einer
eigenen Liste (z. B. für eine gedruckte Leiste mit einzelnen Pixelplatinen genau neben jedem Fach).

Aus derselben Positionsliste lässt sich die 3D-Druck-Halterung (OpenSCAD) erzeugen.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Location, LocationLight, WledController

MODES = {"cells": "Eine LED je Fach", "pitch": "Gleichmäßiger Abstand", "manual": "Eigene Positionen"}
SIDES = {"rows": ("left", "right"), "cols": ("top", "bottom")}
TOL_CM = 0.8  # kleinster Wert für den Mindestabstand (cm)


@dataclass
class Cell:
    loc: Location
    lo_x: float
    hi_x: float
    lo_y: float
    hi_y: float

    def span(self, axis: str) -> tuple[float, float]:
        return (self.lo_y, self.hi_y) if axis == "rows" else (self.lo_x, self.hi_x)


# ------------------------------------------------------------------ Konfiguration
def edges_of(wall: Location) -> list[dict]:
    return list((wall.layout or {}).get("edges", []))


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(float(str(v).replace(",", ".")), hi))
    except (TypeError, ValueError):
        return default


def parse_positions(raw) -> list[float]:
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw or "").replace(";", ",").replace("\n", ",").split(",")
    out = []
    for p in parts:
        try:
            out.append(float(p.strip().replace(",", ".")))
        except ValueError:
            continue
    return sorted(out)[:600]


def clean_edges(db: Session, wall: Location, edges: list[dict]) -> list[dict]:
    """Prüft die Leisten-Konfiguration (Controller muss existieren, Werte werden begrenzt)."""
    out, used_ids = [], set()
    for i, e in enumerate(edges[:8]):
        cid = int(e.get("controller_id") or 0)
        if not cid or db.get(WledController, cid) is None:
            continue
        axis = e.get("axis") if e.get("axis") in SIDES else "rows"
        side = e.get("side") if e.get("side") in SIDES[axis] else SIDES[axis][0]
        mode = e.get("mode") if e.get("mode") in MODES else "cells"
        eid = str(e.get("id") or f"e{i + 1}")[:12]
        while eid in used_ids:
            eid += "x"
        used_ids.add(eid)
        out.append({"id": eid, "axis": axis, "side": side, "controller_id": cid, "start": int(_num(e.get("start"), 0, 0, 5000)), "mode": mode,
                    "pitch_mm": round(_num(e.get("pitch_mm"), 16.7, 3, 200), 2), "offset_cm": round(_num(e.get("offset_cm"), 0, -500, 500), 2),
                    "merge_mm": round(_num(e.get("merge_mm"), 15, 0, 100), 1),
                    "positions": ", ".join(f"{p:g}" for p in parse_positions(e.get("positions"))), "reverse": bool(e.get("reverse"))})
    return out


# ------------------------------------------------------------------ Geometrie
def wall_cells(wall: Location) -> list[Cell]:
    """Alle Fächer der platzierten Module mit ihrer Lage in der Wandansicht (cm)."""
    out = []
    for m in wall.children:
        if m.wall_x is None or not m.grid_rows:
            continue
        w, h = m.wall_w or 40, m.wall_h or 30
        rows, cols = m.grid_rows, m.grid_cols or 1
        for c in m.children:
            if not c.grid_row:
                continue
            x0 = m.wall_x + (c.grid_col - 1) / cols * w
            x1 = m.wall_x + (c.grid_col - 1 + c.grid_colspan) / cols * w
            y0 = m.wall_y + (c.grid_row - 1) / rows * h
            y1 = m.wall_y + (c.grid_row - 1 + c.grid_rowspan) / rows * h
            out.append(Cell(c, x0, x1, y0, y1))
    return out


def positions_for(edge: dict, cells: list[Cell]) -> list[float]:
    """LED-Positionen (cm, in Wandkoordinaten) aufsteigend: bei „rows“ von oben nach unten, bei „cols“ von links nach rechts."""
    axis, mode = edge["axis"], edge["mode"]
    if mode == "manual":
        return parse_positions(edge.get("positions"))
    spans = [c.span(axis) for c in cells]
    if not spans:
        return []
    if mode == "pitch":
        lo, hi = min(s[0] for s in spans), max(s[1] for s in spans)
        step = edge["pitch_mm"] / 10.0
        n = max(1, int((hi - lo) / step + 1e-6))
        return [round(lo + edge["offset_cm"] + step / 2 + i * step, 3) for i in range(min(n, 600))]
    centers = sorted((a + b) / 2 for a, b in spans)
    tol = max(TOL_CM / 10, float(edge.get("merge_mm", 15)) / 10)  # Fächer, deren Mitten näher beieinanderliegen, teilen sich eine LED
    groups: list[list[float]] = []
    for c in centers:
        if groups and c - groups[-1][-1] <= tol:
            groups[-1].append(c)
        else:
            groups.append([c])
    return [round(sum(g) / len(g) + edge["offset_cm"], 3) for g in groups][:600]


def led_numbers(edge: dict, count: int) -> list[int]:
    """LED-Nummer je Position (Position 0 = oben/links). Mit „umkehren“ läuft der Strip von unten/rechts."""
    nums = [edge["start"] + i for i in range(count)]
    return nums[::-1] if edge["reverse"] else nums


def leds_of_cell(edge: dict, positions: list[float], numbers: list[int], cell: Cell) -> list[int]:
    lo, hi = cell.span(edge["axis"])
    inside = [i for i, p in enumerate(positions) if lo - 0.01 <= p <= hi + 0.01]
    if not inside and positions:
        mid = (lo + hi) / 2
        inside = [min(range(len(positions)), key=lambda i: abs(positions[i] - mid))]
    return sorted(numbers[i] for i in inside)


def _ranges(nums: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(set(nums)):
        if out and n == out[-1][0] + out[-1][1]:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((n, 1))
    return out


def edge_ranges(wall: Location) -> dict[int, list[tuple[int, int]]]:
    """LED-Bereiche [start, ende) der Kantenleisten je Controller – nur diese LEDs gehören den Leisten (der Rest der Ketten bleibt unberührt)."""
    out: dict[int, list[tuple[int, int]]] = {}
    cells = wall_cells(wall)
    for e in edges_of(wall):
        n = len(positions_for(e, cells))
        out.setdefault(e["controller_id"], []).append((e["start"], e["start"] + n))
    return out


def owned_by_edge(ranges: dict[int, list[tuple[int, int]]], controller_id: int, led_start: int) -> bool:
    return any(a <= led_start < b for a, b in ranges.get(controller_id, []))


def clear_edges(db: Session, wall: Location) -> None:
    """Entfernt die LED-Zuordnungen der bisherigen Kantenleisten (vor dem Ändern der Einstellungen aufrufen)."""
    ranges = edge_ranges(wall)
    for c in wall_cells(wall):
        for lt in list(c.loc.lights):
            if owned_by_edge(ranges, lt.controller_id, lt.led_start):
                c.loc.lights.remove(lt)  # delete-orphan
    db.flush()


def apply_edges(db: Session, wall: Location) -> dict:
    """Schreibt die LED-Bereiche aller Fächer neu. Rückgabe: {Leisten-ID: LED-Anzahl}."""
    edges = edges_of(wall)
    cells = wall_cells(wall)
    ranges = edge_ranges(wall)
    for c in cells:
        for lt in list(c.loc.lights):
            if owned_by_edge(ranges, lt.controller_id, lt.led_start):
                c.loc.lights.remove(lt)  # delete-orphan
    db.flush()
    counts = {}
    for e in edges:
        pos = positions_for(e, cells)
        nums = led_numbers(e, len(pos))
        counts[e["id"]] = len(pos)
        for c in cells:
            for s, n in _ranges(leds_of_cell(e, pos, nums, c)):
                c.loc.lights.append(LocationLight(controller_id=e["controller_id"], led_start=s, led_count=n))
    db.flush()
    return counts


def edge_data(wall: Location) -> list[dict]:
    """Leisten mit Positionen und Nummern für die Oberfläche."""
    cells = wall_cells(wall)
    out = []
    for e in edges_of(wall):
        pos = positions_for(e, cells)
        nums = led_numbers(e, len(pos))
        gaps = [round((b - a) * 10, 1) for a, b in zip(pos, pos[1:], strict=False)]
        out.append({**e, "pos": pos, "nums": nums, "count": len(pos), "end": e["start"] + len(pos), "min_gap_mm": min(gaps) if gaps else None})
    return out


# ------------------------------------------------------------------ 3D-Druck (OpenSCAD)
SCAD_DEFAULTS = {"board_w": 10.0, "board_l": 10.0, "board_t": 2.0, "bar_w": 16.0, "bar_t": 8.0, "seg_mm": 200.0, "margin": 8.0,
                 "channel_w": 4.0, "channel_d": 3.0, "screw_d": 3.4}


def scad_params(raw: dict) -> dict:
    p = dict(SCAD_DEFAULTS)
    lims = {"board_w": (3, 60), "board_l": (3, 60), "board_t": (0.4, 12), "bar_w": (6, 60), "bar_t": (3, 30), "seg_mm": (40, 600),
            "margin": (3, 40), "channel_w": (0, 20), "channel_d": (0, 10), "screw_d": (0, 8)}
    for k, (lo, hi) in lims.items():
        if raw.get(k) not in (None, ""):
            p[k] = _num(raw[k], p[k], lo, hi)
    return p


def split_segments(pos_mm: list[float], seg_mm: float, margin: float) -> list[list[float]]:
    """Teilt die Positionen (mm ab der ersten LED) in Stücke, die auf das Druckbett passen; geschnitten wird zwischen zwei LEDs."""
    segs: list[list[float]] = []
    cur: list[float] = []
    for p in pos_mm:
        if cur and (p - cur[0]) + 2 * margin > seg_mm:
            segs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segs.append(cur)
    return segs


def scad_source(edge: dict, positions_cm: list[float], numbers: list[int], title: str, params: dict) -> str:
    """OpenSCAD-Quelltext der Halterung: ein Balken je Druckstück, pro LED eine Tasche für die Pixelplatine, Kabelrinne und Schraublöcher."""
    p = scad_params(params)
    if not positions_cm:
        raise ValueError("Die Leiste hat keine LED-Positionen.")
    mm = [(x - positions_cm[0]) * 10 for x in positions_cm]
    segs = split_segments(mm, p["seg_mm"], p["margin"])
    # Nummern in Positionsreihenfolge je Stück (für die Beschriftung im Kommentar)
    idx = 0
    seg_defs, comments = [], []
    for s in segs:
        first = s[0]
        local = [round(x - first + p["margin"], 2) for x in s]
        length = round(local[-1] + p["margin"], 2)
        seg_defs.append(f"  [{length}, [{', '.join(f'{x:g}' for x in local)}]]")
        comments.append(f"//   Stück {len(seg_defs)}: {len(s)} LEDs, Nr. {', '.join(str(numbers[idx + i]) for i in range(len(s)))}, Länge {length:g} mm")
        idx += len(s)
    gaps = [b - a for a, b in zip(mm, mm[1:], strict=False)]
    warn = ""
    if gaps and min(gaps) < p["board_l"] + 1:
        warn = f"\n// WARNUNG: Zwei LEDs liegen nur {min(gaps):.1f} mm auseinander – die Platinen (board_l = {p['board_l']:g} mm) passen dort nicht nebeneinander. Mindestabstand der LEDs erhöhen."
    side = {"left": "linke", "right": "rechte", "top": "obere", "bottom": "untere"}[edge["side"]]
    return f"""// DaLiegt's – Halterung für die {side} Kantenleiste „{title}“ ({len(positions_cm)} LEDs, {len(segs)} Druckstück(e))
// Erzeugt aus der Wandansicht. Die LED-Positionen sind die Mitten der Taschen; alle Maße in Millimetern.
// Bitte Platinengröße (board_*) messen und anpassen. Schalter „stueck“: 0 = alle Stücke nebeneinander, 1..n = nur dieses Stück.
{chr(10).join(comments)}{warn}
board_w = {p['board_w']:g};   // Breite der Pixelplatine
board_l = {p['board_l']:g};   // Länge der Pixelplatine (in Richtung der Leiste)
board_t = {p['board_t']:g};   // Dicke der Platine samt LED
clearance = 0.3;              // Spiel in der Tasche
bar_w = {p['bar_w']:g};       // Breite der Leiste
bar_t = {p['bar_t']:g};       // Dicke der Leiste
channel_w = {p['channel_w']:g};   // Kabelrinne (Breite)
channel_d = {p['channel_d']:g};   // Kabelrinne (Tiefe, von hinten)
screw_d = {p['screw_d']:g};       // Schraubenloch
stueck = 0;

segments = [
{(',' + chr(10)).join(seg_defs)}
];

module piece(len, leds) {{
  difference() {{
    cube([len, bar_w, bar_t]);
    // Taschen für die Pixelplatinen (offen nach vorn = +Z)
    for (x = leds)
      translate([x - (board_l + clearance) / 2, (bar_w - board_w - clearance) / 2, bar_t - board_t - 0.2])
        cube([board_l + clearance, board_w + clearance, board_t + 1]);
    // Kabelrinne auf der Rückseite mit Durchgang zu jeder Tasche
    if (channel_w > 0 && channel_d > 0) {{
      translate([-1, (bar_w - channel_w) / 2, -0.01]) cube([len + 2, channel_w, channel_d]);
      for (x = leds)
        translate([x - channel_w / 2, (bar_w - channel_w) / 2, 0]) cube([channel_w, channel_w, bar_t - board_t]);
    }}
    // Schraublöcher an beiden Enden
    if (screw_d > 0) for (x = [3, len - 3])
      translate([x, bar_w / 2, -1]) cylinder(d = screw_d, h = bar_t + 2, $fn = 24);
  }}
}}

for (i = [0 : len(segments) - 1])
  if (stueck == 0 || stueck == i + 1)
    translate([0, (stueck == 0 ? i * (bar_w + 6) : 0), 0]) piece(segments[i][0], segments[i][1]);
"""


def scad_to_stl(source: str, timeout: int = 90) -> bytes:
    """Rendert den OpenSCAD-Quelltext zu STL, falls OpenSCAD auf dem Rechner installiert ist."""
    exe = shutil.which("openscad")
    if not exe:
        raise RuntimeError("OpenSCAD ist auf diesem Rechner nicht installiert. Lade die .scad-Datei herunter und exportiere dort die STL-Datei (F6, dann F7).")
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "leiste.scad", Path(d) / "leiste.stl"
        src.write_text(source, encoding="utf-8")
        r = subprocess.run([exe, "-o", str(dst), str(src)], capture_output=True, timeout=timeout, check=False)
        if r.returncode != 0 or not dst.exists():
            raise RuntimeError("OpenSCAD konnte die Leiste nicht berechnen: " + r.stderr.decode("utf-8", "replace")[-300:])
        return dst.read_bytes()
