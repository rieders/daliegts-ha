"""Eigene Etikettenvorlagen je Etikettenart

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28 21:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('label_templates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('kind', sa.String(length=12), nullable=False),
        sa.Column('media', sa.String(length=30), nullable=False),
        sa.Column('width_mm', sa.Float(), nullable=False),
        sa.Column('height_mm', sa.Float(), nullable=False),
        sa.Column('symbology', sa.String(length=12), nullable=False),
        sa.Column('code_pos', sa.String(length=8), nullable=False),
        sa.Column('lines', sa.JSON(), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_label_templates_kind', 'label_templates', ['kind'])


def downgrade() -> None:
    op.drop_table('label_templates')
