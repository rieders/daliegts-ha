"""Cover per Handykamera (v0.34.4): Foto + vier Eckpunkte -> perspektivisch entzerrtes Cover.

Der Browser verkleinert große Fotos vorher auf höchstens 2500 px und schickt die vier Punkte in Pixeln des
geschickten Bildes (so, wie es angezeigt wird – also nach EXIF-Drehung). Hier: EXIF-Drehung anwenden, zur
Sicherheit nochmals begrenzen, Punkte ordnen, mit Image.transform(PERSPECTIVE) entzerren. Ergebnis als WebP;
gespeichert wird es erst beim Speichern des Formulars (wie ein hochgeladenes Cover über store_upload).
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageOps

MAX_INPUT_BYTES = 30 * 1024 * 1024  # Handyfotos sind nach dem Verkleinern im Browser meist < 2 MB
MAX_SIDE = 2500  # wie im Browser
SQUARE = 1000  # Quadratisch (LP/CD)
MAX_OUT = 1280  # wie store_upload (größer wird dort ohnehin verkleinert)


class WarpError(ValueError):
    pass


def order_points(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Vier Punkte in der Reihenfolge oben links, oben rechts, unten rechts, unten links – egal, wie gezogen wurde."""
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    ring = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))  # im Uhrzeigersinn (y nach unten)
    start = min(range(4), key=lambda i: ring[i][0] + ring[i][1])
    return ring[start:] + ring[:start]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Lineares Gleichungssystem (Gauß mit Pivotsuche) – 8×8, dafür braucht es kein numpy."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise WarpError("Die vier Punkte bilden kein Viereck.")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                if f:
                    for c in range(col, n + 1):
                        m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def perspective_coeffs(dst: list[tuple[float, float]], src: list[tuple[float, float]]) -> list[float]:
    """Koeffizienten (a…h) für Image.transform(PERSPECTIVE): Ausgabepunkt (x, y) -> Quellpunkt
    ((a·x + b·y + c) / (g·x + h·y + 1), (d·x + e·y + f) / (g·x + h·y + 1)). dst = Ecken im Ergebnis, src = im Foto."""
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    return _solve(a, b)


def _area(p: list[tuple[float, float]]) -> float:
    return abs(sum(p[i][0] * p[(i + 1) % 4][1] - p[(i + 1) % 4][0] * p[i][1] for i in range(4))) / 2


def output_size(pts: list[tuple[float, float]], square: bool) -> tuple[int, int]:
    """Quadratisch: 1000×1000. Sonst Seitenverhältnis aus den mittleren Kantenlängen, längste Seite höchstens 1280 px."""
    if square:
        return SQUARE, SQUARE
    tl, tr, br, bl = pts
    w = (math.dist(tl, tr) + math.dist(bl, br)) / 2
    h = (math.dist(tl, bl) + math.dist(tr, br)) / 2
    k = min(1.0, MAX_OUT / max(w, h, 1))
    return max(1, round(w * k)), max(1, round(h * k))


def load_photo(data: bytes) -> Image.Image:
    """Foto öffnen, EXIF-Drehung anwenden, auf höchstens 2500 px begrenzen."""
    if not data:
        raise WarpError("Kein Bild übertragen.")
    if len(data) > MAX_INPUT_BYTES:
        raise WarpError(f"Das Bild ist zu groß (höchstens {MAX_INPUT_BYTES // (1024 * 1024)} MB).")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception as e:  # noqa: BLE001 – kaputte/unbekannte Datei
        raise WarpError("Das Bild lässt sich nicht lesen.") from e
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def warp(data: bytes, points: list, square: bool = True, src_size: tuple[float, float] | None = None) -> Image.Image:
    """Foto entzerren. points: vier [x, y] in Pixeln des Bildes, wie der Browser es angezeigt hat (src_size = dessen
    Breite/Höhe; fehlt sie, gelten die Pixel des übertragenen Bildes nach EXIF-Drehung)."""
    img = load_photo(data)
    try:
        pts = [(float(p[0]), float(p[1])) for p in points]
    except (TypeError, ValueError, IndexError) as e:
        raise WarpError("Eckpunkte fehlen oder sind ungültig.") from e
    if len(pts) != 4 or not all(math.isfinite(c) for p in pts for c in p):
        raise WarpError("Es braucht genau vier Eckpunkte.")
    if src_size and src_size[0] > 0 and src_size[1] > 0:  # Punkte auf die tatsächliche Bildgröße umrechnen
        sx, sy = img.width / float(src_size[0]), img.height / float(src_size[1])
        pts = [(x * sx, y * sy) for x, y in pts]
    if max(img.size) > MAX_SIDE:
        k = MAX_SIDE / max(img.size)
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.LANCZOS)
        pts = [(x * k, y * k) for x, y in pts]
    pts = [(min(max(x, 0.0), img.width), min(max(y, 0.0), img.height)) for x, y in pts]
    pts = order_points(pts)
    if _area(pts) < 0.0025 * img.width * img.height:
        raise WarpError("Der gewählte Ausschnitt ist zu klein – Eckpunkte an die Ecken des Covers ziehen.")
    w, h = output_size(pts, square)
    coeffs = perspective_coeffs([(0, 0), (w, 0), (w, h), (0, h)], pts)
    return img.transform((w, h), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC)


def to_webp(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality)
    return buf.getvalue()
