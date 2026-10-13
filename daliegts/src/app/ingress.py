"""Betrieb als Home-Assistant-App (v0.22): Oberfläche in der HA-Seitenleiste über Ingress.

Home Assistant reicht Anfragen unter /api/hassio_ingress/<token>/… an die App weiter (immer von 172.30.32.2)
und schickt den Pfad im Header X-Ingress-Path sowie den angemeldeten HA-Benutzer (X-Remote-User-*).
* HTML-Seiten: Pfade „/…“ in Links, Formularen, Bildern bekommen den Ingress-Pfad; ingress.js erledigt den Rest im Browser.
* Weiterleitungen (Location) ebenso.
* Anmeldung: der HA-Benutzer wird automatisch angemeldet (erster = Admin, weitere = Familie).
Nur aktiv, wenn LAGERWERK_HA_INGRESS=1 gesetzt ist (macht das Start-Skript der App).
"""
from __future__ import annotations

import json
import os
import re

# Supervisor; zum Testen über LAGERWERK_HA_TRUSTED erweiterbar (kommagetrennt)
TRUSTED = {"172.30.32.2", *filter(None, os.environ.get("LAGERWERK_HA_TRUSTED", "").split(","))}
_PREFIX_RE = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_\-]{1,128}")
_ATTR_RE = re.compile(rb'(\s(?:href|src|action|formaction|poster|hx-get|hx-post|hx-put|hx-delete|hx-patch)=["\'])/(?!/)')


def rewrite_html(body: bytes, prefix: str, version: str = "") -> bytes:
    p = prefix.encode()
    body = _ATTR_RE.sub(lambda m: m.group(1) + p + b"/", body)
    inject = (f'<script>window.LW_PREFIX={json.dumps(prefix)};</script>'  # prefix ist geprüft (nur a-z0-9_-/)
              f'<script src="{prefix}/static/js/ingress.js?v={version}"></script>').encode()
    return body.replace(b"<head>", b"<head>" + inject, 1) if b"<head>" in body else body


def fix_location(value: bytes, prefix: str) -> bytes:
    return prefix.encode() + value if value.startswith(b"/") and not value.startswith(b"//") and not value.startswith(prefix.encode() + b"/") else value


class IngressMiddleware:
    def __init__(self, app, version: str = ""):
        self.app, self.version = app, version

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        prefix = headers.get(b"x-ingress-path", b"").decode("latin-1").rstrip("/")
        client = (scope.get("client") or ("",))[0]
        if not prefix or client not in TRUSTED or not _PREFIX_RE.fullmatch(prefix):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        state["ingress_prefix"] = prefix
        state["ingress_user"] = {
            "id": headers.get(b"x-remote-user-id", b"").decode("utf-8", "replace"),
            "name": headers.get(b"x-remote-user-name", b"").decode("utf-8", "replace"),
            "display": headers.get(b"x-remote-user-display-name", b"").decode("utf-8", "replace"),
        }
        start: dict = {}
        chunks: list[bytes] = []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                hdrs = [(k, fix_location(v, prefix) if k.lower() == b"location" else v) for k, v in msg.get("headers", [])]
                ctype = next((v for k, v in hdrs if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html"):
                    start.update(msg, headers=hdrs)
                    return
                return await send({**msg, "headers": hdrs})
            if msg["type"] == "http.response.body" and start:
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                data = rewrite_html(b"".join(chunks), prefix, self.version)
                hdrs = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"] + [(b"content-length", str(len(data)).encode())]
                await send({**start, "headers": hdrs})
                await send({"type": "http.response.body", "body": data})
                return
            await send(msg)

        await self.app(scope, receive, _send)


def ingress_user(request, db):
    """Angemeldeten HA-Benutzer als DaLiegt’s-Benutzer zurückgeben (bei Bedarf anlegen). None, wenn kein Ingress."""
    info = getattr(request.state, "ingress_user", None)
    if not info or not (info.get("name") or info.get("id")):
        return None
    from sqlalchemy import func, select

    from .models import Group, User
    from .services import audit

    username = (info.get("name") or f"ha-{info['id'][:12]}")[:64]
    u = db.scalar(select(User).where(User.username == username))
    if u is None:
        first = not db.scalar(select(func.count(User.id)))
        g = db.scalar(select(Group).where(Group.name == ("Admin" if first else "Familie")))
        u = User(username=username, display_name=(info.get("display") or username)[:120], password_hash="", group_id=g.id if g else None)
        db.add(u)
        db.flush()
        audit.log(db, u.id, "create", "user", u.id, f"Home-Assistant-Benutzer {username} automatisch angelegt" + (" (Admin)" if first else ""))
        db.commit()
    return u if u.active else None
