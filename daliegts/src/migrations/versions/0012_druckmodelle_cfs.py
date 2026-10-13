"""3D-Druck: Druckmodelle, CFS-Plätze, Mitschnitt

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-29 18:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('printers_3d') as b:
        b.add_column(sa.Column('model', sa.String(length=40), nullable=False, server_default='K1C'))
        b.add_column(sa.Column('has_cfs', sa.Boolean(), nullable=False, server_default='0'))
        b.add_column(sa.Column('cfs_slots', sa.JSON(), nullable=True))
        b.add_column(sa.Column('recording_until', sa.DateTime(), nullable=True))
    op.create_table('print_models',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('printer_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(length=500), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('expected_g', sa.Float(), nullable=True),
        sa.Column('expected_mm', sa.Float(), nullable=True),
        sa.Column('last_g', sa.Float(), nullable=True),
        sa.Column('prints', sa.Integer(), nullable=False),
        sa.Column('last_printed', sa.DateTime(), nullable=True),
        sa.Column('on_printer', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['printer_id'], ['printers_3d.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('printer_id', 'path', name='uq_print_models_printer_path'),
    )
    op.create_index('ix_print_models_printer_id', 'print_models', ['printer_id'])


def downgrade() -> None:
    op.drop_table('print_models')
