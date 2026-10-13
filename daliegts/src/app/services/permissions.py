"""Feine Rechte pro Bereich (wie Part-DB) mit Stufen keine/lesen/bearbeiten/anlegen/löschen."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Group, GroupPermission, User

NONE, READ, EDIT, CREATE, DELETE = 0, 1, 2, 3, 4
LEVELS = {NONE: "keine", READ: "lesen", EDIT: "bearbeiten", CREATE: "anlegen", DELETE: "löschen"}

# Bereich -> (Bezeichnung, erfordert Admin-Modus ab Stufe)
AREAS: dict[str, tuple[str, int | None]] = {
    "items": ("Artikel", DELETE),
    "stock": ("Bestand buchen", None),
    "inventory": ("Inventur", None),
    "locations": ("Lagerorte & Lagermöbel", EDIT),
    "categories": ("Kategorien & Footprints", EDIT),
    "parameters": ("Parameter-Vorlagen", EDIT),
    "suppliers": ("Lieferanten & Hersteller", DELETE),
    "prices": ("Preise", None),
    "purchasing": ("Einkaufsliste & Bestellungen", None),
    "attachments": ("Anhänge", DELETE),
    "loans": ("Verleih", None),
    "orders": ("Aufträge & Projekte", DELETE),
    "labels": ("Etiketten drucken", None),
    "wled_show": ("WLED anzeigen", None),
    "wled_config": ("WLED konfigurieren", EDIT),
    "import_export": ("Import/Export", READ),
    "backup": ("Backup", READ),
    "users": ("Benutzer & Gruppen", READ),
    "system": ("Systemeinstellungen", READ),
    "api_tokens": ("API-Tokens", CREATE),
    "audit": ("Audit-Log", READ),
}

DEFAULT_GROUPS = {
    "Admin": ("Alle Rechte", {a: DELETE for a in AREAS}),
    "Familie": (
        "Suchen, scannen, LED, ein-/auslagern, Artikel anlegen/bearbeiten, verleihen",
        {
            "items": CREATE, "stock": CREATE, "inventory": EDIT, "locations": READ, "categories": READ,
            "parameters": READ, "suppliers": READ, "prices": READ, "purchasing": CREATE, "attachments": CREATE, "loans": CREATE, "orders": CREATE,
            "labels": READ, "wled_show": READ, "api_tokens": CREATE,
        },
    ),
    "Gast": (
        "Nur suchen, Lagerort sehen, LED anzeigen",
        {"items": READ, "locations": READ, "categories": READ, "wled_show": READ},
    ),
}


def ensure_default_groups(db: Session) -> None:
    for name, (desc, perms) in DEFAULT_GROUPS.items():
        g = db.scalar(select(Group).where(Group.name == name))
        if g is None:
            g = Group(name=name, description=desc, builtin=True)
            db.add(g)
            db.flush()
            for area, lvl in perms.items():
                db.add(GroupPermission(group_id=g.id, area=area, level=lvl))
        else:  # neue Bereiche in späteren Versionen ergänzen (additiv)
            have = {p.area for p in g.permissions}
            for area in AREAS:
                if area not in have:
                    db.add(GroupPermission(group_id=g.id, area=area, level=perms.get(area, NONE)))
    db.flush()


def effective_levels(user: User | None, guest_group: Group | None = None) -> dict[str, int]:
    group = user.group if user else guest_group
    levels = {a: NONE for a in AREAS}
    if group:
        for p in group.permissions:
            if p.area in levels:
                levels[p.area] = p.level
    if user:
        for o in user.overrides:
            if o.area in levels:
                levels[o.area] = o.level
    return levels


def needs_admin_mode(area: str, level: int) -> bool:
    threshold = AREAS.get(area, ("", None))[1]
    return threshold is not None and level >= threshold
