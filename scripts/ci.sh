#!/bin/sh
# Use a disposable database: migrations modify DATABASE_URL.
set -eu
: "${DATABASE_URL:?Set a disposable PostgreSQL DATABASE_URL}"
uv sync --frozen --python 3.13
uv run pytest -q
uv run ruff check .
uv run alembic upgrade head
uv run alembic check
docker build --build-arg "REVISION=$(git rev-parse HEAD)" -t novatrack:ci .
