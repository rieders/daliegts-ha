"""Schema-Versionierung und sichere Migration (Grundregeln 1 + 2)."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from ..config import BASE_DIR

ROOT = BASE_DIR.parent
CORE_TABLES = ["locations", "categories", "items", "stock_lots", "stock_movements", "users", "location_aliases", "item_codes"]


class SchemaError(RuntimeError):
    pass


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def current_revision(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.connect() as conn:
        rev = MigrationContext.configure(conn).get_current_revision()
    eng.dispose()
    return rev


def known_revisions() -> set[str]:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return {r.revision for r in script.walk_revisions()}


def check_schema(db_path: Path) -> tuple[str, str | None, str]:
    """Liefert (status, current, head). status: ok | empty | pending | newer."""
    head = head_revision()
    cur = current_revision(db_path)
    if cur is None:
        return "empty", cur, head
    if cur == head:
        return "ok", cur, head
    if cur not in known_revisions():
        return "newer", cur, head
    return "pending", cur, head


def table_counts(db_path: Path) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    out = {}
    for t in CORE_TABLES:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = -1
    con.close()
    return out


def upgrade(db_path: Path, revision: str = "head") -> None:
    command.upgrade(alembic_config(f"sqlite:///{db_path}"), revision)


def safe_migrate(db_path: Path, backup_fn=None, log=print) -> dict:
    """Probelauf auf einer Kopie, Prüfung der Zeilenzahlen, dann echte Migration.

    Rückgabe: Bericht als dict. Wirft SchemaError, wenn etwas nicht stimmt – die echte DB bleibt dann unverändert.
    """
    status, cur, head = check_schema(db_path)
    report = {"status": status, "from": cur, "to": head}
    if status == "newer":
        raise SchemaError(f"Datenbank-Version {cur} ist neuer als diese Programmversion ({head}). Bitte Programm aktualisieren.")
    if status == "ok":
        log(f"Schema ist aktuell ({head}).")
        return report
    if status == "empty":
        log("Neue Datenbank wird angelegt …")
        upgrade(db_path)
        return report

    before = table_counts(db_path)
    if backup_fn:
        report["backup"] = str(backup_fn())
        log(f"Backup erstellt: {report['backup']}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "probe.sqlite"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        dst.close()
        log(f"Probelauf der Migration {cur} → {head} auf einer Kopie …")
        try:
            upgrade(copy)
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"Probelauf fehlgeschlagen, echte Datenbank unverändert: {exc}") from exc
        after = table_counts(copy)
        lost = {t: (before[t], after.get(t)) for t in before if before[t] > 0 and after.get(t, -1) < before[t]}
        if lost:
            raise SchemaError(f"Probelauf würde Daten verlieren {lost} – abgebrochen, echte Datenbank unverändert.")
    log("Probelauf erfolgreich, migriere echte Datenbank …")
    upgrade(db_path)
    final = table_counts(db_path)
    report["counts_before"], report["counts_after"] = before, final
    if any(final[t] < before[t] for t in before if before[t] > 0):
        raise SchemaError("Zeilenzahlen nach Migration kleiner als vorher – bitte Backup zurückspielen!")
    log("Migration abgeschlossen.")
    return report


def restore_file(backup_file: Path, db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(backup_file, db_path)
