FROM ghcr.io/astral-sh/uv:0.11.18@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 AS uv
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

ARG VERSION=0.1.3
ARG REVISION=unknown
LABEL org.opencontainers.image.title="NovaTrack" \
      org.opencontainers.image.description="Self-hosted personal parcel tracking" \
      org.opencontainers.image.source="https://github.com/carlos-otero/novatrack" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY novatrack ./novatrack
COPY alembic ./alembic
COPY alembic.ini LICENSE ./
COPY scripts/entrypoint.sh /usr/local/bin/novatrack
RUN uv sync --frozen --no-dev --no-editable && \
    groupadd --gid 10001 novatrack && useradd --uid 10001 --gid 10001 --no-create-home novatrack && \
    chmod 755 /usr/local/bin/novatrack && chmod -R a+rX /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"]
ENTRYPOINT ["novatrack"]
CMD ["web"]
