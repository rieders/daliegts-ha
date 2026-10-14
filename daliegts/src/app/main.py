"""FastAPI-Anwendung."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from . import db as dbmod
from .config import APP_VERSION, BASE_DIR, settings
from .security import Forbidden, LoginRequired, forbidden_message, load_ctx
from .services import schema
from .services.seed import seed
from .services.wled import lowstock_targets, manager

log = logging.getLogger("lagerwerk")


def startup_checks() -> None:
    status, cur, head = schema.check_schema(settings.db_path)
    if status == "empty":
        log.info("Neue Datenbank – lege Schema an.")
        schema.upgrade(settings.db_path)
    elif status == "newer":
        raise SystemExit(f"Datenbank (Version {cur}) ist neuer als das Programm ({head}). Bitte Programm aktualisieren.")
    elif status == "pending":
        raise SystemExit(f"Ausstehende Datenbank-Migrationen ({cur} → {head}). Bitte `./scripts/update.sh` bzw. "
                         f"`python -m app.cli migrate` ausführen (mit Backup und Probelauf).")
    with dbmod.session_scope() as db:
        seed(db)


async def _daily_backup_loop() -> None:
    from datetime import datetime, timedelta

    from .services.backup import daily_backup

    while True:
        now = datetime.now()
        nxt = (now + timedelta(days=1)).replace(hour=3, minute=17, second=0, microsecond=0)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            with dbmod.session_scope() as db:
                daily_backup(db)
            log.info("Tägliches Backup erstellt.")
        except Exception:  # noqa: BLE001
            log.exception("Tägliches Backup fehlgeschlagen")


async def _provider_loop() -> None:
    """Nachtlauf der Info-Provider (03:47): Preise aktualisieren, bei anderen Shops vergleichen."""
    from datetime import datetime, timedelta

    from .services import infosync
    from .services.providers import load_config

    while True:
        now = datetime.now()
        nxt = now.replace(hour=3, minute=47, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            with dbmod.session_scope() as db:
                _, opts = load_config(db)
            if opts.get("nightly"):
                summary = await infosync.nightly()
                log.info("Info-Provider-Nachtlauf: %s", summary)
        except Exception:  # noqa: BLE001
            log.exception("Info-Provider-Nachtlauf fehlgeschlagen")
        try:
            from .services import datasheets

            log.info("Datenblätter offline: %s", await datasheets.nightly())
        except Exception:  # noqa: BLE001
            log.exception("Datenblätter-Nachtlauf fehlgeschlagen")


async def _idle_loop() -> None:
    from .services.wled import controller_infos

    while True:
        await asyncio.sleep(60)
        try:
            db = dbmod.SessionLocal()
            try:
                infos = {k: v for k, v in controller_infos(db).items() if v.idle_mode != "off"}
            finally:
                db.close()
            if infos:
                await manager.idle_tick(infos)
        except Exception:  # noqa: BLE001
            log.exception("Ruhemodus-Prüfung fehlgeschlagen")
        try:  # WiFi-Steckdose nach Inaktivität ausschalten (nicht, solange noch etwas leuchtet)
            from .services.power import power

            busy = any(st.highlights for st in manager.states.values())
            if await power.idle_tick(busy=busy):
                for st in manager.states.values():
                    st.idle_active, st.saved_state = False, None
        except Exception:  # noqa: BLE001
            log.exception("Steckdosen-Prüfung fehlgeschlagen")


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_checks()

    def _lowstock(cid: int):
        db = dbmod.SessionLocal()
        try:
            return lowstock_targets(db, cid)
        finally:
            db.close()

    manager.lowstock_provider = _lowstock
    try:  # WiFi-Steckdose für die WLED-Controller
        from .services import power as power_svc

        _pdb = dbmod.SessionLocal()
        try:
            power_svc.power.configure(power_svc.load(_pdb))
        finally:
            _pdb.close()
    except Exception:  # noqa: BLE001
        log.exception("Steckdosen-Einstellung konnte nicht gelesen werden")
    try:  # „Ruhemodus pausiert“ überlebt Neustarts
        from .security import get_setting

        _db = dbmod.SessionLocal()
        try:
            manager.paused_idle = bool(get_setting(_db, "wled.idle_paused", False))
        finally:
            _db.close()
    except Exception:  # noqa: BLE001
        log.exception("Ruhemodus-Einstellung konnte nicht gelesen werden")
    from .services import printer3d

    tasks = ([asyncio.create_task(_daily_backup_loop()), asyncio.create_task(_idle_loop()), asyncio.create_task(_provider_loop()),
              asyncio.create_task(printer3d.supervise())]
             if app.state.background_jobs else [])
    yield
    for task in tasks:
        task.cancel()
    for mon in list(printer3d.MONITORS.values()):
        mon.stop()
    printer3d.MONITORS.clear()
    await manager.close()


def create_app(background_jobs: bool = True) -> FastAPI:
    app = FastAPI(title="DaLiegt’s", version=APP_VERSION, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.background_jobs = background_jobs

    @app.middleware("http")
    async def wake_power(request: Request, call_next):
        """Optional: WiFi-Steckdose schon beim Öffnen von Suche/Orten einschalten, damit WLED bereit ist, wenn das Fach leuchten soll."""
        from .services.power import WAKE_PREFIXES, power

        if (power.enabled and power.cfg.get("wake_on_pages") and request.method == "GET" and request.url.path.startswith(WAKE_PREFIXES)
                and "text/html" in request.headers.get("accept", "") and request.cookies.get("daliegts_ha" if settings.ha_ingress else "lagerwerk")):
            if power.is_on:
                power.touch()
            elif power.is_on is not True:
                asyncio.create_task(power.ensure_on())
        return await call_next(request)

    @app.middleware("http")
    async def origin_check(request: Request, call_next):
        """Einfacher CSRF-Schutz für Formulare: Origin/Referer muss zum Host passen."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            has_token = request.headers.get("authorization", "").lower().startswith("bearer ") or request.headers.get("x-api-token")
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and not has_token and not getattr(request.state, "ingress_prefix", None):  # Ingress: HA prüft selbst
                host = urlparse(origin).netloc if origin != "null" else "null"
                if host != request.headers.get("host") and host != urlparse(settings.base_url).netloc:
                    return PlainTextResponse("Ungültige Herkunft der Anfrage.", status_code=403)
            if request.url.path.startswith("/api/") and not has_token and request.headers.get("x-lagerwerk") != "1":
                return JSONResponse({"detail": "Header X-Lagerwerk: 1 fehlt (Schutz vor fremden Seiten)."}, status_code=403)
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="daliegts_ha" if settings.ha_ingress else "lagerwerk",
                       max_age=90 * 24 * 3600, same_site="lax", https_only=settings.base_url.startswith("https") and not settings.ha_ingress)
    if settings.ha_ingress:
        from .ingress import IngressMiddleware

        app.add_middleware(IngressMiddleware, version=APP_VERSION)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    from .services.license import FEATURES, ProRequired

    @app.exception_handler(ProRequired)
    async def _pro_required(request: Request, exc: ProRequired):
        msg = f"„{FEATURES.get(exc.feature, exc.feature)}“ gehört zu DaLiegt’s Pro."
        if request.url.path.startswith("/api/") or request.headers.get("hx-request") or request.headers.get("x-lagerwerk"):
            return JSONResponse({"detail": msg, "pro_required": exc.feature}, status_code=402)
        from .web import render

        return render(request, "pro_required.html", {"message": msg, "feature": exc.feature, "features": FEATURES}, status=402)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        msg = forbidden_message(exc)
        if request.url.path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"detail": msg}, status_code=403)
        from .web import render

        return render(request, "error.html", {"message": msg, "admin_hint": exc.admin_mode_needed}, status=403)

    from sqlalchemy.exc import IntegrityError

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        msg = "Ungültige Eingabe: Verweis existiert nicht oder Eintrag ist doppelt."
        if request.url.path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"detail": msg}, status_code=400)
        from .web import render

        return render(request, "error.html", {"message": msg, "title": "Fehler"}, status=400)

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError):
        msg = f"Ungültige Eingabe: {exc}"
        if request.url.path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"detail": msg}, status_code=400)
        from .web import render

        return render(request, "error.html", {"message": msg, "title": "Fehler"}, status=400)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/media/{path:path}", include_in_schema=False)
    async def media(path: str, request: Request):
        db = dbmod.SessionLocal()
        try:
            ctx = load_ctx(request, db)
            if ctx.user is None and not any(ctx.levels.values()):
                return PlainTextResponse("Anmeldung erforderlich", status_code=401)
        finally:
            db.close()
        target = (settings.media_dir / path).resolve()
        if not target.is_relative_to(settings.media_dir.resolve()) or not target.is_file():
            return PlainTextResponse("Nicht gefunden", status_code=404)
        headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"}
        suffix = target.suffix.lower()
        if suffix in (".webp", ".png", ".jpg", ".jpeg", ".gif", ".bmp"):
            return FileResponse(target, headers=headers)
        if suffix == ".pdf":
            return FileResponse(target, media_type="application/pdf", headers=headers)
        if suffix in (".html", ".htm", ".svg", ".xml", ".js", ".xhtml"):
            return FileResponse(target, media_type="application/octet-stream", filename=target.name,
                                headers=headers | {"Content-Disposition": f'attachment; filename="{target.name}"'})
        return FileResponse(target, filename=target.name, headers=headers)

    @app.get("/sw.js", include_in_schema=False)
    async def sw():
        # Versionsnummer im Cache-Namen: nach einem Update lädt das Handy CSS/JS neu (alter Cache wird gelöscht)
        js = (BASE_DIR / "static" / "js" / "sw.js").read_text(encoding="utf-8").replace("lagerwerk-shell-v1", f"lagerwerk-shell-{APP_VERSION}")
        return Response(js, media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        status, cur, head = schema.check_schema(settings.db_path)
        ok = status == "ok"
        counts = schema.table_counts(settings.db_path) if ok else {}
        try:
            with dbmod.session_scope() as db:
                db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            ok = False
        return JSONResponse({"ok": ok, "schema": cur, "head": head, "status": status, "version": APP_VERSION, "counts": counts},
                            status_code=200 if ok else 503)

    from .routers import admin, api, auth, items, labels, locations, orders, pages, printers3d, projects, providers, purchasing, recognize, scan, spotcheck, stats, walls, wled

    for r in (auth.router, pages.router, stats.router, spotcheck.router, orders.router, projects.router, recognize.router, items.router, purchasing.router, providers.router, locations.router, walls.router, scan.router, labels.router, printers3d.router,
              wled.router, admin.router, api.router):
        app.include_router(r)
    return app


app = create_app()
