"""Warenbestand in Zahlen: Lagerwert, Verteilung, Buchungen und Einkäufe (v0.5).

Wert je Einheit (in dieser Reihenfolge):
1. Kaufpreis am Artikel (Werkzeug/Gerät: „Gekauft für …“)
2. zuletzt bezahlter Preis aus der Einkaufsliste (Wareneingang)
3. günstigster aktueller Shop-Preis der kleinsten Staffel (Wiederbeschaffungswert)
Artikel ohne jeden Preis werden gezählt und verlinkt, aber nicht bewertet.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, PriceInfo, ShoppingItem, StockLot, StockMovement
from .items import category_path
from .locations import TreeCache

IN_KINDS = {"created", "added", "returned", "imported"}
OUT_KINDS = {"removed", "loan"}
SOURCES = {"kauf": "Kaufpreis", "einkauf": "zuletzt bezahlt", "shop": "Shop-Preis"}


def unit_values(db: Session) -> dict[int, tuple[float, str]]:
    """Artikel-ID -> (Wert je Einheit, Quelle)."""
    out: dict[int, tuple[float, str]] = {}
    # 3. Shop-Preise: je Artikel die günstigste Einstiegsstaffel
    best: dict[int, tuple[float, float]] = {}
    for p in db.scalars(select(PriceInfo)):
        cur = best.get(p.item_id)
        if cur is None or (p.min_qty, p.unit_price) < cur:
            best[p.item_id] = (p.min_qty, p.unit_price)
    for iid, (_, price) in best.items():
        out[iid] = (price, "shop")
    # 2. zuletzt bezahlt
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.status == "received", ShoppingItem.unit_price.is_not(None),
                                                   ShoppingItem.item_id.is_not(None)).order_by(ShoppingItem.received_at)):
        out[e.item_id] = (e.unit_price, "einkauf")
    # 1. Kaufpreis
    for iid, price in db.execute(select(Item.id, Item.purchase_price).where(Item.purchase_price.is_not(None), Item.purchase_price > 0)):
        out[iid] = (price, "kauf")
    return out


@dataclass
class Bucket:
    label: str
    count: int = 0
    qty: float = 0
    value: float = 0


@dataclass
class Inventory:
    items: int = 0
    lots: int = 0
    qty: float = 0
    value: float = 0
    valued_items: int = 0
    unpriced: list = field(default_factory=list)  # Artikel mit Bestand, aber ohne Preis
    coarse_items: int = 0
    collector_value: float = 0
    by_category: list[Bucket] = field(default_factory=list)
    by_location: list[Bucket] = field(default_factory=list)
    by_kind: list[Bucket] = field(default_factory=list)
    top_items: list = field(default_factory=list)  # (Item, qty, value, source)
    sources: dict = field(default_factory=dict)


def _loc_group(tc: TreeCache, loc_id: int | None, depth: int = 2) -> str:
    if not loc_id or loc_id not in tc.nodes:
        return "ohne Lagerort"
    chain = [*tc.ancestors(loc_id), loc_id]
    return " / ".join(tc.nodes[i].name for i in chain[:depth])


def inventory(db: Session, kind_names: dict[str, str] | None = None) -> Inventory:
    vals = unit_values(db)
    tc = TreeCache(db)
    inv = Inventory()
    cats: dict[str, Bucket] = {}
    locs: dict[str, Bucket] = {}
    kinds: dict[str, Bucket] = {}
    per_item: dict[int, list] = {}
    items = {i.id: i for i in db.scalars(select(Item).where(Item.active.is_(True))).unique()}
    inv.items = len(items)
    for it in items.values():
        if it.collector_value:
            inv.collector_value += it.collector_value
    for lot in db.scalars(select(StockLot).where(StockLot.status.notin_(("lost", "used")))):
        it = items.get(lot.item_id)
        if it is None:
            continue
        inv.lots += 1
        if it.stock_mode != "exact":
            continue
        qty = lot.quantity or 0
        price = vals.get(it.id)
        value = qty * price[0] if price else 0.0
        inv.qty += qty
        inv.value += value
        rec = per_item.setdefault(it.id, [it, 0.0, 0.0, price[1] if price else ""])
        rec[1] += qty
        rec[2] += value
        cat = (category_path(it.category, " / ").split(" / ")[0] if it.category else "ohne Kategorie")
        for key, store in ((cat, cats), (_loc_group(tc, lot.location_id), locs), ((kind_names or {}).get(it.kind, it.kind), kinds)):
            b = store.setdefault(key, Bucket(key))
            b.qty += qty
            b.value += value
    inv.coarse_items = sum(1 for i in items.values() if i.stock_mode == "coarse")
    for it, qty, _value, src in per_item.values():
        for key, store in ((category_path(it.category, " / ").split(" / ")[0] if it.category else "ohne Kategorie", cats),
                           ((kind_names or {}).get(it.kind, it.kind), kinds)):
            store[key].count += 1
        if src:
            inv.valued_items += 1
            inv.sources[src] = inv.sources.get(src, 0) + 1
        elif qty > 0:
            inv.unpriced.append(it)
    seen_loc: dict[str, set] = defaultdict(set)
    for lot in db.scalars(select(StockLot).where(StockLot.status.notin_(("lost", "used")))):
        it = items.get(lot.item_id)
        if it is not None and it.stock_mode == "exact":
            seen_loc[_loc_group(tc, lot.location_id)].add(it.id)
    for key, ids in seen_loc.items():
        if key in locs:
            locs[key].count = len(ids)
    order = lambda bs: sorted(bs, key=lambda b: (-b.value, -b.qty, b.label))  # noqa: E731
    inv.by_category, inv.by_location, inv.by_kind = order(cats.values()), order(locs.values()), order(kinds.values())
    inv.top_items = sorted(per_item.values(), key=lambda r: -r[2])[:15]
    inv.unpriced.sort(key=lambda i: i.name.lower())
    return inv


# ------------------------------------------------------------------ Buchungen über die Zeit
PERIODS = {30: "30 Tage", 90: "90 Tage", 365: "12 Monate", 1825: "5 Jahre"}


def _bucket_key(d: datetime, days: int) -> date:
    if days <= 31:
        return d.date()
    if days <= 120:
        return (d - timedelta(days=d.weekday())).date()
    return date(d.year, d.month, 1)


def _bucket_label(k: date, days: int) -> str:
    if days <= 31:
        return k.strftime("%d.%m.")
    if days <= 120:
        return f"KW {k.isocalendar()[1]}"
    return k.strftime("%m/%y")


def _all_buckets(days: int) -> list[date]:
    today = datetime.now()
    start = today - timedelta(days=days - 1)
    keys, cur = [], start
    while cur <= today:
        k = _bucket_key(cur, days)
        if not keys or keys[-1] != k:
            keys.append(k)
        cur += timedelta(days=1)
    return keys


@dataclass
class Activity:
    days: int
    labels: list[str]
    bookings: list[int]
    qty_in: list[float]
    qty_out: list[float]
    value_in: list[float]
    value_out: list[float]
    totals: dict
    top_out: list  # (Item, Menge, Wert)
    purchases: list[float]
    purchase_total: float
    by_supplier: list  # (Name, Betrag)
    by_user: list  # (Name, Buchungen)


def activity(db: Session, days: int = 90) -> Activity:
    days = days if days in PERIODS else 90
    since = datetime.now() - timedelta(days=days)
    keys = _all_buckets(days)
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    bookings, qin, qout, vin, vout = [0] * n, [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
    vals = unit_values(db)
    kinds_total: dict[str, int] = defaultdict(int)
    out_items: dict[int, list] = {}
    users: dict[str, int] = defaultdict(int)
    for m in db.scalars(select(StockMovement).where(StockMovement.ts >= since)):
        i = idx.get(_bucket_key(m.ts, days))
        if i is None:
            continue
        kinds_total[m.kind] += 1
        bookings[i] += 1
        users[m.user.label if m.user else "System/Import"] += 1
        price = vals.get(m.item_id, (0.0, ""))[0]
        amount = abs(m.delta or 0)
        if m.kind in IN_KINDS or (m.kind == "counted" and (m.delta or 0) > 0):
            qin[i] += amount
            vin[i] += amount * price
        elif m.kind in OUT_KINDS or (m.kind == "counted" and (m.delta or 0) < 0):
            qout[i] += amount
            vout[i] += amount * price
            if m.kind == "removed":
                rec = out_items.setdefault(m.item_id, [m.item, 0.0, 0.0])
                rec[1] += amount
                rec[2] += amount * price
    purchases = [0.0] * n
    sup: dict[str, float] = defaultdict(float)
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.status == "received", ShoppingItem.received_at >= since)):
        amount = (e.unit_price or 0) * (e.quantity or 0)
        i = idx.get(_bucket_key(e.received_at, days))
        if i is not None:
            purchases[i] += amount
        sup[e.supplier_part.supplier.name if e.supplier_part and e.supplier_part.supplier else "ohne Lieferant"] += amount
    totals = {"bookings": sum(bookings), "qty_in": sum(qin), "qty_out": sum(qout), "value_in": sum(vin), "value_out": sum(vout),
              "kinds": dict(kinds_total)}
    return Activity(days, [_bucket_label(k, days) for k in keys], bookings, qin, qout, vin, vout, totals,
                    sorted(out_items.values(), key=lambda r: (-r[1], r[0].name))[:12], purchases, sum(purchases),
                    sorted(sup.items(), key=lambda kv: -kv[1]), sorted(users.items(), key=lambda kv: -kv[1])[:8])


# ------------------------------------------------------------------ Export
def inventory_csv(db: Session) -> str:
    """Wertliste für Inventur/Versicherung: je Bestand eine Zeile (Semikolon, Dezimalkomma)."""
    vals = unit_values(db)
    tc = TreeCache(db)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Artikel-Code", "Artikel", "Hersteller-Nr.", "Kategorie", "Lagerort", "Bestand-Code", "Menge", "Einheit",
                "Wert je Einheit", "Wertquelle", "Wert gesamt"])

    def n(v):
        return "" if v is None else f"{v:.4f}".rstrip("0").rstrip(".").replace(".", ",")

    rows = []
    for lot in db.scalars(select(StockLot).where(StockLot.status.notin_(("lost", "used")))):
        it = lot.item
        price = vals.get(it.id)
        qty = lot.quantity if it.stock_mode == "exact" else None
        rows.append([it.code, it.name, it.mpn, category_path(it.category), tc.path(lot.location_id) if lot.location_id else "",
                     lot.code, n(qty) if qty is not None else (lot.coarse or ""), it.unit, n(price[0]) if price else "",
                     SOURCES.get(price[1], "") if price else "", n(qty * price[0]) if price and qty is not None else ""])
    for r in sorted(rows, key=lambda r: (r[4], r[1].lower())):
        w.writerow(r)
    return buf.getvalue()
