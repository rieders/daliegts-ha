"""„Wo ist …?“ für Sprachassistenten (Home Assistant Assist) – Antwortsatz + Lagerorte zum Aufleuchten (v0.16)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import Item, Loan
from .locations import TreeCache
from .search import SearchQuery, search

FILLER = {"der", "die", "das", "den", "dem", "des", "mein", "meine", "meinen", "meinem", "ein", "eine", "einen", "unser", "unsere",
          "ist", "sind", "liegt", "liegen", "steht", "stehen", "wo", "finde", "ich", "hab", "habe", "gibt", "es", "noch", "denn", "eigentlich"}


@dataclass
class Answer:
    speech: str
    item: Item | None = None
    location_ids: list[int] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)


def clean_query(q: str) -> str:
    words = [w for w in re.findall(r"[\wäöüÄÖÜß.,+\-/µΩ]+", q or "") if w.lower() not in FILLER]
    return " ".join(words).strip(" .,?!")


def spoken_place(tc: TreeCache, loc_id: int) -> str:
    """„Zuhause / Keller / Werkzeugschrank / Fach 3“ -> „Keller, Werkzeugschrank, Fach 3“ (höchstens 3 Ebenen)."""
    parts = [p.strip() for p in tc.path(loc_id).split(" / ") if p.strip()]
    if len(parts) > 3:
        parts = parts[-3:]
    return ", ".join(parts)


def _pick(items: list[Item], q: str) -> Item | None:
    """Bester Treffer: Name genau > Name beginnt so > alle Wörter im Namen > Suchreihenfolge; jeweils mit Bestand zuerst."""
    if not items:
        return None
    ql = q.casefold()
    words = [w for w in re.split(r"\s+", ql) if w]
    stem = lambda w: w[:-1] if len(w) > 4 and w[-1] in "nes" else w  # noqa: E731

    def tier(i: Item) -> int:
        n = i.name.casefold()
        if n == ql or n.replace(" ", "") == ql.replace(" ", ""):
            return 0
        if n.startswith(ql) or n.startswith(stem(ql)):
            return 1
        if all(stem(w) in n for w in words):
            return 2
        if any(stem(w) in n for w in words if len(w) >= 3):
            return 3
        return 4

    def stocked(i: Item) -> bool:
        return any(lt.location_id and lt.status not in ("lost", "used") for lt in i.lots)

    return min(enumerate(items), key=lambda t: (tier(t[1]), not stocked(t[1]), t[0]))[1]


def where_is(db: Session, raw: str) -> Answer:
    q = clean_query(raw)
    if not q:
        return Answer("Was soll ich suchen?")
    items, _ = search(db, SearchQuery(q=q, limit=8))
    if not items and q.lower().endswith(("n", "e", "s")) and len(q) > 4:  # „Lötkolben“/„Zangen“ -> Einzahl versuchen
        items, _ = search(db, SearchQuery(q=q[:-1], limit=8))
    # zusätzlich einfacher Namensvergleich (neue Artikel sind evtl. noch nicht im Suchindex)
    stem = q[:-1] if len(q) > 4 and q.lower().endswith(("n", "e", "s")) else q
    compact = func.replace(Item.name, " ", "")
    by_name = list(db.scalars(select(Item).where(Item.active.is_(True), compact.ilike(f"%{stem.replace(' ', '')}%")).limit(8)).unique())
    words = [w[:-1] if len(w) > 4 and w[-1] in "nes" else w for w in q.split() if len(w) >= 3]
    if not by_name and len(words) > 1:  # „Filament PETG“ -> Artikel, die möglichst viele der Wörter im Namen haben
        by_name = list(db.scalars(select(Item).where(Item.active.is_(True), or_(*[Item.name.ilike(f"%{w}%") for w in words])).limit(20)).unique())
        by_name.sort(key=lambda i: -sum(w.casefold() in i.name.casefold() for w in words))
        by_name = by_name[:8]
    items = list({i.id: i for i in by_name + items}.values())
    item = _pick(items, q)
    if item is None:
        return Answer(f"Ich habe nichts zu {q} gefunden.")
    tc = TreeCache(db)
    lots = [lt for lt in item.lots if lt.status not in ("lost", "used") and ((lt.quantity or 0) > 0 or lt.coarse not in (None, "empty") or item.stock_mode != "exact")]
    loc_ids = list(dict.fromkeys(lt.location_id for lt in lots if lt.location_id))
    loans = list(db.scalars(select(Loan).where(Loan.item_id == item.id, Loan.returned_at.is_(None))))
    alts = [i.name for i in items if i.id != item.id][:3]
    parts = []
    if loc_ids:
        places = [spoken_place(tc, lid) for lid in loc_ids[:3]]
        if len(places) == 1:
            parts.append(f"{item.name} liegt hier: {places[0]}.")
        else:
            more = f" und an {len(loc_ids) - 3} weiteren Orten" if len(loc_ids) > 3 else ""
            parts.append(f"{item.name} liegt an {len(loc_ids)} Orten: " + " sowie ".join(places) + more + ".")
    elif not loans:
        parts.append(f"{item.name} ist gerade nicht auf Lager." if item.lots else f"Für {item.name} ist kein Lagerort eingetragen.")
    for ln in loans[:2]:
        who = ln.borrower_name or "jemanden"
        parts.append(f"Einer ist an {who} verliehen." if loc_ids else f"{item.name} ist an {who} verliehen.")
    return Answer(" ".join(parts), item, loc_ids, alts)
