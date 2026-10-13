"""Statistik: Warenbestand in Zahlen, Lagerwert, Buchungen und Einkäufe (v0.5)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ITEM_KINDS, MOVEMENT_KINDS
from ..security import require
from ..services import stats
from ..web import render

router = APIRouter()


@router.get("/statistik")
def page(request: Request, tage: int = 90, db: Session = Depends(get_db), ctx=Depends(require("items", 1))):
    inv = stats.inventory(db, ITEM_KINDS)
    act = stats.activity(db, tage)
    return render(request, "stats.html", {"inv": inv, "act": act, "PERIODS": stats.PERIODS, "SOURCES": stats.SOURCES,
                                          "MOVEMENT_KINDS": MOVEMENT_KINDS, "show_value": ctx.can("prices", 1)})


@router.get("/statistik/bestand.csv")
def export(db: Session = Depends(get_db), ctx=Depends(require("prices", 1))):
    name = f"lagerwerk-bestand-{date.today().isoformat()}.csv"
    return Response("﻿" + stats.inventory_csv(db), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
