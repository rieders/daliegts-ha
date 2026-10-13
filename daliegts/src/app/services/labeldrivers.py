"""Druckersprachen für den Direktdruck (v0.19) – eigene Umsetzung, keine GPL-Abhängigkeit.

* Brother QL (Raster-Befehle nach Brothers „Raster Command Reference“ für die QL-Serie)
* Zebra (ZPL II, Etikett als Grafik ^GFA)
* TSC/Xprinter/Munbyn/Godex … (TSPL, BITMAP)
* Systemdrucker über CUPS (``lp``) – jeder Drucker mit Linux-Treiber

Alle Treiber bekommen fertige Graustufenbilder in Druckerauflösung und liefern die Bytes für den Drucker.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from PIL import Image, ImageOps

# ------------------------------------------------------------------ Brother QL: Geräte- und Mediendaten
# Werte aus Brothers Dokumentation (Druckkopf 720 bzw. 1296 Punkte, bedruckbare Breite, Rand rechts, Vorschub).


@dataclass(frozen=True)
class BrotherModel:
    bytes_per_row: int  # 90 = 720 Punkte, 162 = 1296 Punkte (QL-10xx/11xx)
    extra_offset_r: int
    mode_switch: bool  # ESC i a 01 (Rastermodus) nötig
    cutter: bool
    expanded: bool  # ESC i K
    invalidate: int  # Anzahl 0x00 zum Zurücksetzen
    compression: bool = False  # kennt „M“ (Kompression an/aus)


_Q = BrotherModel
BROTHER_MODELS: dict[str, BrotherModel] = {
    "QL-500": _Q(90, 0, False, False, False, 200, False), "QL-550": _Q(90, 0, False, True, True, 200, False), "QL-560": _Q(90, 0, False, True, True, 200, False),
    "QL-570": _Q(90, 0, False, True, True, 200, False), "QL-580N": _Q(90, 0, True, True, True, 200, True), "QL-600": _Q(90, 0, True, True, True, 200, True),
    "QL-650TD": _Q(90, 0, True, True, True, 200, True), "QL-700": _Q(90, 0, False, True, True, 200, False), "QL-710W": _Q(90, 0, True, True, True, 200, True),
    "QL-720NW": _Q(90, 0, True, True, True, 200, True), "QL-800": _Q(90, 0, True, True, True, 400, False), "QL-810W": _Q(90, 0, True, True, True, 400, True),
    "QL-820NWB": _Q(90, 0, True, True, True, 400, True), "QL-1050": _Q(162, 44, True, True, True, 200, True), "QL-1060N": _Q(162, 44, True, True, True, 200, True),
    "QL-1100": _Q(162, 44, True, True, True, 200, True), "QL-1110NWB": _Q(162, 44, True, True, True, 200, True), "QL-1115NWB": _Q(162, 44, True, True, True, 200, True),
}
WIDE = ("QL-1050", "QL-1060N", "QL-1100", "QL-1110NWB", "QL-1115NWB")


@dataclass(frozen=True)
class BrotherMedia:
    width_mm: int
    length_mm: int  # 0 = Endlosband
    kind: str  # endless | die | round
    dots_w: int  # bedruckbare Breite (Punkte)
    dots_l: int  # bedruckbare Länge (Einzeletiketten)
    offset_r: int
    feed: int  # Vorschub/Rand in Punkten
    only: tuple = ()

    @property
    def endless(self) -> bool:
        return self.kind == "endless"


_M = BrotherMedia
BROTHER_MEDIA: dict[str, BrotherMedia] = {
    "12": _M(12, 0, "endless", 106, 0, 29, 35), "29": _M(29, 0, "endless", 306, 0, 6, 35), "38": _M(38, 0, "endless", 413, 0, 12, 35),
    "50": _M(50, 0, "endless", 554, 0, 12, 35), "54": _M(54, 0, "endless", 590, 0, 0, 35), "62": _M(62, 0, "endless", 696, 0, 12, 35),
    "62red": _M(62, 0, "endless", 696, 0, 12, 35), "102": _M(102, 0, "endless", 1164, 0, 12, 35, WIDE),
    "17x54": _M(17, 54, "die", 165, 566, 0, 0), "17x87": _M(17, 87, "die", 165, 956, 0, 0), "23x23": _M(23, 23, "die", 202, 202, 42, 0),
    "29x42": _M(29, 42, "die", 306, 425, 6, 0), "29x90": _M(29, 90, "die", 306, 991, 6, 0), "39x90": _M(38, 90, "die", 413, 991, 12, 0),
    "39x48": _M(39, 48, "die", 425, 495, 6, 0), "52x29": _M(52, 29, "die", 578, 271, 0, 0), "54x29": _M(54, 29, "die", 598, 271, 60, 0),
    "60x86": _M(60, 87, "die", 672, 954, 18, 0), "62x29": _M(62, 29, "die", 696, 271, 12, 0), "62x100": _M(62, 100, "die", 696, 1109, 12, 0),
    "102x51": _M(102, 51, "die", 1164, 526, 12, 0, WIDE), "102x152": _M(102, 153, "die", 1164, 1660, 12, 0, WIDE),
    "d12": _M(12, 12, "round", 94, 94, 113, 35), "d24": _M(24, 24, "round", 236, 236, 42, 0), "d58": _M(58, 58, "round", 618, 618, 51, 0),
}


def brother_media_label(key: str) -> str:
    m = BROTHER_MEDIA[key]
    if m.endless:
        return f"{m.width_mm} mm Endlosband" + (" (schwarz/rot)" if "red" in key else "")
    return f"{m.width_mm}×{m.length_mm} mm " + ("rund" if m.kind == "round" else "Einzeletiketten")


def _mono(im: Image.Image, threshold_pct: int) -> Image.Image:
    """Graustufen -> 1 Bit. threshold_pct wie bei Brother-Treibern: höher = mehr wird schwarz."""
    lim = int((100 - max(1, min(threshold_pct, 99))) / 100 * 255)
    return im.convert("L").point(lambda x: 0 if 255 - x >= lim else 255, mode="1")  # 70 % -> alles bis 70 % Helligkeit wird schwarz


def brother_ql(images: list[Image.Image], model: str, media: str, cut: bool = True, hq: bool = True, threshold: int = 70) -> bytes:
    """Druckauftrag im Brother-Rasterformat. Bilder: Breite = bedruckbare Breite des Mediums (quer zum Band)."""
    mdl, med = BROTHER_MODELS[model], BROTHER_MEDIA[media]
    width_px = mdl.bytes_per_row * 8
    right = med.offset_r + mdl.extra_offset_r
    out = bytearray(b"\x1bia\x01" if mdl.mode_switch else b"")  # Rastermodus (vor dem Zurücksetzen und danach)
    out += b"\x00" * mdl.invalidate
    out += b"\x1b@"  # initialisieren
    if mdl.mode_switch:
        out += b"\x1bia\x01"
    for i, im in enumerate(images):
        mono = _mono(im, threshold)
        if mono.width != med.dots_w:
            raise ValueError(f"Bildbreite {mono.width} passt nicht zum Medium ({med.dots_w} Punkte)")
        rows = mono.height
        out += b"\x1biS"  # Statusabfrage je Seite (Antwort wird nicht gebraucht)
        flags = 0x80 | 0x02 | 0x04 | 0x08 | (0x40 if hq else 0)
        kind = 0x0A if med.endless else 0x0B
        out += b"\x1biz" + bytes([flags, kind, med.width_mm & 0xFF, med.length_mm & 0xFF]) + struct.pack("<L", rows) + bytes([0, 0])  # Seitenangabe 0: so drucken die Geräte in der Praxis zuverlässig
        if mdl.cutter and cut:
            out += b"\x1biM" + bytes([0x40]) + b"\x1biA" + bytes([1])  # automatisch schneiden, nach jedem Etikett
        if mdl.expanded:
            out += b"\x1biK" + bytes([0x08 if cut else 0])
        out += b"\x1bid" + struct.pack("<H", med.feed)
        if mdl.compression:
            out += b"M\x00"  # ohne Kompression
        # Kopfzeile: Bild rechtsbündig mit Rand, gespiegelt, schwarz = 1
        line = Image.new("1", (width_px, rows), 255)
        line.paste(mono, (width_px - mono.width - right, 0))
        data = ImageOps.invert(line.convert("L")).transpose(Image.FLIP_LEFT_RIGHT).convert("1").tobytes()
        bpr = mdl.bytes_per_row
        for r in range(rows):
            out += b"g\x00" + bytes([bpr]) + data[r * bpr:(r + 1) * bpr]
        out += b"\x1a" if i == len(images) - 1 else b"\x0c"
    return bytes(out)


# ------------------------------------------------------------------ Zebra ZPL II
def _packed(im: Image.Image, threshold: int, black_is_one: bool = True) -> tuple[bytes, int]:
    mono = _mono(im, threshold)
    w8 = (mono.width + 7) // 8 * 8
    canvas = Image.new("1", (w8, mono.height), 255)
    canvas.paste(mono, (0, 0))
    data = ImageOps.invert(canvas.convert("L")).convert("1").tobytes() if black_is_one else canvas.tobytes()
    return data, w8 // 8


def zpl(images: list[Image.Image], threshold: int = 70, dark: int | None = None, x_dots: int = 0, y_dots: int = 0) -> bytes:
    """Jedes Etikett als ^GFA-Grafik (unkomprimiert, hex). ^PW/^LL aus der Bildgröße."""
    out = []
    for im in images:
        data, bpr = _packed(im, threshold)
        hexdata = data.hex().upper()
        out.append("^XA" + (f"~SD{max(0, min(dark, 30)):02d}" if dark is not None else "") +
                   f"^PW{im.width}^LL{im.height}^LH0,0^FO{x_dots},{y_dots}^GFA,{len(data)},{len(data)},{bpr},{hexdata}^FS^PQ1^XZ")
    return "\n".join(out).encode("ascii")


# ------------------------------------------------------------------ TSPL (TSC, Xprinter, Munbyn, Godex im TSPL-Modus …)
def tspl(images: list[Image.Image], width_mm: float, height_mm: float, gap_mm: float = 2.0, threshold: int = 70,
         density: int | None = None, invert: bool = False) -> bytes:
    """BITMAP-Befehl: bei TSPL steht ein 0-Bit für einen schwarzen Punkt (invert=True, falls ein Drucker es andersherum macht)."""
    out = bytearray()
    for im in images:
        data, bpr = _packed(im, threshold, black_is_one=invert)
        out += f"SIZE {width_mm:g} mm,{height_mm:g} mm\r\nGAP {gap_mm:g} mm,0 mm\r\nDIRECTION 1\r\n".encode()
        if density is not None:
            out += f"DENSITY {max(0, min(density, 15))}\r\n".encode()
        out += f"CLS\r\nBITMAP 0,0,{bpr},{im.height},0,".encode() + data + b"\r\nPRINT 1,1\r\n"
    return bytes(out)
