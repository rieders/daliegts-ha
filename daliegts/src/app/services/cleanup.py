"""Aufräumen nach dem Import (v0.7): Platzhalter-Artikel und leere Hilfs-Lagerorte entfernen."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Location, StockLot
from . import locations as locsvc
from .search import remove_from_index

PLACEHOLDER_PREFIX = "Unbenannter Lagerartikel"
IMPORT_AREA_NAMES = ("InvenTree (unbekannte Lagerorte)",)


def placeholders(db: Session) -> list[Item]:
    return list(db.scalars(select(Item).where(Item.name.like(PLACEHOLDER_PREFIX + "%")).order_by(Item.id)))


def empty_import_locations(db: Session, ignore_items: set[int] | None = None) -> list[Location]:
    """Leere Hilfsorte aus dem Import („InvenTree-Lagerort 568“ …) – ohne Bestand (außer ignore_items) und ohne Unterorte."""
    ignore_items = ignore_items or set()
    tc = locsvc.TreeCache(db)
    roots = [lid for lid, n in tc.nodes.items() if n.name in IMPORT_AREA_NAMES]
    candidates = set()
    for r in roots:
        candidates |= set(tc.descendants(r))
    candidates |= {lid for lid, n in tc.nodes.items() if n.name.startswith("InvenTree-Lagerort ")}
    used = {lot.location_id for lot in db.scalars(select(StockLot).where(StockLot.location_id.in_(candidates))) if lot.item_id not in ignore_items}
    out = [db.get(Location, lid) for lid in candidates if lid not in used and not tc.children.get(lid)]
    # den Sammelordner selbst, wenn danach nichts mehr drin ist
    for r in roots:
        if set(tc.descendants(r)) <= {loc.id for loc in out} and r not in used:
            out.append(db.get(Location, r))
    return sorted((o for o in out if o is not None), key=lambda x: locsvc.natural_key(x.name))


@dataclass
class Result:
    items: int = 0
    lots: int = 0
    locations: int = 0
    notes: list = field(default_factory=list)


def remove_placeholders(db: Session) -> Result:
    res = Result()
    for it in placeholders(db):
        res.lots += len(it.lots)
        for v in it.variants:
            v.variant_of_id = None
        remove_from_index(db, it.id)
        db.delete(it)
        res.items += 1
    db.flush()
    return res


def remove_locations(db: Session, locs: list[Location]) -> int:
    ids = {loc.id for loc in locs}
    tops = [loc for loc in locs if loc.parent_id not in ids]  # ganze Teilbäume auf einmal
    tc = locsvc.TreeCache(db)
    n = sum(1 + len(tc.descendants(t.id)) for t in tops)
    for t in tops:
        locsvc.delete_location(db, t)
    db.flush()
    return n


IMPORT_ROOTS = ("Part-DB (Altbestand)", "InvenTree (unbekannte Lagerorte)")


def import_groups(db: Session) -> list[dict]:
    """Alte Import-Orte zum Zuordnen: Möbel mit Fächern einzeln, flache Fachreihen („Wandregal oben101…133“) als Gruppe."""
    from .locmerge import default_prefix

    tc = locsvc.TreeCache(db)
    counts: dict[int, int] = {}
    for (lid,) in db.execute(select(StockLot.location_id).where(StockLot.location_id.is_not(None))):
        for a in [lid, *tc.ancestors(lid)]:
            counts[a] = counts.get(a, 0) + 1
    out = []
    for root_id, n in tc.nodes.items():
        if n.name not in IMPORT_ROOTS or n.parent_id is not None:
            continue
        groups: dict[str, list[int]] = {}
        for cid in tc.children.get(root_id, []):
            kid = tc.nodes[cid]
            if tc.children.get(cid):
                out.append({"root": n.name, "id": cid, "name": kid.name, "kids": len(tc.children[cid]), "lots": counts.get(cid, 0), "prefix": ""})
            else:
                groups.setdefault(default_prefix(kid.name), []).append(cid)
        for pf, ids in groups.items():
            ids.sort(key=lambda i: locsvc.natural_key(tc.nodes[i].name))
            if len(ids) == 1:
                i = ids[0]
                out.append({"root": n.name, "id": i, "name": tc.nodes[i].name, "kids": 0, "lots": counts.get(i, 0), "prefix": ""})
            else:
                out.append({"root": n.name, "id": ids[0], "name": f"{pf}… ({tc.nodes[ids[0]].name} – {tc.nodes[ids[-1]].name})", "kids": len(ids),
                            "lots": sum(counts.get(i, 0) for i in ids), "prefix": pf})
    return sorted(out, key=lambda r: (r["root"], locsvc.natural_key(r["name"])))
