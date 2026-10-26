"""Migrations read DATABASE_URL only and never log connection parameters."""

import os
from logging.config import fileConfig

from alembic import context
from novatrack.db import make_engine
from novatrack.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata
url = os.environ["DATABASE_URL"]

if context.is_offline_mode():
    context.configure(
        url=url.replace("postgresql://", "postgresql+psycopg://", 1),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
