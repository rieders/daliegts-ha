"""Sammlung: Tonträger (media_records, media_tracks) und Bücher (book_records)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-16 18:00:00

Regel: nur additive Änderungen – bestehende Tabellen und Daten bleiben unverändert.
Idempotent: Tabellen und Indizes werden nur angelegt, wenn sie fehlen. Datenbanken aus dem früheren
main-Zweig stehen schon auf „0017“ (dort: led_manuell) – für sie legt 0019_sammlung_reparatur die
Tabellen über create_missing() aus dieser Datei an.
"""
from alembic import op
import sqlalchemy as sa


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def _ts():
    return [sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp())]


def create_missing() -> list[str]:
    """Legt fehlende Sammlungstabellen samt Indizes an. Rückgabe: neu angelegte Tabellen."""
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    created = []

    def index(name, table, cols):
        if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
            op.create_index(name, table, cols)

    if 'media_records' not in tables:
        created.append('media_records')
        op.create_table(
            'media_records',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='CASCADE'), nullable=False, unique=True),
            sa.Column('artist', sa.String(255), nullable=False, server_default=''),
            sa.Column('artist_sort', sa.String(255), nullable=False, server_default=''),
            sa.Column('album', sa.String(255), nullable=False, server_default=''),
            sa.Column('year', sa.Integer(), nullable=True),
            sa.Column('genre', sa.String(120), nullable=False, server_default=''),
            sa.Column('label', sa.String(200), nullable=False, server_default=''),
            sa.Column('catalog_no', sa.String(80), nullable=False, server_default=''),
            sa.Column('medium', sa.String(20), nullable=False, server_default='LP'),
            sa.Column('country', sa.String(60), nullable=False, server_default=''),
            sa.Column('musicbrainz_id', sa.String(36), nullable=True),
            sa.Column('discogs_id', sa.String(20), nullable=True),
            sa.Column('media_condition', sa.String(4), nullable=False, server_default=''),
            sa.Column('sleeve_condition', sa.String(4), nullable=False, server_default=''),
            sa.Column('digitized', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('digitized_path', sa.String(500), nullable=False, server_default=''),
            sa.Column('market_lowest', sa.Float(), nullable=True),
            sa.Column('market_suggested', sa.Float(), nullable=True),
            sa.Column('market_currency', sa.String(3), nullable=False, server_default='EUR'),
            sa.Column('market_for_sale', sa.Integer(), nullable=True),
            sa.Column('market_checked_at', sa.DateTime(), nullable=True),
            sa.Column('market_error', sa.String(300), nullable=False, server_default=''),
            *_ts(),
        )
    index('ix_media_records_artist_sort', 'media_records', ['artist_sort'])
    index('ix_media_records_musicbrainz_id', 'media_records', ['musicbrainz_id'])
    index('ix_media_records_discogs_id', 'media_records', ['discogs_id'])
    if 'media_tracks' not in tables:
        created.append('media_tracks')
        op.create_table(
            'media_tracks',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('record_id', sa.Integer(), sa.ForeignKey('media_records.id', ondelete='CASCADE'), nullable=False),
            sa.Column('side', sa.String(4), nullable=False, server_default='A'),
            sa.Column('position', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('title', sa.String(255), nullable=False, server_default=''),
            sa.Column('artist', sa.String(255), nullable=False, server_default=''),
            sa.Column('length_seconds', sa.Integer(), nullable=True),
        )
    index('ix_media_tracks_record_id', 'media_tracks', ['record_id'])
    if 'book_records' not in tables:
        created.append('book_records')
        op.create_table(
            'book_records',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='CASCADE'), nullable=False, unique=True),
            sa.Column('title', sa.String(300), nullable=False, server_default=''),
            sa.Column('subtitle', sa.String(300), nullable=False, server_default=''),
            sa.Column('authors', sa.String(500), nullable=False, server_default=''),
            sa.Column('author_sort', sa.String(255), nullable=False, server_default=''),
            sa.Column('publisher', sa.String(200), nullable=False, server_default=''),
            sa.Column('year', sa.Integer(), nullable=True),
            sa.Column('edition', sa.String(80), nullable=False, server_default=''),
            sa.Column('isbn13', sa.String(13), nullable=True),
            sa.Column('isbn10', sa.String(10), nullable=True),
            sa.Column('language', sa.String(20), nullable=False, server_default=''),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('series', sa.String(200), nullable=False, server_default=''),
            sa.Column('series_number', sa.String(20), nullable=False, server_default=''),
            sa.Column('genre', sa.String(120), nullable=False, server_default=''),
            sa.Column('binding', sa.String(20), nullable=False, server_default=''),
            sa.Column('condition', sa.String(40), nullable=False, server_default=''),
            sa.Column('signed', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('read', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('market_value', sa.Float(), nullable=True),
            sa.Column('market_currency', sa.String(3), nullable=False, server_default='EUR'),
            sa.Column('market_checked_at', sa.DateTime(), nullable=True),
            *_ts(),
        )
    index('ix_book_records_author_sort', 'book_records', ['author_sort'])
    index('ix_book_records_isbn13', 'book_records', ['isbn13'])
    return created


def upgrade() -> None:
    create_missing()


def downgrade() -> None:
    op.drop_index('ix_book_records_isbn13', table_name='book_records')
    op.drop_index('ix_book_records_author_sort', table_name='book_records')
    op.drop_table('book_records')
    op.drop_index('ix_media_tracks_record_id', table_name='media_tracks')
    op.drop_table('media_tracks')
    op.drop_index('ix_media_records_discogs_id', table_name='media_records')
    op.drop_index('ix_media_records_musicbrainz_id', table_name='media_records')
    op.drop_index('ix_media_records_artist_sort', table_name='media_records')
    op.drop_table('media_records')
