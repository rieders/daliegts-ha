"""Template-Umgebung und Hilfsfunktionen für die Weboberfläche."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .config import APP_NAME, APP_VERSION, BASE_DIR, settings
from .models import COARSE_LEVELS, ITEM_KINDS, LOT_STATUS, MOVEMENT_KINDS, ORDER_STATUS, STOCK_MODES
from .services.items import image_url, safe_url
from .services.locations import LOCATION_TYPES
from .services.params import format_si
from .services.permissions import AREAS, LEVELS

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
env = templates.env


def fmt_qty(v, unit: str = "") -> str:
    if v is None:
        return "–"
    s = f"{v:g}".replace(".", ",") if isinstance(v, float) else str(v)
    return f"{s} {unit}".strip()


def fmt_dt(v) -> str:
    if not v:
        return ""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return str(v)


def fmt_euro(v) -> str:
    """12.5 -> „12,50 €“."""
    if v is None:
        return "–"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".") + " €"


def fmt_price(v) -> str:
    """Einzelpreise: mind. 2, höchstens 4 Nachkommastellen (0,1 -> „0,10 €“, 0,0123 -> „0,0123 €“)."""
    if v is None:
        return "–"
    s = f"{v:.4f}".rstrip("0")
    if len(s.split(".")[1]) < 2:
        s = f"{v:.2f}"
    return s.replace(".", ",") + " €"


def nl2br(v: str) -> Markup:
    return Markup("<br>".join(escape(v or "").split("\n")))


env.filters.update(safe_url=safe_url, ts=lambda v: datetime.fromtimestamp(v).strftime("%d.%m.%Y %H:%M"), qty=fmt_qty, dt=fmt_dt, img=image_url, si=format_si, nl2br=nl2br, euro=fmt_euro, price=fmt_price)
env.globals.update(
    APP_NAME=APP_NAME, APP_VERSION=APP_VERSION, ITEM_KINDS=ITEM_KINDS, STOCK_MODES=STOCK_MODES, COARSE_LEVELS=COARSE_LEVELS,
    LOT_STATUS=LOT_STATUS, MOVEMENT_KINDS=MOVEMENT_KINDS, ORDER_STATUS=ORDER_STATUS, LOCATION_TYPES=LOCATION_TYPES, AREAS=AREAS, LEVELS=LEVELS,
    BASE_URL=settings.base_url,
)


def has_wled() -> bool:
    """True, wenn mindestens ein aktiver WLED-Controller angelegt ist (für 💡-Buttons)."""
    from sqlalchemy import func, select

    from . import db as dbmod
    from .models import WledController

    with dbmod.SessionLocal() as db:
        return bool(db.scalar(select(func.count(WledController.id)).where(WledController.enabled.is_(True))))


env.globals["has_wled"] = has_wled


def printers3d_nav() -> list:
    """3D-Drucker für Menü und Startseite (Name, Zustand, Hinweis)."""
    from sqlalchemy import select

    from . import db as dbmod
    from .models import Printer3D

    with dbmod.SessionLocal() as db:
        return [{"id": p.id, "name": p.name, "state": p.last_state, "warning": p.warning, "pending": len(p.pending or []),
                 "job": (p.job or {}).get("file", "").rsplit("/", 1)[-1]} for p in db.scalars(select(Printer3D).where(Printer3D.enabled.is_(True)))]


env.globals["printers3d_nav"] = printers3d_nav


def license_info():
    """Aktuelle Lizenz (für Menü-Hinweise „Pro“ und die Lizenzseite)."""
    from . import db as dbmod
    from .services import license as lic

    with dbmod.SessionLocal() as db:
        return lic.current(db)


env.globals["license_info"] = license_info


def open_shopping_count() -> int:
    """Offene Positionen der Einkaufsliste (Badge in der Navigation)."""
    from sqlalchemy import func, select

    from . import db as dbmod
    from .models import ShoppingItem

    with dbmod.SessionLocal() as db:
        return db.scalar(select(func.count(ShoppingItem.id)).where(ShoppingItem.status == "open")) or 0


env.globals["open_shopping_count"] = open_shopping_count


class _BuyMap:
    """Bestellstatus aller Artikel – wird pro Seitenaufruf höchstens einmal geladen."""

    def __init__(self) -> None:
        self._d = None

    def get(self, item_id):
        if self._d is None:
            from . import db as dbmod
            from .services.purchasing import buy_states

            with dbmod.SessionLocal() as db:
                self._d = buy_states(db)
        return self._d.get(item_id)


_buy_ctx: ContextVar[_BuyMap | None] = ContextVar("lagerwerk_buy", default=None)


def buy_state(item_id):
    """Im Template: buy_state(item.id) -> BuyState (offen/bestellt) oder None."""
    m = _buy_ctx.get()
    if m is None:
        m = _BuyMap()
        _buy_ctx.set(m)
    return m.get(item_id)


env.globals["buy_state"] = buy_state


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
    _buy_ctx.set(_BuyMap())  # Bestellstatus frisch für diese Seite
    c = dict(ctx or {})
    c["request"] = request
    c["me"] = getattr(request.state, "ctx", None)
    c["flashes"] = request.session.pop("flash", []) if "session" in request.scope else []
    c["is_htmx"] = request.headers.get("hx-request") == "true"
    return templates.TemplateResponse(request, name, c, status_code=status)


def redirect(url: str, status: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status)


def back(request: Request, fallback: str = "/") -> RedirectResponse:
    ref = request.headers.get("referer") or fallback
    return redirect(ref)
