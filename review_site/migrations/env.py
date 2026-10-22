"""Alembic environment. The database URL comes from the app's settings unless one is passed in programmatically."""
from alembic import context
from sqlalchemy import create_engine

from review_site.app import models  # noqa: F401  (registers the tables on Base.metadata)
from review_site.app.config import load_settings
from review_site.app.db import Base

target_metadata = Base.metadata


def url():
    return context.config.get_main_option("sqlalchemy.url") or load_settings().db_url


def run_migrations_offline():
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
