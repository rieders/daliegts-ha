"""Druckmodelle: zuletzt auf dem Drucker gesehen (Archiv)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02 12:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('print_models') as b:
        b.add_column(sa.Column('seen_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('print_models') as b:
        b.drop_column('seen_at')
