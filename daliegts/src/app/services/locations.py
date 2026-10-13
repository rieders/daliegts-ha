"""Lagerorte: Baum, Pfade, Text-Massenanlage und Lagermöbel-Assistent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Location, LocationLight, StockLot, StorageTemplate
from .codes import next_code

LOCATION_TYPES = {
    "raum": "Raum",
    "bereich": "Bereich",
    "lagerwand": "Lagerwand",
    "regal": "Regal",
    "schrank": "Schrank",
    "magazin": "Kleinteilemagazin",
    "sortierbox": "Sortierbox",
    "fach": "Fach / Schublade",
    "abteil": "Abteil",
    "kiste": "Kiste",
    "zone": "Zone",
    "lochwand": "Lochwand",
    "sonstiges": "Sonstiges",
}
FURNITURE_TYPES = {"regal", "schrank", "magazin", "sortierbox", "lagerwand"}


# ------------------------------------------------------------------ Baum-Helfer
class TreeCache:
    """Lädt alle Orte einmal (wenige tausend) – schnell auch auf dem Pi."""

    def __init__(self, db: Session):
        rows = db.execute(select(Location.id, Location.parent_id, Location.name, Location.sort_order, Location.type, Location.code)).all()
        self.nodes = {r.id: r for r in rows}
        self.children: dict[int | None, list[int]] = {}
        for r in sorted(rows, key=lambda r: (r.sort_order, natural_key(r.name))):
            self.children.setdefault(r.parent_id, []).append(r.id)

    def path(self, loc_id: int | None, sep: str = " / ") -> str:
        names, seen = [], set()
        while loc_id and loc_id in self.nodes and loc_id not in seen:
            seen.add(loc_id)
            names.append(self.nodes[loc_id].name)
            loc_id = self.nodes[loc_id].parent_id
        return sep.join(reversed(names))

    def ancestors(self, loc_id: int) -> list[int]:
        out, seen = [], set()
        cur = self.nodes.get(loc_id)
        while cur and cur.parent_id and cur.parent_id not in seen:
            seen.add(cur.parent_id)
            out.append(cur.parent_id)
            cur = self.nodes.get(cur.parent_id)
        return list(reversed(out))

    def descendants(self, loc_id: int) -> list[int]:
        out, stack = [], list(self.children.get(loc_id, []))
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(self.children.get(n, []))
        return out

    def walk(self, parent: int | None = None, depth: int = 0):
        for cid in self.children.get(parent, []):
            yield depth, self.nodes[cid]
            yield from self.walk(cid, depth + 1)


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def location_path(db: Session, loc: Location | None, sep: str = " / ") -> str:
    parts, seen = [], set()
    while loc is not None and loc.id not in seen:
        seen.add(loc.id)
        parts.append(loc.name)
        loc = loc.parent
    return sep.join(reversed(parts))


def create_location(db: Session, name: str, parent_id: int | None = None, type_: str = "sonstiges", **kw) -> Location:
    loc = Location(name=name.strip(), parent_id=parent_id, type=type_, code=next_code(db, "location"), **kw)
    if "sort_order" not in kw:
        loc.sort_order = (db.scalar(select(func.max(Location.sort_order)).where(Location.parent_id == parent_id)) or 0) + 1
    db.add(loc)
    db.flush()
    return loc


def move_location(db: Session, loc: Location, new_parent_id: int | None, index: int | None = None) -> None:
    if new_parent_id is not None:
        tc = TreeCache(db)
        if new_parent_id == loc.id or new_parent_id in tc.descendants(loc.id):
            raise ValueError("Ein Lagerort kann nicht in sich selbst verschoben werden.")
    loc.parent_id = new_parent_id
    siblings = db.scalars(select(Location).where(Location.parent_id == new_parent_id, Location.id != loc.id).order_by(Location.sort_order, Location.id)).all()
    siblings = list(siblings)
    idx = len(siblings) if index is None else max(0, min(index, len(siblings)))
    siblings.insert(idx, loc)
    for i, s in enumerate(siblings):
        s.sort_order = i + 1
    db.flush()


def location_is_empty(db: Session, loc_id: int) -> bool:
    tc = TreeCache(db)
    ids = [loc_id, *tc.descendants(loc_id)]
    return not db.scalar(select(func.count(StockLot.id)).where(StockLot.location_id.in_(ids)))


def delete_location(db: Session, loc: Location, move_contents_to: int | None = None) -> None:
    tc = TreeCache(db)
    ids = [loc.id, *tc.descendants(loc.id)]
    if move_contents_to in ids:
        raise ValueError("Das Ziel liegt innerhalb des zu löschenden Lagerorts.")
    lots = db.scalars(select(StockLot).where(StockLot.location_id.in_(ids))).all()
    if lots and move_contents_to is None:
        raise ValueError("Lagerort ist nicht leer – bitte Ziel für den Inhalt wählen.")
    for lot in lots:
        lot.location_id = move_contents_to
    for lid in reversed(ids):  # Kinder zuerst
        obj = db.get(Location, lid)
        if obj:
            db.delete(obj)
    db.flush()


# ------------------------------------------------------------------ Text-Massenanlage
@dataclass
class OutlineNode:
    name: str
    depth: int
    children: list["OutlineNode"] = field(default_factory=list)


def parse_outline(text: str) -> list[OutlineNode]:
    """Eingerückter Text -> Baum. 2 Leerzeichen oder 1 Tab = eine Ebene."""
    roots: list[OutlineNode] = []
    stack: list[OutlineNode] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        expanded = raw.replace("\t", "  ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        depth = indent // 2
        name = expanded.strip().lstrip("-*• ").strip()
        node = OutlineNode(name, depth)
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def outline_to_text(nodes: list[OutlineNode], depth: int = 0) -> str:
    out = []
    for n in nodes:
        out.append("  " * depth + n.name)
        if n.children:
            out.append(outline_to_text(n.children, depth + 1))
    return "\n".join(out)


def guess_type(name: str) -> str:
    n = name.lower()
    if re.search(r"(oben|unten|fach|schublade|register)\s*\d+$", n):
        return "fach"
    for key, t in (("magazin", "magazin"), ("sortierbox", "sortierbox"), ("schublade", "fach"), ("fach", "fach"), ("register", "fach"),
                   ("abteil", "abteil"), ("regal", "regal"), ("schrank", "schrank"), ("kiste", "kiste"), ("box", "kiste"),
                   ("lochwand", "lochwand"), ("keller", "raum"), ("garage", "raum"), ("raum", "raum"), ("schuppen", "raum"),
                   ("zimmer", "raum"), ("werkstatt", "raum"), ("garten", "bereich"), ("wand", "lagerwand")):
        if key in n:
            return t
    return "sonstiges"


def create_from_outline(db: Session, text: str, parent_id: int | None = None) -> int:
    count = 0

    def _add(nodes: list[OutlineNode], pid: int | None):
        nonlocal count
        for n in nodes:
            existing = db.scalar(select(Location).where(Location.parent_id == pid, Location.name == n.name))
            loc = existing or create_location(db, n.name, pid, guess_type(n.name))
            if not existing:
                count += 1
            _add(n.children, loc.id)

    _add(parse_outline(text), parent_id)
    return count


def export_outline(db: Session, parent_id: int | None = None) -> str:
    tc = TreeCache(db)
    return "\n".join("  " * d + n.name for d, n in tc.walk(parent_id))


# ------------------------------------------------------------------ Lagermöbel-Assistent
MAX_GRID = 60


@dataclass
class FurnitureSpec:
    name: str
    type: str = "magazin"
    rows: int = 4
    cols: int = 8
    row_counts: list[int] | None = None  # unterschiedliche Fachzahl je Reihe
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)  # (row, col, rowspan, colspan), 1-basiert
    gaps: list[tuple[int, int]] = field(default_factory=list)  # leere Plätze (row, col); row 0 = in allen Reihen
    naming: str = "letter_number"  # letter_number (A1), number (1..n), row_col (R1-F1), prefix
    prefix: str = "Fach "
    count_direction: str = "rows"  # rows | cols | bands (blockweise: je Reihenblock links -> rechts, oben -> unten)
    bottom_up: bool = False
    subdivision: int = 0  # Abteile je Fach (0/1 = keine)
    subdivision_layout: str = "row"  # row (nebeneinander) | column (hintereinander) | grid
    subdivision_naming: str = "dot"  # dot (B3.1) | words (links/mitte/rechts)
    light_mode: str = "both"
    # LED-Mapping
    controller_id: int | None = None
    led_start: int = 0
    leds_per_cell: int = 3
    led_gap: int = 0
    led_row_gap: int = 0
    serpentine: bool = True
    led_start_corner: str = "top_left"  # top_left | top_right | bottom_left | bottom_right
    led_direction: str = "rows"  # rows | cols
    # Ansteuerung (v0.30): "cell" = LEDs je Fach (voll), "xy" = je Reihe eine LED am Rand + je Spalte eine LED am oberen/unteren Rand;
    # ein Fach leuchtet dann als Kreuzungspunkt von Zeilen- und Spalten-LED und spart LEDs.
    led_mode: str = "cell"  # cell | xy
    xy_rows_side: str = "left"  # left | right (Seite der Zeilen-LEDs)
    xy_cols_side: str = "top"  # top | bottom (Seite der Spalten-LEDs)
    xy_first: str = "rows"  # rows | cols (welche Gruppe zuerst am Strip hängt)
    xy_rows_reverse: bool = False  # Zeilen-LEDs von unten nach oben nummeriert
    xy_cols_reverse: bool = False  # Spalten-LEDs von rechts nach links nummeriert
    xy_gap: int = 0  # LEDs (Kabelbrücke) zwischen den beiden Gruppen

    def __post_init__(self) -> None:
        # Grenzen schützen vor versehentlich riesigen Rastern (Pi Zero!)
        self.rows = max(1, min(int(self.rows), MAX_GRID))
        self.cols = max(1, min(int(self.cols), MAX_GRID))
        if self.row_counts:
            self.row_counts = [max(1, min(int(n), MAX_GRID)) for n in self.row_counts[:MAX_GRID]]
        self.subdivision = max(0, min(int(self.subdivision or 0), 12))
        self.leds_per_cell = max(1, min(int(self.leds_per_cell), 100))
        self.led_mode = self.led_mode if self.led_mode in ("cell", "xy") else "cell"
        self.xy_rows_side = self.xy_rows_side if self.xy_rows_side in ("left", "right") else "left"
        self.xy_cols_side = self.xy_cols_side if self.xy_cols_side in ("top", "bottom") else "top"
        self.xy_first = self.xy_first if self.xy_first in ("rows", "cols") else "rows"
        self.xy_rows_reverse, self.xy_cols_reverse = bool(self.xy_rows_reverse), bool(self.xy_cols_reverse)
        self.xy_gap = max(0, min(int(self.xy_gap or 0), 500))

    def to_template(self) -> dict:
        d = dict(self.__dict__)
        d.pop("name", None)
        d.pop("controller_id", None)
        d.pop("led_start", None)
        return d


def _cells(spec: FurnitureSpec) -> list[dict]:
    """Rasterzellen unter Berücksichtigung zusammengefasster Zellen."""
    covered = set()
    for (r, c) in spec.gaps or []:  # leere Plätze (z. B. Lücke in der Mitte)
        for rr in (range(1, spec.rows + 1) if int(r) == 0 else [int(r)]):
            covered.add((rr, int(c)))
    merged = {}
    for (r, c, rs, cs) in spec.merges:
        merged[(r, c)] = (rs, cs)
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                if (rr, cc) != (r, c):
                    covered.add((rr, cc))
    cells = []
    for r in range(1, spec.rows + 1):
        # row_counts = Anzahl Schubladen in der Reihe; eine breite (zusammengefasste) Schublade zählt als eine
        want = spec.row_counts[r - 1] if spec.row_counts and r - 1 < len(spec.row_counts) else None
        c, placed = 1, 0
        while c <= MAX_GRID:
            if (want is None and c > spec.cols) or (want is not None and placed >= want):
                break
            if (r, c) in covered:
                c += 1
                continue
            rs, cs = merged.get((r, c), (1, 1))
            cells.append({"row": r, "col": c, "rowspan": rs, "colspan": cs})
            placed += 1
            c += cs
    return cells


def row_widths(spec: FurnitureSpec, cells: list[dict] | None = None) -> list[int]:
    """Belegte Rasterbreite (Spalten) je Reihe – für LED-Verlauf und Darstellung."""
    cells = cells if cells is not None else _cells(spec)
    widths = [0] * spec.rows
    for c in cells:
        for r in range(c["row"], min(c["row"] + c["rowspan"], spec.rows + 1)):
            widths[r - 1] = max(widths[r - 1], c["col"] + c["colspan"] - 1)
    return widths


def grid_width(spec: FurnitureSpec, cells: list[dict] | None = None) -> int:
    return max([spec.cols if not spec.row_counts else 1] + row_widths(spec, cells))


def row_bands(cells: list[dict]) -> dict[int, int]:
    """Reihe -> Blocknummer. Ein Block umfasst alle Reihen, die hohe (zusammengefasste) Schubladen gemeinsam überspannen."""
    rows = sorted({c["row"] for c in cells})
    band_of, band, end = {}, -1, 0
    for r in rows:
        if r > end:
            band += 1
            end = r
        end = max([end] + [c["row"] + c["rowspan"] - 1 for c in cells if c["row"] == r])
        band_of[r] = band
    return band_of


def order_cells(spec: FurnitureSpec, cells: list[dict]) -> list[dict]:
    """Zählreihenfolge der Fächer (für Namen und für das Zuordnen vorhandener Fächer)."""
    if spec.count_direction == "cols":
        key = (lambda c: (c["col"], -c["row"])) if spec.bottom_up else (lambda c: (c["col"], c["row"]))
    elif spec.count_direction == "bands":
        bands = row_bands(cells)
        sign = -1 if spec.bottom_up else 1
        key = lambda c: (sign * bands[c["row"]], c["col"], c["row"])  # noqa: E731
    else:
        key = (lambda c: (-c["row"], c["col"])) if spec.bottom_up else (lambda c: (c["row"], c["col"]))
    return sorted(cells, key=key)


def _cell_name(spec: FurnitureSpec, cell: dict, seq: int) -> str:
    r, c = cell["row"], cell["col"]
    if spec.bottom_up:
        r = spec.rows - (r + cell["rowspan"] - 1) + 1  # nach unterster belegter Reihe nummerieren
    if spec.naming == "number":
        return f"{spec.prefix}{seq}".strip()
    if spec.naming == "row_col":
        return f"R{r}-F{c}"
    if spec.naming == "prefix":
        return f"{spec.prefix}{seq:02d}"
    # letter_number: Buchstabe = Reihe, Zahl = Spalte (A1, A2, …)
    letters = ""
    n = r
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{c}"


def led_mapping(spec: FurnitureSpec, cells: list[dict]) -> dict[tuple[int, int], tuple[int, int]]:
    """Berechnet (led_start, led_count) je Zelle anhand der Verkabelung.

    Modell: Der Strip läuft Reihe für Reihe (bzw. Spalte für Spalte) über die Front, je Rastereinheit
    `leds_per_cell` LEDs plus `led_gap` zwischen Einheiten; optional Schlangenlinie.
    """
    rows = spec.rows
    widths = row_widths(spec, _cells(spec))
    cols = max([spec.cols if not spec.row_counts else 1] + widths)
    lines = list(range(1, (rows if spec.led_direction == "rows" else cols) + 1))
    along = cols if spec.led_direction == "rows" else rows
    if spec.led_start_corner.startswith("bottom") and spec.led_direction == "rows":
        lines.reverse()
    if spec.led_start_corner.endswith("right") and spec.led_direction == "cols":
        lines.reverse()
    reverse_first = spec.led_start_corner.endswith("right") if spec.led_direction == "rows" else spec.led_start_corner.startswith("bottom")
    unit_led: dict[tuple[int, int], int] = {}
    pos = spec.led_start
    for i, line in enumerate(lines):
        line_len = along
        if spec.led_direction == "rows" and spec.row_counts and line - 1 < len(widths):
            line_len = widths[line - 1]
        order = list(range(1, line_len + 1))
        rev = reverse_first ^ (spec.serpentine and i % 2 == 1)
        if rev:
            order.reverse()
        for a in order:
            key = (line, a) if spec.led_direction == "rows" else (a, line)
            unit_led[key] = pos
            pos += spec.leds_per_cell + spec.led_gap
        pos += spec.led_row_gap
    out = {}
    for cell in cells:
        # Zusammengefasste Zellen: LEDs der ersten Reihe/Spalte der Zelle entlang des Strips
        units = []
        if spec.led_direction == "rows":
            units = [(cell["row"], c) for c in range(cell["col"], cell["col"] + cell["colspan"])]
        else:
            units = [(r, cell["col"]) for r in range(cell["row"], cell["row"] + cell["rowspan"])]
        starts = sorted(unit_led[u] for u in units if u in unit_led)
        if not starts:
            continue
        first, last = starts[0], starts[-1]
        out[(cell["row"], cell["col"])] = (first, last - first + spec.leds_per_cell)
    return out


def xy_layout(spec: FurnitureSpec, rows: int, cols: int, start: int) -> tuple[list[int], list[int], int]:
    """Kreuz-Modus: (LED-Nummer je Zeile, LED-Nummer je Spalte, benutzte LEDs). Zeile 1 = oben, Spalte 1 = links."""
    def group(n: int, first: int, reverse: bool) -> list[int]:
        idx = [first + i for i in range(n)]
        return idx[::-1] if reverse else idx

    if spec.xy_first == "rows":
        row_leds = group(rows, start, spec.xy_rows_reverse)
        col_leds = group(cols, start + rows + spec.xy_gap, spec.xy_cols_reverse)
    else:
        col_leds = group(cols, start, spec.xy_cols_reverse)
        row_leds = group(rows, start + cols + spec.xy_gap, spec.xy_rows_reverse)
    return row_leds, col_leds, rows + cols + spec.xy_gap


def xy_cell_ranges(row_leds: list[int], col_leds: list[int], cell: dict) -> list[tuple[int, int]]:
    """LED-Bereiche einer Zelle im Kreuz-Modus: Zeilen-LED(s) und Spalten-LED(s), die sie überdeckt (zusammengefasste Zellen = mehrere)."""
    r0, c0 = cell["row"] - 1, cell["col"] - 1
    rr = row_leds[r0:r0 + cell.get("rowspan", 1)]
    cc = col_leds[c0:c0 + cell.get("colspan", 1)]
    out = []
    for grp in (rr, cc):
        if grp:
            out.append((min(grp), max(grp) - min(grp) + 1))
    return out


SUBDIV_WORDS = {2: ["links", "rechts"], 3: ["links", "mitte", "rechts"]}
SUBDIV_WORDS_DEPTH = {2: ["vorne", "hinten"], 3: ["vorne", "mitte", "hinten"]}


def create_furniture(db: Session, spec: FurnitureSpec, parent_id: int | None) -> Location:
    furniture = create_location(db, spec.name, parent_id, spec.type, structural=True)
    furniture.grid_rows = spec.rows
    furniture.grid_cols = grid_width(spec)
    furniture.light_mode = spec.light_mode
    # Benennung und Verkabelung merken (für späteres Umbauen und LED-Ketten in der Wandansicht)
    furniture.layout = {"naming": spec.naming, "prefix": spec.prefix, "bottom_up": spec.bottom_up, "count_direction": spec.count_direction}
    if spec.controller_id:
        furniture.led_spec = {k: getattr(spec, k) for k in ("leds_per_cell", "led_gap", "led_row_gap", "serpentine", "led_start_corner", "led_direction")}
    cells = order_cells(spec, _cells(spec)) if spec.count_direction != "rows" else _cells(spec)
    leds = led_mapping(spec, cells) if spec.controller_id else {}
    for seq, cell in enumerate(cells, start=1):
        loc = create_location(
            db, _cell_name(spec, cell, seq), furniture.id, "fach", sort_order=seq,
            grid_row=cell["row"], grid_col=cell["col"], grid_rowspan=cell["rowspan"], grid_colspan=cell["colspan"],
        )
        if spec.controller_id and (cell["row"], cell["col"]) in leds:
            start, count = leds[(cell["row"], cell["col"])]
            db.add(LocationLight(location_id=loc.id, controller_id=spec.controller_id, led_start=start, led_count=count))
        if spec.subdivision and spec.subdivision > 1:
            add_compartments(db, loc, spec.subdivision, spec.subdivision_layout, spec.subdivision_naming)
    db.flush()
    return furniture


def add_compartments(db: Session, fach: Location, count: int, layout: str = "row", naming: str = "dot") -> list[Location]:
    out = []
    words = (SUBDIV_WORDS_DEPTH if layout == "column" else SUBDIV_WORDS).get(count)
    for i in range(1, count + 1):
        name = f"{fach.name}.{i}" if naming != "words" or not words else f"{fach.name} {words[i - 1]}"
        out.append(create_location(db, name, fach.id, "abteil", sort_order=i, compartment_index=i, compartment_count=count, compartment_layout=layout))
    return out


BUILTIN_TEMPLATES = {
    "Magazin 4×8 (32 Schubladen)": FurnitureSpec(name="", rows=8, cols=4),
    "Magazin 4×8 + 2 breite unten": FurnitureSpec(name="", rows=9, cols=4, merges=[(9, 1, 1, 2), (9, 3, 1, 2)]),
    "Magazin 5×12 (60 Schubladen)": FurnitureSpec(name="", rows=12, cols=5),
    "Magazin 3×5 große Schubladen": FurnitureSpec(name="", rows=5, cols=3),
    "Regal 5×5": FurnitureSpec(name="", type="regal", rows=5, cols=5, naming="row_col"),
    "Sortimentskasten 4×6": FurnitureSpec(name="", type="sortierbox", rows=4, cols=6, naming="number", prefix="Fach "),
}


def ensure_builtin_templates(db: Session) -> None:
    for name, spec in BUILTIN_TEMPLATES.items():
        if not db.scalar(select(StorageTemplate).where(StorageTemplate.name == name)):
            db.add(StorageTemplate(name=name, kind=spec.type, definition=spec.to_template(), builtin=True))
    db.flush()


def spec_from_template(tpl: StorageTemplate, name: str, **overrides) -> FurnitureSpec:
    d = dict(tpl.definition)
    d.update(overrides)
    d["name"] = name
    d["merges"] = [tuple(m) for m in d.get("merges", [])]
    d["gaps"] = [tuple(g) for g in d.get("gaps", [])]
    allowed = FurnitureSpec.__dataclass_fields__
    return FurnitureSpec(**{k: v for k, v in d.items() if k in allowed})


def spec_from_furniture(db: Session, furniture: Location) -> dict:
    """Rekonstruiert eine Vorlage aus einem angelegten Möbel (für "Als Vorlage speichern")."""
    cells = [c for c in furniture.children if c.grid_row]
    merges = [(c.grid_row, c.grid_col, c.grid_rowspan, c.grid_colspan) for c in cells if c.grid_rowspan > 1 or c.grid_colspan > 1]
    sub = max((len([k for k in c.children if k.type == "abteil"]) for c in cells), default=0)
    layout = next((k.compartment_layout for c in cells for k in c.children if k.compartment_layout), "row")
    rows, cols = furniture.grid_rows or 1, furniture.grid_cols or 1
    occupied = {(r, c) for x in cells for r in range(x.grid_row, x.grid_row + x.grid_rowspan) for c in range(x.grid_col, x.grid_col + x.grid_colspan)}
    gaps = [(r, c) for r in range(1, rows + 1) for c in range(1, cols + 1) if (r, c) not in occupied]
    meta = furniture.layout or {}
    spec = FurnitureSpec(name="", type=furniture.type, rows=rows, cols=cols, merges=merges, gaps=gaps,
                         subdivision=sub, subdivision_layout=layout, light_mode=furniture.light_mode,
                         naming=meta.get("naming", "letter_number"), prefix=meta.get("prefix", "Fach "),
                         count_direction=meta.get("count_direction", "rows"), bottom_up=bool(meta.get("bottom_up")))
    return spec.to_template()


def duplicate_furniture(db: Session, furniture: Location, new_name: str) -> Location:
    """Dupliziert ein Möbel inkl. Unterteilung; LED-Start wird hinter das Original gesetzt."""
    copy = create_location(db, new_name, furniture.parent_id, furniture.type, structural=True,
                           grid_rows=furniture.grid_rows, grid_cols=furniture.grid_cols, light_mode=furniture.light_mode,
                           layout=dict(furniture.layout) if furniture.layout else None, led_spec=dict(furniture.led_spec) if furniture.led_spec else None,
                           wall_w=furniture.wall_w, wall_h=furniture.wall_h)
    all_lights = [lt for c in furniture.children for lt in c.lights]
    offset = 0
    if all_lights:
        lo = min(lt.led_start for lt in all_lights)
        hi = max(lt.led_start + lt.led_count for lt in all_lights)
        offset = hi - lo
    for c in furniture.children:
        nc = create_location(db, c.name, copy.id, c.type, sort_order=c.sort_order, grid_row=c.grid_row, grid_col=c.grid_col,
                             grid_rowspan=c.grid_rowspan, grid_colspan=c.grid_colspan)
        for lt in c.lights:
            db.add(LocationLight(location_id=nc.id, controller_id=lt.controller_id, led_start=lt.led_start + offset, led_count=lt.led_count, color=lt.color))
        for k in c.children:
            create_location(db, k.name, nc.id, k.type, sort_order=k.sort_order, compartment_index=k.compartment_index,
                            compartment_count=k.compartment_count, compartment_layout=k.compartment_layout)
    db.flush()
    return copy


def assign_grid(db: Session, furniture: Location, rows: int, cols: int, count_direction: str = "rows", bottom_up: bool = False,
                merges: list[tuple[int, int, int, int]] | None = None, row_counts: list[int] | None = None,
                gaps: list[tuple[int, int]] | None = None) -> int:
    """Ordnet bestehenden Kind-Orten (z. B. importierte „Fach1…Fach33“) Rasterpositionen zu (natürliche Sortierung)."""
    spec = FurnitureSpec(name=furniture.name, rows=rows, cols=cols, merges=merges or [], count_direction=count_direction, bottom_up=bottom_up,
                         row_counts=row_counts, gaps=gaps or [])
    rows, cols = spec.rows, grid_width(spec)
    cells = order_cells(spec, _cells(spec))
    children = sorted([c for c in furniture.children if c.type != "abteil" or not c.compartment_index], key=lambda c: natural_key(c.name))
    for cell, child in zip(cells, children, strict=False):
        child.grid_row, child.grid_col, child.grid_rowspan, child.grid_colspan = cell["row"], cell["col"], cell["rowspan"], cell["colspan"]
        if child.type == "sonstiges":
            child.type = "fach"
    for child in children[len(cells):]:
        child.grid_row = child.grid_col = None
    furniture.grid_rows, furniture.grid_cols = rows, cols
    furniture.structural = True
    db.flush()
    return min(len(cells), len(children))
