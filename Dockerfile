# syntax=docker/dockerfile:1

# ---- builder: compile wheels so the runtime image carries no toolchain ------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ---- runtime ----------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=Conf.settings \
    DRAIN_FILE=/tmp/drain

# curl backs the HEALTHCHECK; libpq5 is psycopg's runtime library. Neither
# build-essential nor libpq-dev is present here -- that is the point of the
# builder stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels

COPY --chown=app:app . .
RUN chmod +x docker/entrypoint.sh

# Owned by app so collectstatic, media writes, and the SQLite fallback all work
# as a non-root user. Note this chowns /app ITSELF, not just its contents:
# `WORKDIR /app` creates the directory as root:root, and `COPY --chown` only
# sets ownership on the files it copies. Without this, uid 10001 cannot create
# /app/db.sqlite3 (the DATABASE_URL-unset fallback), migrate fails with
# "unable to open database file", and entrypoint.sh aborts under `set -e`
# before gunicorn ever binds.
RUN mkdir -p /app/staticfiles /app/media && chown -R app:app /app

USER app

EXPOSE 8000

# start-period covers migrate + collectstatic on first boot, which run in the
# entrypoint before gunicorn binds.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "Conf.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
