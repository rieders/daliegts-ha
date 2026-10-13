"""Online-Abgleich: Bestellnummern selbst eintragen, Preise aktualisieren, bei anderen Shops vergleichen (v0.3).

Regeln
- Automatisch eingetragen wird nur, was eindeutig ist: gleiche Hersteller-Nr. (MPN) und – falls bekannt – derselbe Hersteller.
- Mehrdeutige oder unsichere Treffer landen als „Entscheidung“ (ProviderCandidate) beim Benutzer.
- Reichen die Angaben nicht (keine MPN, zu allgemeiner Name), fragt Lagerwerk nach statt zu raten.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Attachment, Item, ProviderCandidate, ShoppingItem, Supplier, SupplierPart
from . import purchasing as pur
from .providers import USER_AGENT, Hit, Provider, ProviderError, active_providers, load_config, normalize_mpn, provider_for_supplier_name, registry

log = logging.getLogger("lagerwerk.infosync")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})


# ------------------------------------------------------------------ Lieferant <-> Provider
def supplier_for(db: Session, provider: Provider | type[Provider]) -> Supplier:
    for s in db.scalars(select(Supplier)):
        if provider_for_supplier_name(s.name) == provider.key:
            return s
    preset = next((p for p in pur.SUPPLIER_PRESETS if p["name"].lower() == provider.supplier_name.lower()), {})
    s = Supplier(name=provider.supplier_name, website=preset.get("website", ""), search_url=preset.get("search_url", ""))
    db.add(s)
    db.flush()
    return s


def provider_key_of(sp: SupplierPart) -> str | None:
    return provider_for_supplier_name(sp.supplier.name) if sp.supplier else None


# ------------------------------------------------------------------ Suche
VAGUE_WORDS = {"widerstand", "kondensator", "elko", "diode", "led", "transistor", "ic", "schraube", "mutter", "kabel", "stecker",
               "buchse", "schalter", "relais", "sicherung", "spule", "quarz", "trafo", "platine", "teil", "bauteil"}


def query_problems(query: str) -> list[str]:
    """Gründe, warum eine Suche zu ungenau wäre – dann fragt Lagerwerk nach."""
    q = (query or "").strip()
    words = re.findall(r"[\wµΩ.,-]+", q.lower())
    if len(q) < 3:
        return ["Suchbegriff zu kurz."]
    if not re.search(r"\d", q) and (len(words) < 2 or all(w in VAGUE_WORDS for w in words)):
        return ["Zu allgemein – bitte Hersteller-Nr. (z. B. NE555P) oder Wert und Gehäuse (z. B. 10k 0805) angeben."]
    return []


def default_query(item: Item) -> str:
    if item.mpn:
        return item.mpn
    parts = [item.name]
    if item.footprint and item.footprint.name.lower() not in item.name.lower():
        parts.append(item.footprint.name)
    return " ".join(parts)


async def search_all(providers: list[Provider], query: str, limit: int = 8) -> dict[str, list[Hit] | str]:
    async def one(p: Provider):
        try:
            return p.key, await p.search(query, limit)
        except ProviderError as e:
            return p.key, str(e)
        except httpx.HTTPError as e:
            return p.key, f"{p.name}: nicht erreichbar ({type(e).__name__})."
        except Exception as e:  # noqa: BLE001 – Parser-Fehler etc. nicht bis zum Benutzer durchreichen
            log.exception("Provider %s", p.key)
            return p.key, f"{p.name}: Antwort nicht verstanden ({type(e).__name__}) – evtl. hat der Shop seine Seite geändert."

    return dict(await asyncio.gather(*(one(p) for p in providers)))


def exact_matches(item: Item, hits: list[Hit]) -> tuple[list[Hit], bool]:
    """(Treffer mit gleicher MPN, Hersteller unsicher?)."""
    want = normalize_mpn(item.mpn)
    if not want:
        return [], False
    exact = [h for h in hits if normalize_mpn(h.mpn) == want or (not h.mpn and normalize_mpn(h.sku) == want)]
    unsure = False
    if exact and item.manufacturer and item.manufacturer.name:
        mine = {w for w in re.findall(r"[a-z0-9]{3,}", item.manufacturer.name.lower())}
        unsure = not any(mine & set(re.findall(r"[a-z0-9]{3,}", (h.manufacturer or "").lower())) or not h.manufacturer for h in exact)
    return exact, unsure


# ------------------------------------------------------------------ Übernehmen
def _convert(hit: Hit, rate: float) -> tuple[list[tuple[float, float]], str]:
    if hit.currency in ("EUR", "") or not hit.prices:
        return list(hit.prices), ""
    if hit.currency == "USD" and rate:
        return [(q, round(p * rate, 5)) for q, p in hit.prices], f"umgerechnet aus USD (Kurs {rate:g})"
    return [], f"Preis in {hit.currency} – nicht übernommen"


def apply_hit(db: Session, item: Item, hit: Hit, origin: str = "manuell", fill: set[str] | None = None, rate: float = 0.9) -> SupplierPart:
    """Legt die Bezugsquelle an bzw. aktualisiert sie und füllt auf Wunsch leere Artikelfelder."""
    cls = registry().get(hit.provider)
    supplier = supplier_for(db, cls) if cls else None
    if supplier is None:
        raise ValueError("Unbekannter Provider.")
    sp = next((x for x in db.scalars(select(SupplierPart).where(SupplierPart.item_id == item.id, SupplierPart.supplier_id == supplier.id))
               if normalize_mpn(x.sku) == normalize_mpn(hit.sku)), None)
    if sp is None:
        sp = SupplierPart(item_id=item.id, supplier_id=supplier.id, sku=hit.sku[:120], origin=origin)
        db.add(sp)
        db.flush()
        if hit.pack_qty and hit.pack_qty > 0:
            sp.pack_qty = hit.pack_qty
    update_part(db, sp, hit, rate)
    fill = fill or set()
    if "mpn" in fill and not item.mpn and hit.mpn:
        item.mpn = hit.mpn[:120]
    if "manufacturer" in fill and not item.manufacturer_id and hit.manufacturer:
        from .items import ensure_manufacturer

        item.manufacturer_id = ensure_manufacturer(db, hit.manufacturer).id
    if "description" in fill and not item.description and hit.description:
        item.description = hit.description[:500]
    if "datasheet" in fill and hit.datasheet.startswith(("http://", "https://")):
        exists = db.scalar(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == item.id, Attachment.url == hit.datasheet))
        if not exists:
            db.add(Attachment(owner_type="item", owner_id=item.id, kind="datasheet", title=f"Datenblatt ({supplier.name})", url=hit.datasheet[:1000]))
    if "image" in fill and not item.image and hit.image.startswith(("http://", "https://")):
        from ..routers.items import _download_image

        item.image = _download_image(hit.image) or item.image
    db.flush()
    return sp


def update_part(db: Session, sp: SupplierPart, hit: Hit, rate: float = 0.9) -> None:
    prices, note = _convert(hit, rate)
    if prices:
        pur.set_prices(db, sp, prices, source="online")
    if hit.url and (not sp.url or sp.origin == "automatisch"):
        sp.url = hit.url[:500]
    sp.stock_available = hit.stock
    sp.checked_at = _now()
    sp.check_error = note


# ------------------------------------------------------------------ Entscheidungen (nachfragen)
def add_candidate(db: Session, item: Item, provider: str, reason: str, hits: list[Hit]) -> ProviderCandidate | None:
    open_ = db.scalar(select(ProviderCandidate).where(ProviderCandidate.item_id == item.id, ProviderCandidate.provider == provider,
                                                      ProviderCandidate.status == "open"))
    if open_ is not None:
        open_.data, open_.reason = [h.to_dict() for h in hits[:6]], reason
        return open_
    dismissed = db.scalar(select(ProviderCandidate).where(ProviderCandidate.item_id == item.id, ProviderCandidate.provider == provider,
                                                          ProviderCandidate.status == "dismissed", ProviderCandidate.reason == reason))
    if dismissed is not None:
        return None  # „keiner passt“ wurde schon beantwortet – nicht erneut fragen
    c = ProviderCandidate(item_id=item.id, provider=provider, reason=reason, data=[h.to_dict() for h in hits[:6]])
    db.add(c)
    db.flush()
    return c


def open_candidate_count(db: Session) -> int:
    from sqlalchemy import func

    return db.scalar(select(func.count(ProviderCandidate.id)).where(ProviderCandidate.status == "open")) or 0


# ------------------------------------------------------------------ Aktualisieren & Vergleichen
async def refresh_item(db: Session, item: Item, providers: dict[str, Provider], rate: float = 0.9) -> list[str]:
    """Preise/Lagerbestand aller verknüpften Bezugsquellen mit Online-Anbindung neu holen."""
    msgs = []
    for sp in list(db.scalars(select(SupplierPart).where(SupplierPart.item_id == item.id))):
        key = provider_key_of(sp)
        p = providers.get(key or "")
        if p is None or not (sp.sku or sp.url):
            continue
        try:
            hit = await p.get(sp.sku, sp.url)
        except ProviderError as e:
            sp.check_error, sp.checked_at = str(e)[:300], _now()
            msgs.append(str(e))
            continue
        except httpx.HTTPError as e:
            sp.check_error, sp.checked_at = f"nicht erreichbar ({type(e).__name__})", _now()
            msgs.append(f"{p.name}: nicht erreichbar")
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("refresh %s", key)
            sp.check_error, sp.checked_at = f"Antwort nicht verstanden ({type(e).__name__})", _now()
            continue
        if hit is None:
            sp.check_error, sp.checked_at = "Bestellnummer beim Shop nicht (mehr) gefunden", _now()
            msgs.append(f"{p.name}: {sp.sku} nicht gefunden")
            continue
        update_part(db, sp, hit, rate)
        if not item.mpn and hit.mpn:  # Hersteller-Nr. nachtragen, wenn sie eindeutig vom Shop kommt
            item.mpn = hit.mpn[:120]
        msgs.append(f"{p.name}: {sp.sku} aktualisiert")
    db.flush()
    return msgs


async def cross_check(db: Session, item: Item, providers: dict[str, Provider], rate: float = 0.9, ask_if_vague: bool = True) -> dict:
    """Bei Shops, die den Artikel noch nicht führen, nach der Hersteller-Nr. suchen: eindeutig -> eintragen, sonst nachfragen."""
    result = {"linked": [], "asked": [], "errors": []}
    have = {provider_key_of(sp) for sp in db.scalars(select(SupplierPart).where(SupplierPart.item_id == item.id))}
    todo = [p for k, p in providers.items() if k not in have]
    if not todo:
        item.online_checked_at = _now()
        return result
    if not item.mpn:
        if ask_if_vague and query_problems(default_query(item)):
            if add_candidate(db, item, "*", "zu_wenig", []):
                result["asked"].append("*")
            return result
        if not ask_if_vague:
            return result
    query = item.mpn or default_query(item)
    found = await search_all(todo, query, 8)
    need = pur.need_qty(item)
    for key, hits in found.items():
        if isinstance(hits, str):
            result["errors"].append(hits)
            continue
        if not hits:
            continue
        exact, unsure = exact_matches(item, hits)
        if exact and not unsure:
            best = min(exact, key=lambda h: (h.total_for(need) is None, h.total_for(need) or 0, h.pack_qty))
            apply_hit(db, item, best, origin="automatisch", rate=rate)
            result["linked"].append(key)
        elif item.mpn:
            if add_candidate(db, item, key, "unsicher" if exact else "mehrdeutig", exact or hits):
                result["asked"].append(key)
        else:
            if add_candidate(db, item, key, "mehrdeutig", hits):
                result["asked"].append(key)
    item.online_checked_at = _now()
    db.flush()
    return result


# ------------------------------------------------------------------ Hintergrund-Aufträge (Fortschritt im Browser)
JOBS: dict[str, dict] = {}


def job_status(job_id: str) -> dict | None:
    return JOBS.get(job_id)


def start_job(item_ids: list[int], cross: bool = True, label: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"total": len(item_ids), "done": 0, "messages": [], "finished": False, "label": label, "linked": 0, "asked": 0}
    for old in list(JOBS)[:-20]:
        JOBS.pop(old, None)
    asyncio.get_running_loop().create_task(_run_job(job_id, item_ids, cross))
    return job_id


async def _run_job(job_id: str, item_ids: list[int], cross: bool) -> None:
    from .. import db as dbmod

    st = JOBS[job_id]
    try:
        async with new_client() as client:
            with dbmod.session_scope() as db:
                _, opts = load_config(db)
                provs = {p.key: p for p in active_providers(db, client)}
            if not provs:
                st["messages"].append("Kein Info-Provider aktiv – unter Verwaltung › Info-Provider einschalten.")
                return
            delay = max((p.delay for p in provs.values() if p.kind == "webshop"), default=0.5)
            for iid in item_ids:
                with dbmod.session_scope() as db:
                    item = db.get(Item, iid)
                    if item is not None:
                        msgs = await refresh_item(db, item, provs, opts["usd_eur"])
                        st["messages"].extend(f"{item.name}: {m}" for m in msgs if "aktualisiert" not in m)
                        if cross:
                            r = await cross_check(db, item, provs, opts["usd_eur"])
                            st["linked"] += len(r["linked"])
                            st["asked"] += len(r["asked"])
                            st["messages"].extend(f"{item.name}: {e}" for e in r["errors"][:2])
                st["done"] += 1
                await asyncio.sleep(delay if st["done"] < st["total"] else 0)
    except Exception as e:  # noqa: BLE001
        log.exception("Info-Provider-Auftrag")
        st["messages"].append(f"Abgebrochen: {e}")
    finally:
        st["finished"] = True
        st["messages"] = st["messages"][-50:]


def shopping_item_ids(db: Session) -> list[int]:
    return sorted({i for i in db.scalars(select(ShoppingItem.item_id).where(ShoppingItem.status == "open", ShoppingItem.item_id.is_not(None)))})


async def nightly(budget: int | None = None) -> dict:
    """Nachtlauf: zuerst Einkaufsliste und Artikel unter Mindestbestand, dann die am längsten nicht geprüften Bezugsquellen."""
    from .. import db as dbmod

    summary = {"started": _now().isoformat(), "items": 0, "linked": 0, "asked": 0, "errors": 0}
    with dbmod.session_scope() as db:
        conf, opts = load_config(db)
        budget = budget or int(opts.get("budget") or 150)
        ids = shopping_item_ids(db)
        ids += [i.id for i, _ in pur.suggestions(db, 200) if i.id not in ids]
        stale = db.execute(select(SupplierPart.item_id, SupplierPart.checked_at).order_by(SupplierPart.checked_at.is_not(None), SupplierPart.checked_at)).all()
        for iid, _ in stale:
            if iid not in ids:
                ids.append(iid)
        ids = ids[:budget]
        cross_days = int(opts.get("cross_check_days") or 30)
        cross_ok = bool(opts.get("cross_check"))
        cutoff = _now() - timedelta(days=cross_days)
        cross_ids = {i.id for i in db.scalars(select(Item).where(Item.id.in_(ids), Item.mpn != "",
                                                                  (Item.online_checked_at.is_(None)) | (Item.online_checked_at < cutoff)))}
    async with new_client() as client:
        with dbmod.session_scope() as db:
            provs = {p.key: p for p in active_providers(db, client)}
        if not provs:
            return summary
        delay = max((p.delay for p in provs.values()), default=1)
        for iid in ids:
            with dbmod.session_scope() as db:
                item = db.get(Item, iid)
                if item is None:
                    continue
                msgs = await refresh_item(db, item, provs, opts["usd_eur"])
                summary["errors"] += sum(1 for m in msgs if "aktualisiert" not in m)
                if cross_ok and iid in cross_ids:
                    r = await cross_check(db, item, provs, opts["usd_eur"], ask_if_vague=False)
                    summary["linked"] += len(r["linked"])
                    summary["asked"] += len(r["asked"])
                summary["items"] += 1
            await asyncio.sleep(delay)
    summary["finished"] = _now().isoformat()
    with dbmod.session_scope() as db:
        from ..security import set_setting

        set_setting(db, "providers.last_run", summary)
    return summary
