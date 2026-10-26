#!/bin/sh
set -eu
case "${1:-web}" in
  web)
    exec uvicorn novatrack.app:create_app --factory --host 0.0.0.0 --port 8000 \
      --workers 1 --proxy-headers --forwarded-allow-ips "${TRUSTED_PROXY_IPS:-127.0.0.1}" --no-access-log
    ;;
  worker) exec python -m novatrack.worker ;;
  migrate) exec alembic upgrade head ;;
  *) exec "$@" ;;
esac
