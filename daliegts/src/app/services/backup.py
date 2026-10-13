"""Backup, offenes Exportformat "Lagerwerk-Export v1" und Wiederherstellung (Grundregel 3)."""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tarfile
import zipfile
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from ..config import APP_VERSION, EXPORT_FORMAT_VERSION, settings
from ..db import Base

# Reihenfolge = Abhängigkeitsreihenfolge für den Import
EXPORT_TABLES = [
    "groups", "group_permissions", "users", "user_permissions", "api_tokens", "settings", "code_sequences",
    "external_ids", "manufacturers", "suppliers", "parameter_templates", "footprints", "categories", "locations",
    "location_aliases", "items", "item_parameters", "item_codes", "stock_lots", "stock_movements", "supplier_parts",
    "price_infos", "shopping_items", "provider_candidates", "attachments", "equivalents", "loans", "wled_controllers", "location_lights", "storage_templates",
    "audit_log",
]
SELF_REFERENCING = {"locations": "parent_id", "categories": "parent_id", "footprints": "parent_id", "items": "variant_of_id"}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_database(db_path: Path | None = None, target_dir: Path | None = None, label: str = "") -> Path:
    """Konsistente Kopie per SQLite-Online-Backup (läuft auch bei laufender App)."""
    db_path = db_path or settings.db_path
    target_dir = target_dir or settings.backup_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"lagerwerk-{_stamp()}{('-' + label) if label else ''}.sqlite"
    dst_path = target_dir / name
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dst_path


def backup_media(target_dir: Path | None = None) -> Path:
    target_dir = target_dir or settings.backup_dir
    out = target_dir / f"media-{_stamp()}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(settings.media_dir, arcname="media")
    return out


def rotate(target_dir: Path | None = None, keep: int | None = None) -> list[Path]:
    target_dir = target_dir or settings.backup_dir
    keep = keep or settings.backup_keep
    removed = []
    for pattern in ("lagerwerk-*.sqlite", "media-*.tar.gz", "export-*.zip"):
        files = sorted(target_dir.glob(pattern))
        auto = [f for f in files if "-vor-" not in f.name]  # Sicherungen vor Updates bleiben
        for f in auto[:-keep] if len(auto) > keep else []:
            f.unlink()
            removed.append(f)
    return removed


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.hex()
    raise TypeError(type(o))


def export_v1(db: Session, target: Path | None = None, include_media: bool = True) -> Path:
    """Exportiert alle Daten im dokumentierten, schema-unabhängigen Format (docs/EXPORT_FORMAT.md)."""
    target = target or settings.backup_dir / f"export-{_stamp()}.zip"
    insp = inspect(db.get_bind())
    existing = set(insp.get_table_names())
    manifest = {
        "format": "lagerwerk-export",
        "format_version": EXPORT_FORMAT_VERSION,
        "app_version": APP_VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in EXPORT_TABLES:
            if table not in existing:
                continue
            rows = [dict(r._mapping) for r in db.execute(text(f'SELECT * FROM "{table}"'))]
            if table == "settings":  # Zugangsdaten (API-Schlüssel) nie exportieren
                rows = [r for r in rows if not str(r.get("key", "")).startswith("secret.")]
            manifest["tables"][table] = len(rows)
            zf.writestr(f"data/{table}.json", json.dumps(rows, ensure_ascii=False, default=_json_default, indent=1))
            if rows:
                buf = io.StringIO()
                import csv

                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
                w.writeheader()
                for r in rows:
                    w.writerow({k: (json.dumps(v, ensure_ascii=False, default=_json_default) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
                zf.writestr(f"csv/{table}.csv", buf.getvalue())
        if include_media and settings.media_dir.exists():
            for f in settings.media_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, f"media/{f.relative_to(settings.media_dir)}")
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return target


def import_v1(db: Session, source: Path, media_dir: Path | None = None) -> dict:
    """Importiert einen Export in eine LEERE Datenbank (Umzug/Wiederherstellung)."""
    media_dir = media_dir or settings.media_dir
    with zipfile.ZipFile(source) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("format") != "lagerwerk-export":
            raise ValueError("Keine DaLiegt’s-/Lagerwerk-Exportdatei.")
        if int(manifest.get("format_version", 0)) > EXPORT_FORMAT_VERSION:
            raise ValueError("Exportformat ist neuer als diese Programmversion.")
        if db.execute(text("SELECT COUNT(*) FROM items")).scalar() or db.execute(text("SELECT COUNT(*) FROM locations")).scalar():
            raise ValueError("Import nur in eine leere Datenbank möglich.")
        insp = inspect(db.get_bind())
        counts = {}
        db.commit()
        db.execute(text("PRAGMA foreign_keys=OFF"))  # außerhalb einer Transaktion -> wirksam
        try:
            # Standardgruppen/-daten einer frischen Installation ersetzen
            for table in reversed(EXPORT_TABLES):
                if f"data/{table}.json" in zf.namelist() and table in insp.get_table_names():
                    db.execute(text(f'DELETE FROM "{table}"'))
            for table in EXPORT_TABLES:
                name = f"data/{table}.json"
                if name not in zf.namelist() or table not in insp.get_table_names():
                    continue
                rows = json.loads(zf.read(name))
                cols = {c["name"]: c for c in insp.get_columns(table)}
                for row in rows:
                    row = {k: v for k, v in row.items() if k in cols}  # unbekannte (neuere) Felder ignorieren
                    for k, v in row.items():
                        if isinstance(v, (dict, list)):
                            row[k] = json.dumps(v, ensure_ascii=False)
                    keys = ", ".join(f'"{k}"' for k in row)
                    vals = ", ".join(f":{k}" for k in row)
                    db.execute(text(f'INSERT INTO "{table}" ({keys}) VALUES ({vals})'), row)
                counts[table] = len(rows)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute(text("PRAGMA foreign_keys=ON"))
            db.commit()
            from .. import db as dbmod

            dbmod.engine.dispose()  # alle Pool-Verbindungen neu (mit foreign_keys=ON)
        media_root = media_dir.resolve()
        for n in zf.namelist():
            if n.startswith("media/") and not n.endswith("/"):
                out = (media_dir / n[len("media/"):]).resolve()
                if not out.is_relative_to(media_root):
                    continue  # Schutz gegen ../-Pfade im Archiv
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    from .search import rebuild_index

    rebuild_index(db)
    return counts


def copy_to_nas(path: Path) -> Path | None:
    if not settings.nas_backup_dir:
        return None
    dst = Path(settings.nas_backup_dir)
    if not dst.exists():
        return None
    return Path(shutil.copy2(path, dst / path.name))


def daily_backup(db: Session) -> dict:
    out = {"db": backup_database(), "export": export_v1(db, include_media=False), "media": backup_media()}
    for p in list(out.values()):
        copy_to_nas(p)
    out["rotated"] = rotate()
    return out


def all_models_tables() -> list[str]:
    return sorted(Base.metadata.tables)
