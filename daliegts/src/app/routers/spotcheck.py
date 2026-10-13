"""Stichproben-Inventur: Raum wählen, zufällige (rotierende) Fächer prüfen."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Location
from ..security import require
from ..services import audit
from ..services import locations as locsvc
from ..services import spotcheck, walls
from ..web import flash, redirect, render

router = APIRouter()


def _roots(db: Session) -> list[dict]:
    """Auswahl: Räume/Wände zuerst, dann Lagermöbel und Bereiche (alles außer Fächern/Abteilen)."""
    tc = locsvc.TreeCache(db)
    rooms = walls.rooms(db)
    seen = {r["id"] for r in rooms}
    furniture = []
    for lid, n in tc.nodes.items():
        if lid in seen or n.type in ("fach", "abteil") or not tc.children.get(lid):
            continue
        furniture.append({"id": lid, "path": tc.path(lid)})
    furniture.sort(key=lambda r: locsvc.natural_key(r["path"]))
    return [{"id": r["id"], "path": r["path"]} for r in rooms] + furniture


def _ids(v: str) -> list[int]:
    return [int(x) for x in (v or "").split(",") if x.strip().isdigit()][:100]


@router.get("/inventur")
def page(request: Request, raum: int | None = None, orte: str = "", t: str = "", n: int = 10, leer: int = 0,
         db: Session = Depends(get_db), ctx=Depends(require("inventory", 1))):
    roots = _roots(db)
    if raum is None:
        last = request.cookies.get("lw_raum", "")
        raum = int(last) if last.isdigit() and any(r["id"] == int(last) for r in roots) else (roots[0]["id"] if roots else None)
    root = db.get(Location, raum) if raum else None
    tc = locsvc.TreeCache(db)
    since = None
    try:
        since = datetime.fromisoformat(t) if t else None
    except ValueError:
        since = None
    sample = []
    for lid in _ids(orte):
        loc = db.get(Location, lid)
        if loc is None:
            continue
        done = bool(since and loc.last_checked_at and loc.last_checked_at >= since)
        sample.append({"loc": loc, "path": tc.path(lid), "lots": spotcheck.lots_in(db, lid), "done": done})
    wall = None
    if root is not None:
        wall = root if walls.placed_modules(root) else walls.wall_of(root)
    return render(request, "spotcheck.html", {
        "roots": roots, "root": root, "n": max(1, min(n, 100)), "leer": leer, "sample": sample, "orte": orte, "t": t,
        "cov": spotcheck.coverage(db, root.id, bool(leer)) if root is not None else None,
        "done": sum(1 for s in sample if s["done"]), "wall": wall, "back": str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
    })


@router.post("/inventur/ziehen")
async def draw(request: Request, db: Session = Depends(get_db), ctx=Depends(require("inventory", 1))):
    f = await request.form()
    try:
        raum = int(f.get("raum") or 0)
        n = max(1, min(int(f.get("n") or 10), 100))
    except ValueError:
        raise HTTPException(400, "Ungültige Auswahl")
    if db.get(Location, raum) is None:
        raise HTTPException(404, "Lagerort nicht gefunden")
    leer = 1 if f.get("leer") else 0
    ids = spotcheck.draw(db, raum, n, bool(leer))
    if not ids:
        flash(request, "Hier gibt es keine Fächer mit Inhalt." + ("" if leer else " Mit „auch leere Fächer“ werden leere mitgeprüft."), "err")
        return redirect(f"/inventur?raum={raum}&leer={leer}")
    q = {"raum": raum, "n": n, "leer": leer, "orte": ",".join(map(str, ids)), "t": datetime.now().replace(microsecond=0).isoformat()}
    return redirect("/inventur?" + urlencode(q))


@router.post("/inventur/fach/{loc_id}")
async def check(loc_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("inventory", 2))):
    loc = db.get(Location, loc_id)
    if loc is None:
        raise HTTPException(404, "Lagerort nicht gefunden")
    f = await request.form()
    values = {}
    for k, v in f.items():
        if k.startswith("lot_") and k[4:].isdigit():
            values[int(k[4:])] = str(v)
    back = str(f.get("back") or "/inventur")
    if not back.startswith("/inventur"):
        back = "/inventur"
    try:
        changed = spotcheck.check_spot(db, loc, values, ctx.user_id)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"{back}#fach-{loc_id}")
    audit.log(db, ctx.user_id, "update", "location", loc.id, f"Stichproben-Inventur {loc.name}: {changed} Korrektur(en)")
    db.commit()
    flash(request, f"{loc.name} geprüft" + (f" – {changed} Menge(n) korrigiert." if changed else " – alles stimmt."))
    return redirect(f"{back}#fach-{loc_id}")
