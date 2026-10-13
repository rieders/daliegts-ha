"""Stichproben-Inventur: wann wurde ein Fach zuletzt geprüft

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27 15:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('locations') as b:
        b.add_column(sa.Column('last_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('locations') as b:
        b.drop_column('last_checked_at')
