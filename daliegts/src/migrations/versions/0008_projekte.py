"""Projekte/Baugruppen mit Stückliste (KiCad), Auftrag aus Projekt

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28 20:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('projects',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('revision', sa.String(length=40), server_default='', nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('source', sa.String(length=255), server_default='', nullable=False),
        sa.Column('pcb_item_id', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['pcb_item_id'], ['items.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_table('project_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=True),
        sa.Column('refs', sa.Text(), server_default='', nullable=False),
        sa.Column('value', sa.String(length=200), server_default='', nullable=False),
        sa.Column('footprint', sa.String(length=200), server_default='', nullable=False),
        sa.Column('mpn', sa.String(length=120), server_default='', nullable=False),
        sa.Column('qty', sa.Float(), nullable=False),
        sa.Column('dnp', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('match', sa.String(length=12), server_default='', nullable=False),
        sa.Column('note', sa.String(length=255), server_default='', nullable=False),
        sa.Column('sort', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_project_lines_project_id', 'project_lines', ['project_id'])
    op.create_index('ix_project_lines_item_id', 'project_lines', ['item_id'])
    with op.batch_alter_table('orders') as b:
        b.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('build_qty', sa.Integer(), nullable=True))
        b.create_foreign_key('fk_orders_project_id', 'projects', ['project_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('orders') as b:
        b.drop_constraint('fk_orders_project_id', type_='foreignkey')
        b.drop_column('build_qty')
        b.drop_column('project_id')
    op.drop_table('project_lines')
    op.drop_table('projects')
