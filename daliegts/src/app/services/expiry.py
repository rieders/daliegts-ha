"""Haltbarkeit (v0.16): Bestände mit Ablaufdatum – Lötpaste, Kleber, Flussmittel, Akkus, Batterien, Filament …"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, StockLot


@dataclass
class Group:
    key: str
    title: str
    lots: list[StockLot]


def expiring(db: Session, horizon_days: int = 180, today: date | None = None) -> list[Group]:
    today = today or date.today()
    lots = list(db.scalars(select(StockLot).where(StockLot.expiry.is_not(None), StockLot.status.notin_(("lost", "used")),
                                                  StockLot.expiry <= today + timedelta(days=horizon_days)).order_by(StockLot.expiry)))
    lots = [lt for lt in lots if (lt.quantity or 0) > 0 or lt.coarse not in (None, "empty")]
    groups = [Group("abgelaufen", "Abgelaufen", [lt for lt in lots if lt.expiry < today]),
              Group("30", "In den nächsten 30 Tagen", [lt for lt in lots if today <= lt.expiry <= today + timedelta(days=30)]),
              Group("180", f"In {horizon_days // 30} Monaten", [lt for lt in lots if lt.expiry > today + timedelta(days=30)])]
    return groups


def counts(db: Session, today: date | None = None) -> dict:
    g = {x.key: len(x.lots) for x in expiring(db, 30, today)}
    return {"expired": g.get("abgelaufen", 0), "soon": g.get("30", 0)}


def warranties(db: Session, days: int = 60, today: date | None = None) -> list[Item]:
    today = today or date.today()
    return list(db.scalars(select(Item).where(Item.warranty_until.is_not(None), Item.warranty_until >= today,
                                              Item.warranty_until <= today + timedelta(days=days)).order_by(Item.warranty_until)).unique())
