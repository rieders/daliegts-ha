"""Anmeldung, Sitzungen, Rechteprüfung und Admin-Modus."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiToken, Group, Setting, User
from .services.permissions import AREAS, LEVELS, effective_levels, needs_admin_mode

ph = PasswordHasher()
ADMIN_MODE_SECONDS = 15 * 60


def hash_password(pw: str) -> str:
    return ph.hash(pw)


def verify_password(hash_: str | None, pw: str) -> bool:
    if not hash_:
        return False
    try:
        return ph.verify(hash_, pw)
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: BLE001
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_token() -> str:
    return "lw_" + secrets.token_urlsafe(32)


def get_setting(db: Session, key: str, default=None):
    s = db.get(Setting, key)
    return s.value if s is not None else default


def set_setting(db: Session, key: str, value) -> None:
    s = db.get(Setting, key)
    if s is None:
        db.add(Setting(key=key, value=value))
    else:
        s.value = value


@dataclass
class Ctx:
    """Aktueller Benutzer + Rechte für eine Anfrage."""
    user: User | None
    levels: dict[str, int]
    admin_mode: bool
    is_guest: bool
    via_token: bool = False

    @property
    def user_id(self) -> int | None:
        return self.user.id if self.user else None

    @property
    def name(self) -> str:
        return self.user.label if self.user else "Gast"

    def can(self, area: str, level: int = 1, ignore_admin_mode: bool = False) -> bool:
        if self.levels.get(area, 0) < level:
            return False
        if needs_admin_mode(area, level) and not self.admin_mode and not ignore_admin_mode:
            return False  # gilt auch für API-Tokens: Admin-Aktionen nur interaktiv im Admin-Modus
        return True

    def has_right(self, area: str, level: int = 1) -> bool:
        """Recht vorhanden (unabhängig vom Admin-Modus) – für Anzeigen "im Admin-Modus verfügbar"."""
        return self.levels.get(area, 0) >= level

    @property
    def any_admin_right(self) -> bool:
        return any(needs_admin_mode(a, lvl) for a, lvl in self.levels.items() if lvl > 0)


def _guest_group(db: Session) -> Group | None:
    if not get_setting(db, "guest_enabled", False):
        return None
    return db.scalar(select(Group).where(Group.name == "Gast"))


def load_ctx(request: Request, db: Session) -> Ctx:
    cached = getattr(request.state, "ctx", None)
    if cached is not None:
        return cached
    user = None
    via_token = False
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") or request.headers.get("x-api-token"):
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-token", "")
        t = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(tok)))
        if t and t.user.active:
            user, via_token = t.user, True
            t.last_used = __import__("datetime").datetime.now().replace(microsecond=0)
            db.commit()
    else:
        uid = request.session.get("uid")
        if getattr(request.state, "ingress_user", None):  # Home-Assistant-App: angemeldeten HA-Benutzer übernehmen
            from .ingress import ingress_user

            iu = ingress_user(request, db)
            if iu is not None and uid != iu.id:
                request.session.clear()
                request.session.update(uid=iu.id, sv=iu.session_version or 0)
                uid = iu.id
        if uid:
            user = db.get(User, uid)
            exp = request.session.get("exp")
            if user is not None and (not user.active or request.session.get("sv", 0) != (user.session_version or 0)
                                     or (exp and exp < time.time())):
                request.session.clear()  # abgemeldet (Konto deaktiviert, Passwort geändert, Sitzung abgelaufen)
                user = None
    guest = None if user else _guest_group(db)
    levels = effective_levels(user, guest)
    admin_until = request.session.get("admin_until", 0) if user else 0
    admin_mode = bool(user) and admin_until > time.time()
    if admin_mode:
        request.session["admin_until"] = time.time() + ADMIN_MODE_SECONDS  # Aktivität verlängert
    ctx = Ctx(user, levels, admin_mode, user is None, via_token)
    request.state.ctx = ctx
    return ctx


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, area: str, level: int, admin_mode_needed: bool):
        self.area, self.level, self.admin_mode_needed = area, level, admin_mode_needed


def current(request: Request, db: Session = Depends(get_db)) -> Ctx:
    ctx = load_ctx(request, db)
    if ctx.user is None and not any(ctx.levels.values()):
        raise LoginRequired()
    return ctx


def require(area: str, level: int = 1):
    def dep(request: Request, db: Session = Depends(get_db)) -> Ctx:
        ctx = load_ctx(request, db)
        if ctx.user is None and not any(ctx.levels.values()):
            raise LoginRequired()
        if not ctx.can(area, level):
            raise Forbidden(area, level, ctx.has_right(area, level))
        return ctx

    return dep


def check(ctx: Ctx, area: str, level: int = 1) -> None:
    if not ctx.can(area, level):
        raise Forbidden(area, level, ctx.has_right(area, level))


def forbidden_message(exc: Forbidden) -> str:
    area = AREAS.get(exc.area, (exc.area,))[0]
    if exc.admin_mode_needed:
        return f"Für „{area}: {LEVELS[exc.level]}“ bitte zuerst den Admin-Modus einschalten."
    return f"Keine Berechtigung für „{area}: {LEVELS[exc.level]}“."


def api_error(status: int, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail=msg)
