"""Database construction, with SQLite available for isolated tests only."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def make_engine(url: str):
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("sqlite"):
        options.update(connect_args={"check_same_thread": False})
        if url in {"sqlite://", "sqlite:///:memory:"}:
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
