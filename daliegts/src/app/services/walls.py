"""Wandansicht: Lagermöbel (Module) auf einer Fläche anordnen, umbauen und per LED-Kette verbinden (v0.2).

Koordinaten in Zentimetern, Ursprung oben links. Jeder Lagerort kann eine Wand sein; seine direkten
Unterorte mit `wall_x` sind die platzierten Module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import COARSE_LEVELS, Location, LocationLight, StockLot, StorageTemplate, WledController
from . import locations as locsvc

SIDES = ("right", "left", "above", "below")
MIN_SIZE, MAX_SIZE = 2.0, 2000.0


# ------------------------------------------------------------------ Größen
def default_size(loc: Location, definition: dict | None = None) -> tuple[float, float]:
    d = definition or {}
    if d.get("width_cm") and d.get("height_cm"):
        return float(d["width_cm"]), float(d["height_cm"])
    if loc.grid_rows:
        return float(max(20, (loc.grid_cols or 1) * 8)), float(max(10, loc.grid_rows * 6))
    return 40.0, 30.0


def _clamp(v: float) -> float:
    return round(max(MIN_SIZE, min(MAX_SIZE, float(v))), 1)


# ------------------------------------------------------------------ Anordnung
@dataclass
class Rect:
    id: int
    x: float
    y: float
    w: float
    h: float

    def overlaps(self, o: "Rect") -> bool:
        eps = 0.05
        return self.x < o.x + o.w - eps and o.x < self.x + self.w - eps and self.y < o.y + o.h - eps and o.y < self.y + self.h - eps


def placed_modules(wall: Location) -> list[Location]:
    return [c for c in wall.children if c.wall_x is not None and c.wall_y is not None]


AREA_TYPES = ("lagerwand", "raum", "bereich")


def is_area(loc: Location) -> bool:
    """Bereich eines Raums (z. B. „Regalwand“, „Schreibtisch“): eigene Fläche mit eigenen Modulen, selbst nicht platziert."""
    return loc.wall_x is None and (loc.type == "lagerwand" or (loc.type in AREA_TYPES and bool(placed_modules(loc))))


def areas_of(wall: Location | None) -> list[Location]:
    return [c for c in wall.children if is_area(c)] if wall is not None else []


def area_root(wall: Location) -> Location | None:
    """Der Raum, dessen Bereiche als Reiter angezeigt werden (der Ort selbst oder sein Elternraum)."""
    if areas_of(wall):
        return wall
    if wall.parent is not None and is_area(wall):
        return wall.parent
    return None


def add_area(db: Session, room: Location, name: str) -> Location:
    name = (name or "").strip()[:200]
    if not name:
        raise ValueError("Bitte einen Namen für den Bereich angeben.")
    if any(c.name.lower() == name.lower() for c in room.children):
        raise ValueError(f"„{name}“ gibt es in {room.name} schon.")
    return locsvc.create_location(db, name, room.id, "lagerwand")


def move_to_area(db: Session, wall: Location, module: Location, target: Location) -> None:
    """Modul samt Fächern in einen anderen Bereich (oder zurück in den Raum) verschieben – Codes und Etiketten bleiben."""
    if module.parent_id != wall.id:
        raise ValueError("Das Modul gehört nicht zu dieser Fläche.")
    allowed = {a.id for a in areas_of(wall)} | ({wall.parent_id} if wall.parent is not None and is_area(wall) else set())
    allowed |= {a.id for a in areas_of(wall.parent)} - {wall.id} if wall.parent is not None and is_area(wall) else set()
    if target.id not in allowed:
        raise ValueError("Ziel ist kein Bereich dieses Raums.")
    layout = dict(wall.layout or {})
    if layout.get("chains"):  # aus LED-Ketten der alten Fläche entfernen
        layout["chains"] = [{**c, "modules": [e for e in c.get("modules", []) if e.get("id") != module.id]} for c in layout["chains"]]
        wall.layout = layout
    module.parent_id = target.id
    db.flush()
    db.refresh(target)
    db.refresh(wall)
    place(db, target, module, None, "right")
    normalize(wall)
    db.flush()


def _rect(m: Location) -> Rect:
    return Rect(m.id, m.wall_x or 0.0, m.wall_y or 0.0, m.wall_w or 40.0, m.wall_h or 30.0)


def _push(rects: dict[int, Rect], moved_id: int, side: str) -> None:
    """Schiebt überlappende Module in Einfügerichtung weg (kaskadierend)."""
    queue, guard = [moved_id], 0
    while queue and guard < 500:
        guard += 1
        cur = rects[queue.pop(0)]
        for r in rects.values():
            if r.id == cur.id or not cur.overlaps(r):
                continue
            if side == "right":
                r.x = cur.x + cur.w
            elif side == "left":
                r.x = cur.x - r.w
            elif side == "above":
                r.y = cur.y - r.h
            else:
                r.y = cur.y + cur.h
            queue.append(r.id)


def normalize(wall: Location) -> None:
    mods = placed_modules(wall)
    if not mods:
        return
    dx, dy = min(m.wall_x for m in mods), min(m.wall_y for m in mods)
    for m in mods:
        m.wall_x, m.wall_y = round(m.wall_x - dx, 1), round(m.wall_y - dy, 1)


def place(db: Session, wall: Location, module: Location, relative_to: Location | None = None, side: str = "right") -> None:
    """Setzt ein Modul neben/über/unter ein anderes; ohne Bezug rechts ans Ende, unten bündig („auf den Boden“)."""
    if module.parent_id != wall.id:
        raise ValueError("Das Modul gehört nicht zu dieser Wand.")
    if side not in SIDES:
        raise ValueError("Unbekannte Richtung.")
    if not module.wall_w or not module.wall_h:
        module.wall_w, module.wall_h = default_size(module)
    others = [m for m in placed_modules(wall) if m.id != module.id]
    w, h = module.wall_w, module.wall_h
    if relative_to is not None:
        if relative_to.parent_id != wall.id or relative_to.wall_x is None:
            raise ValueError("Bezugsmodul ist nicht auf dieser Wand platziert.")
        r = _rect(relative_to)
        x, y = {
            "right": (r.x + r.w, r.y + r.h - h),
            "left": (r.x - w, r.y + r.h - h),
            "above": (r.x, r.y - h),
            "below": (r.x, r.y + r.h),
        }[side]
    elif others:
        floor = max(m.wall_y + m.wall_h for m in others)
        x, y, side = max(m.wall_x + m.wall_w for m in others), floor - h, "right"
    else:
        x, y = 0.0, 0.0
    rects = {m.id: _rect(m) for m in others}
    rects[module.id] = Rect(module.id, x, y, w, h)
    _push(rects, module.id, side)
    for m in [*others, module]:
        rr = rects[m.id]
        m.wall_x, m.wall_y = round(rr.x, 1), round(rr.y, 1)
    normalize(wall)
    db.flush()


def move(db: Session, wall: Location, module: Location, x: float, y: float, w: float | None = None, h: float | None = None) -> None:
    if module.parent_id != wall.id:
        raise ValueError("Das Modul gehört nicht zu dieser Wand.")
    module.wall_x, module.wall_y = round(float(x), 1), round(float(y), 1)
    if w:
        module.wall_w = _clamp(w)
    if h:
        module.wall_h = _clamp(h)
    normalize(wall)
    db.flush()


def unplace(db: Session, wall: Location, module: Location) -> None:
    if module.parent_id != wall.id:
        raise ValueError("Das Modul gehört nicht zu dieser Wand.")
    module.wall_x = module.wall_y = None
    normalize(wall)
    db.flush()


def overlapping(wall: Location) -> set[int]:
    rects = [_rect(m) for m in placed_modules(wall)]
    out = set()
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if a.overlaps(b):
                out |= {a.id, b.id}
    return out


# ------------------------------------------------------------------ Neues Modul
def create_module(db: Session, wall: Location, spec: locsvc.FurnitureSpec, width: float | None, height: float | None,
                  relative_to: Location | None = None, side: str = "right") -> Location:
    furniture = locsvc.create_furniture(db, spec, wall.id)
    furniture.wall_w, furniture.wall_h = (_clamp(width), _clamp(height)) if width and height else default_size(furniture)
    db.flush()
    db.refresh(wall)
    place(db, wall, furniture, relative_to, side)
    return furniture


def create_box(db: Session, wall: Location, name: str, type_: str, width: float | None, height: float | None,
               relative_to: Location | None = None, side: str = "right") -> Location:
    """Modul ohne Raster (Kiste, Karton, Gerät, Werkzeugkoffer …)."""
    loc = locsvc.create_location(db, name, wall.id, type_ or "kiste")
    loc.wall_w, loc.wall_h = (_clamp(width), _clamp(height)) if width and height else (40.0, 30.0)
    db.flush()
    db.refresh(wall)
    place(db, wall, loc, relative_to, side)
    return loc


# ------------------------------------------------------------------ Umbauen (Reihen/Fächer ändern)
_NAMING_PATTERNS = [
    ("row_col", re.compile(r"^R\d+-F\d+$")),
    ("letter_number", re.compile(r"^[A-Z]{1,3}\d+$")),
]


def guess_naming(furniture: Location) -> dict:
    if furniture.layout and furniture.layout.get("naming"):
        return furniture.layout
    names = [c.name for c in furniture.children if c.grid_row]
    for naming, pat in _NAMING_PATTERNS:
        if names and all(pat.match(n) for n in names):
            return {"naming": naming, "prefix": "", "bottom_up": False}
    m = re.match(r"^(.*?)(\d+)$", names[0]) if names else None
    return {"naming": "number", "prefix": m.group(1) if m else "Fach ", "bottom_up": False}


@dataclass
class ReshapeResult:
    added: list[str]
    removed: list[str]
    blocked: list[str]


def reshape(db: Session, furniture: Location, rows: int, cols: int, row_counts: list[int] | None = None,
            apply: bool = True) -> ReshapeResult:
    """Ändert Reihen/Fächer eines Möbels. Neue Positionen bekommen neue Fächer, bestehende Namen/Codes bleiben.
    Wegfallende Fächer werden nur entfernt, wenn sie (inkl. Abteilen) leer sind – sonst wird abgebrochen."""
    meta = guess_naming(furniture)
    cells_now = [c for c in furniture.children if c.grid_row]
    merges = [(c.grid_row, c.grid_col, c.grid_rowspan, c.grid_colspan) for c in cells_now if c.grid_rowspan > 1 or c.grid_colspan > 1]
    spec = locsvc.FurnitureSpec(name=furniture.name, type=furniture.type, rows=rows, cols=cols, row_counts=row_counts,
                                naming=meta.get("naming", "letter_number"), prefix=meta.get("prefix", "Fach "),
                                bottom_up=bool(meta.get("bottom_up")))
    # bestehende Lücken (Plätze ohne Fach) bleiben Lücken
    occupied = {(r, c) for x in cells_now for r in range(x.grid_row, x.grid_row + x.grid_rowspan) for c in range(x.grid_col, x.grid_col + x.grid_colspan)}
    old_rows, old_cols_n = furniture.grid_rows or 0, furniture.grid_cols or 0
    spec.gaps = [(r, c) for r in range(1, min(old_rows, spec.rows) + 1) for c in range(1, min(old_cols_n, spec.cols) + 1)
                 if (r, c) not in occupied and not spec.row_counts]
    max_cols = spec.cols if not spec.row_counts else max(spec.cols, *spec.row_counts, *(c.grid_col + c.grid_colspan - 1 for c in cells_now))
    spec.merges = [m for m in merges if m[0] + m[2] - 1 <= spec.rows and m[1] + m[3] - 1 <= max_cols]
    target = {(c["row"], c["col"]): c for c in locsvc._cells(spec)}
    existing = {(c.grid_row, c.grid_col): c for c in cells_now}
    tc = locsvc.TreeCache(db)
    removed, blocked = [], []
    for pos, cell in existing.items():
        if pos in target:
            continue
        ids = [cell.id, *tc.descendants(cell.id)]
        if db.scalar(select(func.count(StockLot.id)).where(StockLot.location_id.in_(ids))):
            blocked.append(cell.name)
        else:
            removed.append(cell.name)
    new_cells = [c for pos, c in target.items() if pos not in existing]
    used = {c.name for c in furniture.children}
    seq = max([int(m.group(1)) for c in furniture.children if (m := re.search(r"(\d+)$", c.name))] + [0])
    added = []
    for c in sorted(new_cells, key=lambda c: (c["row"], c["col"])):
        seq += 1
        name = locsvc._cell_name(spec, c, seq)
        while name in used:
            seq += 1
            name = locsvc._cell_name(spec, c, seq) if spec.naming in ("number", "prefix") else f"{name}'"
        used.add(name)
        c["name"] = name
        added.append(name)
    result = ReshapeResult(added, removed, blocked)
    if not apply or blocked:
        return result
    for pos, cell in list(existing.items()):
        if pos not in target:
            locsvc.delete_location(db, cell)
    for c in new_cells:
        locsvc.create_location(db, c["name"], furniture.id, "fach", grid_row=c["row"], grid_col=c["col"],
                               grid_rowspan=c["rowspan"], grid_colspan=c["colspan"])
    for pos, cell in existing.items():  # Spannweiten ggf. an geänderte Zusammenfassungen anpassen
        if pos in target:
            cell.grid_rowspan, cell.grid_colspan = target[pos]["rowspan"], target[pos]["colspan"]
    old_rows, old_cols = furniture.grid_rows or 1, furniture.grid_cols or 1
    furniture.grid_rows = spec.rows
    furniture.grid_cols = locsvc.grid_width(spec)
    if furniture.wall_w and furniture.wall_h:  # Größe proportional mitwachsen lassen
        furniture.wall_w = _clamp(furniture.wall_w * furniture.grid_cols / old_cols)
        furniture.wall_h = _clamp(furniture.wall_h * furniture.grid_rows / old_rows)
    db.flush()
    db.expire(furniture, ["children"])
    return result


def row_counts_of(furniture: Location) -> list[int]:
    """Anzahl Schubladen, die in jeder Reihe beginnen (Semantik wie „Abweichende Fachzahl je Reihe“)."""
    counts: dict[int, int] = {}
    for c in furniture.children:
        if c.grid_row:
            counts[c.grid_row] = counts.get(c.grid_row, 0) + 1
    return [counts.get(r, 0) for r in range(1, (furniture.grid_rows or 0) + 1)]


# ------------------------------------------------------------------ LED-Ketten über mehrere Module
LED_KEYS = ("leds_per_cell", "led_gap", "led_row_gap", "serpentine", "led_start_corner", "led_direction",
            "led_mode", "xy_rows_side", "xy_cols_side", "xy_first", "xy_rows_reverse", "xy_cols_reverse", "xy_gap")


def led_spec_from(spec: locsvc.FurnitureSpec) -> dict:
    return {k: getattr(spec, k) for k in LED_KEYS}


def module_spec(m: Location) -> locsvc.FurnitureSpec:
    d = {k: v for k, v in (m.led_spec or {}).items() if k in LED_KEYS}
    spec = locsvc.FurnitureSpec(name=m.name, rows=m.grid_rows or 1, cols=m.grid_cols or 1, **d)
    cells = [c for c in m.children if c.grid_row]
    spec.merges = [(c.grid_row, c.grid_col, c.grid_rowspan, c.grid_colspan) for c in cells if c.grid_rowspan > 1 or c.grid_colspan > 1]
    rc = row_counts_of(m) if m.grid_rows else []
    if rc and (len(set(rc)) > 1 or rc[0] != spec.cols):
        spec.row_counts = rc
    return spec


def module_mapping(m: Location, start: int) -> tuple[dict[int, list[tuple[int, int]]], int]:
    """(location_id -> [(led_start, led_count), …]), Anzahl benutzter LEDs ab start.

    Im Modus „cell“ hat jedes Fach einen LED-Bereich, im Modus „xy“ zwei (Zeilen- und Spalten-LED).
    """
    spec = module_spec(m)
    if not m.grid_rows:
        n = spec.leds_per_cell
        return {m.id: [(start, n)]}, n
    cells = [c for c in m.children if c.grid_row]
    if spec.led_mode == "xy":
        row_leds, col_leds, used = locsvc.xy_layout(spec, m.grid_rows, m.grid_cols or spec.cols, start)
        out = {c.id: locsvc.xy_cell_ranges(row_leds, col_leds, {"row": c.grid_row, "col": c.grid_col, "rowspan": c.grid_rowspan, "colspan": c.grid_colspan})
               for c in cells}
        return out, used
    spec.led_start = start
    mp = locsvc.led_mapping(spec, [{"row": c.grid_row, "col": c.grid_col, "rowspan": c.grid_rowspan, "colspan": c.grid_colspan} for c in cells])
    out = {c.id: [mp[(c.grid_row, c.grid_col)]] for c in cells if (c.grid_row, c.grid_col) in mp}
    end = max((s + n for rs in out.values() for s, n in rs), default=start)
    return out, end - start


def chains_of(wall: Location) -> list[dict]:
    return list((wall.layout or {}).get("chains", []))


def clean_chains(db: Session, wall: Location, chains: list[dict]) -> list[dict]:
    child_ids = {c.id for c in wall.children}
    out, seen = [], set()
    for ch in chains[:8]:
        cid = int(ch.get("controller_id") or 0)
        if not cid or db.get(WledController, cid) is None:
            continue
        mods = []
        for m in ch.get("modules", []):
            mid = int(m.get("id") or 0)
            if mid in child_ids and (cid, mid) not in seen:
                seen.add((cid, mid))
                mods.append({"id": mid, "gap": max(0, min(int(m.get("gap") or 0), 500))})
        out.append({"controller_id": cid, "start": max(0, min(int(ch.get("start") or 0), 5000)), "modules": mods})
    return out


def apply_chains(db: Session, wall: Location) -> dict[int, int]:
    """Berechnet die LED-Nummern aller Module der Ketten fortlaufend neu. Rückgabe: Modul-ID -> Start-LED."""
    starts: dict[int, int] = {}
    for ch in chains_of(wall):
        pos, cid = ch["start"], ch["controller_id"]
        for entry in ch["modules"]:
            m = db.get(Location, entry["id"])
            if m is None:
                continue
            mapping, used = module_mapping(m, pos)
            targets = {t.id: t for t in [m, *[c for c in m.children if c.grid_row]]}
            for t in targets.values():
                for lt in list(t.lights):
                    if lt.controller_id == cid:
                        t.lights.remove(lt)  # delete-orphan
            db.flush()
            for lid, ranges in mapping.items():
                for s, n in ranges:
                    targets[lid].lights.append(LocationLight(controller_id=cid, led_start=s, led_count=n))
            starts[m.id] = pos
            pos += used + entry["gap"]
    db.flush()
    return starts


# ------------------------------------------------------------------ Daten für die Oberfläche
def wall_data(db: Session, wall: Location) -> dict:
    tc = locsvc.TreeCache(db)
    all_ids = tc.descendants(wall.id)
    lots = list(db.scalars(select(StockLot).where(StockLot.location_id.in_(all_ids)))) if all_ids else []
    shown: dict[int, dict] = {}
    modules, unplaced = [], []
    for m in wall.children:
        if m.wall_x is None and is_area(m):
            continue
        if m.wall_x is None:
            unplaced.append({"id": m.id, "name": m.name, "type": m.type, "grid": bool(m.grid_rows)})
            continue
        cells = []
        rows, cols = m.grid_rows or 0, m.grid_cols or 0
        virtual = False
        kids = [c for c in m.children if not c.compartment_index]
        if not rows and len(kids) >= 2 and m.type not in ("raum", "bereich", "lagerwand"):
            # Raster noch nicht festgelegt (z. B. importiertes Magazin): Fächer vorläufig der Reihe nach zeigen
            virtual = True
            cols = 4 if len(kids) >= 8 else len(kids)
            rows = -(-len(kids) // cols)
            ordered = sorted(kids, key=lambda c: locsvc.natural_key(c.name))
            placed = [(c, i // cols + 1, i % cols + 1) for i, c in enumerate(ordered)]
        else:
            placed = [(c, c.grid_row, c.grid_col) for c in m.children if c.grid_row]
        for c, r, col in placed:
            cd = {"id": c.id, "name": c.name, "code": c.code, "r": r, "c": col, "rs": 1 if virtual else c.grid_rowspan,
                  "cs": 1 if virtual else c.grid_colspan,
                  "items": [], "n": 0, "low": False, "lit": bool(c.lights), "leds": [[lt.led_start, lt.led_count] for lt in c.lights], "comps": sum(1 for k in c.children if k.type == "abteil")}
            cells.append(cd)
            shown[c.id] = cd
        _, leds = module_mapping(m, 0)
        mspec = module_spec(m)
        lights = [lt for c in m.children for lt in c.lights]
        led_start = min((lt.led_start for lt in lights), default=0)
        xy = None
        if mspec.led_mode == "xy" and m.grid_rows:
            rl, cl, _used = locsvc.xy_layout(mspec, m.grid_rows, m.grid_cols or mspec.cols, led_start)
            xy = {"rows": rl, "cols": cl, "rows_side": mspec.xy_rows_side, "cols_side": mspec.xy_cols_side}
        md = {"id": m.id, "name": m.name, "code": m.code, "type": m.type, "x": m.wall_x, "y": m.wall_y, "w": m.wall_w or 40, "h": m.wall_h or 30,
              "rows": rows, "cols": cols, "virtual": virtual, "count": len(kids), "cells": cells, "items": [], "n": 0, "low": False,
              "children": [c.name for c in m.children if not c.grid_row][:8] if not virtual else [], "lit": bool(m.lights) or any(x["lit"] for x in cells),
              "leds": leds, "led_spec": {k: getattr(mspec, k) for k in LED_KEYS}, "led_start": led_start, "xy": xy,
              "led_placed": bool(lights)}
        modules.append(md)
        shown[m.id] = md
    for lot in sorted(lots, key=lambda x: locsvc.natural_key(x.item.name)):
        cur = lot.location_id
        while cur is not None and cur not in shown and cur != wall.id:
            cur = tc.nodes[cur].parent_id if cur in tc.nodes else None
        if cur in shown:
            d = shown[cur]
            d["n"] += 1
            if len(d["items"]) < 4 and lot.item.name not in d["items"]:
                d["items"].append(lot.item.name)
            d["low"] = d["low"] or lot.item.below_min
            if len(d.setdefault("lots", [])) < 40:
                d["lots"].append(lot_info(lot, tc, cur))
    for md in modules:  # Modulsummen
        md["n"] += sum(c["n"] for c in md["cells"])
        md["free"] = sum(1 for c in md["cells"] if not c["n"])
        md.setdefault("lots", [])
        for c in md["cells"]:
            c.setdefault("lots", [])
    _add_buy(db, [ld for md in modules for d in [md, *md["cells"]] for ld in d.get("lots", [])])
    width = max([m["x"] + m["w"] for m in modules] + [100])
    height = max([m["y"] + m["h"] for m in modules] + [60])
    cells_total = sum(len(m["cells"]) or 1 for m in modules)
    cells_free = sum(m["free"] if m["cells"] else (0 if m["n"] else 1) for m in modules)
    return {"wall": {"id": wall.id, "name": wall.name}, "modules": modules, "unplaced": unplaced, "width": width, "height": height,
            "overlap": sorted(overlapping(wall)), "chains": chains_of(wall), "cells_total": cells_total, "cells_free": cells_free,
            "areas": [area_summary(db, a) for a in areas_of(wall)]}


def area_summary(db: Session, area: Location) -> dict:
    d = wall_data(db, area)
    return {"id": area.id, "name": area.name, "modules": [m["name"] for m in d["modules"]], "cells_total": d["cells_total"],
            "cells_free": d["cells_free"], "areas": len(d["areas"])}


def _add_buy(db: Session, lots: list[dict]) -> None:
    """Bestellstatus (bestellt / zu bestellen) an die Fachanzeige hängen."""
    from ..web import fmt_qty
    from .purchasing import buy_states

    if not lots:
        return
    bs = buy_states(db, {ld["item"] for ld in lots})
    for ld in lots:
        b = bs.get(ld["item"])
        ld["buy"] = ("📦 bestellt " + fmt_qty(b.ordered)) if b and b.ordered else (("🛒 zu bestellen " + fmt_qty(b.open)) if b and b.open else "")


def lot_info(lot: StockLot, tc: locsvc.TreeCache, shown_id: int | None = None) -> dict:
    """Ein Bestand für die Fachanzeige: „Bauteil X mit Anzahl Y“."""
    from ..web import fmt_qty

    it = lot.item
    if it.stock_mode == "exact":
        amount = fmt_qty(lot.quantity, it.unit)
    elif it.stock_mode == "coarse":
        amount = COARSE_LEVELS.get(lot.coarse or "", "–")
    else:
        amount = "vorhanden"
    sub = tc.nodes[lot.location_id].name if lot.location_id != shown_id and lot.location_id in tc.nodes else ""
    return {"lot": lot.id, "item": it.id, "name": it.name, "desc": (it.description or "")[:80], "amount": amount, "mode": it.stock_mode,
            "coarse": lot.coarse, "qty": lot.quantity, "sub": sub, "low": bool(it.below_min), "code": lot.code}


def wall_of(loc: Location | None) -> Location | None:
    """Die Wand/den Raum, in dem ein Ort sichtbar ist (erstes Modul mit Platz auf einer Fläche, von innen nach außen)."""
    seen = set()
    while loc is not None and loc.id not in seen:
        seen.add(loc.id)
        if loc.wall_x is not None and loc.parent is not None:
            return loc.parent
        loc = loc.parent
    return None


def rooms(db: Session) -> list[dict]:
    """Räume/Wände für die Auswahl: alles, was platzierte Module hat, dazu leere Räume."""
    tc = locsvc.TreeCache(db)
    with_modules = set(db.scalars(select(Location.parent_id).where(Location.wall_x.is_not(None)).distinct()))
    with_areas = {tc.nodes[w].parent_id for w in with_modules if w in tc.nodes and tc.nodes[w].parent_id}
    out = []
    for lid, n in tc.nodes.items():
        if lid in with_modules or lid in with_areas or n.type in ("raum", "lagerwand"):
            out.append({"id": lid, "name": n.name, "path": tc.path(lid), "modules": lid in with_modules or lid in with_areas})
    out.sort(key=lambda r: (not r["modules"], locsvc.natural_key(r["path"])))
    return out


def visible_ids(db: Session, wall: Location, loc_ids) -> list[int]:
    """Orte (z. B. Lagerplätze eines Artikels) auf die sichtbaren Module/Fächer der Wand abbilden."""
    tc = locsvc.TreeCache(db)
    visible = {m.id for m in placed_modules(wall)} | {c.id for m in placed_modules(wall) for c in m.children if c.grid_row}
    out = set()
    for lid in loc_ids:
        cur = lid
        while cur is not None and cur not in visible and cur != wall.id:
            cur = tc.nodes[cur].parent_id if cur in tc.nodes else None
        if cur in visible:
            out.add(cur)
    return sorted(out)


def search_hits(db: Session, wall: Location, q: str) -> dict:
    """Welche Module/Fächer der Wand enthalten Artikel zur Suche?"""
    from .search import SearchQuery, search

    items, _ = search(db, SearchQuery(q=q, location_id=wall.id, limit=500))
    return hits_for_items(db, wall, items)


def hits_for_items(db: Session, wall: Location, items) -> dict:
    if not items:
        return {"cells": [], "locations": [], "count": 0, "places": []}
    tc = locsvc.TreeCache(db)
    sub = set(tc.descendants(wall.id))
    lots = db.scalars(select(StockLot).where(StockLot.item_id.in_([i.id for i in items]), StockLot.location_id.in_(sub))).all()
    visible = {m.id for m in placed_modules(wall)} | {c.id for m in placed_modules(wall) for c in m.children if c.grid_row}
    cells, locs, places = set(), set(), []
    area_ids, area_hits = {a.id for a in areas_of(wall)}, {}
    for lot in lots:
        locs.add(lot.location_id)
        cur = lot.location_id
        while cur is not None and cur not in visible and cur != wall.id:
            cur = tc.nodes[cur].parent_id if cur in tc.nodes else None
        rel = lambda lid: tc.path(lid).split(" / ", len(tc.ancestors(wall.id)) + 1)[-1]  # noqa: E731
        if cur in visible:
            cells.add(cur)
            if len(places) < 60:
                places.append({**lot_info(lot, tc, cur), "cell": cur, "where": rel(cur)})
        else:
            w = wall_of(lot.location)
            if w is not None and w.id != wall.id and len(places) < 60:
                area = next((a for a in [w.id, *reversed(tc.ancestors(w.id))] if a in area_ids), None)
                if area is not None:
                    area_hits[area] = area_hits.get(area, 0) + 1
                shown = visible_ids(db, w, [lot.location_id])
                places.append({**lot_info(lot, tc, shown[0] if shown else None), "cell": None, "where": rel(lot.location_id),
                               "href": f"/orte/{w.id}/wand?artikel={lot.item_id}"})
    places.sort(key=lambda p: (locsvc.natural_key(p["name"]), locsvc.natural_key(p["where"])))
    _add_buy(db, places)
    return {"cells": sorted(cells), "locations": sorted(locs), "count": len(items), "names": [i.name for i in items[:8]], "places": places,
            "areas": area_hits}


def apply_grid(db: Session, furniture: Location, spec: locsvc.FurnitureSpec, width: float | None = None, height: float | None = None) -> int:
    """Raster (z. B. aus einer Vorlage) auf die vorhandenen Fächer eines Möbels legen – Namen, Codes und Inhalt bleiben."""
    n = locsvc.assign_grid(db, furniture, spec.rows, spec.cols, spec.count_direction, spec.bottom_up, spec.merges, spec.row_counts, spec.gaps)
    if furniture.type in ("sonstiges", "kiste", "bereich"):
        furniture.type = spec.type or "magazin"
    if width and height:
        furniture.wall_w, furniture.wall_h = _clamp(width), _clamp(height)
    db.flush()
    db.expire(furniture, ["children"])
    return n


# ------------------------------------------------------------------ Vorlagen mit Maßen
TEMPLATE_SIZES = {  # Richtwerte in cm – im Dialog anpassbar
    "Magazin 4×8 (32 Schubladen)": (30, 40),
    "Magazin 4×8 + 2 breite unten": (30, 45),
    "Magazin 5×12 (60 Schubladen)": (38, 60),
    "Magazin 3×5 große Schubladen": (38, 30),
    "Regal 5×5": (80, 180),
    "Sortimentskasten 4×6": (33, 25),
}
EXTRA_TEMPLATES = {
    "Stapelmagazin 3×3 (9 Schubladen)": ({"rows": 3, "cols": 3, "type": "magazin"}, (26, 16)),
    "Stapelmagazin 4×4 (16 Schubladen)": ({"rows": 4, "cols": 4, "type": "magazin"}, (32, 20)),
    "Regal 4 Böden × 3 Plätze": ({"rows": 4, "cols": 3, "type": "regal", "naming": "row_col"}, (90, 180)),
    "Schwerlastregal 5 Böden × 2 Plätze": ({"rows": 5, "cols": 2, "type": "regal", "naming": "row_col"}, (100, 180)),
    # 3 Spalten: links/rechts große Schubladen, in der Mitte je zwei kleine übereinander, unten eine breite (17 Fächer)
    "Magazin groß/2×klein/groß + breit unten (17 Fächer)": (
        {"rows": 9, "cols": 3, "type": "magazin", "naming": "number", "prefix": "Fach", "count_direction": "bands",
         "merges": [(r, c, 2, 1) for r in (1, 3, 5, 7) for c in (1, 3)] + [(9, 1, 1, 3)]}, (None, None)),
}


# v0.24: Vorlagen für verbreitete Möbel und Kleinteilemagazine (Maße laut Hersteller/Händler, Stand 10/2026).
# Raster teils aus Maßen bzw. Schubladengröße abgeleitet – Hinweis steht in den Notizen der Vorlage.
_ABG = "Raster aus Maßen/Schubladengröße abgeleitet – bei Abweichung im Raster-Dialog anpassen."
_REG = {"type": "regal", "naming": "row_col"}
PRODUCT_TEMPLATES: dict[str, tuple[dict, tuple[float, float], str]] = {
    # IKEA KALLAX (Tiefe 39 cm, Fach ca. 33×33 cm) – Spalten × Reihen
    "IKEA KALLAX 1×3 (42×112 cm)": ({**_REG, "rows": 3, "cols": 1}, (42, 112), ""),
    "IKEA KALLAX 1×4 (42×147 cm)": ({**_REG, "rows": 4, "cols": 1}, (42, 147), ""),
    "IKEA KALLAX 2×2 (77×77 cm)": ({**_REG, "rows": 2, "cols": 2}, (77, 77), ""),
    "IKEA KALLAX 2×3 (77×112 cm)": ({**_REG, "rows": 3, "cols": 2}, (77, 112), ""),
    "IKEA KALLAX 2×4 (77×147 cm)": ({**_REG, "rows": 4, "cols": 2}, (77, 147), ""),
    "IKEA KALLAX 3×4 (112×147 cm)": ({**_REG, "rows": 4, "cols": 3}, (112, 147), ""),
    "IKEA KALLAX 4×4 (147×147 cm)": ({**_REG, "rows": 4, "cols": 4}, (147, 147), ""),
    "IKEA KALLAX 5×5 (182×182 cm)": ({**_REG, "rows": 5, "cols": 5}, (182, 182), ""),
    # IKEA BILLY (Tiefe 28 cm) – Böden ergeben Fächer übereinander
    "IKEA BILLY 40×202 cm (6 Fächer)": ({**_REG, "rows": 6, "cols": 1}, (40, 202), "1 fester + 4 verstellbare Böden."),
    "IKEA BILLY 80×202 cm (6 Fächer)": ({**_REG, "rows": 6, "cols": 1}, (80, 202), "1 fester + 4 verstellbare Böden."),
    "IKEA BILLY 80×106 cm (3 Fächer)": ({**_REG, "rows": 3, "cols": 1}, (80, 106), "2 verstellbare Böden."),
    # IKEA Holz-/Metallsysteme
    "IKEA IVAR 1 Element 89×179 cm (4 Böden)": ({**_REG, "rows": 4, "cols": 1}, (89, 179),
                                                 "Kiefer massiv, Tiefe 30 cm, Böden 83 cm breit. Weitere Elemente einfach daneben anfügen."),
    "IKEA OMAR 1 Element 92×181 cm (5 Böden)": ({**_REG, "rows": 5, "cols": 1}, (92, 181), "Stahl verzinkt, Tiefe 36 cm."),
    "IKEA TROFAST 99×94 cm (2 Spalten, 6 Boxen)": ({**_REG, "rows": 3, "cols": 2, "naming": "letter_number"}, (99, 94),
                                                  "Je Spalte 1 flache + 2 tiefe Boxen (Standard-Set)."),
    # IKEA Schubladen
    "IKEA ALEX Schubladenelement 36×70 cm (5 Schubladen)": ({"type": "schrank", "rows": 5, "cols": 1, "naming": "number", "prefix": "Schublade "},
                                                           (36, 70), "Schubladen unterschiedlich hoch."),
    "IKEA HELMER Rollcontainer 28×69 cm (6 Schubladen)": ({"type": "schrank", "rows": 6, "cols": 1, "naming": "number", "prefix": "Schublade "},
                                                         (28, 69), "A4-Schubladen. Anzahl bitte am eigenen Exemplar prüfen."),
    # raaco Kleinteilemagazine (Stahlgehäuse)
    "raaco 945-00 – 45 Schubladen 150-00 (5×9)": ({"type": "magazin", "rows": 9, "cols": 5}, (30.6, 41.7), _ABG),
    "raaco 936-01 – 36 Schubladen 150-01 (4×9)": ({"type": "magazin", "rows": 9, "cols": 4}, (30.6, 41.7), _ABG),
    "raaco 918-02 – 18 Schubladen 150-02 (3×6)": ({"type": "magazin", "rows": 6, "cols": 3}, (30.6, 41.7), _ABG),
    "raaco 906-03 – 6 Schubladen 150-03 (2×3)": ({"type": "magazin", "rows": 3, "cols": 2}, (30.6, 41.7), _ABG),
    "raaco 1260-00 – 60 Schubladen 150-00 (5×12)": ({"type": "magazin", "rows": 12, "cols": 5}, (30.6, 55.2), _ABG),
    "raaco 250/24-1 – 24 große Schubladen (4×6)": ({"type": "magazin", "rows": 6, "cols": 4}, (35.7, 43.5), _ABG),
    # Allit
    "Allit VarioPlus Pro 53/100 – 50 Schubladen (5×10)": ({"type": "magazin", "rows": 10, "cols": 5}, (30, 52.5), _ABG),
    "Allit VarioPlus Metall 73 – 48 Schubladen (4×12)": ({"type": "magazin", "rows": 12, "cols": 4}, (30, 56.5),
                                                       "Stahlgehäuse 30 × 56,5 × 14 cm. " + _ABG),
    # v0.27: Parkside (Lidl) und Stanley – Gehäusemaße laut Händlerangaben, Raster abgeleitet
    "Parkside Kleinteilemagazin 33 Schubladen (30,5×41,5 cm)": ({"type": "magazin", "rows": 11, "cols": 3}, (30.5, 41.5),
                                                                "Gehäuse 30,5 × 41,5 × 13,5 cm, Kunststoff, wandmontierbar. " + _ABG),
    "Parkside Kleinteilemagazin 17 Schubladen (30,5×41,5 cm)": (
        {"type": "magazin", "rows": 9, "cols": 2, "merges": [(9, 1, 1, 2)]}, (30.5, 41.5),
        "Gehäuse 30,5 × 41,5 × 16,5 cm, 8 Reihen à 2 Schubladen + 1 breite unten. " + _ABG),
    "Stanley Kleinteilemagazin 30 Fächer (36,5×22 cm)": ({"type": "magazin", "rows": 5, "cols": 6}, (36.5, 22),
                                                         "Gehäuse 36,5 × 22 × 15,3 cm, stapel- und wandmontierbar. " + _ABG),
    "Stanley Kleinteilemagazin 9 Schubladen 1-93-978 (36,5×21,3 cm)": ({"type": "magazin", "rows": 3, "cols": 3}, (36.5, 21.3),
                                                                      "Gehäuse 36,5 × 21,3 × 15,5 cm, wandmontierbar. " + _ABG),
}


GROUP_BOXES = "Sortimentskästen"
_BOX_BRANDS = ("raaco", "Allit", "Parkside", "Stanley", "Würth", "Hazet", "Gedore", "Magazin", "Stapelmagazin", "Sortimentskasten")


def template_group(name: str, builtin: bool = True, definition: dict | None = None) -> str:
    """Rubrik für die Vorlagen-Auswahl: IKEA, Sortimentskästen (alle Hersteller), Allgemein, Eigene Vorlagen.

    Eine Vorlage kann ihre Rubrik selbst mitbringen (definition["group"], z. B. aus dem Katalog)."""
    g = ((definition or {}).get("group") or "").strip()
    if g:
        return g[:40]
    if name.startswith("IKEA "):
        return "IKEA"
    if any(name.startswith(b + " ") for b in _BOX_BRANDS):
        return GROUP_BOXES
    return "Allgemein" if builtin else "Eigene Vorlagen"


def ensure_template_sizes(db: Session) -> None:
    """Ergänzt Maße bei den eingebauten Vorlagen und legt zusätzliche an (additiv)."""
    for name, (w, h) in TEMPLATE_SIZES.items():
        t = db.scalar(select(StorageTemplate).where(StorageTemplate.name == name))
        if t is not None and "width_cm" not in (t.definition or {}):
            t.definition = {**t.definition, "width_cm": w, "height_cm": h}
    for name, (d, (w, h)) in EXTRA_TEMPLATES.items():
        if not db.scalar(select(StorageTemplate).where(StorageTemplate.name == name)):
            spec = locsvc.FurnitureSpec(name="", **d)
            definition = spec.to_template()
            if w and h:
                definition.update(width_cm=w, height_cm=h)
            db.add(StorageTemplate(name=name, kind=spec.type, definition=definition, builtin=True))
    for name, (d, (w, h), note) in PRODUCT_TEMPLATES.items():
        if not db.scalar(select(StorageTemplate).where(StorageTemplate.name == name)):
            spec = locsvc.FurnitureSpec(name="", **d)
            db.add(StorageTemplate(name=name, kind=spec.type, definition={**spec.to_template(), "width_cm": w, "height_cm": h},
                                   notes=note, builtin=True))
    db.flush()
