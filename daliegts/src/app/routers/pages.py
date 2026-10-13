"""Dashboard, Suche, Code-Auflösung, Historie, Kategorien."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Category, Footprint, Item, Loan, Location, StockLot, StockMovement
from ..security import current, require
from ..services import audit
from ..services.codes import resolve
from ..services.items import category_path
from ..services.locations import TreeCache
from ..services.search import ParamFilter, SearchQuery, parameter_names, search
from ..services.seed import CATEGORY_STARTERS
from ..web import flash, redirect, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), ctx=Depends(current)):
    stats = {
        "items": db.scalar(select(func.count(Item.id))),
        "lots": db.scalar(select(func.count(StockLot.id))),
        "locations": db.scalar(select(func.count(Location.id))),
        "review": db.scalar(select(func.count(StockLot.id)).where(StockLot.needs_review.is_(True))),
    }
    if ctx.can("prices", 1):
        from ..services.stats import inventory

        stats["value"] = inventory(db).value
    if ctx.can("purchasing", 1):
        from ..models import ShoppingItem

        stats["to_order"] = db.scalar(select(func.count(ShoppingItem.id)).where(ShoppingItem.status == "open")) or 0
        stats["ordered"] = db.scalar(select(func.count(ShoppingItem.id)).where(ShoppingItem.status == "ordered")) or 0
    if ctx.can("orders", 1):
        from ..models import ORDER_ACTIVE, Order

        stats["orders"] = db.scalar(select(func.count(Order.id)).where(Order.status.in_(ORDER_ACTIVE))) or 0
    low = [i for i in db.scalars(select(Item).where((Item.min_stock > 0) | (Item.stock_mode == "coarse"))).unique() if i.below_min][:30]
    loans = list(db.scalars(select(Loan).where(Loan.returned_at.is_(None)).order_by(Loan.due)))
    recent = list(db.scalars(select(StockMovement).order_by(StockMovement.id.desc()).limit(15)))
    favorites = list(db.scalars(select(Item).where(Item.favorite.is_(True)).limit(12)))
    from ..services import expiry

    exp = expiry.counts(db)
    return render(request, "dashboard.html", {"stats": stats, "low": low, "loans": loans, "recent": recent,
                                              "favorites": favorites, "exp": exp, "today": date.today(), "soon": date.today() + timedelta(days=30)})


@router.get("/suche")
def search_page(request: Request, q: str = "", kategorie: str = "", ort: str = "", art: str = "",
                unter_min: int = 0, pruefen: int = 0, herkunft: str = "", nur: str = "", seite: int = 1, db: Session = Depends(get_db),
                ctx=Depends(require("items", 1))):
    qp = request.query_params
    kategorie = int(kategorie) if kategorie.strip().isdigit() else None  # leeres Filterfeld („alle“) darf keinen Fehler auslösen
    pfs = []
    for i in range(1, 4):
        n = qp.get(f"p{i}")
        if n:
            pfs.append(ParamFilter(n, qp.get(f"p{i}min", ""), qp.get(f"p{i}max", ""), qp.get(f"p{i}txt", "")))
    from .items import try_resolve_location

    ort_id, err = try_resolve_location(db, ort) if ort.strip() else (None, "")
    if err:
        flash(request, err + " – Filter ignoriert.", "warn")
    nur = nur if nur in ("platzhalter", "ohne_ort", "ohne_bestand") else ""
    sq = SearchQuery(q=q, category_id=kategorie, location_id=ort_id, kind=art, below_min=bool(unter_min), needs_review=bool(pruefen),
                     origin=herkunft, params=pfs, special=nur, limit=100, offset=(max(1, seite) - 1) * 100)
    # Code direkt erkannt? -> sofort springen
    if q.strip() and not any([kategorie, ort, art, unter_min, pfs]):
        r = resolve(db, q)
        if r.kind != "unknown" and r.code and (r.code[:2] in ("L-", "A-", "S-") or r.code.startswith(("INVENTREE", "AU-", "PR-"))):
            return redirect(_url_for_resolved(r))
    items, total = search(db, sq)  # ohne Suchbegriff: alle Artikel (A–Z, seitenweise)
    tc = TreeCache(db)
    cats = _category_options(db)
    return render(request, "search.html", {"items": items, "total": total, "sq": sq, "tc": tc, "cats": cats, "ort": ort, "nur": nur,
                                           "param_names": parameter_names(db)[:80], "seite": seite, "pfs": pfs})


def _url_for_resolved(r) -> str:
    return {"location": f"/orte/{r.id}", "item": f"/artikel/{r.id}", "lot": f"/lot/{r.id}", "order": f"/auftraege/{r.id}", "project": f"/projekte/{r.id}"}.get(r.kind, f"/suche?q={r.raw}")


@router.get("/s/{code}")
def short_code(code: str, request: Request, db: Session = Depends(get_db), ctx=Depends(current)):
    r = resolve(db, code)
    if r.kind == "unknown":
        flash(request, f"Code „{code}“ ist unbekannt. Du kannst ihn einem Lagerort oder Artikel zuordnen.", "warn")
        return redirect(f"/scan?unbekannt={code}")
    return redirect(_url_for_resolved(r))


@router.get("/historie")
def history(request: Request, seite: int = 1, db: Session = Depends(get_db), ctx=Depends(require("stock", 1))):
    rows = list(db.scalars(select(StockMovement).order_by(StockMovement.id.desc()).offset((seite - 1) * 100).limit(100)))
    return render(request, "history.html", {"rows": rows, "seite": seite})


# ------------------------------------------------------------------ Kategorien
def _category_options(db: Session) -> list[tuple[int, str]]:
    cats = list(db.scalars(select(Category)))
    return sorted(((c.id, category_path(c)) for c in cats), key=lambda t: t[1].lower())


def _category_tree(db: Session):
    cats = list(db.scalars(select(Category).order_by(Category.sort_order, Category.name)))
    counts = dict(db.execute(select(Item.category_id, func.count()).group_by(Item.category_id)).all())
    children: dict[int | None, list[Category]] = {}
    for c in cats:
        children.setdefault(c.parent_id, []).append(c)
    return children, counts


@router.get("/kategorien")
def categories(request: Request, db: Session = Depends(get_db), ctx=Depends(require("categories", 1))):
    children, counts = _category_tree(db)
    return render(request, "categories.html", {"children": children, "counts": counts, "starters": CATEGORY_STARTERS,
                                               "footprints": list(db.scalars(select(Footprint).order_by(Footprint.name)))})


@router.post("/kategorien/text")
def categories_bulk(request: Request, text: str = Form(""), starter: list[str] = Form([]), db: Session = Depends(get_db),
                    ctx=Depends(require("categories", 3))):
    blocks = [CATEGORY_STARTERS[s] for s in starter if s in CATEGORY_STARTERS]
    if text.strip():
        blocks.append(text)
    from ..services.locations import parse_outline

    n = 0

    def _add(nodes, pid):
        nonlocal n
        for node in nodes:
            q = select(Category).where(Category.name == node.name)
            q = q.where(Category.parent_id.is_(None)) if pid is None else q.where(Category.parent_id == pid)
            c = db.scalar(q)
            if c is None:
                c = Category(name=node.name, parent_id=pid)
                db.add(c)
                db.flush()
                n += 1
            _add(node.children, c.id)

    for b in blocks:
        _add(parse_outline(b), None)
    audit.log(db, ctx.user_id, "create", "category", None, f"{n} Kategorien per Text angelegt")
    db.commit()
    flash(request, f"{n} Kategorien angelegt.")
    return redirect("/kategorien")


@router.post("/kategorien/neu")
def category_new(request: Request, name: str = Form(...), parent_id: int | None = Form(None), db: Session = Depends(get_db),
                 ctx=Depends(require("categories", 3))):
    c = Category(name=name.strip(), parent_id=parent_id or None)
    db.add(c)
    db.flush()
    audit.log(db, ctx.user_id, "create", "category", c.id, f"Kategorie {c.name}")
    db.commit()
    return redirect("/kategorien")


@router.post("/kategorien/{cid}/umbenennen")
def category_rename(cid: int, request: Request, name: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("categories", 2))):
    c = db.get(Category, cid)
    before = audit.snapshot(c)
    c.name = name.strip()
    audit.log(db, ctx.user_id, "update", "category", c.id, f"Kategorie umbenannt: {before['name']} → {c.name}", audit.diff(before, audit.snapshot(c)))
    db.commit()
    if request.headers.get("hx-request"):
        return render(request, "partials/inline_name.html", {"name": c.name})
    return redirect("/kategorien")


@router.post("/kategorien/{cid}/verschieben")
def category_move(cid: int, parent_id: int | None = Form(None), index: int = Form(0), db: Session = Depends(get_db),
                  ctx=Depends(require("categories", 2))):
    c = db.get(Category, cid)
    parent_id = parent_id or None
    # Zyklus verhindern
    p = db.get(Category, parent_id) if parent_id else None
    while p is not None:
        if p.id == c.id:
            return {"ok": False, "error": "Zyklus"}
        p = p.parent
    c.parent_id = parent_id
    sibs = [s for s in db.scalars(select(Category).where(Category.parent_id == parent_id if parent_id else Category.parent_id.is_(None),
                                                          Category.id != c.id).order_by(Category.sort_order, Category.name))]
    sibs.insert(max(0, min(index, len(sibs))), c)
    for i, s in enumerate(sibs):
        s.sort_order = i + 1
    db.commit()
    return {"ok": True}


@router.post("/kategorien/{cid}/loeschen")
def category_delete(cid: int, request: Request, target: int | None = Form(None), db: Session = Depends(get_db), ctx=Depends(require("categories", 4))):
    c = db.get(Category, cid)
    if c is None:
        return redirect("/kategorien")
    items = list(db.scalars(select(Item).where(Item.category_id == cid)))
    kids = list(c.children)
    tgt = db.get(Category, target) if target else None
    if (items or kids) and tgt is None:
        flash(request, "Kategorie ist nicht leer – bitte Ziel für Inhalt wählen.", "err")
        return redirect("/kategorien")
    # Ziel darf nicht die Kategorie selbst oder ein Unterknoten sein
    p = tgt
    while p is not None:
        if p.id == c.id:
            flash(request, "Ziel liegt innerhalb der zu löschenden Kategorie.", "err")
            return redirect("/kategorien")
        p = p.parent
    for i in items:
        i.category_id = tgt.id if tgt else None
    for ch in kids:
        ch.parent_id = tgt.id if tgt else None
    db.flush()
    db.expire(c)
    audit.log(db, ctx.user_id, "delete", "category", c.id, f"Kategorie {c.name} gelöscht")
    db.delete(c)
    db.commit()
    return redirect("/kategorien")


@router.get("/kategorien/export.txt")
def categories_export(db: Session = Depends(get_db), ctx=Depends(require("categories", 1))):
    from fastapi.responses import PlainTextResponse

    children, _ = _category_tree(db)
    lines = []

    def walk(pid, d):
        for c in children.get(pid, []):
            lines.append("  " * d + c.name)
            walk(c.id, d + 1)

    walk(None, 0)
    return PlainTextResponse("\n".join(lines), headers={"Content-Disposition": "attachment; filename=kategorien.txt"})


@router.post("/footprints/{fid}/umbenennen")
def footprint_rename(fid: int, name: str = Form(...), db: Session = Depends(get_db), ctx=Depends(require("categories", 2))):
    f = db.get(Footprint, fid)
    f.name = name.strip()
    db.commit()
    return redirect("/kategorien#footprints")



@router.get("/ablauf")
def expiry_page(request: Request, monate: int = 6, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    """Haltbarkeit: was ist abgelaufen oder läuft bald ab? Dazu Garantien, die bald enden."""
    from ..services import expiry

    monate = max(1, min(monate, 36))
    return render(request, "expiry.html", {"groups": expiry.expiring(db, monate * 30), "warranties": expiry.warranties(db),
                                           "monate": monate, "today": date.today(), "tc": TreeCache(db)})
