"""Volltextsuche (FTS5) mit Filtern inkl. Parameterbereichen."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.orm import Session

from ..models import Equivalent, Item, ItemCode, ItemParameter, StockLot
from .locations import TreeCache
from .params import parse_value


def _extra_text(db: Session, item: Item) -> str:
    parts = [item.footprint.name if item.footprint else "", item.category.name if item.category else "",
             item.manufacturer.name if item.manufacturer else "", item.origin, item.historic_manufacturer, item.notes or "", item.code]
    parts += [f"{p.name} {p.value_text}{p.unit}" for p in item.parameters]
    parts += [c.code for c in item.codes]
    names = {item.name, item.mpn} - {""}
    for eq in db.scalars(select(Equivalent).where(or_(Equivalent.type_a.in_(names), Equivalent.type_b.in_(names)))):
        parts += [eq.type_a, eq.type_b]
    return " ".join(p for p in parts if p)


def index_item(db: Session, item: Item) -> None:
    db.execute(text("DELETE FROM items_fts WHERE rowid = :id"), {"id": item.id})
    db.execute(
        text("INSERT INTO items_fts(rowid, name, description, mpn, tags, extra) VALUES (:id, :n, :d, :m, :t, :e)"),
        {"id": item.id, "n": item.name, "d": item.description or "", "m": item.mpn or "", "t": item.tags or "", "e": _extra_text(db, item)},
    )


def remove_from_index(db: Session, item_id: int) -> None:
    db.execute(text("DELETE FROM items_fts WHERE rowid = :id"), {"id": item_id})


def rebuild_index(db: Session) -> int:
    db.execute(text("DELETE FROM items_fts"))
    n = 0
    for item in db.scalars(select(Item)).unique():
        index_item(db, item)
        n += 1
    db.commit()
    return n


def fts_query(q: str) -> str:
    """Nutzereingabe -> sichere FTS5-Abfrage mit Präfixsuche."""
    tokens = re.findall(r"[\wµΩ.,+\-/]+", q, flags=re.UNICODE)
    out = []
    for t in tokens:
        t = t.replace('"', "")
        if not t:
            continue
        out.append(f'"{t}"*')
    return " AND ".join(out)


@dataclass
class ParamFilter:
    name: str
    min: str = ""
    max: str = ""
    text: str = ""


@dataclass
class SearchQuery:
    q: str = ""
    category_id: int | None = None
    location_id: int | None = None
    kind: str = ""
    footprint_id: int | None = None
    below_min: bool = False
    origin: str = ""
    needs_review: bool = False
    params: list[ParamFilter] = field(default_factory=list)
    special: str = ""  # platzhalter | ohne_ort | ohne_bestand
    limit: int = 200
    offset: int = 0


def search(db: Session, sq: SearchQuery) -> tuple[list[Item], int]:
    stmt = select(Item).where(Item.active.is_(True))
    if sq.q.strip():
        fq = fts_query(sq.q)
        if fq:
            ids = [r[0] for r in db.execute(text("SELECT rowid FROM items_fts WHERE items_fts MATCH :q ORDER BY rank LIMIT 2000"), {"q": fq})]
            code_hit = db.scalars(select(ItemCode.item_id).where(ItemCode.code == sq.q.strip().upper())).all()
            ids = list(dict.fromkeys(list(code_hit) + ids))
            if not ids:
                return [], 0
            stmt = stmt.where(Item.id.in_(ids))
            order_case = {i: n for n, i in enumerate(ids)}
        else:
            order_case = None
    else:
        order_case = None
    if sq.category_id:

        cat_ids = _category_descendants(db, sq.category_id)
        stmt = stmt.where(Item.category_id.in_(cat_ids))
    if sq.location_id:
        tc = TreeCache(db)
        loc_ids = [sq.location_id, *tc.descendants(sq.location_id)]
        stmt = stmt.where(exists().where(and_(StockLot.item_id == Item.id, StockLot.location_id.in_(loc_ids))))
    if sq.kind:
        stmt = stmt.where(Item.kind == sq.kind)
    if sq.special == "platzhalter":
        stmt = stmt.where(Item.name.like("Unbenannter Lagerartikel%"))
    elif sq.special == "ohne_ort":
        stmt = stmt.where(exists().where(and_(StockLot.item_id == Item.id, StockLot.location_id.is_(None))))
    elif sq.special == "ohne_bestand":
        stmt = stmt.where(~exists().where(StockLot.item_id == Item.id))
    if sq.footprint_id:
        stmt = stmt.where(Item.footprint_id == sq.footprint_id)
    if sq.origin:
        stmt = stmt.where(Item.origin == sq.origin)
    if sq.needs_review:
        stmt = stmt.where(exists().where(and_(StockLot.item_id == Item.id, StockLot.needs_review.is_(True))))
    for pf in sq.params:
        conds = [ItemParameter.item_id == Item.id, func.lower(ItemParameter.name) == pf.name.lower()]
        lo = parse_value(pf.min) if pf.min else None
        hi = parse_value(pf.max) if pf.max else None
        if lo is not None:
            conds.append(ItemParameter.value_num >= lo * 0.999999)
        if hi is not None:
            conds.append(ItemParameter.value_num <= hi * 1.000001)
        if pf.text:
            conds.append(ItemParameter.value_text.ilike(f"%{pf.text}%"))
        stmt = stmt.where(exists().where(and_(*conds)))
    items = list(db.scalars(stmt).unique())
    if sq.below_min:
        items = [i for i in items if i.below_min]
    if order_case:
        items.sort(key=lambda i: order_case.get(i.id, 10**9))
    else:
        items.sort(key=lambda i: i.name.lower())
    total = len(items)
    return items[sq.offset: sq.offset + sq.limit], total


def _category_descendants(db: Session, cat_id: int) -> list[int]:
    from ..models import Category

    rows = db.execute(select(Category.id, Category.parent_id)).all()
    children: dict[int | None, list[int]] = {}
    for r in rows:
        children.setdefault(r.parent_id, []).append(r.id)
    out, stack = [cat_id], [cat_id]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def parameter_names(db: Session) -> list[str]:
    return [r[0] for r in db.execute(select(ItemParameter.name, func.count()).group_by(ItemParameter.name).order_by(func.count().desc())).all()]
