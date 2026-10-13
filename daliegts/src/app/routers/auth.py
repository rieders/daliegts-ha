"""Anmeldung, Ersteinrichtung, Admin-Modus."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Group, User, now
from ..security import ADMIN_MODE_SECONDS, current, get_setting, hash_password, load_ctx, verify_password
from ..services import audit
from ..web import flash, redirect, render

router = APIRouter()


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(func.count(User.id))):
        return redirect("/login")
    return render(request, "setup.html")


@router.post("/setup")
def setup_submit(request: Request, username: str = Form(...), display_name: str = Form(""), password: str = Form(...),
                 password2: str = Form(...), db: Session = Depends(get_db)):
    if db.scalar(select(func.count(User.id))):
        return redirect("/login")
    if password != password2 or len(password) < 8:
        flash(request, "Passwörter stimmen nicht überein oder sind kürzer als 8 Zeichen.", "err")
        return redirect("/setup")
    admin = db.scalar(select(Group).where(Group.name == "Admin"))
    u = User(username=username.strip(), display_name=display_name.strip(), password_hash=hash_password(password), group_id=admin.id)
    db.add(u)
    db.flush()
    audit.log(db, u.id, "create", "user", u.id, f"Ersteinrichtung: Admin {u.username}")
    db.commit()
    request.session["uid"] = u.id
    request.session["sv"] = 0
    flash(request, "Willkommen! Lege als Nächstes Lagerorte an oder importiere deine Altdaten (Admin-Modus → Import).")
    return redirect("/")


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if not db.scalar(select(func.count(User.id))):
        return redirect("/setup")
    pin_users = []
    if get_setting(db, "pin_login_enabled", True) and request.cookies.get("lw_trusted") == "1":
        pin_users = list(db.scalars(select(User).where(User.active.is_(True), User.pin_hash.is_not(None)).order_by(User.display_name)))
    return render(request, "login.html", {"next": _safe_next(next), "pin_users": pin_users, "guest": get_setting(db, "guest_enabled", False)})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
                 remember: bool = Form(False), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))
    if not u or not u.active or not verify_password(u.password_hash, password):
        time.sleep(0.6)
        flash(request, "Benutzername oder Passwort falsch.", "err")
        return redirect(f"/login?next={_safe_next(next)}")
    return _do_login(request, db, u, next, remember)


@router.post("/login/pin")
def login_pin(request: Request, user_id: int = Form(...), pin: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if request.cookies.get("lw_trusted") != "1" or not u or not u.active or not verify_password(u.pin_hash, pin):
        time.sleep(0.6)
        flash(request, "PIN falsch.", "err")
        return redirect("/login")
    return _do_login(request, db, u, next, True)


def _do_login(request: Request, db: Session, u: User, next: str, remember: bool):
    request.session.clear()
    request.session["uid"] = u.id
    request.session["sv"] = u.session_version or 0
    if not remember:
        request.session["exp"] = time.time() + 12 * 3600  # ohne „angemeldet bleiben“: 12 Stunden
    u.last_login = now()
    audit.log(db, u.id, "login", "user", u.id, "Anmeldung")
    db.commit()
    return redirect(_safe_next(next))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.post("/admin-mode")
def admin_mode(request: Request, on: int = Form(1), db: Session = Depends(get_db), ctx=Depends(current)):
    if ctx.user is None:
        return redirect("/login")
    if on:
        request.session["admin_until"] = time.time() + ADMIN_MODE_SECONDS
        flash(request, "Admin-Modus eingeschaltet (schaltet sich nach 15 Minuten ohne Aktivität ab).", "warn")
    else:
        request.session.pop("admin_until", None)
    return redirect(request.headers.get("referer") or "/")


@router.post("/theme")
def set_theme(request: Request, theme: str = Form("auto"), db: Session = Depends(get_db)):
    ctx = load_ctx(request, db)
    if ctx.user and theme in ("auto", "light", "dark"):
        ctx.user.theme = theme
        db.commit()
    return redirect(request.headers.get("referer") or "/")


@router.post("/trust-device")
def trust_device(request: Request, trust: int = Form(1), ctx=Depends(current)):
    resp = redirect(request.headers.get("referer") or "/")
    if trust:
        resp.set_cookie("lw_trusted", "1", max_age=3 * 365 * 24 * 3600, samesite="lax")
        flash(request, "Dieses Gerät erlaubt jetzt die Schnellanmeldung per PIN.")
    else:
        resp.delete_cookie("lw_trusted")
    return resp
