"""Audit-Log: jede Änderung mit Benutzer, Zeit und Alt/Neu-Werten."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from ..models import AuditLog


def _plain(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def snapshot(obj) -> dict:
    return {c.key: _plain(getattr(obj, c.key)) for c in sa_inspect(obj).mapper.column_attrs}


def diff(before: dict, after: dict) -> dict:
    return {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k) and k not in ("updated_at",)}


def log(db: Session, user_id: int | None, action: str, entity: str, entity_id: int | None, summary: str = "", changes: dict | None = None) -> AuditLog:
    entry = AuditLog(user_id=user_id, action=action, entity=entity, entity_id=entity_id, summary=summary[:2000], diff=changes or None)
    db.add(entry)
    return entry


UNDOABLE = {"item": "Item", "location": "Location", "category": "Category"}


def undo(db: Session, entry: AuditLog, user_id: int | None) -> bool:
    """Macht einfache Feldänderungen rückgängig."""
    from .. import models

    if entry.undone or entry.action != "update" or entry.entity not in UNDOABLE or not entry.diff:
        return False
    model = getattr(models, UNDOABLE[entry.entity])
    obj = db.get(model, entry.entity_id)
    if obj is None:
        return False
    before = snapshot(obj)
    for field, (old, _new) in entry.diff.items():
        if hasattr(obj, field):
            col = sa_inspect(model).columns.get(field)
            if col is not None and old is not None and col.type.python_type in (datetime, date):
                old = col.type.python_type.fromisoformat(old)
            setattr(obj, field, old)
    entry.undone = True
    log(db, user_id, "undo", entry.entity, entry.entity_id, f"Rückgängig: #{entry.id}", diff(before, snapshot(obj)))
    return True
