"""3D-Drucker (Filamentverbrauch)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29 12:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('printers_3d',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('host', sa.String(length=200), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('diameter_mm', sa.Float(), nullable=False),
        sa.Column('spool_lot_id', sa.Integer(), nullable=True),
        sa.Column('job', sa.JSON(), nullable=True),
        sa.Column('pending', sa.JSON(), nullable=True),
        sa.Column('last_state', sa.String(length=20), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('warning', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['spool_lot_id'], ['stock_lots.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('printers_3d')
