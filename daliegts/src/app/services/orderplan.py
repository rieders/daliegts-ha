"""Bestellplaner für Projekte (v0.14).

Fehlteile einer Platine (× Anzahl) + offene Einkaufsliste + Standardteile unter Mindestbestand werden zu einem
Bedarf zusammengefasst. Daraus rechnet Lagerwerk mit den hinterlegten Angeboten (Preisstaffeln, Verpackungseinheit,
Versandkosten) die günstigste Aufteilung – auf Wunsch mit Aufschlag je zusätzlichem Paket – und zum Vergleich
„alles bei einem Händler“.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Project, ProjectLine, ShoppingItem, Supplier, SupplierPart
from . import purchasing as pur
from . import projects as psvc

MODES = {
    "guenstig": ("Günstigste Summe", 0.0),
    "wenige": ("Wenige Pakete bevorzugen", 5.0),
    "einer": ("Möglichst alles bei einem Händler", 25.0),
}


@dataclass
class PlanEntry:
    """Wie ShoppingItem (für make_plan), aber nicht gespeichert."""
    item: Item
    quantity: float
    source: str  # projekt | liste | standard
    list_entry: ShoppingItem | None = None
    detail: str = ""
    supplier_part: SupplierPart | None = None
    text: str = ""
    id: int | None = None
    list_qty: float = 0.0  # Menge, die davon schon als offener Eintrag auf der Einkaufsliste steht

    @property
    def item_id(self) -> int:
        return self.item.id

    @property
    def supplier_part_id(self) -> int | None:
        return self.supplier_part.id if self.supplier_part else None


@dataclass
class Demand:
    entries: list[PlanEntry]
    unassigned: list[ProjectLine]
    suggestions: list[tuple[Item, float]]  # Standardteile unter Mindestbestand (alle, zur Auswahl)
    covered: list[tuple[ProjectLine, float]] = field(default_factory=list)  # Projektteile, die schon bestellt/auf Liste sind


def _open_qty(db: Session) -> tuple[dict[int, float], dict[int, float], dict[int, list[ShoppingItem]]]:
    open_q: dict[int, float] = {}
    ordered_q: dict[int, float] = {}
    open_rows: dict[int, list[ShoppingItem]] = {}
    for e in db.scalars(select(ShoppingItem).where(ShoppingItem.status.in_(("open", "ordered")), ShoppingItem.item_id.is_not(None))):
        if e.status == "open":
            open_q[e.item_id] = open_q.get(e.item_id, 0) + (e.quantity or 0)
            open_rows.setdefault(e.item_id, []).append(e)
        else:
            ordered_q[e.item_id] = ordered_q.get(e.item_id, 0) + (e.quantity or 0)
    return open_q, ordered_q, open_rows


def demand(db: Session, project: Project, boards: int, include_list: bool = True, standard_ids: set[int] | None = None) -> Demand:
    """standard_ids=None: alle Standardteile unter Mindestbestand mitnehmen; leere Menge: keine."""
    open_q, ordered_q, open_rows = _open_qty(db)
    entries: dict[int, PlanEntry] = {}
    covered = []
    if include_list:
        for iid, rows in open_rows.items():
            it = rows[0].item
            fixed = next((r.supplier_part for r in rows if r.supplier_part_id), None)
            q = sum(r.quantity or 0 for r in rows)
            entries[iid] = PlanEntry(it, q, "liste", rows[0], "steht auf der Einkaufsliste", fixed, list_qty=q)
    unassigned = []
    for nd in psvc.availability(db, project, boards):
        ln = nd.line
        if ln.item is None:
            unassigned.append(ln)
            continue
        if nd.short <= 0:
            continue
        iid = ln.item.id
        # schon bestellt oder schon auf der Einkaufsliste -> nicht doppelt kaufen
        already = ordered_q.get(iid, 0) + open_q.get(iid, 0)
        extra = max(0.0, nd.short - already)
        if extra <= 0:
            covered.append((ln, nd.short))
            continue
        if iid in entries:
            e = entries[iid]
            e.quantity += extra
            e.source, e.detail = "projekt", f"{ln.refs} · fehlt {nd.short:g}, davon {already:g} schon auf Liste/bestellt"
        else:
            entries[iid] = PlanEntry(ln.item, extra, "projekt", None, f"{ln.refs} · fehlt {nd.short:g}" + (f", {already:g} schon bestellt" if already else ""))
    sugg = [(it, q) for it, q in pur.suggestions(db) if it.id not in entries]
    for it, q in sugg:
        if standard_ids is None or it.id in standard_ids:
            entries[it.id] = PlanEntry(it, q, "standard", None, "unter Mindestbestand")
    order = {"projekt": 0, "liste": 1, "standard": 2}
    out = sorted(entries.values(), key=lambda e: (order[e.source], e.item.name.lower()))
    return Demand(out, unassigned, sugg, covered)


@dataclass
class SingleOption:
    supplier: Supplier
    subtotal: float
    shipping: float
    covered: int
    missing: list[str]
    unpriced: int

    @property
    def total(self) -> float:
        return round(self.subtotal + self.shipping, 2)

    @property
    def complete(self) -> bool:
        return not self.missing


def single_supplier_options(db: Session, entries: list[PlanEntry]) -> list[SingleOption]:
    """Für jeden Händler: Was kostet alles bei ihm, was fehlt dort?"""
    parts = pur.parts_by_item(db, [e.item_id for e in entries])
    by_sup: dict[int, dict] = {}
    for e in entries:
        for sp, of in pur.offers_for(e.item, e.quantity, parts.get(e.item_id, [])):
            if sp.supplier is None:
                continue
            d = by_sup.setdefault(sp.supplier_id, {"supplier": sp.supplier, "best": {}})
            cur = d["best"].get(e.item_id)
            if cur is None or (of.priced and (not cur.priced or of.total < cur.total)):
                d["best"][e.item_id] = of
    out = []
    for d in by_sup.values():
        best = d["best"]
        subtotal = round(sum(of.total for of in best.values() if of.priced), 2)
        missing = [e.item.name for e in entries if e.item_id not in best]
        out.append(SingleOption(d["supplier"], subtotal, pur.shipping_for(d["supplier"], subtotal), len(best), missing,
                                sum(1 for of in best.values() if not of.priced)))
    return sorted(out, key=lambda o: (len(o.missing), o.unpriced, o.total))


def plan(db: Session, entries: list[PlanEntry], mode: str = "wenige", only_supplier: int | None = None) -> pur.Plan:
    """Aufteilung berechnen. only_supplier: alles, was dieser Händler hat, dort; Rest günstigst verteilt."""
    penalty = MODES.get(mode, MODES["wenige"])[1]
    if only_supplier:
        parts = pur.parts_by_item(db, [e.item_id for e in entries])
        pinned = []
        for e in entries:
            offers = [(sp, of) for sp, of in pur.offers_for(e.item, e.quantity, parts.get(e.item_id, [])) if sp.supplier_id == only_supplier]
            priced = [t for t in offers if t[1].priced] or offers
            pinned.append(PlanEntry(e.item, e.quantity, e.source, e.list_entry, e.detail, priced[0][0] if priced else e.supplier_part,
                                    list_qty=e.list_qty))
        entries = pinned
    return pur.make_plan(db, entries, optimize_shipping=True, package_penalty=penalty)


def item_ids_to_check(db: Session, entries: list[PlanEntry]) -> list[int]:
    """Artikel, für die online nachgesehen werden soll: alle im Bedarf (aktuelle Preise + weitere Händler)."""
    return [e.item_id for e in entries]


def unpriced(db: Session, entries: list[PlanEntry]) -> list[PlanEntry]:
    parts = pur.parts_by_item(db, [e.item_id for e in entries])
    return [e for e in entries if not any(of.priced for _, of in pur.offers_for(e.item, e.quantity, parts.get(e.item_id, [])))]


def missing_shipping(p: pur.Plan, singles: list[SingleOption]) -> list[Supplier]:
    seen: dict[int, Supplier] = {}
    for g in p.groups:
        if g.supplier and g.supplier.shipping_cost is None:
            seen[g.supplier.id] = g.supplier
    for o in singles[:5]:
        if o.supplier.shipping_cost is None:
            seen.setdefault(o.supplier.id, o.supplier)
    return list(seen.values())


def apply(db: Session, p: pur.Plan, project: Project, boards: int, user_id: int | None) -> int:
    """Gewählte Aufteilung auf die Einkaufsliste übernehmen (mit festem Händler je Zeile)."""
    n = 0
    for ln in [x for g in p.groups for x in g.lines] + p.without_supplier:
        e: PlanEntry = ln.entry
        spid = ln.part.id if ln.part else None
        if e.source == "liste" and e.list_entry is not None:
            if spid:
                e.list_entry.supplier_part_id = spid
            n += 1
            continue
        note = f"Projekt {project.name} ×{boards}" if e.source == "projekt" else "Standardteil (Mindestbestand)"
        if e.list_entry is not None and e.source == "projekt":  # Liste + Mehrbedarf aus Projekt: Menge erhöhen
            e.list_entry.quantity = (e.list_entry.quantity or 0) + max(0.0, e.quantity - e.list_qty)
            if spid:
                e.list_entry.supplier_part_id = spid
        else:
            pur.add_to_list(db, e.item, e.quantity, user_id, supplier_part_id=spid, note=note)
        n += 1
    db.flush()
    return n
