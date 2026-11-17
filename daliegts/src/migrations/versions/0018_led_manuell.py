"""LED-Zuordnungen: von Hand gesetzte Zeilen markieren (bleiben beim Neuberechnen der Ketten erhalten)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-12 18:00:00

Regel: nur additive Änderungen.
Idempotent: Datenbanken aus dem früheren main-Zweig haben die Spalte schon (dort als Revision „0017“).
"""
from alembic import op
import sqlalchemy as sa


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns('location_lights')}
    if 'manual' in cols:
        return
    with op.batch_alter_table('location_lights', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manual', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('location_lights', schema=None) as batch_op:
        batch_op.drop_column('manual')
