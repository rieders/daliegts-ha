"""Etikettendrucker: weitere Druckersprachen (Zebra ZPL, TSPL, CUPS)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30 10:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('label_printers') as b:
        b.add_column(sa.Column('driver', sa.String(length=20), nullable=False, server_default='brother_ql'))
        b.add_column(sa.Column('dpi', sa.Integer(), nullable=False, server_default='300'))
        b.add_column(sa.Column('media_w_mm', sa.Float(), nullable=True))
        b.add_column(sa.Column('media_h_mm', sa.Float(), nullable=True))
        b.add_column(sa.Column('gap_mm', sa.Float(), nullable=True))
        b.add_column(sa.Column('invert', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('label_printers') as b:
        for c in ('invert', 'gap_mm', 'media_h_mm', 'media_w_mm', 'dpi', 'driver'):
            b.drop_column(c)
