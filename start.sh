#!/usr/bin/env sh
set -eu

: "${PORT:=10000}"
: "${GUNICORN_THREADS:=4}"
: "${GUNICORN_TIMEOUT:=120}"

# Keep a single Gunicorn worker because the app stores queue state in-process.
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads "${GUNICORN_THREADS}" \
  --timeout "${GUNICORN_TIMEOUT}" \
  app:app
