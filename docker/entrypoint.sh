#!/bin/sh
# Container entrypoint. Only the web service runs migrations/collectstatic
# (RUN_MIGRATIONS=1); the worker just execs its command.
set -e

if [ "$RUN_MIGRATIONS" = "1" ]; then
  echo "[entrypoint] Applying migrations..."
  python manage.py migrate --noinput

  echo "[entrypoint] Collecting static files..."
  python manage.py collectstatic --noinput

  if [ "$RAG_VECTOR_BACKEND" = "pgvector" ]; then
    echo "[entrypoint] Setting up pgvector (extension + index + backfill)..."
    python manage.py setup_pgvector || echo "[entrypoint] setup_pgvector skipped (run again after first ingest)"
  fi
fi

exec "$@"
