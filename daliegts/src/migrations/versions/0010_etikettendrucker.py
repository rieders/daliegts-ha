"""Netzwerk-Etikettendrucker (Brother QL) für Direktdruck

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-28 22:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('label_printers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('model', sa.String(length=20), nullable=False),
        sa.Column('host', sa.String(length=200), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('media', sa.String(length=12), nullable=False),
        sa.Column('cut', sa.Boolean(), nullable=False),
        sa.Column('hq', sa.Boolean(), nullable=False),
        sa.Column('threshold', sa.Integer(), nullable=False),
        sa.Column('rotate', sa.Integer(), nullable=False),
        sa.Column('offset_mm', sa.Float(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('last_ok', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('label_printers')
