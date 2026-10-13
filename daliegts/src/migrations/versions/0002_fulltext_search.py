"""Volltextsuche (SQLite FTS5)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Eigenständige FTS-Tabelle; wird vom Such-Service gepflegt (app/services/search.py).
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
        "name, description, mpn, tags, extra, tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS items_fts")
