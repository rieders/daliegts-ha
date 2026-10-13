"""Projekte / Baugruppen: Stückliste speichern oder aus KiCad übernehmen, Bedarf für N Platinen, Auftrag erzeugen (v0.10)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, Order, Project, ProjectLine
from ..security import check, require
from ..services import audit
from ..services import projects as psvc
from ..services.items import create_item
from ..services.locations import TreeCache
from ..services.license import require_pro
from ..web import flash, redirect, render

router = APIRouter(dependencies=[Depends(require_pro("projects"))])
MAX_UPLOAD = 20 * 1024 * 1024


def _project(db: Session, pid: int) -> Project:
    p = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden")
    return p


def _line(db: Session, lid: int) -> ProjectLine:
    ln = db.get(ProjectLine, lid)
    if ln is None:
        raise HTTPException(404, "Zeile nicht gefunden")
    return ln


def _num(v, default: float) -> float:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else default
    except ValueError:
        raise ValueError(f"„{v}“ ist keine Zahl.")


async def _read_upload(f) -> tuple[str, bytes] | None:
    up = f.get("datei")
    if up is None or not getattr(up, "filename", ""):
        return None
    data = await up.read()
    if len(data) > MAX_UPLOAD:
        raise ValueError("Datei ist zu groß (max. 20 MB).")
    return up.filename, data


def _import(db: Session, p: Project, name: str, data: bytes, replace: bool = True) -> str:
    rows = psvc.parse_file(name, data)
    if not rows:
        raise ValueError("In der Datei wurden keine Bauteile gefunden. Unterstützt: KiCad-Schaltplan (.kicad_sch), Netzliste (.xml/.net), Stückliste (.csv).")
    keep = [(ln.item_id, ln.refs, ln.value, ln.qty) for ln in p.lines if ln.match == "leiterplatte"] if replace else []
    st = psvc.import_rows(db, p, rows, replace)
    for item_id, refs, value, qty in keep:  # die Leiterplatte selbst bleibt beim Neuimport erhalten
        db.add(ProjectLine(project_id=p.id, item_id=item_id, refs=refs, value=value, qty=qty, match="leiterplatte", sort=-1))
    p.source = name[:255]
    db.flush()
    return (f"{st['lines']} Stücklistenzeilen ({st['parts']} Bauteile) übernommen, {st['matched']} automatisch Lagerartikeln zugeordnet"
            + (f", {st['dnp']} „nicht bestücken“" if st["dnp"] else "") + ".")


@router.get("/projekte")
def project_list(request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 1))):
    projects = list(db.scalars(select(Project).order_by(Project.name)))
    info = {}
    for p in projects:
        needs = psvc.availability(db, p, 1)
        info[p.id] = {"lines": len([ln for ln in p.lines if not ln.dnp]), "open": sum(1 for ln in p.lines if not ln.dnp and ln.item is None),
                      "buildable": psvc.buildable(needs) if needs else 0}
    return render(request, "projects.html", {"projects": projects, "info": info})


@router.post("/projekte/neu")
async def project_create(request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 3))):
    f = await request.form()
    try:
        p = psvc.create_project(db, str(f.get("name") or ""), ctx.user_id, revision=str(f.get("revision") or "")[:40],
                                description=str(f.get("description") or ""))
        msg = f"Projekt {p.code} angelegt."
        if f.get("leiterplatte"):
            check(ctx, "items", 3)
            pcb = create_item(db, f"Leiterplatte {p.name}" + (f" Rev. {p.revision}" if p.revision else ""), ctx.user_id,
                              description=f"Platine für Projekt {p.code}")
            p.pcb_item_id = pcb.id
            db.add(ProjectLine(project_id=p.id, item_id=pcb.id, refs="PCB", value="Leiterplatte", qty=1, match="leiterplatte", sort=-1))
            db.flush()
            db.refresh(p)
            msg += f" Leiterplatte als Artikel {pcb.code} angelegt – nach dem Eintreffen einfach einlagern."
        up = await _read_upload(f)
        if up:
            msg += " " + _import(db, p, *up)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect("/projekte")
    audit.log(db, ctx.user_id, "create", "project", p.id, f"Projekt {p.code} {p.name} angelegt")
    db.commit()
    flash(request, msg)
    return redirect(f"/projekte/{p.id}")


@router.get("/projekte/{pid}")
def project_detail(pid: int, request: Request, anzahl: int = 1, db: Session = Depends(get_db), ctx=Depends(require("orders", 1))):
    p = _project(db, pid)
    anzahl = max(1, min(anzahl, 10000))
    needs = psvc.availability(db, p, anzahl)
    one = psvc.availability(db, p, 1)
    index = None
    cands = {}
    for ln in p.lines:
        if ln.item is None and not ln.dnp:
            index = index if index is not None else psvc.build_index(db)
            m = psvc.match_row(db, ln.value, ln.footprint, ln.mpn, "", "", ln.refs.split(",")[0].strip(), index)
            cands[ln.id] = ([(m.item, 100)] if m.item else []) + [c for c in m.candidates if not m.item or c[0].id != m.item.id]
    orders = list(db.scalars(select(Order).where(Order.project_id == p.id).order_by(Order.created_at.desc()).limit(10)))
    from ..services import walls

    where = {nd.line.id: (w.id if (w := walls.wall_of(nd.lots[0].location)) is not None else None) for nd in needs if nd.lots and nd.lots[0].location}
    return render(request, "project_detail.html", {
        "p": p, "needs": needs, "anzahl": anzahl, "buildable": psvc.buildable(one) if one else 0, "cands": cands, "orders": orders,
        "tc": TreeCache(db), "package_of": psvc.package_of, "where": where,
        "unassigned": sum(1 for ln in p.lines if ln.item is None and not ln.dnp), "short": sum(1 for nd in needs if nd.short > 0 or nd.line.item is None),
    })


@router.post("/projekte/{pid}/import")
async def project_import(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    p = _project(db, pid)
    f = await request.form()
    try:
        up = await _read_upload(f)
        if not up:
            raise ValueError("Bitte eine Datei wählen.")
        msg = _import(db, p, *up, replace=bool(f.get("ersetzen")))
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/projekte/{pid}")
    audit.log(db, ctx.user_id, "update", "project", p.id, f"Stückliste für {p.name} importiert ({up[0]})")
    db.commit()
    flash(request, msg)
    return redirect(f"/projekte/{pid}")


@router.post("/projekte/{pid}/bearbeiten")
async def project_edit(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    p = _project(db, pid)
    f = await request.form()
    if str(f.get("name") or "").strip():
        p.name = str(f.get("name")).strip()[:200]
    p.revision = str(f.get("revision") or "")[:40]
    p.description = str(f.get("description") or "")
    db.commit()
    flash(request, "Gespeichert.")
    return redirect(f"/projekte/{pid}")


@router.post("/projekte/{pid}/loeschen")
def project_delete(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 4))):
    p = _project(db, pid)
    name = p.name
    audit.log(db, ctx.user_id, "delete", "project", p.id, f"Projekt {p.code} {name} gelöscht")
    db.delete(p)
    db.commit()
    flash(request, f"Projekt „{name}“ gelöscht (Artikel und Aufträge bleiben).")
    return redirect("/projekte")


@router.post("/projekte/{pid}/zeile")
async def line_add(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    from ..services.orders import _find_item

    p = _project(db, pid)
    f = await request.form()
    raw = str(f.get("item_id") or "")
    item = db.get(Item, int(raw)) if raw.isdigit() else _find_item(db, str(f.get("item") or ""))
    try:
        if item is None:
            raise ValueError("Artikel nicht gefunden – bitte aus den Vorschlägen wählen.")
        db.add(ProjectLine(project_id=p.id, item_id=item.id, refs=str(f.get("refs") or "")[:1000], value=item.name[:200],
                           qty=_num(f.get("qty"), 1), match="hand", sort=len(p.lines)))
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect(f"/projekte/{pid}")
    db.commit()
    flash(request, f"{item.name} hinzugefügt.")
    return redirect(f"/projekte/{pid}")


@router.post("/projekte/zeile/{lid}/zuordnen")
async def line_assign(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    from ..services.orders import _find_item

    ln = _line(db, lid)
    f = await request.form()
    raw = str(f.get("item_id") or "")
    if f.get("neu"):
        check(ctx, "items", 3)
        name = " ".join(x for x in (ln.value, psvc.package_of(ln.footprint)) if x) or ln.refs
        item = create_item(db, name[:200], ctx.user_id, description=f"aus Projekt {ln.project.name}: {ln.refs}"[:500], mpn=ln.mpn or "")
    else:
        item = db.get(Item, int(raw)) if raw.isdigit() else _find_item(db, str(f.get("item") or ""))
    if item is None:
        flash(request, "Artikel nicht gefunden – bitte aus der Liste oder den Vorschlägen wählen.", "err")
        return redirect(f"/projekte/{ln.project_id}#z-{lid}")
    ln.item_id = item.id
    ln.match = "neu" if f.get("neu") else "hand"
    psvc.remember(db, ln, item)
    db.commit()
    flash(request, f"{ln.refs or ln.value} → {item.name} (gemerkt für künftige KiCad-Importe).")
    return redirect(f"/projekte/{ln.project_id}#z-{lid}")


@router.post("/projekte/zeile/{lid}/aendern")
async def line_edit(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    ln = _line(db, lid)
    f = await request.form()
    try:
        if f.get("qty") not in (None, ""):
            q = _num(f.get("qty"), ln.qty)
            if q <= 0:
                raise ValueError("Menge muss größer als 0 sein.")
            ln.qty = q
        if f.get("refs") is not None:
            ln.refs = str(f.get("refs"))[:1000]
        if f.get("dnp") is not None:
            ln.dnp = f.get("dnp") == "1"
        if f.get("loesen"):
            ln.item_id, ln.match = None, ""
    except ValueError as e:
        flash(request, str(e), "err")
        return redirect(f"/projekte/{ln.project_id}")
    db.commit()
    return redirect(f"/projekte/{ln.project_id}#z-{lid}")


@router.post("/projekte/zeile/{lid}/loeschen")
def line_delete(lid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 2))):
    ln = _line(db, lid)
    pid = ln.project_id
    db.delete(ln)
    db.commit()
    flash(request, "Zeile entfernt.")
    return redirect(f"/projekte/{pid}")


@router.post("/projekte/{pid}/bauen")
async def project_build(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("orders", 3))):
    p = _project(db, pid)
    f = await request.form()
    try:
        n = int(f.get("anzahl") or 1)
        create_missing = bool(f.get("neu"))
        if create_missing:
            check(ctx, "items", 3)
        order, short = psvc.build_order(db, p, n, ctx.user_id, create_missing)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/projekte/{pid}?anzahl={f.get('anzahl') or 1}")
    audit.log(db, ctx.user_id, "create", "order", order.id, f"Auftrag {order.code} aus Projekt {p.code} ×{n}")
    db.commit()
    flash(request, f"Auftrag {order.code} für {n}× {p.name} angelegt – Bauteile sind reserviert."
          + (f" {short} Fehlteil(e) auf die Einkaufsliste gesetzt." if short else " Alles auf Lager."))
    return redirect(f"/auftraege/{order.id}")


# ------------------------------------------------------------------ Bestellplaner (v0.14)
def _plan_params(q) -> dict:
    def g(k, d=""):
        v = q.get(k, d)
        return v[-1] if isinstance(v, list) else v

    try:
        n = max(1, min(int(g("anzahl", "1") or 1), 10000))
    except ValueError:
        n = 1
    submitted = g("f") == "1"
    std_raw = q.getlist("std") if hasattr(q, "getlist") else (q.get("std") or [])
    std = None if not submitted else {int(x) for x in std_raw if str(x).isdigit()}
    mode = g("modus", "wenige")
    return {"anzahl": n, "liste": (g("liste") == "1") if submitted else True, "std": std,
            "modus": mode if mode in ("guenstig", "wenige", "einer") else "wenige"}


def _plan_all(db: Session, p: Project, prm: dict, variante: str = ""):
    from ..services import orderplan as op

    dm = op.demand(db, p, prm["anzahl"], prm["liste"], prm["std"])
    singles = op.single_supplier_options(db, dm.entries)
    only = None
    if variante.isdigit():
        only = int(variante)
    elif prm["modus"] == "einer":  # ein Händler, der alles hat -> dort; sonst mit hohem Paket-Aufschlag
        full = [o for o in singles if o.complete and not o.unpriced]
        only = full[0].supplier.id if full else None
    plan = op.plan(db, dm.entries, prm["modus"], only)
    return dm, singles, plan


@router.get("/projekte/{pid}/bestellen")
def order_planner(pid: int, request: Request, job: str = "", db: Session = Depends(get_db), ctx=Depends(require("purchasing", 1))):
    from ..services import orderplan as op
    from ..services.infosync import open_candidate_count
    from ..services.providers import active_providers

    p = _project(db, pid)
    prm = _plan_params(request.query_params)
    dm, singles, plan = _plan_all(db, p, prm, request.query_params.get("variante", ""))
    from urllib.parse import urlencode

    qs_base = urlencode([(k, v) for k, v in request.query_params.multi_items() if k not in ("variante", "job")])
    qs = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "job"])
    return render(request, "order_plan.html", {"qs_base": qs_base,
        "p": p, "prm": prm, "dm": dm, "singles": singles, "plan": plan, "modes": op.MODES, "job": job,
        "unpriced": op.unpriced(db, dm.entries), "no_shipping": op.missing_shipping(plan, singles),
        "providers": [x.name for x in active_providers(db)], "questions": open_candidate_count(db), "qs": qs,
        "variante": request.query_params.get("variante", ""),
    })


@router.post("/projekte/{pid}/bestellen/online")
async def order_planner_online(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 2))):
    from urllib.parse import parse_qs

    from ..services import infosync
    from ..services import orderplan as op

    p = _project(db, pid)
    f = await request.form()
    qs = str(f.get("qs") or "")
    prm = _plan_params({k: v for k, v in parse_qs(qs, keep_blank_values=True).items()} | {"std": parse_qs(qs).get("std", [])})
    dm = op.demand(db, p, prm["anzahl"], prm["liste"], prm["std"])
    ids = op.item_ids_to_check(db, dm.entries)
    if not ids:
        flash(request, "Nichts zu bestellen – keine Online-Suche nötig.")
        return redirect(f"/projekte/{pid}/bestellen?{qs}")
    job = infosync.start_job(ids, cross=True, label=f"Bestellplaner {p.name}")
    return redirect(f"/projekte/{pid}/bestellen?{qs}&job={job}")


@router.post("/projekte/{pid}/bestellen/anlegen")
async def order_planner_create(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("items", 3))):
    p = _project(db, pid)
    f = await request.form()
    n = psvc.create_missing_items(db, p, ctx.user_id)
    audit.log(db, ctx.user_id, "update", "project", p.id, f"{n} nicht zugeordnete Bauteile als Artikel angelegt")
    db.commit()
    flash(request, f"{n} neue Artikel angelegt. Mit „Online suchen“ Händler und Preise dazu finden.")
    return redirect(f"/projekte/{pid}/bestellen?{f.get('qs') or ''}")


@router.post("/projekte/{pid}/bestellen/uebernehmen")
async def order_planner_apply(pid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("purchasing", 3))):
    from urllib.parse import parse_qs

    from ..services import orderplan as op

    p = _project(db, pid)
    f = await request.form()
    qs = str(f.get("qs") or "")
    parsed = parse_qs(qs, keep_blank_values=True)
    prm = _plan_params({k: v for k, v in parsed.items()} | {"std": parsed.get("std", [])})
    _, _, plan = _plan_all(db, p, prm, str(f.get("variante") or ""))
    n = op.apply(db, plan, p, prm["anzahl"], ctx.user_id)
    audit.log(db, ctx.user_id, "update", "shopping", None, f"Bestellplaner {p.code} ×{prm['anzahl']}: {n} Positionen übernommen")
    db.commit()
    flash(request, f"{n} Positionen auf die Einkaufsliste übernommen – je Händler mit Warenkorb-Export (CSV / Schnellbestellung).")
    return redirect("/einkauf")
