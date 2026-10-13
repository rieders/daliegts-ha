"""Datenblätter offline speichern (v0.16): verlinkte PDFs herunterladen und beim Artikel ablegen.

Die Links kommen von den Info-Providern (LCSC, Mouser, DigiKey, TME, Reichelt …) oder werden von Hand eingetragen.
Liefert ein Link statt PDF eine Webseite, wird dort nach einem PDF-Link gesucht (ein Schritt). Fehlschläge werden
30 Tage lang nicht erneut versucht.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Attachment, Item
from ..security import get_setting, set_setting
from .items import store_upload
from .providers import USER_AGENT

log = logging.getLogger("lagerwerk.datasheets")
MAX_BYTES = 30 * 1024 * 1024
RETRY_DAYS = 30
FAILED_KEY = "datasheets.failed"


def candidates(db: Session, item_ids: list[int] | None = None, force: bool = False) -> list[int]:
    q = select(Attachment.id).where(Attachment.owner_type == "item", Attachment.kind == "datasheet", Attachment.filename.is_(None),
                                    Attachment.url.is_not(None), Attachment.url != "")
    if item_ids is not None:
        q = q.where(Attachment.owner_id.in_(item_ids))
    ids = list(db.scalars(q.order_by(Attachment.id)))
    if force:
        return ids
    failed = get_setting(db, FAILED_KEY, {}) or {}
    cutoff = datetime.now() - timedelta(days=RETRY_DAYS)
    return [i for i in ids if not (str(i) in failed and datetime.fromisoformat(failed[str(i)]) > cutoff)]


async def _get(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise ValueError("Datei größer als 30 MB")
        return bytes(buf), str(r.url)


async def fetch_pdf(client: httpx.AsyncClient, url: str) -> bytes:
    data, final = await _get(client, url)
    if data[:5] == b"%PDF-":
        return data
    text = data[:400_000].decode("utf-8", "ignore")
    m = re.search(r"""(?:href|src|data)=["']([^"']+?\.pdf(?:\?[^"']*)?)["']""", text, re.I)
    if m:
        data2, _ = await _get(client, urljoin(final, m.group(1)))
        if data2[:5] == b"%PDF-":
            return data2
    raise ValueError("Link liefert kein PDF (Webseite, Anmeldung oder Captcha)")


def _filename(db: Session, att: Attachment) -> str:
    item = db.get(Item, att.owner_id)
    base = re.sub(r"[^\w.-]+", "_", (item.mpn or item.name) if item else "datenblatt")[:60] or "datenblatt"
    return f"{base}.pdf"


async def download_many(att_ids: list[int], progress: dict | None = None, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Lädt die Anhänge (eigene DB-Sitzungen, geeignet für Hintergrund-Aufträge)."""
    from .. import db as dbmod

    res = {"ok": 0, "failed": 0, "messages": []}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, transport=transport,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"}) as client:
        for aid in att_ids:
            with dbmod.session_scope() as db:
                att = db.get(Attachment, aid)
                url, name = (att.url, _filename(db, att)) if att else (None, "")
            if url:
                try:
                    pdf = await fetch_pdf(client, url)
                    path = await asyncio.to_thread(store_upload, pdf, name, "datasheets")
                    with dbmod.session_scope() as db:
                        att = db.get(Attachment, aid)
                        if att:
                            att.filename, att.missing = path, False
                            failed = dict(get_setting(db, FAILED_KEY, {}) or {})
                            failed.pop(str(aid), None)
                            set_setting(db, FAILED_KEY, failed)
                    res["ok"] += 1
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}"
                    res["failed"] += 1
                    res["messages"].append(f"{name}: {msg}")
                    with dbmod.session_scope() as db:
                        failed = dict(get_setting(db, FAILED_KEY, {}) or {})
                        failed[str(aid)] = datetime.now().replace(microsecond=0).isoformat()
                        set_setting(db, FAILED_KEY, failed)
            if progress is not None:
                progress["done"] += 1
                progress["linked"] = res["ok"]
                progress["messages"] = res["messages"][-20:]
            if transport is None:
                await asyncio.sleep(0.5)
    return res


def start_job(att_ids: list[int], label: str = "Datenblätter") -> str:
    from .infosync import JOBS

    job_id = uuid.uuid4().hex[:12]
    st = JOBS[job_id] = {"total": len(att_ids), "done": 0, "messages": [], "finished": False, "label": label, "linked": 0, "asked": 0}

    async def _run():
        try:
            await download_many(att_ids, st)
        finally:
            st["finished"] = True

    asyncio.get_running_loop().create_task(_run())
    return job_id


async def nightly(budget: int = 40) -> dict:
    from .. import db as dbmod

    with dbmod.session_scope() as db:
        if not (get_setting(db, "datasheets.offline", True)):
            return {"skipped": True}
        ids = candidates(db)[:budget]
    return await download_many(ids) if ids else {"ok": 0, "failed": 0}
