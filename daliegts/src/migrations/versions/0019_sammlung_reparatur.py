"""Reparatur: Sammlungstabellen nachlegen, falls sie fehlen

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-17 18:00:00

Datenbanken aus dem früheren main-Zweig (Pick-to-Light) stehen auf Revision „0017“, die dort
led_manuell hieß. Für sie wurde 0017_sammlung nie ausgeführt – diese Migration legt media_records,
media_tracks und book_records (mit Indizes) an, wenn sie fehlen. Sonst nichts.

Regel: nur additive Änderungen.
"""
import importlib.util
from pathlib import Path


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def _sammlung():
    """Tabellendefinition aus 0017_sammlung.py (eine Quelle für beide Migrationen)."""
    path = Path(__file__).with_name('0017_sammlung.py')
    spec = importlib.util.spec_from_file_location('_daliegts_mig_0017_sammlung', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def upgrade() -> None:
    _sammlung().create_missing()


def downgrade() -> None:
    pass  # Die Tabellen gehören zu 0017_sammlung und werden dort entfernt.
