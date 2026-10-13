"""Direktdruck auf Etikettendrucker (v0.13, Treiber v0.19).

Das Etikett wird serverseitig mit Pillow als Rasterbild in Druckerauflösung gezeichnet – gleiche Aufteilung wie
die HTML-Vorschau (Code links/rechts, Textzeilen in S/M/L/XL, Strichcode unten) – und in der Sprache des
Druckers verschickt: Brother QL (Raster), Zebra (ZPL), TSPL (TSC, Xprinter, Munbyn …) oder über CUPS.
Übertragung: Netzwerk (Port 9100), USB-Gerät am Server (``usb:/dev/usb/lp0``) oder CUPS-Warteschlange.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from . import labeldrivers as drv
from .labeldesign import Spec
from .labels import qr_url

log = logging.getLogger("lagerwerk.print")

DPI = 300
PX_MM = DPI / 25.4
SIZE_FACTOR = {"s": 0.78, "m": 1.0, "l": 1.25, "xl": 1.6}
LINE_HEIGHT = 1.15
MONO_FIELDS = {"code", "location_code"}

DRIVERS = {
    "brother_ql": "Brother QL (Netzwerk, Raster)",
    "zpl": "Zebra (ZPL) – ZD/GK/GX/ZT …",
    "tspl": "TSPL – TSC, Xprinter, Munbyn, Godex …",
    "cups": "Systemdrucker über CUPS (DYMO, Laser, …)",
}
DEFAULT_PORT = {"brother_ql": 9100, "zpl": 9100, "tspl": 9100, "cups": 0}
MODELS = list(drv.BROTHER_MODELS)


def media_choices() -> dict[str, str]:
    return {k: drv.brother_media_label(k) for k in drv.BROTHER_MEDIA if k != "62red"}


@dataclass
class MediaInfo:
    endless: bool
    width_px: int  # bedruckbare Breite quer zum Band
    length_px: int  # nur Einzeletiketten: bedruckbare Länge
    width_mm: float
    length_mm: float


def media_info(media: str) -> MediaInfo:
    m = drv.BROTHER_MEDIA.get(media)
    if m is None:
        raise ValueError(f"Unbekanntes Etikettenformat „{media}“")
    return MediaInfo(m.endless, m.dots_w, m.dots_l, m.width_mm, m.length_mm)


def driver_of(printer) -> str:
    return getattr(printer, "driver", None) or "brother_ql"


def dpi_of(printer) -> int:
    return 300 if driver_of(printer) == "brother_ql" else int(getattr(printer, "dpi", None) or 203)


# ------------------------------------------------------------------ Schriften
_FONT_FILES = {
    "regular": ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf"],
    "bold": ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"],
    "mono": ["/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "DejaVuSansMono.ttf", "cour.ttf"],
}


@lru_cache(maxsize=256)
def font(style: str, px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for f in _FONT_FILES[style]:
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def _fit_text(d: ImageDraw.ImageDraw, text: str, fnt, max_w: float) -> str:
    if d.textlength(text, font=fnt) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # längster Anfang, der mit „…“ passt
        mid = (lo + hi + 1) // 2
        if d.textlength(text[:mid].rstrip() + "…", font=fnt) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…" if lo else ""


def _wrap_text(d: ImageDraw.ImageDraw, text: str, fnt, max_w: float, max_lines: int) -> list[str]:
    """Wortweise umbrechen (zu lange Wörter zeichenweise), höchstens max_lines Zeilen, Rest mit „…“."""
    lines: list[str] = []
    rest = text.split()
    while rest and len(lines) < max_lines:
        if len(lines) == max_lines - 1:  # letzte erlaubte Zeile: alles Übrige, ggf. gekürzt
            lines.append(_fit_text(d, " ".join(rest), fnt, max_w))
            rest = []
            break
        cur = ""
        while rest:
            cand = (cur + " " + rest[0]).strip()
            if d.textlength(cand, font=fnt) <= max_w:
                cur, rest = cand, rest[1:]
                continue
            if not cur:  # einzelnes Wort passt nicht: so viele Zeichen wie möglich
                w = rest[0]
                k = len(w)
                while k > 1 and d.textlength(w[:k], font=fnt) > max_w:
                    k -= 1
                cur, rest = w[:k], ([w[k:]] + rest[1:]) if w[k:] else rest[1:]
            break
        lines.append(cur)
    return lines or [""]


def _mm(v: float, pxmm: float = PX_MM) -> int:
    return int(round(v * pxmm))


def _draw_qr(img: Image.Image, data: str, x: int, y: int, size: int) -> None:
    import segno

    q = segno.make(data, error="m", micro=False)
    m = q.matrix
    n = len(m) + 4  # Ruhezone 2 Module je Seite wie in der Vorschau
    mod = max(1, size // n)
    off_x = x + (size - mod * n) // 2 + 2 * mod
    off_y = y + (size - mod * n) // 2 + 2 * mod
    d = ImageDraw.Draw(img)
    for r, row in enumerate(m):
        for c, v in enumerate(row):
            if v:
                d.rectangle([off_x + c * mod, off_y + r * mod, off_x + (c + 1) * mod - 1, off_y + (r + 1) * mod - 1], fill=0)


def _draw_code128(img: Image.Image, data: str, x: int, y: int, w: int, h: int) -> None:
    from barcode import Code128

    modules = "".join(Code128(data).build())
    total = len(modules) + 2
    unit = w / total
    d = ImageDraw.Draw(img)
    for i, bit in enumerate(modules):
        if bit == "1":
            x0 = x + int(round((i + 1) * unit))
            x1 = x + int(round((i + 2) * unit)) - 1
            d.rectangle([x0, y, max(x0, x1), y + h - 1], fill=0)


def render_label(rec: dict, spec: Spec, dpi: int = DPI) -> Image.Image:
    """Ein Etikett (mit finish() aufbereitet) als Graustufenbild in Etikettengröße und Druckerauflösung."""
    pxmm = dpi / 25.4

    def _mm(v: float) -> int:  # noqa: F811 – Auflösung des Druckers
        return int(round(v * pxmm))

    W, H = _mm(spec.width_mm), _mm(spec.height_mm)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    pad_y = _mm(max(spec.height_mm * 0.05, 0.6))
    pad_x = _mm(max(spec.height_mm * 0.06, 0.8))
    gap = _mm(1.2)
    x0, x1 = pad_x, W - pad_x
    if "qr" in spec.symbology and rec.get("code"):
        q = min(_mm(spec.height_mm - max(spec.height_mm * 0.1, 1.2)), int(W * 0.45))
        qy = (H - q) // 2
        if spec.code_pos == "right":
            _draw_qr(img, qr_url(rec["code"]), x1 - q, qy, q)
            x1 -= q + gap
        else:
            _draw_qr(img, qr_url(rec["code"]), x0, qy, q)
            x0 += q + gap
    base = spec.base_font * pxmm
    rows = []
    for r in rec.get("rows", []):
        px = max(8, int(round(base * SIZE_FACTOR.get(r["size"], 1.0))))
        style = "mono" if r.get("f") in MONO_FIELDS else ("bold" if r.get("bold") else "regular")
        fnt = font(style, px)
        wrap = int(r.get("wrap") or 1)
        parts = _wrap_text(d, str(r["v"]), fnt, x1 - x0, wrap) if wrap > 1 else [_fit_text(d, str(r["v"]), fnt, x1 - x0)]
        lh = int(px * (1.1 if wrap > 1 else LINE_HEIGHT))
        rows.append((parts, fnt, lh))
    bar_h = _mm(spec.height_mm * 0.26) if "code128" in spec.symbology and rec.get("code") else 0
    total = sum(lh * len(parts) for parts, _, lh in rows) + bar_h
    y = max(pad_y, (H - total) // 2)
    for parts, fnt, lh in rows:
        for t in parts:
            if t:
                d.text((x0, y + lh / 2), t, font=fnt, fill=0, anchor="lm")
            y += lh
    if bar_h:
        _draw_code128(img, rec["code"], x0, min(y, H - pad_y - bar_h), x1 - x0, bar_h)
    return img


def test_pattern(printer, spec_w: float = 80) -> Image.Image:
    """Testdruck: Rahmen an der bedruckbaren Kante, Messstriche, Name, Format, Datum, QR – in Druckerauflösung."""
    dpi = dpi_of(printer)
    pxmm = dpi / 25.4

    def mm(v: float) -> int:
        return int(round(v * pxmm))

    if driver_of(printer) == "brother_ql":
        mi = media_info(printer.media)
        width_mm = mi.width_px / pxmm
        length_mm = mi.length_px / pxmm if not mi.endless else spec_w
        W, H = mm(length_mm), mi.width_px  # Bild quer liegend: Länge × Bandbreite
        fmt = f"{printer.model} · {media_choices().get(printer.media, printer.media)}"
    else:
        w, h = printer.media_w_mm or 50, printer.media_h_mm or 30
        W, H = mm(w), mm(h)
        width_mm, length_mm = w, w
        fmt = f"{DRIVERS.get(driver_of(printer), driver_of(printer)).split(' (')[0].split(' –')[0]} · {w:g}×{h:g} mm · {dpi} dpi"
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, H - 1], outline=0, width=max(2, mm(0.25)))
    for x_mm in range(5, int(length_mm), 5):
        ln = mm(2.5 if x_mm % 10 else 4)
        d.line([mm(x_mm), 0, mm(x_mm), ln], fill=0, width=max(1, mm(0.17)))
        d.line([mm(x_mm), H - 1 - ln, mm(x_mm), H - 1], fill=0, width=max(1, mm(0.17)))
    q = min(H - mm(3), mm(18))
    _draw_qr(img, "DaLiegt’s Testdruck", mm(2), (H - q) // 2, q)
    x = mm(3) + q
    lines = [("DaLiegt’s Testdruck", "bold"), (printer.name, "regular"), (fmt, "regular"),
             (f"druckbar {width_mm:.1f} mm · {datetime.now():%d.%m.%Y %H:%M}".replace('.', ',', 1), "regular")]
    px = max(10, min(int(H / (len(lines) * 1.4)), mm(4)))
    y = (H - int(px * LINE_HEIGHT) * len(lines)) // 2
    for t, st in lines:
        fnt = font(st, px)
        d.text((x, y + px * LINE_HEIGHT / 2), _fit_text(d, t, fnt, W - x - mm(2)), font=fnt, fill=0, anchor="lm")
        y += int(px * LINE_HEIGHT)
    return img


# ------------------------------------------------------------------ Auf Band/Etikett einpassen
def place_on_media(label: Image.Image, printer) -> Image.Image:
    """Etikettenbild (quer: Länge × Höhe) auf das Druckmedium bringen.

    Brother Endlosband: Etikett um 90° gedreht, Etikettenhöhe quer zum Band (zentriert, ggf. verkleinert),
    Etikettenlänge = Abschnittlänge. Brother Einzeletiketten: so gedreht/verkleinert, dass es passt.
    Zebra/TSPL/CUPS: Etikett in der eingestellten Drehung auf die Etikettengröße des Druckers (falls angegeben).
    """
    pxmm = dpi_of(printer) / 25.4
    shift = int(round((printer.offset_mm or 0) * pxmm))
    if driver_of(printer) != "brother_ql":
        rot = int(printer.rotate or 0) % 360
        im = label.rotate(rot, expand=True) if rot else label
        if not (printer.media_w_mm and printer.media_h_mm):
            return im
        W, H = int(round(printer.media_w_mm * pxmm)), int(round(printer.media_h_mm * pxmm))
        f = min(W / im.width, H / im.height, 1.0)
        if f < 1.0:
            im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))), Image.LANCZOS)
        canvas = Image.new("L", (W, H), 255)
        canvas.paste(im, ((W - im.width) // 2 + shift, (H - im.height) // 2))
        return canvas
    mi = media_info(printer.media)
    rot = 270 if printer.rotate == 270 else 90
    if mi.endless:
        im = label.rotate(rot, expand=True)  # jetzt: Breite = Etikettenhöhe, Höhe = Etikettenlänge
        if im.width > mi.width_px:
            f = mi.width_px / im.width
            im = im.resize((mi.width_px, max(1, int(im.height * f))), Image.LANCZOS)
        canvas = Image.new("L", (mi.width_px, im.height), 255)
        canvas.paste(im, ((mi.width_px - im.width) // 2 + shift, 0))
        return canvas
    W, H = mi.width_px, mi.length_px
    im = label.rotate(rot, expand=True) if (label.width > label.height) != (W > H) else label
    f = min(W / im.width, H / im.height, 1.0)
    if f < 1.0:
        im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))), Image.LANCZOS)
    canvas = Image.new("L", (W, H), 255)
    canvas.paste(im, ((W - im.width) // 2 + shift, (H - im.height) // 2))
    return canvas


def build_job(images: list[Image.Image], printer) -> bytes:
    thr = max(10, min(int(printer.threshold or 70), 95))
    if driver_of(printer) == "zpl":
        return drv.zpl(images, thr)
    if driver_of(printer) == "tspl":
        pxmm = dpi_of(printer) / 25.4
        w = printer.media_w_mm or round(images[0].width / pxmm, 1)
        h = printer.media_h_mm or round(images[0].height / pxmm, 1)
        return drv.tspl(images, w, h, printer.gap_mm if printer.gap_mm is not None else 2.0, thr, invert=bool(printer.invert))
    if driver_of(printer) == "cups":
        raise ValueError("CUPS-Drucker bekommen PDF, keinen Rohdatenauftrag")
    return drv.brother_ql(images, printer.model, printer.media, cut=bool(printer.cut), hq=bool(printer.hq), threshold=thr)


def _usb_path(printer) -> str | None:
    h = (printer.host or "").strip()
    return h[4:] if h.startswith("usb:") else (h if h.startswith("/dev/") else None)


def send(printer, data: bytes, timeout: float = 15.0) -> None:
    path = _usb_path(printer)
    if path:
        with open(path, "wb", buffering=0) as f:
            f.write(data)
        return
    with socket.create_connection((printer.host, int(printer.port or 9100)), timeout=timeout) as s:
        s.sendall(data)


def _cups_print(printer, images: list[Image.Image]) -> None:
    """Etiketten als PDF (Seitengröße = Etikett) an eine CUPS-Warteschlange."""
    dpi = dpi_of(printer)
    queue = (printer.host or "").strip()
    if not queue:
        raise ValueError("Name der CUPS-Warteschlange fehlt")
    pages = [im.convert("L") for im in images]
    w_mm, h_mm = pages[0].width / dpi * 25.4, pages[0].height / dpi * 25.4
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pages[0].save(tmp, "PDF", resolution=dpi, save_all=True, append_images=pages[1:])
        name = tmp.name
    try:
        cmd = ["lp", "-d", queue, "-o", f"media=Custom.{w_mm:.1f}x{h_mm:.1f}mm", "-o", "fit-to-page", "-t", "DaLiegts-Etiketten", name]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout or "lp fehlgeschlagen").strip()[:250])
    except FileNotFoundError:
        raise OSError("CUPS ist auf dem Server nicht installiert (apt install cups) – oder Brother/Zebra/TSPL direkt verwenden.")
    finally:
        os.unlink(name)


def reachable(printer, timeout: float = 3.0) -> tuple[bool, str]:
    if driver_of(printer) == "cups":
        try:
            r = subprocess.run(["lpstat", "-p", printer.host], capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            return False, "CUPS ist auf dem Server nicht installiert (apt install cups)"
        return (r.returncode == 0), (r.stderr or r.stdout).strip()[:300]
    path = _usb_path(printer)
    if path:
        ok = os.path.exists(path) and os.access(path, os.W_OK)
        return ok, "" if ok else f"{path} fehlt oder ist nicht beschreibbar (Drucker eingesteckt? Benutzer lagerwerk in Gruppe lp?)"
    try:
        with socket.create_connection((printer.host, int(printer.port or 9100)), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]


def print_images(printer, labels: list[Image.Image]) -> None:
    placed = [place_on_media(im, printer) for im in labels]
    if driver_of(printer) == "cups":
        _cups_print(printer, placed)
    else:
        send(printer, build_job(placed, printer))
