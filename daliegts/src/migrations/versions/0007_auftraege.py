"""Aufträge mit Materialliste und Reservierung

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28 18:00:00

Regel: nur additive Änderungen.
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('requester', sa.String(length=120), server_default='', nullable=False),
        sa.Column('status', sa.String(length=12), server_default='offen', nullable=False),
        sa.Column('due', sa.Date(), nullable=True),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_orders_status', 'orders', ['status'])
    op.create_table('order_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('qty_required', sa.Float(), nullable=False),
        sa.Column('qty_issued', sa.Float(), server_default='0', nullable=False),
        sa.Column('note', sa.String(length=255), server_default='', nullable=False),
        sa.Column('sort', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_order_lines_order_id', 'order_lines', ['order_id'])
    op.create_index('ix_order_lines_item_id', 'order_lines', ['item_id'])
    with op.batch_alter_table('shopping_items') as b:
        b.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        b.create_index('ix_shopping_items_order_id', ['order_id'])
        b.create_foreign_key('fk_shopping_items_order_id', 'orders', ['order_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('shopping_items') as b:
        b.drop_constraint('fk_shopping_items_order_id', type_='foreignkey')
        b.drop_index('ix_shopping_items_order_id')
        b.drop_column('order_id')
    op.drop_table('order_lines')
    op.drop_table('orders')
