"""Handy-Scanner (PWA)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import StorageTemplate
from ..security import require
from ..web import render

router = APIRouter()


@router.get("/scan")
def scan(request: Request, modus: str = "info", unbekannt: str = "", moebel: int | None = None, db: Session = Depends(get_db),
         ctx=Depends(require("items", 1))):
    from .pages import _category_options

    return render(request, "scan.html", {"modus": modus, "unbekannt": unbekannt, "moebel": moebel, "cats": _category_options(db),
                                         "templates": list(db.scalars(select(StorageTemplate)))})


@router.get("/buchen")
def book(request: Request, modus: str = "aus", ctx=Depends(require("stock", 2))):
    """Umbuchen, Einräumen und Ausbuchen per Handscanner (PC) oder Kamera (Handy)."""
    return render(request, "book.html", {"modus": modus if modus in ("aus", "um", "ein") else "aus"})
