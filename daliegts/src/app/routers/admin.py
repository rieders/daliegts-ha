"""Verwaltung: Benutzer, Gruppen & Rechte, Einstellungen, Audit-Log, Backup/Export, Import, eigenes Konto."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import ApiToken, AuditLog, Group, GroupPermission, User, UserPermission
from ..security import current, get_setting, hash_password, new_api_token, require, set_setting, token_hash, verify_password
from ..services import audit
from ..services.backup import backup_database, export_v1, import_v1, rotate
from ..services.permissions import AREAS
from ..web import back, flash, redirect, render

router = APIRouter()


@router.get("/admin")
def overview(request: Request, ctx=Depends(current)):
    return render(request, "admin.html")


# ------------------------------------------------------------------ Benutzer
@router.get("/admin/benutzer")
def users(request: Request, db: Session = Depends(get_db), ctx=Depends(require("users", 1))):
    return render(request, "admin_users.html", {"users": list(db.scalars(select(User).order_by(User.username))),
                                                "groups": list(db.scalars(select(Group).order_by(Group.name)))})


@router.post("/admin/benutzer/speichern")
async def user_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("users", 2))):
    f = await request.form()
    uid = int(f.get("id") or 0)
    if uid:
        u = db.get(User, uid)
        if u is None:
            flash(request, "Benutzer nicht gefunden.", "err")
            return redirect("/admin/benutzer")
    else:
        from ..security import check

        check(ctx, "users", 3)
        username = (f.get("username") or "").strip()
        if not username or not f.get("password") or len(f.get("password", "")) < 8:
            flash(request, "Neues Konto braucht Benutzername und Passwort (mind. 8 Zeichen).", "err")
            return redirect("/admin/benutzer")
        if db.scalar(select(User).where(User.username == username)):
            flash(request, "Benutzername ist bereits vergeben.", "err")
            return redirect("/admin/benutzer")
        u = User(username=username)
        db.add(u)
    new_group = int(f["group_id"]) if f.get("group_id") else None
    if u.id == ctx.user_id:
        admin_group = db.scalar(select(Group).where(Group.name == "Admin"))
        if not f.get("active") or (admin_group and u.group_id == admin_group.id and new_group != admin_group.id):
            flash(request, "Du kannst dich nicht selbst deaktivieren oder aus der Admin-Gruppe entfernen.", "err")
            return redirect("/admin/benutzer")
    u.display_name = f.get("display_name", "").strip()
    u.group_id = new_group
    was_active = u.active
    u.active = bool(f.get("active"))
    if f.get("password"):
        u.password_hash = hash_password(f["password"])
        u.session_version = (u.session_version or 0) + 1  # andere Sitzungen abmelden
    if was_active and not u.active:
        u.session_version = (u.session_version or 0) + 1
    if f.get("pin"):
        u.pin_hash = hash_password(f["pin"])
    db.flush()
    # Rechte-Overrides
    for area in AREAS:
        v = f.get(f"ov_{area}", "")
        existing = next((o for o in u.overrides if o.area == area), None)
        if v == "":
            if existing:
                db.delete(existing)
        else:
            if existing:
                existing.level = int(v)
            else:
                db.add(UserPermission(user_id=u.id, area=area, level=int(v)))
    audit.log(db, ctx.user_id, "update" if uid else "create", "user", u.id, f"Benutzer {u.username}")
    db.commit()
    flash(request, "Benutzer gespeichert.")
    return redirect("/admin/benutzer")


@router.get("/admin/gruppen")
def groups(request: Request, db: Session = Depends(get_db), ctx=Depends(require("users", 1))):
    gs = list(db.scalars(select(Group).order_by(Group.name)))
    matrix = {g.id: {p.area: p.level for p in g.permissions} for g in gs}
    return render(request, "admin_groups.html", {"groups": gs, "matrix": matrix})


@router.post("/admin/gruppen/speichern")
async def groups_save(request: Request, db: Session = Depends(get_db), ctx=Depends(require("users", 2))):
    f = await request.form()
    for g in db.scalars(select(Group)):
        have = {p.area: p for p in g.permissions}
        for area in AREAS:
            key = f"g{g.id}_{area}"
            if key in f:
                lvl = int(f[key])
                if g.name == "Admin" and area == "users":
                    lvl = 4  # Aussperren verhindern
                if area in have:
                    have[area].level = lvl
                else:
                    db.add(GroupPermission(group_id=g.id, area=area, level=lvl))
    if f.get("new_group"):
        g = Group(name=f["new_group"].strip(), description="")
        db.add(g)
        db.flush()
        for area in AREAS:
            db.add(GroupPermission(group_id=g.id, area=area, level=0))
    audit.log(db, ctx.user_id, "update", "group", None, "Rechte-Matrix geändert")
    db.commit()
    flash(request, "Rechte gespeichert.")
    return redirect("/admin/gruppen")


# ------------------------------------------------------------------ Einstellungen
@router.get("/admin/einstellungen")
def settings_page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    return render(request, "admin_settings.html", {
        "guest": get_setting(db, "guest_enabled", False), "pin": get_setting(db, "pin_login_enabled", True),
        "regex": json.dumps(get_setting(db, "scan_regex_mappings", []), ensure_ascii=False, indent=1),
        "web": json.dumps(get_setting(db, "web_search_urls", []), ensure_ascii=False, indent=1),
        "base_url": settings.base_url, "data_dir": settings.data_dir,
    })


@router.post("/admin/einstellungen")
def settings_save(request: Request, guest: bool = Form(False), pin: bool = Form(False), regex: str = Form("[]"), web: str = Form("[]"),
                  db: Session = Depends(get_db), ctx=Depends(require("system", 2))):
    try:
        set_setting(db, "scan_regex_mappings", json.loads(regex or "[]"))
        set_setting(db, "web_search_urls", json.loads(web or "[]"))
    except json.JSONDecodeError as e:
        flash(request, f"JSON-Fehler: {e}", "err")
        return redirect("/admin/einstellungen")
    set_setting(db, "guest_enabled", guest)
    set_setting(db, "pin_login_enabled", pin)
    audit.log(db, ctx.user_id, "update", "settings", None, "Einstellungen geändert")
    db.commit()
    flash(request, "Einstellungen gespeichert.")
    return redirect("/admin/einstellungen")


# ------------------------------------------------------------------ Lizenz (v0.22)
@router.get("/admin/lizenz")
def license_page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 1))):
    from ..config import APP_RELEASE_DATE
    from ..services import license as lic

    return render(request, "admin_license.html", {"lic": lic.current(db), "features": lic.FEATURES, "release": APP_RELEASE_DATE,
                                                  "free_ctrl": lic.FREE_WLED_CONTROLLERS})


@router.post("/admin/lizenz")
def license_save(request: Request, key: str = Form(""), entfernen: str = Form(""), db: Session = Depends(get_db),
                 ctx=Depends(require("system", 2))):
    from ..services import license as lic

    if entfernen:
        set_setting(db, lic.SETTING, "")
        audit.log(db, ctx.user_id, "update", "settings", None, "Lizenz entfernt")
        db.commit()
        flash(request, "Lizenz entfernt – DaLiegt’s läuft als Grundversion weiter. Deine Daten bleiben vollständig erhalten.")
        return redirect("/admin/lizenz")
    parsed = lic.parse(lic.normalize(key))
    if not parsed.ok:
        flash(request, parsed.error or "Bitte einen Lizenzschlüssel einfügen.", "err")
        return redirect("/admin/lizenz")
    set_setting(db, lic.SETTING, lic.normalize(key))
    audit.log(db, ctx.user_id, "update", "settings", None, f"Lizenz eingetragen ({parsed.name}, {parsed.lic_id})")
    db.commit()
    flash(request, f"Danke, {parsed.name}! DaLiegt’s Pro ist freigeschaltet." if parsed.pro else parsed.error, "ok" if parsed.pro else "err")
    return redirect("/admin/lizenz")


# ------------------------------------------------------------------ Audit-Log
@router.get("/admin/audit")
def audit_page(request: Request, seite: int = 1, entity: str = "", db: Session = Depends(get_db), ctx=Depends(require("audit", 1))):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    if entity:
        q = q.where(AuditLog.entity == entity)
    rows = list(db.scalars(q.offset((seite - 1) * 100).limit(100)))
    return render(request, "admin_audit.html", {"rows": rows, "seite": seite, "entity": entity})


@router.post("/admin/audit/{entry_id}/rueckgaengig")
def audit_undo(entry_id: int, request: Request, db: Session = Depends(get_db), ctx=Depends(require("audit", 2))):
    e = db.get(AuditLog, entry_id)
    if e is not None and audit.undo(db, e, ctx.user_id):
        db.commit()
        flash(request, "Änderung rückgängig gemacht.")
    else:
        flash(request, "Diese Änderung kann nicht automatisch rückgängig gemacht werden.", "err")
    return back(request, "/admin/audit")


# ------------------------------------------------------------------ Backup & Export
@router.get("/admin/backup")
def backup_page(request: Request, ctx=Depends(require("backup", 1))):
    files = sorted(settings.backup_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return render(request, "admin_backup.html", {"files": files})


@router.post("/admin/backup/jetzt")
def backup_now(request: Request, art: str = Form("db"), db: Session = Depends(get_db), ctx=Depends(require("backup", 1))):
    p = export_v1(db) if art == "export" else backup_database()
    rotate()
    flash(request, f"Erstellt: {p.name}")
    return redirect("/admin/backup")


@router.get("/admin/backup/datei/{name}")
def backup_download(name: str, ctx=Depends(require("backup", 1))):
    p = (settings.backup_dir / name).resolve()
    if p.parent != settings.backup_dir.resolve() or not p.exists():
        return redirect("/admin/backup")
    return FileResponse(p, filename=p.name)


# ------------------------------------------------------------------ Import
@router.get("/admin/import")
def import_page(request: Request, ctx=Depends(require("import_export", 1))):
    return render(request, "admin_import.html", {"report": request.session.pop("import_report", None)})


async def _save_upload(up: UploadFile | None, tmp: Path) -> Path | None:
    if up is None or not up.filename:
        return None
    p = tmp / Path(up.filename).name
    p.write_bytes(await up.read())
    return p


@router.post("/admin/import/partdb")
async def import_partdb(request: Request, dump: UploadFile = File(...), dump_neuer: UploadFile | None = File(None),
                        media: UploadFile | None = File(None), probelauf: bool = Form(False), db: Session = Depends(get_db),
                        ctx=Depends(require("import_export", 3))):
    from ..importers.partdb import import_partdb as run

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        p1, p2, pm = await _save_upload(dump, t), await _save_upload(dump_neuer, t), await _save_upload(media, t)
        if not probelauf:
            backup_database(label="vor-import")
        report = run(db, p1, newer_dump=p2, media_archive=pm, user_id=ctx.user_id, dry_run=probelauf)
    request.session["import_report"] = report.as_text()
    return redirect("/admin/import")


@router.post("/admin/import/inventree")
async def import_inventree(request: Request, sqlite: UploadFile | None = File(None), tracking: UploadFile | None = File(None),
                           media: UploadFile | None = File(None), probelauf: bool = Form(False), db: Session = Depends(get_db),
                           ctx=Depends(require("import_export", 3))):
    from ..importers.inventree import import_inventree as run

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        ps, pt, pm = await _save_upload(sqlite, t), await _save_upload(tracking, t), await _save_upload(media, t)
        if not probelauf:
            backup_database(label="vor-import")
        report = run(db, sqlite_path=ps, tracking_csv=pt, media_archive=pm, user_id=ctx.user_id, dry_run=probelauf)
    request.session["import_report"] = report.as_text()
    return redirect("/admin/import")


# ------------------------------------------------------------------ Import-Assistent (v0.29)
_JOB_RE = __import__("re").compile(r"^[0-9a-f]{32}$")


def _jobs_dir() -> Path:
    d = settings.data_dir / "import_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_job(job: str):
    from ..importers import assistant

    p = _jobs_dir() / f"{job}.json"
    if not _JOB_RE.match(job) or not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return assistant.Table(d["columns"], d["rows"], d.get("kind", "tabelle"), d.get("notes", [])), d.get("filename", "")


def _profiles(db: Session) -> dict:
    v = get_setting(db, "import_profiles", {})
    return v if isinstance(v, dict) else {}


@router.post("/admin/import/assistent")
async def assistant_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), ctx=Depends(require("import_export", 3))):
    import time
    import uuid

    from ..importers import assistant

    for old in _jobs_dir().glob("*.json"):  # alte Zwischenstände aufräumen
        if time.time() - old.stat().st_mtime > 86400:
            old.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        p = await _save_upload(file, Path(tmp))
        if p is None:
            flash(request, "Keine Datei gewählt.", "err")
            return redirect("/admin/import")
        try:
            table = assistant.read_table(p)
        except Exception as e:  # noqa: BLE001 – jede Art von kaputter Datei als Meldung zeigen
            flash(request, f"Datei nicht lesbar: {e}", "err")
            return redirect("/admin/import")
    job = uuid.uuid4().hex
    (_jobs_dir() / f"{job}.json").write_text(json.dumps({"columns": table.columns, "rows": table.rows, "kind": table.kind, "notes": table.notes,
                                                         "filename": p.name}, ensure_ascii=False), encoding="utf-8")
    return redirect(f"/admin/import/assistent/{job}")


def _assistant_page(request, db, job, cfg, report=None, results=None, saved=""):
    from ..importers import assistant

    loaded = _load_job(job)
    if loaded is None:
        flash(request, "Der Import ist abgelaufen – bitte die Datei erneut hochladen.", "err")
        return redirect("/admin/import")
    table, filename = loaded
    if not cfg.get("columns"):
        cfg = dict(cfg, columns=assistant.guess_mapping(table.columns))
    samples = {c: [v for v in (r[i] for r in table.rows[:200]) if v][:assistant.SAMPLE_VALUES] for i, c in enumerate(table.columns)}
    return render(request, "admin_import_assistant.html", {
        "job": job, "table": table, "filename": filename, "cfg": cfg, "samples": samples, "targets": assistant.TARGETS, "ops": assistant.OPS,
        "actions": assistant.ACTIONS, "values": assistant.distinct_values(table, cfg), "report": report.as_text() if report else "",
        "results": results or [], "profiles": _profiles(db), "saved": saved, "fields": [t for t in assistant.TARGETS if t[0] and t[0] != "param:"],
        "dry": report.dry_run if report else None})


@router.get("/admin/import/assistent/{job}")
def assistant_page(job: str, request: Request, profil: str = "", db: Session = Depends(get_db), ctx=Depends(require("import_export", 1))):
    cfg = _profiles(db).get(profil, {}) if profil else {}
    return _assistant_page(request, db, job, cfg)


@router.post("/admin/import/assistent/{job}")
async def assistant_run(job: str, request: Request, db: Session = Depends(get_db), ctx=Depends(require("import_export", 3))):
    from ..importers import assistant

    f = await request.form()
    try:
        cfg = json.loads(f.get("config") or "{}")
        assert isinstance(cfg, dict)
    except (ValueError, AssertionError):
        flash(request, "Die Einstellungen waren nicht lesbar.", "err")
        return redirect(f"/admin/import/assistent/{job}")
    mode = f.get("mode", "preview")
    loaded = _load_job(job)
    if loaded is None:
        flash(request, "Der Import ist abgelaufen – bitte die Datei erneut hochladen.", "err")
        return redirect("/admin/import")
    if mode == "save":
        name = (f.get("profile_name") or "").strip()[:60]
        if not name:
            flash(request, "Bitte einen Profilnamen eingeben.", "err")
            return _assistant_page(request, db, job, cfg)
        profs = _profiles(db)
        profs[name] = cfg
        set_setting(db, "import_profiles", profs)
        db.commit()
        return _assistant_page(request, db, job, cfg, saved=f"Profil „{name}“ gespeichert.")
    if mode == "run":
        backup_database(label="vor-import")
    try:
        report, results = assistant.run(db, loaded[0], cfg, user_id=ctx.user_id, dry_run=(mode != "run"))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        flash(request, f"Import abgebrochen, nichts gespeichert: {e}", "err")
        return _assistant_page(request, db, job, cfg)
    if mode == "run":
        request.session["import_report"] = report.as_text()
        (_jobs_dir() / f"{job}.json").unlink(missing_ok=True)
        return redirect("/admin/import")
    return _assistant_page(request, db, job, cfg, report, results)


@router.post("/admin/import/assistent/profil/{name}/loeschen")
def assistant_profile_delete(name: str, request: Request, db: Session = Depends(get_db), ctx=Depends(require("import_export", 3))):
    profs = _profiles(db)
    if profs.pop(name, None) is not None:
        set_setting(db, "import_profiles", profs)
        db.commit()
    flash(request, "Profil gelöscht.")
    return redirect("/admin/import")


@router.post("/admin/import/lagerwerk")
async def import_lagerwerk(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), ctx=Depends(require("import_export", 4))):
    with tempfile.TemporaryDirectory() as tmp:
        p = await _save_upload(file, Path(tmp))
        try:
            counts = import_v1(db, p)
        except ValueError as e:
            flash(request, str(e), "err")
            return redirect("/admin/import")
    request.session.clear()
    flash(request, f"Wiederhergestellt: {sum(counts.values())} Datensätze. Bitte neu anmelden.")
    return redirect("/login")


@router.get("/admin/aufraeumen")
def cleanup_page(request: Request, db: Session = Depends(get_db), ctx=Depends(require("import_export", 1))):
    from ..services import cleanup
    from ..services.locations import TreeCache

    ph = cleanup.placeholders(db)
    tc = TreeCache(db)
    where: dict[str, int] = {}
    for it in ph:
        for lot in it.lots:
            key = tc.path(lot.location_id) if lot.location_id else "ohne Ort"
            where[key] = where.get(key, 0) + 1
    locs = cleanup.empty_import_locations(db, {i.id for i in ph})
    return render(request, "admin_cleanup.html", {
        "ph": ph, "ph_lots": sum(len(i.lots) for i in ph), "where": sorted(where.items(), key=lambda kv: -kv[1])[:15],
        "locs": locs, "tc": tc, "old": cleanup.import_groups(db),
    })


@router.post("/admin/aufraeumen")
def cleanup_run(request: Request, platzhalter: bool = Form(False), leere_orte: bool = Form(False), db: Session = Depends(get_db),
                ctx=Depends(require("items", 4))):
    from ..services import cleanup

    if not (platzhalter or leere_orte):
        flash(request, "Bitte auswählen, was entfernt werden soll.", "err")
        return redirect("/admin/aufraeumen")
    backup = backup_database(label="vor-aufraeumen")
    parts = []
    ignore = {i.id for i in cleanup.placeholders(db)} if platzhalter else set()
    locs = cleanup.empty_import_locations(db, ignore) if leere_orte else []
    if platzhalter:
        r = cleanup.remove_placeholders(db)
        parts.append(f"{r.items} Platzhalter-Artikel ({r.lots} Bestände)")
    if leere_orte:
        db.expire_all()
        n = cleanup.remove_locations(db, locs)
        parts.append(f"{n} leere Import-Lagerorte")
    audit.log(db, ctx.user_id, "delete", "item", None, "Aufgeräumt: " + ", ".join(parts))
    db.commit()
    flash(request, "Entfernt: " + ", ".join(parts) + f". Sicherung vorher: {backup.name}")
    return redirect("/admin/aufraeumen")


@router.post("/admin/suchindex")
def reindex_all(request: Request, db: Session = Depends(get_db), ctx=Depends(require("system", 2))):
    from ..services.search import rebuild_index

    n = rebuild_index(db)
    flash(request, f"Suchindex neu aufgebaut ({n} Artikel).")
    return redirect("/admin")


# ------------------------------------------------------------------ Eigenes Konto
@router.get("/konto")
def account(request: Request, db: Session = Depends(get_db), ctx=Depends(current)):
    tokens = list(db.scalars(select(ApiToken).where(ApiToken.user_id == ctx.user_id))) if ctx.user else []
    return render(request, "account.html", {"tokens": tokens, "new_token": request.session.pop("new_token", None),
                                            "trusted": request.cookies.get("lw_trusted") == "1"})


@router.post("/konto")
def account_save(request: Request, old: str = Form(""), new: str = Form(""), pin: str = Form(""), display_name: str = Form(""),
                 db: Session = Depends(get_db), ctx=Depends(current)):
    u = ctx.user
    if u is None:
        return redirect("/login")
    if display_name:
        u.display_name = display_name.strip()
    if new:
        if not verify_password(u.password_hash, old) or len(new) < 8:
            flash(request, "Altes Passwort falsch oder neues zu kurz (mind. 8 Zeichen).", "err")
            return redirect("/konto")
        u.password_hash = hash_password(new)
        u.session_version = (u.session_version or 0) + 1
        request.session["sv"] = u.session_version  # diese Sitzung bleibt angemeldet
    if pin:
        if not pin.isdigit() or not 4 <= len(pin) <= 6:
            flash(request, "PIN: 4–6 Ziffern.", "err")
            return redirect("/konto")
        u.pin_hash = hash_password(pin)
    db.commit()
    flash(request, "Gespeichert.")
    return redirect("/konto")


@router.post("/konto/ueberall-abmelden")
def logout_everywhere(request: Request, db: Session = Depends(get_db), ctx=Depends(current)):
    if ctx.user:
        ctx.user.session_version = (ctx.user.session_version or 0) + 1
        db.commit()
    request.session.clear()
    return redirect("/login")


@router.post("/konto/token")
def token_new(request: Request, name: str = Form("Token"), db: Session = Depends(get_db), ctx=Depends(require("api_tokens", 3))):
    tok = new_api_token()
    db.add(ApiToken(user_id=ctx.user_id, name=name.strip() or "Token", token_hash=token_hash(tok)))
    db.commit()
    request.session["new_token"] = tok
    return redirect("/konto")


@router.post("/konto/token/{tid}/loeschen")
def token_delete(tid: int, request: Request, db: Session = Depends(get_db), ctx=Depends(current)):
    t = db.get(ApiToken, tid)
    if t and t.user_id == ctx.user_id:
        db.delete(t)
        db.commit()
    return redirect("/konto")


@router.get("/admin/home-assistant")
def home_assistant(request: Request, ctx=Depends(current), db: Session = Depends(get_db)):
    """Fertige Home-Assistant-Konfiguration zum Kopieren: „Wo ist …?“ per Sprache, Sensoren."""
    from ..services import license as lic

    lic.check(db, "homeassistant")
    return render(request, "admin_ha.html")
