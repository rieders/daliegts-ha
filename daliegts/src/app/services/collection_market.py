"""Marktwerte der Sammlung abrufen: einzeln, für alle (Hintergrund) und im Nachtlauf (ältester Stand zuerst)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BookRecord, MediaRecord
from .collection import apply_market, book_json
from .providers import ProviderError
from .providers import sammlung as cfg
from .providers.books import book_market_provider
from .providers.music import Discogs

log = logging.getLogger("lagerwerk")


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True)


async def refresh_record(db: Session, rec: MediaRecord, conf: dict | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """Marktwert eines Tonträgers neu holen. Fehler werden am Tonträger vermerkt (kein Abbruch).

    Rückgabe: {"ok": bool, "message": str, "notes": [...]}.
    """
    conf = conf if conf is not None else cfg.load(db)
    if not conf.get("discogs", True):
        return {"ok": False, "message": "Discogs ist in den Einstellungen ausgeschaltet.", "notes": []}
    if not rec.discogs_id:
        rec.market_error = "Keine Discogs-ID – Marktwert kommt von Discogs."
        return {"ok": False, "message": rec.market_error, "notes": []}
    try:
        m = await Discogs(conf, client).market(rec.discogs_id, rec.media_condition)
    except ProviderError as e:
        rec.market_error = str(e)[:300]
        rec.market_checked_at = datetime.now().replace(microsecond=0)
        return {"ok": False, "message": str(e), "notes": []}
    apply_market(rec, m["lowest"], m["suggested"], m["currency"], m["for_sale"], " ".join(m["notes"]))
    return {"ok": True, "message": "Marktwert aktualisiert.", "notes": m["notes"]}


async def refresh_book(db: Session, book: BookRecord, conf: dict | None = None, client: httpx.AsyncClient | None = None) -> dict:
    conf = conf if conf is not None else cfg.load(db)
    prov = book_market_provider(conf, client)
    if prov is None:
        return {"ok": False, "message": "Kein Marktwert-Provider für Bücher eingerichtet – Wert bitte von Hand eintragen."}
    try:
        q = await prov.quote(book_json(db, book))
    except ProviderError as e:
        return {"ok": False, "message": str(e)}
    if not q:
        return {"ok": False, "message": f"{prov.name}: kein Preis gefunden."}
    book.market_value, book.market_currency = round(float(q[0]), 2), (q[1] or "EUR")[:3]
    book.market_checked_at = datetime.now().replace(microsecond=0)
    book.item.collector_value = book.market_value
    return {"ok": True, "message": "Marktwert aktualisiert."}


def due_records(db: Session, limit: int) -> list[int]:
    """Tonträger mit Discogs-ID, ältester Marktwert-Stand zuerst (nie geprüfte ganz vorn)."""
    q = (select(MediaRecord.id).where(MediaRecord.discogs_id.is_not(None), MediaRecord.discogs_id != "")
         .order_by(MediaRecord.market_checked_at.is_not(None), MediaRecord.market_checked_at, MediaRecord.id).limit(limit))
    return list(db.scalars(q))


async def refresh_many(record_ids: list[int], status: dict | None = None, delay: float = 1.0) -> dict:
    """Mehrere Tonträger nacheinander (eigene DB-Sitzung je Eintrag, damit Teilfortschritt gespeichert bleibt)."""
    from .. import db as dbmod

    st = status if status is not None else {}
    st.update({"total": len(record_ids), "done": 0, "ok": 0, "errors": 0, "messages": [], "finished": False})
    with dbmod.session_scope() as db:
        conf = cfg.load(db)
    async with new_client() as client:
        for rid in record_ids:
            with dbmod.session_scope() as db:
                rec = db.get(MediaRecord, rid)
                if rec is not None:
                    res = await refresh_record(db, rec, conf, client)
                    st["ok" if res["ok"] else "errors"] += 1
                    if not res["ok"]:
                        st["messages"] = (st["messages"] + [f"{rec.item.name}: {res['message']}"])[-20:]
                        if "Anfragelimit" in res["message"]:
                            await asyncio.sleep(60)
            st["done"] += 1
            if delay:
                await asyncio.sleep(delay)
    st["finished"] = True
    return st


JOBS: dict[str, dict] = {}


def start_job(record_ids: list[int]) -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"total": len(record_ids), "done": 0, "finished": False, "messages": []}
    for old in list(JOBS)[:-10]:
        JOBS.pop(old, None)

    async def _run():
        try:
            await refresh_many(record_ids, JOBS[job_id])
        except Exception:  # noqa: BLE001
            log.exception("Marktwert-Aktualisierung fehlgeschlagen")
            JOBS[job_id]["finished"] = True

    asyncio.get_running_loop().create_task(_run())
    return job_id


async def nightly() -> dict | None:
    """Nachtlauf (im Info-Provider-Zeitplan): begrenzte Anzahl, ältester Stand zuerst."""
    from .. import db as dbmod
    from ..security import set_setting

    with dbmod.session_scope() as db:
        conf = cfg.load(db)
        if not conf.get("nightly_market") or not conf.get("discogs", True):
            return None
        ids = due_records(db, max(1, min(int(conf.get("market_budget") or 25), 500)))
    st = await refresh_many(ids)
    books = 0
    with dbmod.session_scope() as db:
        if book_market_provider(conf) is not None:
            async with new_client() as client:
                q = select(BookRecord).order_by(BookRecord.market_checked_at.is_not(None), BookRecord.market_checked_at).limit(
                    int(conf.get("market_budget") or 25))
                for b in db.scalars(q):
                    if (await refresh_book(db, b, conf, client))["ok"]:
                        books += 1
        set_setting(db, "collection.last_run", {"started": datetime.now().isoformat(timespec="seconds"), "records": st["done"],
                                                "ok": st["ok"], "errors": st["errors"], "books": books})
    return {"records": st["done"], "ok": st["ok"], "errors": st["errors"], "books": books}
