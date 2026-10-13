"""Lieferanten, Preise, Einkaufsliste und Wareneingang (v0.2).

Preise: `PriceInfo.unit_price` ist der Preis je Einheit des Artikels (z. B. je Stück) ab `min_qty` Einheiten.
`SupplierPart.pack_qty` ist die Verpackungseinheit: bestellt wird immer ein Vielfaches davon.
"""
from __future__ import annotations

import csv
import io
import itertools
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote_plus

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Item, Manufacturer, PriceInfo, ShoppingItem, StockLot, Supplier, SupplierPart
from .items import safe_url

# Vorlagen für gängige Shops. Die Such-URLs lassen sich unter „Lieferanten“ jederzeit anpassen.
SUPPLIER_PRESETS = [
    {"name": "Reichelt", "website": "https://www.reichelt.de", "search_url": "https://www.reichelt.de/index.html?ACTION=446&LA=0&SEARCH={query}"},
    {"name": "Conrad", "website": "https://www.conrad.de", "search_url": "https://www.conrad.de/de/search.html?search={query}"},
    {"name": "Pollin", "website": "https://www.pollin.de", "search_url": "https://www.pollin.de/search?query={query}"},
    {"name": "Mouser", "website": "https://www.mouser.de", "search_url": "https://www.mouser.de/c/?q={query}"},
    {"name": "DigiKey", "website": "https://www.digikey.de", "search_url": "https://www.digikey.de/de/products/result?keywords={query}"},
    {"name": "LCSC", "website": "https://www.lcsc.com", "search_url": "https://www.lcsc.com/search?q={query}"},
    {"name": "TME", "website": "https://www.tme.eu", "search_url": "https://www.tme.eu/de/katalog/?queryPhrase={query}"},
    {"name": "Amazon", "website": "https://www.amazon.de", "search_url": "https://www.amazon.de/s?k={query}"},
    {"name": "AliExpress", "website": "https://www.aliexpress.com", "search_url": "https://www.aliexpress.com/wholesale?SearchText={query}"},
    {"name": "eBay", "website": "https://www.ebay.de", "search_url": "https://www.ebay.de/sch/i.html?_nkw={query}"},
]


def ensure_supplier_presets(db: Session) -> int:
    """Legt fehlende Standard-Shops an; ergänzt bei vorhandenen (z. B. aus Part-DB importierten) nur leere Felder."""
    n = 0
    existing = list(db.scalars(select(Supplier)))
    for p in SUPPLIER_PRESETS:
        key = p["name"].lower()
        # „reichelt elektronik GmbH & Co. KG“ aus dem Part-DB-Import zählt als Reichelt
        s = next((x for x in existing if x.name.lower() == key), None) or next(
            (x for x in existing if re.match(rf"{re.escape(key)}\b", x.name.lower())), None)
        if s is None:
            db.add(Supplier(name=p["name"], website=p["website"], search_url=p["search_url"]))
            n += 1
        else:
            if not s.search_url:
                s.search_url = p["search_url"]
            if not s.website:
                s.website = p["website"]
    db.flush()
    return n


# ------------------------------------------------------------------ Links
def fill_template(template: str, query: str) -> str:
    return template.replace("{query}", quote_plus(query.strip())) if template and query and query.strip() else ""


def shop_link(sp: SupplierPart) -> str:
    """Direktlink zum Produkt; sonst Shop-Suche nach der Bestellnummer."""
    if sp.url and safe_url(sp.url):
        return sp.url
    return safe_url(fill_template(sp.supplier.search_url, sp.sku)) if sp.supplier else ""


def search_link(supplier: Supplier, item: Item) -> str:
    return safe_url(fill_template(supplier.search_url, item.mpn or item.name))


# ------------------------------------------------------------------ Preise
_NUM = r"(\d+(?:[.,]\d+)?)"


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def parse_tiers(text: str) -> list[tuple[float, float]]:
    """„0,12“ oder „1: 0,12; 10: 0,08; 100: 0,05“ (Menge: Preis je Einheit) -> [(1, .12), (10, .08), (100, .05)]."""
    text = (text or "").replace("€", " ").replace("EUR", " ").strip()
    if not text:
        return []
    if re.fullmatch(_NUM, text):
        return [(1.0, _f(text))]
    tiers = {}
    for part in re.split(r"[;\n|]+", text):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(rf"(?:ab\s*)?{_NUM}\s*(?:stk\.?|x)?\s*[:=]\s*{_NUM}", part, re.I)
        if not m:
            raise ValueError(f"Preisstaffel nicht verstanden: „{part}“ (Beispiel: 1: 0,12; 10: 0,08)")
        qty, price = _f(m.group(1)), _f(m.group(2))
        if qty <= 0 or price < 0:
            raise ValueError("Menge muss größer 0 und Preis mindestens 0 sein.")
        tiers[qty] = price
    return sorted(tiers.items())


def format_tiers(prices: list[PriceInfo]) -> str:
    def n(v: float) -> str:
        return f"{v:g}".replace(".", ",")

    return "; ".join(f"{n(p.min_qty)}: {n(p.unit_price)}" for p in sorted(prices, key=lambda p: p.min_qty))


def set_prices(db: Session, sp: SupplierPart, tiers: list[tuple[float, float]], source: str = "manuell") -> None:
    for old in db.scalars(select(PriceInfo).where(PriceInfo.supplier_part_id == sp.id)):
        db.delete(old)
    db.flush()
    stamp = datetime.now().replace(microsecond=0)
    for qty, price in tiers:
        db.add(PriceInfo(item_id=sp.item_id, supplier_part_id=sp.id, min_qty=qty, unit_price=price, date=stamp, source=source))
    db.flush()
    db.expire(sp, ["prices"])


@dataclass
class Offer:
    order_qty: float
    unit_price: float | None
    total: float | None

    @property
    def priced(self) -> bool:
        return self.total is not None


def offer(sp: SupplierPart, qty: float) -> Offer:
    """Was kostet `qty` Einheiten bei diesem Lieferanten? Berücksichtigt Verpackungseinheit und Preisstaffel."""
    pack = sp.pack_qty if sp.pack_qty and sp.pack_qty > 0 else 1
    order_qty = max(pack, math.ceil(max(qty, 0) / pack - 1e-9) * pack)
    tiers = sorted(sp.prices, key=lambda p: p.min_qty)
    if not tiers:
        return Offer(order_qty, None, None)
    if order_qty < tiers[0].min_qty:  # Mindestmenge der ersten Staffel
        order_qty = math.ceil(tiers[0].min_qty / pack - 1e-9) * pack
    tier = [p for p in tiers if p.min_qty <= order_qty + 1e-9][-1]
    return Offer(order_qty, tier.unit_price, round(tier.unit_price * order_qty, 4))


def offers_for(item: Item | None, qty: float, parts: list[SupplierPart]) -> list[tuple[SupplierPart, Offer]]:
    """Alle Angebote, günstigstes zuerst; ohne Preis ans Ende (bevorzugte vorn)."""
    out = [(sp, offer(sp, qty)) for sp in parts if not sp.obsolete and (sp.supplier is None or sp.supplier.active)]
    return sorted(out, key=lambda t: (not t[1].priced, t[1].total if t[1].priced else 0, not t[0].preferred, t[0].id))


def parts_by_item(db: Session, item_ids: list[int]) -> dict[int, list[SupplierPart]]:
    out: dict[int, list[SupplierPart]] = {}
    if item_ids:
        for sp in db.scalars(select(SupplierPart).where(SupplierPart.item_id.in_(item_ids))):
            out.setdefault(sp.item_id, []).append(sp)
    return out


# ------------------------------------------------------------------ Bedarf
def need_qty(item: Item) -> float:
    """Vorgeschlagene Bestellmenge: Nachbestellmenge, mindestens aber bis zum Mindestbestand auffüllen."""
    missing = 0.0
    if item.stock_mode == "exact" and item.min_stock:
        missing = max(0.0, item.min_stock - item.total_quantity)
    q = max(item.reorder_qty or 0, missing) or 1
    return float(math.ceil(q)) if item.unit in ("Stk", "Stück", "") else round(q, 3)


def open_item_ids(db: Session) -> set[int]:
    return set(db.scalars(select(ShoppingItem.item_id).where(ShoppingItem.status.in_(("open", "ordered")), ShoppingItem.item_id.is_not(None))))


def suggestions(db: Session, limit: int = 200) -> list[tuple[Item, float]]:
    """Artikel unter Mindestbestand, die noch nicht auf der Liste stehen."""
    on_list = open_item_ids(db)
    cand = db.scalars(select(Item).where(Item.active.is_(True), Item.is_template.is_(False),
                                         ((Item.min_stock > 0) | (Item.stock_mode == "coarse"))).order_by(Item.name)).unique()
    out = []
    for it in cand:
        if it.id not in on_list and it.below_min:
            out.append((it, need_qty(it)))
            if len(out) >= limit:
                break
    return out


def add_to_list(db: Session, item: Item | None, quantity: float | None = None, user_id: int | None = None, text: str = "",
                supplier_part_id: int | None = None, note: str = "") -> ShoppingItem:
    """Fügt hinzu bzw. erhöht die Menge eines offenen Eintrags desselben Artikels."""
    if item is None and not text.strip():
        raise ValueError("Artikel oder Text angeben.")
    qty = quantity if quantity and quantity > 0 else (need_qty(item) if item else 1)
    if item is not None:
        existing = db.scalar(select(ShoppingItem).where(ShoppingItem.item_id == item.id, ShoppingItem.status == "open"))
        if existing:
            existing.quantity += qty
            if supplier_part_id:
                existing.supplier_part_id = supplier_part_id
            db.flush()
            return existing
    if supplier_part_id:
        sp = db.get(SupplierPart, supplier_part_id)
        if sp is None or (item is not None and sp.item_id != item.id):
            raise ValueError("Lieferantenangebot passt nicht zum Artikel.")
    e = ShoppingItem(item_id=item.id if item else None, text=text.strip()[:255], quantity=qty, created_by=user_id,
                     supplier_part_id=supplier_part_id, note=note)
    db.add(e)
    db.flush()
    return e


# ------------------------------------------------------------------ Planung (günstigste Aufteilung)
@dataclass
class Line:
    entry: ShoppingItem
    part: SupplierPart | None
    offer: Offer | None
    alternatives: list[tuple[SupplierPart, Offer]] = field(default_factory=list)
    fixed: bool = False

    @property
    def name(self) -> str:
        return self.entry.item.name if self.entry.item else self.entry.text


@dataclass
class Group:
    supplier: Supplier | None
    lines: list[Line] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(ln.offer.total for ln in self.lines if ln.offer and ln.offer.priced), 2)

    @property
    def unpriced(self) -> int:
        return sum(1 for ln in self.lines if not (ln.offer and ln.offer.priced))

    @property
    def shipping(self) -> float:
        return shipping_for(self.supplier, self.subtotal)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.shipping, 2)

    @property
    def missing_for_free_shipping(self) -> float | None:
        s = self.supplier
        if s and s.free_shipping_from and self.subtotal < s.free_shipping_from:
            return round(s.free_shipping_from - self.subtotal, 2)
        return None

    @property
    def below_min_order(self) -> bool:
        s = self.supplier
        return bool(s and s.min_order_value and self.subtotal < s.min_order_value)


def shipping_for(supplier: Supplier | None, subtotal: float) -> float:
    if supplier is None or not supplier.shipping_cost:
        return 0.0
    if supplier.free_shipping_from and subtotal >= supplier.free_shipping_from:
        return 0.0
    return float(supplier.shipping_cost)


@dataclass
class Plan:
    groups: list[Group]
    without_supplier: list[Line]
    optimized: bool
    naive_total: float

    @property
    def total(self) -> float:
        return round(sum(g.total for g in self.groups), 2)

    @property
    def saving(self) -> float:
        return round(self.naive_total - self.total, 2)


MAX_OPT_SUPPLIERS = 10


def make_plan(db: Session, entries: list, optimize_shipping: bool = True, package_penalty: float = 0.0) -> Plan:
    """Günstigste Aufteilung auf Lieferanten. package_penalty: gedachter Aufschlag je zusätzlichem Paket (bevorzugt wenige Händler)."""
    parts = parts_by_item(db, [e.item_id for e in entries if e.item_id])
    lines: list[Line] = []
    for e in entries:
        alts = offers_for(e.item, e.quantity, parts.get(e.item_id, [])) if e.item_id else []
        if e.supplier_part_id and e.supplier_part:
            sp = e.supplier_part
            lines.append(Line(e, sp, offer(sp, e.quantity), alts, fixed=True))
        elif alts:
            lines.append(Line(e, alts[0][0], alts[0][1], alts))
        else:
            lines.append(Line(e, None, None, []))

    def assign(allowed: set[int] | None) -> list[Line] | None:
        out = []
        for ln in lines:
            if ln.fixed or not ln.alternatives:
                out.append(ln)
                continue
            priced = [(sp, of) for sp, of in ln.alternatives if of.priced]
            choice = next(((sp, of) for sp, of in priced if allowed is None or sp.supplier_id in allowed), None)
            if choice is None and priced:
                return None  # Artikel wäre mit dieser Lieferantenauswahl nicht bestellbar
            if choice is None:
                choice = ln.alternatives[0]  # nur Angebote ohne Preis: bevorzugtes/erstes
            out.append(Line(ln.entry, choice[0], choice[1], ln.alternatives, False))
        return out

    def cost(ls: list[Line]) -> float:
        groups = _group(ls)[0]
        return sum(g.total for g in groups) + package_penalty * max(0, len([g for g in groups if g.supplier]) - 1)

    best = assign(None) or lines
    naive_total = round(sum(g.total for g in _group(best)[0]), 2)
    best_cost = cost(best)
    optimized = False
    if optimize_shipping:
        forced = {ln.part.supplier_id for ln in lines if ln.fixed and ln.part}
        cand = sorted({sp.supplier_id for ln in lines if not ln.fixed for sp, of in ln.alternatives if of.priced} - forced)
        if 0 < len(cand) <= MAX_OPT_SUPPLIERS:
            for r in range(1, len(cand) + 1):
                for combo in itertools.combinations(cand, r):
                    ls = assign(set(combo) | forced)
                    if ls is None:
                        continue
                    c = cost(ls)
                    if c < best_cost - 0.005:
                        best, best_cost, optimized = ls, c, True
    groups, without = _group(best)
    return Plan(groups, without, optimized, naive_total)


def _group(lines: list[Line]) -> tuple[list[Group], list[Line]]:
    groups: dict[int, Group] = {}
    without = []
    for ln in lines:
        if ln.part is None:
            without.append(ln)
            continue
        g = groups.setdefault(ln.part.supplier_id, Group(ln.part.supplier))
        g.lines.append(ln)
    ordered = sorted(groups.values(), key=lambda g: (g.supplier.name.lower() if g.supplier else ""))
    return ordered, without


def order_csv(group: Group) -> str:
    """CSV je Lieferant: Bestellnummer;Menge;Artikel;Einzelpreis;Summe (Semikolon, deutsches Dezimalkomma)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Bestellnummer", "Menge", "Artikel", "Hersteller-Nr.", "Einzelpreis", "Summe"])

    def n(v):
        return "" if v is None else f"{v:.4f}".rstrip("0").rstrip(".").replace(".", ",")

    for ln in group.lines:
        it = ln.entry.item
        w.writerow([ln.part.sku if ln.part else "", n(ln.offer.order_qty if ln.offer else ln.entry.quantity), ln.name,
                    it.mpn if it else "", n(ln.offer.unit_price if ln.offer else None), n(ln.offer.total if ln.offer else None)])
    return buf.getvalue()


def quick_order_text(group: Group) -> str:
    """„Bestellnummer;Menge“ je Zeile – zum Einfügen in Schnellbestell-Felder der Shops."""
    return "\n".join(f"{ln.part.sku};{(ln.offer.order_qty if ln.offer else ln.entry.quantity):g}" for ln in group.lines if ln.part and ln.part.sku)


def mark_ordered(db: Session, lines: list[Line], order_ref: str = "") -> int:
    stamp = datetime.now().replace(microsecond=0)
    for ln in lines:
        e = ln.entry
        e.status = "ordered"
        e.ordered_at = stamp
        e.order_ref = order_ref[:80]
        if ln.part is not None:
            e.supplier_part_id = ln.part.id
            if ln.offer and ln.offer.priced:
                e.quantity = ln.offer.order_qty
                e.unit_price = ln.offer.unit_price
    db.flush()
    return len(lines)


def default_location(db: Session, item: Item) -> int | None:
    """Wo liegt der Artikel meistens? Größter Bestand, sonst erster Lagerort."""
    lots = [lot for lot in item.lots if lot.location_id and lot.status not in ("lost", "used")]
    if not lots:
        return None
    return max(lots, key=lambda lot: (lot.quantity or 0, -lot.id)).location_id


def receive(db: Session, entry: ShoppingItem, quantity: float | None, location_id: int | None, user_id: int | None) -> StockLot | None:
    """Wareneingang: bucht die Menge ein und markiert den Eintrag als erhalten."""
    from . import stock as stocksvc

    lot = None
    qty = entry.quantity if quantity is None else quantity
    if entry.item is not None and qty > 0:
        loc = location_id or entry.location_id or default_location(db, entry.item)
        supplier = entry.supplier_part.supplier.name if entry.supplier_part and entry.supplier_part.supplier else ""
        lot = stocksvc.add_stock(db, entry.item, loc, qty, user_id, note=f"Wareneingang{(' ' + supplier) if supplier else ''}",
                                 ref=entry.order_ref or f"Einkauf #{entry.id}")
        entry.location_id = loc
    entry.status = "received"
    entry.received_at = datetime.now().replace(microsecond=0)
    db.flush()
    return lot


# ------------------------------------------------------------------ Stammdaten-Pflege
def supplier_stats(db: Session) -> dict[int, int]:
    return dict(db.execute(select(SupplierPart.supplier_id, func.count(SupplierPart.id)).group_by(SupplierPart.supplier_id)).all())


def manufacturer_stats(db: Session) -> dict[int, int]:
    return dict(db.execute(select(Item.manufacturer_id, func.count(Item.id)).where(Item.manufacturer_id.is_not(None)).group_by(Item.manufacturer_id)).all())


def merge_suppliers(db: Session, source: Supplier, target: Supplier) -> int:
    if source.id == target.id:
        raise ValueError("Quelle und Ziel sind identisch.")
    n = 0
    for sp in db.scalars(select(SupplierPart).where(SupplierPart.supplier_id == source.id)):
        sp.supplier_id = target.id
        n += 1
    for k in ("website", "search_url", "customer_no", "notes"):
        if not getattr(target, k) and getattr(source, k):
            setattr(target, k, getattr(source, k))
    db.flush()
    db.delete(source)
    db.flush()
    return n


def merge_manufacturers(db: Session, source: Manufacturer, target: Manufacturer) -> int:
    if source.id == target.id:
        raise ValueError("Quelle und Ziel sind identisch.")
    n = 0
    for it in db.scalars(select(Item).where(Item.manufacturer_id == source.id)):
        it.manufacturer_id = target.id
        n += 1
    if not target.website and source.website:
        target.website = source.website
    db.flush()
    db.delete(source)
    db.flush()
    return n


# ------------------------------------------------------------------ Bestellstatus je Artikel (v0.10.1)
@dataclass
class BuyState:
    open: float = 0.0          # steht auf der Einkaufsliste, noch nicht bestellt -> muss bestellt werden
    ordered: float = 0.0       # bestellt, noch nicht eingetroffen
    ordered_at: datetime | None = None
    suppliers: list = field(default_factory=list)
    refs: list = field(default_factory=list)

    @property
    def supplier(self) -> str:
        return ", ".join(dict.fromkeys(s for s in self.suppliers if s))

    @property
    def order_ref(self) -> str:
        return ", ".join(dict.fromkeys(r for r in self.refs if r))


def buy_states(db: Session, item_ids: set[int] | None = None) -> dict[int, BuyState]:
    q = select(ShoppingItem).where(ShoppingItem.status.in_(("open", "ordered")), ShoppingItem.item_id.is_not(None))
    if item_ids is not None:
        q = q.where(ShoppingItem.item_id.in_(item_ids))
    out: dict[int, BuyState] = {}
    for e in db.scalars(q):
        b = out.setdefault(e.item_id, BuyState())
        if e.status == "ordered":
            b.ordered += e.quantity or 0
            if e.ordered_at and (b.ordered_at is None or e.ordered_at > b.ordered_at):
                b.ordered_at = e.ordered_at
            b.suppliers.append(e.supplier_part.supplier.name if e.supplier_part and e.supplier_part.supplier else "")
            b.refs.append(e.order_ref or "")
        else:
            b.open += e.quantity or 0
    return out
