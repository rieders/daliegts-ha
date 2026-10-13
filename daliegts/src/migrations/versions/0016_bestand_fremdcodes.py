"""Fremdcodes an einzelnen Beständen (Spulen, Chargen)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-02 18:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'lot_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('lot_id', sa.Integer(), sa.ForeignKey('stock_lots.id', ondelete='CASCADE'), nullable=False),
        sa.Column('code', sa.String(255), nullable=False, unique=True),
        sa.Column('source', sa.String(40), nullable=False, server_default='manuell'),
    )
    op.create_index('ix_lot_codes_lot_id', 'lot_codes', ['lot_id'])


def downgrade() -> None:
    op.drop_index('ix_lot_codes_lot_id', table_name='lot_codes')
    op.drop_table('lot_codes')
