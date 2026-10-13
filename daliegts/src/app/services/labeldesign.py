"""Etiketten-Designer (v0.12): Vorlagen je Etikettenart mit frei gewählten Informationen.

Eine Vorlage = Format (Größe oder A4-Bogen) + Code (QR/Code128/keiner, links/rechts) + Textzeilen in Reihenfolge.
Jede Zeile zeigt ein Feld (z. B. Name, Wert, Hersteller-Nr., Lagerort) oder freien Text, in Größe S/M/L/XL, optional fett.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, LabelTemplate, Location, StockLot, SupplierPart
from .labels import PRESETS, code128_svg, qr_svg, qr_url
from .locations import LOCATION_TYPES, TreeCache

KINDS = {"location": "Lagerort / Fach", "item": "Artikel", "lot": "Bestand (Charge, Tüte)"}
SIZES = {"s": "klein", "m": "normal", "l": "groß", "xl": "sehr groß"}
SIZE_FACTOR = {"s": 0.78, "m": 1.0, "l": 1.25, "xl": 1.6}

_ITEM_FIELDS = {
    "name": "Name",
    "description": "Beschreibung",
    "value": "Wert (1. Parameter, z. B. 10 kΩ)",
    "params": "Parameter (bis zu 3)",
    "mpn": "Hersteller-Nr. (MPN)",
    "manufacturer": "Hersteller",
    "footprint": "Gehäuse / Bauform",
    "category": "Kategorie",
    "location": "Lagerort (kurz)",
    "location_path": "Lagerort (ganzer Pfad)",
    "location_code": "Lagerort-Code",
    "stock": "Bestand",
    "min_stock": "Mindestbestand",
    "supplier": "Lieferant + Bestellnr.",
    "price": "Preis je Einheit",
    "tags": "Schlagwörter",
    "code": "Artikel-Code",
}
FIELDS: dict[str, dict[str, str]] = {
    "location": {
        "name": "Name",
        "parent": "übergeordneter Ort",
        "path2": "Pfad kurz (letzte 2 Ebenen)",
        "path": "ganzer Pfad",
        "room": "Raum / Bereich",
        "type": "Art (Fach, Magazin …)",
        "content": "Inhalt (Artikel im Fach)",
        "code": "Lagerort-Code",
    },
    "item": _ITEM_FIELDS,
    "lot": {**{k: v for k, v in _ITEM_FIELDS.items() if k not in ("stock", "code")},
            "qty": "Menge dieses Bestands", "batch": "Charge", "serial": "Seriennummer", "expiry": "Haltbar bis", "created": "Angelegt am",
            "code": "Bestands-Code"},
}
COMMON = {"text": "freier Text", "date": "Druckdatum"}


def field_choices(kind: str) -> dict[str, str]:
    return {**FIELDS.get(kind, {}), **COMMON}


# ------------------------------------------------------------------ Vorlagen als Druckspezifikation
@dataclass
class Spec:
    id: int | None
    name: str
    kind: str
    width_mm: float
    height_mm: float
    symbology: str = "qr"
    code_pos: str = "left"
    lines: list = field(default_factory=list)
    sheet: bool = False
    cols: int = 1
    rows: int = 1
    margin_top: float = 0
    margin_left: float = 0
    gap_x: float = 0
    gap_y: float = 0
    media: str = "eigen"

    @property
    def key(self) -> str:
        return f"t{self.id or 0}"

    @property
    def base_font(self) -> float:
        return round(max(2.0, min(self.height_mm / 4.2, 4.5)), 2)


def spec_of(t: LabelTemplate) -> Spec:
    s = Spec(t.id, t.name, t.kind, t.width_mm, t.height_mm, t.symbology, t.code_pos, list(t.lines or []), media=t.media)
    p = PRESETS.get(t.media)
    if p is not None:
        s.width_mm, s.height_mm = p.width_mm, p.height_mm
        if p.sheet:
            s.sheet, s.cols, s.rows = True, p.cols, p.rows
            s.margin_top, s.margin_left, s.gap_x, s.gap_y = p.margin_top, p.margin_left, p.gap_x, p.gap_y
    return s


def clean_lines(raw) -> list[dict]:
    out = []
    for ln in raw or []:
        if not isinstance(ln, dict):
            continue
        f = str(ln.get("f") or "")[:30]
        if not f:
            continue
        size = ln.get("size") if ln.get("size") in SIZES else "m"
        try:
            wrap = max(1, min(int(ln.get("wrap") or 1), 3))
        except (TypeError, ValueError):
            wrap = 1
        out.append({"f": f, "size": size, "bold": bool(ln.get("bold")), "text": str(ln.get("text") or "")[:120], "wrap": wrap})
    return out[:12]


DEFAULTS = [
    ("location", "Fach – Brother 55×16", "brother55x16", 55, 16, "qr", [("name", "l", True), ("parent", "m", False), ("code", "s", False)]),
    ("location", "Schubladenfront 30×12", "drawer", 30, 12, "qr", [("name", "m", True), ("code", "s", False)]),
    ("location", "Kiste 100×50", "box", 100, 50, "qr+code128", [("name", "xl", True), ("path", "m", False), ("content", "s", False), ("code", "s", False)]),
    ("item", "Artikel – Brother 55×16", "brother55x16", 55, 16, "qr", [("name", "l", True), ("description", "s", False), ("location", "s", False)]),
    ("item", "Artikel – Tüte 62×29", "tape62", 62, 29, "qr", [("name", "l", True), ("value", "m", False), ("mpn", "s", False),
                                                              ("location", "s", False), ("code", "s", False)]),
    ("lot", "Bestand – Brother 55×16", "brother55x16", 55, 16, "qr", [("name", "m", True), ("qty", "m", False), ("batch", "s", False), ("code", "s", False)]),
]


def ensure_default_templates(db: Session) -> None:
    have = {k for (k,) in db.execute(select(LabelTemplate.kind).distinct())}
    for kind, name, media, w, h, sym, lines in DEFAULTS:
        if kind in have:
            continue
        first = not db.scalar(select(LabelTemplate.id).where(LabelTemplate.kind == kind, LabelTemplate.is_default.is_(True)))
        db.add(LabelTemplate(name=name, kind=kind, media=media, width_mm=w, height_mm=h, symbology=sym, code_pos="left",
                             lines=[{"f": f, "size": s, "bold": b, "text": ""} for f, s, b in lines], is_default=first))
        db.flush()
    db.flush()


# Filament (v0.20): zwei Etiketten je Spule – klein für die Spule, größer für Karton/Beutel. Gleicher Bestands-Code auf beiden.
SPOOL_TEMPLATES = {
    "spool": ("Filament – Spule 40×16", "eigen", 40, 16, "qr", [("name", "m", True, 2), ("code", "s", False, 1)]),
    "pack": ("Filament – Verpackung 62×29", "tape62", 62, 29, "qr", [("name", "l", True, 2), ("qty", "m", False, 1),
                                                                     ("created", "s", False, 1), ("code", "s", False, 1)]),
}
SPOOL_SETTING = "labels.spool_templates"


def spool_templates(db: Session) -> dict[str, LabelTemplate]:
    """Vorlagen für Spulen- und Verpackungsetikett (werden beim ersten Aufruf angelegt; umbenennen/ändern im Designer ist erlaubt)."""
    from ..security import get_setting, set_setting

    ids = dict(get_setting(db, SPOOL_SETTING, {}) or {})
    out: dict[str, LabelTemplate] = {}
    changed = False
    for role, (name, media, w, h, sym, lines) in SPOOL_TEMPLATES.items():
        t = db.get(LabelTemplate, ids.get(role)) if isinstance(ids.get(role), int) else None
        if t is None or t.kind != "lot":
            t = db.scalar(select(LabelTemplate).where(LabelTemplate.kind == "lot", LabelTemplate.name == name))
        if t is None:
            ensure_default_templates(db)  # Standard-Bestandsvorlage bleibt Standard
            t = LabelTemplate(name=name, kind="lot", media=media, width_mm=w, height_mm=h, symbology=sym, code_pos="left",
                              lines=[{"f": f, "size": sz, "bold": b, "text": "", "wrap": wr} for f, sz, b, wr in lines], is_default=False)
            db.add(t)
            db.flush()
        if ids.get(role) != t.id:
            ids[role] = t.id
            changed = True
        out[role] = t
    if changed:
        set_setting(db, SPOOL_SETTING, ids)
        db.flush()
    return out


def templates_for(db: Session, kind: str) -> list[LabelTemplate]:
    return list(db.scalars(select(LabelTemplate).where(LabelTemplate.kind == kind).order_by(LabelTemplate.is_default.desc(), LabelTemplate.name)))


def default_template(db: Session, kind: str) -> LabelTemplate | None:
    ts = templates_for(db, kind)
    if not ts:  # noch keine Vorlagen (frische Installation): Standardvorlagen anlegen
        ensure_default_templates(db)
        db.flush()
        ts = templates_for(db, kind)
    return ts[0] if ts else None


# ------------------------------------------------------------------ Daten je Etikett
def _fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return ""
    s = f"{v:g}".replace(".", ",")
    return f"{s} {unit}".strip()


def _param_text(p) -> str:
    return (p.value_text or _fmt(p.value_num)) + (f" {p.unit}" if p.unit and p.unit not in (p.value_text or "") else "")


def _item_fields(db: Session, it: Item, tc: TreeCache, lot: StockLot | None = None, prices: dict | None = None) -> dict:
    lots = [lt for lt in it.lots if lt.status not in ("lost", "used")]
    first = lot or next((lt for lt in lots if lt.location_id), None)
    loc_path = tc.path(first.location_id) if first and first.location_id else ""
    params = [p for p in it.parameters if p.value_text or p.value_num is not None]
    sp = db.scalar(select(SupplierPart).where(SupplierPart.item_id == it.id).limit(1))
    price = (prices or {}).get(it.id)
    return {
        "name": it.name, "description": (it.description or "")[:120],
        "value": _param_text(params[0]) if params else "",
        "params": " · ".join(f"{p.name} {_param_text(p)}" for p in params[:3]),
        "mpn": it.mpn or "", "manufacturer": it.manufacturer.name if it.manufacturer else "",
        "footprint": it.footprint.name if it.footprint else "", "category": it.category.name if it.category else "",
        "location": " › ".join(loc_path.split(" / ")[-2:]) if loc_path else "", "location_path": loc_path,
        "location_code": tc.nodes[first.location_id].code if first and first.location_id in tc.nodes else "",
        "stock": _fmt(sum(lt.quantity or 0 for lt in lots), it.unit) if it.stock_mode == "exact" else "",
        "min_stock": _fmt(it.min_stock, it.unit) if it.min_stock else "",
        "supplier": f"{sp.supplier.name}: {sp.sku}" if sp and sp.supplier else "",
        "price": (f"{price[0]:.2f} €".replace(".", ",") + f"/{it.unit}") if price else "",
        "tags": it.tags or "", "code": it.code,
    }


def location_record(loc: Location, tc: TreeCache, content: str = "") -> dict:
    path = tc.path(loc.id)
    parts = path.split(" / ")
    room = next((tc.nodes[a].name for a in tc.ancestors(loc.id) if tc.nodes[a].type in ("raum", "bereich", "lagerwand")), "")
    f = {"name": loc.name, "parent": parts[-2] if len(parts) > 1 else "", "path2": " › ".join(parts[-2:]), "path": path, "room": room,
         "type": LOCATION_TYPES.get(loc.type, loc.type), "content": content[:80], "code": loc.code}
    return {"kind": "location", "id": loc.id, "code": loc.code, "f": f}


def item_record(db: Session, it: Item, tc: TreeCache, prices: dict | None = None) -> dict:
    return {"kind": "item", "id": it.id, "code": it.code, "f": _item_fields(db, it, tc, None, prices)}


def lot_record(db: Session, lot: StockLot, tc: TreeCache, prices: dict | None = None) -> dict:
    f = _item_fields(db, lot.item, tc, lot, prices)
    f.update({"qty": _fmt(lot.quantity, lot.item.unit) if lot.item.stock_mode == "exact" else "", "batch": lot.batch or "",
              "serial": lot.serial or "", "expiry": lot.expiry.strftime("%d.%m.%Y") if getattr(lot, "expiry", None) else "",
              "created": lot.created_at.strftime("%d.%m.%Y") if getattr(lot, "created_at", None) else "", "code": lot.code})
    return {"kind": "lot", "id": lot.id, "code": lot.code, "f": f}


SAMPLE = {
    "location": {"name": "Fach 12", "parent": "Kleinteilemagazin 2", "path2": "Kleinteilemagazin 2 › Fach 12",
                 "path": "Zuhause / Hobbyraum / Regalwand / Kleinteilemagazin 2 / Fach 12", "room": "Hobbyraum", "type": "Fach / Schublade",
                 "content": "BC547, BC557, 2N3904", "code": "L-00123"},
    "item": {"name": "Widerstand 10 kΩ", "description": "Metallschicht 0,25 W 1 %", "value": "10 kΩ", "params": "Widerstand 10 kΩ · Toleranz 1 % · Leistung 0,25 W",
             "mpn": "MF0207FTE52-10K", "manufacturer": "Yageo", "footprint": "Axial", "category": "Widerstände",
             "location": "Kleinteilemagazin 2 › Fach 12", "location_path": "Zuhause / Hobbyraum / Regalwand / Kleinteilemagazin 2 / Fach 12",
             "location_code": "L-00123", "stock": "250 Stk", "min_stock": "50 Stk", "supplier": "Reichelt: METALL 10,0K", "price": "0,08 €/Stk",
             "tags": "THT", "code": "A-00042"},
}
SAMPLE["lot"] = {**SAMPLE["item"], "qty": "100 Stk", "batch": "2409-A", "serial": "", "expiry": "", "created": "01.09.2026",
                 "code": "S-00077"}


def sample_record(db: Session, kind: str) -> dict:
    """Beispieldaten für die Vorschau: ein echter Datensatz, falls vorhanden, sonst ein Muster."""
    tc = TreeCache(db)
    if kind == "location":
        loc = db.scalar(select(Location).where(Location.type == "fach").limit(1))
        if loc is not None:
            return location_record(loc, tc, "Beispielinhalt")
    elif kind == "item":
        it = db.scalar(select(Item).join(StockLot).where(Item.active.is_(True), StockLot.location_id.is_not(None)).limit(1))
        if it is not None:
            return item_record(db, it, tc)
    elif kind == "lot":
        lot = db.scalar(select(StockLot).where(StockLot.location_id.is_not(None)).limit(1))
        if lot is not None:
            return lot_record(db, lot, tc)
    code = SAMPLE[kind]["code"]
    return {"kind": kind, "id": 0, "code": code, "f": dict(SAMPLE[kind])}


def finish(rec: dict, spec: Spec) -> dict:
    """Code-Grafiken und Zeilen für das Etikett erzeugen."""
    rec = dict(rec)
    if "qr" in spec.symbology:
        rec["qr"] = qr_svg(qr_url(rec["code"]))
    if "code128" in spec.symbology:
        rec["bar"] = code128_svg(rec["code"])
    today = date.today().strftime("%d.%m.%Y")
    rows = []
    for ln in spec.lines:
        v = ln.get("text", "") if ln["f"] == "text" else (today if ln["f"] == "date" else rec["f"].get(ln["f"], ""))
        if v:
            rows.append({"v": v, "size": ln.get("size", "m"), "bold": ln.get("bold", False), "f": ln["f"], "wrap": int(ln.get("wrap") or 1)})
    rec["rows"] = rows
    return rec
