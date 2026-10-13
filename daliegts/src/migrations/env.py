"""Alembic-Umgebung. Die DB-URL kommt aus app.config bzw. -x url=... (für Probeläufe auf Kopien)."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (Modelle registrieren)
from app.config import settings
from app.db import Base

config = context.config
url = context.get_x_argument(as_dictionary=True).get("url") or config.attributes.get("url") or settings.db_url
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # FTS5-Tabellen werden per Hand verwaltet
    return not (type_ == "table" and name.startswith("items_fts"))


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        connectable = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
