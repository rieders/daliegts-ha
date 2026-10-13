"""Artikel anlegen/ändern, Parameter, Kategorien-/Footprint-Pfade, Katalog-Übernahme."""
from __future__ import annotations

import json
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BASE_DIR, settings
from ..models import Category, Equivalent, Footprint, Item, ItemParameter, Manufacturer
from .codes import next_code
from .params import parse_range, parse_value
from .search import index_item

CATALOG_DIR = BASE_DIR / "catalog" / "data"
CATALOG_IMG_DIR = BASE_DIR / "static" / "catalog"


# ------------------------------------------------------------------ Pfade
def ensure_category_path(db: Session, path: str, sep: str = "/") -> Category | None:
    parent = None
    for name in [p.strip() for p in path.split(sep) if p.strip()]:
        q = select(Category).where(Category.name == name)
        q = q.where(Category.parent_id.is_(None)) if parent is None else q.where(Category.parent_id == parent.id)
        cat = db.scalar(q)
        if cat is None:
            cat = Category(name=name, parent_id=parent.id if parent else None)
            db.add(cat)
            db.flush()
        parent = cat
    return parent


def category_path(cat: Category | None, sep: str = " / ") -> str:
    parts, seen = [], set()
    while cat is not None and cat.id not in seen:
        seen.add(cat.id)
        parts.append(cat.name)
        cat = cat.parent
    return sep.join(reversed(parts))


def ensure_footprint(db: Session, name: str, image: str | None = None) -> Footprint | None:
    name = (name or "").strip()
    if not name:
        return None
    fp = db.scalar(select(Footprint).where(Footprint.name == name))
    if fp is None:
        fp = Footprint(name=name, image=image)
        db.add(fp)
        db.flush()
    elif image and not fp.image:
        fp.image = image
    return fp


def ensure_manufacturer(db: Session, name: str) -> Manufacturer | None:
    name = (name or "").strip()
    if not name:
        return None
    m = db.scalar(select(Manufacturer).where(Manufacturer.name == name))
    if m is None:
        m = Manufacturer(name=name)
        db.add(m)
        db.flush()
    return m


# ------------------------------------------------------------------ Parameter
def set_parameter(db: Session, item: Item, name: str, value: str, unit: str = "") -> ItemParameter:
    value = str(value).strip()
    lo, typ, hi = parse_range(f"{value}{unit}" if unit and not any(ch in value for ch in "–-") else value)
    num = parse_value(f"{value}{unit}") if unit else parse_value(value)
    if num is None and typ is not None:
        num = typ
    if num is None and lo is not None and hi is not None:
        num = (lo + hi) / 2
    p = next((p for p in item.parameters if p.name == name), None)
    if p is None:
        p = ItemParameter(name=name)
        item.parameters.append(p)
    p.value_text, p.unit, p.value_num, p.value_min, p.value_max = value, unit or "", num, lo, hi
    return p


def set_parameters_from_form(db: Session, item: Item, names: list[str], values: list[str], units: list[str]) -> None:
    keep = set()
    for n, v, u in zip(names, values, units, strict=False):
        n, v = (n or "").strip(), (v or "").strip()
        if n and v:
            set_parameter(db, item, n, v, (u or "").strip())
            keep.add(n)
    for p in list(item.parameters):
        if p.name not in keep:
            item.parameters.remove(p)


# ------------------------------------------------------------------ Artikel
def create_item(db: Session, name: str, user_id: int | None = None, **fields) -> Item:
    item = Item(name=name.strip(), code=next_code(db, "item"), created_by=user_id, **fields)
    db.add(item)
    db.flush()
    return item


def reindex(db: Session, item: Item) -> None:
    db.flush()
    db.refresh(item)
    index_item(db, item)


# ------------------------------------------------------------------ Katalog
@lru_cache(maxsize=1)
def load_catalog() -> tuple[list[dict], dict[str, dict]]:
    parts: list[dict] = []
    for f in sorted(CATALOG_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for p in data.get("parts", []):
            p = dict(p)
            p["group"] = data.get("group", f.stem)
            p["_search"] = " ".join([p.get("name", ""), p.get("description", ""), p.get("mpn", ""), p.get("footprint", ""),
                                     " ".join(p.get("tags", [])), " ".join(p.get("equivalents", [])), p.get("category", "")]).lower()
            parts.append(p)
    return parts, {p["key"]: p for p in parts}


def catalog_search(q: str, limit: int = 40) -> list[dict]:
    parts, _ = load_catalog()
    words = [w for w in q.lower().replace(",", ".").split() if w]
    if not words:
        return []
    scored = []
    for p in parts:
        if all(w in p["_search"] for w in words):
            name = p["name"].lower()
            score = 0 if name == q.lower() else 1 if name.startswith(words[0]) else 2
            scored.append((score, len(p["name"]), p))
    scored.sort(key=lambda t: (t[0], t[1], t[2]["name"]))
    return [s[2] for s in scored[:limit]]


def catalog_part(key: str) -> dict | None:
    return load_catalog()[1].get(key)


def _copy_catalog_image(image: str) -> str | None:
    if not image:
        return None
    src = CATALOG_IMG_DIR / image
    if not src.exists():
        return None
    # Katalogbilder bleiben im static-Ordner; Artikel verweist mit Präfix "catalog:" darauf
    return f"catalog:{image}"


def create_from_catalog(db: Session, key: str, user_id: int | None = None, **overrides) -> Item:
    p = catalog_part(key)
    if p is None:
        raise KeyError(key)
    name = overrides.pop("name", None) or p["name"]
    cat = ensure_category_path(db, p.get("category", "Sonstiges"))
    fp = ensure_footprint(db, p.get("footprint", ""), _copy_catalog_image(p.get("image", "")))
    man = ensure_manufacturer(db, p.get("manufacturer", ""))
    fields = dict(
        description=p.get("description", ""), kind="bauteil", category_id=cat.id if cat else None,
        footprint_id=fp.id if fp else None, manufacturer_id=man.id if man else None, mpn=p.get("mpn", ""),
        tags=", ".join(p.get("tags", [])), unit=p.get("unit", "Stk"), image=_copy_catalog_image(p.get("image", "")),
        catalog_key=key, origin="DDR" if "/DDR-" in p.get("category", "") else "",
    )
    fields.update({k: v for k, v in overrides.items() if v is not None})
    item = create_item(db, name, user_id, **fields)
    for prm in p.get("params", []):
        set_parameter(db, item, prm["name"], prm["value"], prm.get("unit", ""))
    for eq in p.get("equivalents", []):
        if not db.scalar(select(Equivalent).where(Equivalent.type_a == item.name, Equivalent.type_b == eq)):
            rel = "aehnlich" if "ähnlich" in eq.lower() else "funktionsgleich"
            db.add(Equivalent(type_a=item.name, type_b=eq, relation=rel))
    reindex(db, item)
    return item


def seed_equivalents_from_catalog(db: Session) -> int:
    n = 0
    parts, _ = load_catalog()
    existing = {(e.type_a, e.type_b) for e in db.scalars(select(Equivalent))}
    for p in parts:
        for eq in p.get("equivalents", []):
            if (p["name"], eq) not in existing:
                db.add(Equivalent(type_a=p["name"], type_b=eq, relation="aehnlich" if "ähnlich" in eq.lower() else "funktionsgleich"))
                existing.add((p["name"], eq))
                n += 1
    db.flush()
    return n


def image_url(image: str | None) -> str | None:
    if not image:
        return None
    if image.startswith("catalog:"):
        return f"/static/catalog/{image[8:]}"
    if image.startswith(("http://", "https://")):
        return image
    return f"/media/{image}"


IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"}
SAFE_EXTENSIONS = {"pdf", "txt", "csv", "zip", "7z", "stl", "step", "stp", "dxf", "json", "md", "doc", "docx", "xls", "xlsx",
                   "odt", "ods", "kicadsch", "kicadpcb", "brd", "sch", "ino", "hex", "bin"}


def safe_url(url: str | None) -> str:
    """Nur http(s)-Links zulassen (verhindert javascript:-Links)."""
    u = (url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def store_upload(data: bytes, filename: str, subdir: str = "images") -> str:
    """Speichert Upload; Bilder werden verkleinert (max. 1280 px, WebP) – liefert Pfad relativ zu media/."""
    import hashlib
    import io

    import re as _re

    digest = hashlib.sha1(data).hexdigest()[:16]
    subdir = _re.sub(r"[^a-z]", "", subdir) or "files"
    target_dir = settings.media_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = _re.sub(r"[^a-z0-9]", "", (name.rsplit(".", 1)[-1] if "." in name else "bin").lower())[:8] or "bin"
    if ext not in SAFE_EXTENSIONS and ext not in IMAGE_EXTENSIONS:
        ext = "bin"  # z. B. html/svg/js werden nie mit ausführbarer Endung gespeichert
    if ext in IMAGE_EXTENSIONS:
        try:
            from PIL import Image, ImageOps

            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            img.thumbnail((1280, 1280))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            out = target_dir / f"{digest}.webp"
            img.save(out, "WEBP", quality=82)
            thumb = img.copy()
            thumb.thumbnail((256, 256))
            thumb.save(target_dir / f"{digest}.thumb.webp", "WEBP", quality=75)
            return f"{subdir}/{digest}.webp"
        except Exception:  # noqa: BLE001 – unbekanntes Format: Original speichern
            pass
    out = target_dir / f"{digest}.{ext}"
    out.write_bytes(data)
    return f"{subdir}/{out.name}"


def copy_file_to_media(src, subdir: str = "images") -> str:
    data = open(src, "rb").read()
    return store_upload(data, str(src), subdir)



def merge_items(db: Session, source: Item, target: Item) -> None:
    """Führt source in target zusammen (Bestände, Codes, Historie, Anhänge) und löscht source."""
    from ..models import Attachment, ItemCode, Loan, PriceInfo, StockLot, StockMovement, SupplierPart
    from .search import remove_from_index

    if source.id == target.id:
        raise ValueError("Artikel kann nicht mit sich selbst zusammengeführt werden.")
    for model in (StockLot, StockMovement, ItemCode, SupplierPart, PriceInfo, Loan):
        for obj in db.scalars(select(model).where(model.item_id == source.id)):
            obj.item_id = target.id
    for att in db.scalars(select(Attachment).where(Attachment.owner_type == "item", Attachment.owner_id == source.id)):
        att.owner_id = target.id
    for v in source.variants:
        v.variant_of_id = target.id
    if not target.image and source.image:
        target.image = source.image
    if source.notes and source.notes not in (target.notes or ""):
        target.notes = ((target.notes or "") + "\n" + source.notes).strip()
    db.flush()
    db.expire(source)
    remove_from_index(db, source.id)
    db.delete(source)
    db.flush()
    db.expire(target)
    reindex(db, target)
