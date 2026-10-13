"""Etiketten: QR/Code128 als SVG, Vorlagen in mm, Drucklisten in Rasterreihenfolge."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from functools import lru_cache

import segno

from ..config import settings


@dataclass
class LabelPreset:
    key: str
    name: str
    width_mm: float
    height_mm: float
    symbology: str = "qr"  # qr | code128 | qr+code128
    fields: list[str] = field(default_factory=lambda: ["short", "code"])
    # Bogen (0 = Einzeletikett/Endlos)
    sheet: bool = False
    cols: int = 1
    rows: int = 1
    margin_top: float = 0
    margin_left: float = 0
    gap_x: float = 0
    gap_y: float = 0


PRESETS = {
    p.key: p
    for p in [
        LabelPreset("drawer", "Schubladenfront 30×12 mm", 30, 12, "qr", ["short", "code"]),
        LabelPreset("inventree", "Wie bisher (13-mm-QR + Pfad) 62×17 mm", 62, 17, "qr", ["path"]),
        LabelPreset("tape12", "Band 12 mm (Länge 40 mm)", 40, 12, "qr", ["short", "code"]),
        LabelPreset("tape24", "Band 24 mm (Länge 60 mm)", 60, 24, "qr", ["name", "path", "code"]),
        LabelPreset("tape62", "Endlos 62×29 mm (Brother QL)", 62, 29, "qr", ["name", "path", "code"]),
        LabelPreset("brother55x16", "Brother Endlos 55×16 mm", 55, 16, "qr", ["short", "parent", "code"]),
        LabelPreset("box", "Kistenetikett 100×50 mm", 100, 50, "qr+code128", ["name", "path", "content", "code"]),
        LabelPreset("a4_3x8", "A4-Bogen 3×8 (70×37 mm)", 70, 37, "qr", ["name", "path", "code"], True, 3, 8, 0, 0, 0, 0),
        LabelPreset("a4_4x12", "A4-Bogen 4×12 (48,5×25,4 mm)", 48.5, 25.4, "qr", ["short", "path", "code"], True, 4, 12, 21.5, 8, 0, 0),
        LabelPreset("a4_5x13", "A4-Bogen 5×13 (38×21 mm)", 38.1, 21.2, "qr", ["short", "code"], True, 5, 13, 10.7, 4.65, 2.5, 0),
    ]
}


def qr_url(code: str) -> str:
    return f"{settings.base_url}/s/{code}"


@lru_cache(maxsize=4096)
def qr_svg(data: str) -> str:
    q = segno.make(data, error="m", micro=False)
    buf = io.BytesIO()
    q.save(buf, kind="svg", scale=1, border=2, xmldecl=False, svgns=True, nl=False, dark="#000")
    svg = buf.getvalue().decode()
    # viewBox aus der Originalgröße, damit der QR-Code die ganze Etikettenfläche ausfüllt (skalierbar)
    w = re.search(r'width="([\d.]+)"', svg).group(1)
    h = re.search(r'height="([\d.]+)"', svg).group(1)
    svg = re.sub(r'(width|height)="[^"]+"', "", svg, count=2)
    return svg.replace("<svg ", f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" shape-rendering="crispEdges" ', 1)


@lru_cache(maxsize=4096)
def code128_svg(data: str) -> str:
    from barcode import Code128
    from barcode.writer import SVGWriter

    buf = io.BytesIO()
    Code128(data, writer=SVGWriter()).write(buf, options={"write_text": False, "module_height": 8, "quiet_zone": 1, "module_width": 0.25})
    svg = buf.getvalue().decode()
    svg = re.sub(r"<\?xml[^>]*>", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg)
    svg = re.sub(r'(<svg[^>]*?)\s(width|height)="[^"]+"', r"\1", svg)
    svg = re.sub(r'(<svg[^>]*?)\s(width|height)="[^"]+"', r"\1", svg)
    return svg.replace("<svg", '<svg preserveAspectRatio="none"', 1)


def sheet_pages(labels: list[dict], preset: LabelPreset, start_pos: int = 1) -> list[list[dict | None]]:
    """Verteilt Etiketten auf A4-Bögen; start_pos für angebrochene Bögen (1-basiert)."""
    per_page = preset.cols * preset.rows
    slots: list[dict | None] = [None] * max(0, start_pos - 1) + list(labels)
    return [slots[i:i + per_page] + [None] * (per_page - len(slots[i:i + per_page])) for i in range(0, len(slots), per_page)] or [[None] * per_page]


def custom_preset(width_mm: float, height_mm: float, base: LabelPreset | None = None) -> LabelPreset:
    """Eigene Etikettengröße (Endlosband/Einzeletikett), Inhalt wie die gewählte Vorlage."""
    w = max(10.0, min(float(width_mm), 200.0))
    h = max(6.0, min(float(height_mm), 150.0))
    fields = list(base.fields) if base and not base.sheet else (["short", "code"] if h < 14 else ["short", "path", "code"])
    return LabelPreset("eigen", f"Eigene Größe {w:g}×{h:g} mm", w, h, base.symbology if base and not base.sheet else "qr", fields)
