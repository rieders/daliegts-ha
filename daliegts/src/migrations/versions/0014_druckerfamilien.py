"""3D-Drucker: weitere Druckerfamilien (Bambu Lab, Prusa, Klipper, OctoPrint)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-02 10:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('printers_3d') as b:
        b.add_column(sa.Column('access_code', sa.String(length=200), nullable=False, server_default=''))
        b.add_column(sa.Column('serial', sa.String(length=64), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('printers_3d') as b:
        b.drop_column('serial')
        b.drop_column('access_code')
