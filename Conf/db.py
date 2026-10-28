"""Database configuration.

Kept out of ``settings.py`` deliberately. Settings run once at import, which
makes the decision they encode almost impossible to test -- and this particular
decision is one whose failure mode is invisible, so it is exactly the kind that
needs tests. See ``Conf/test_database_config.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

# The engine this project actually runs on. pgvector, the HNSW index and the
# JSON->vector backfill in knowledge/services/vector_store.py all assume it.
_POSTGRES_SCHEMES = ("postgres", "postgresql")
_POSTGRES_ENGINE = "django.db.backends.postgresql"
_SQLITE_ENGINE = "django.db.backends.sqlite3"


def database_config(
    database_url: str | None,
    *,
    debug: bool,
    base_dir: Path,
    allow_sqlite: bool = False,
    running_tests: bool = False,
) -> dict[str, Any]:
    """Return the ``DATABASES["default"]`` mapping, or refuse to produce one.

    The SQLite fallback is genuinely useful in three places -- local
    development, the test suite, and a deliberate one-off -- and genuinely
    dangerous everywhere else, because nothing about a service running on the
    wrong database looks wrong from outside. So it stays, gated on saying so.
    """
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme not in _POSTGRES_SCHEMES:
            raise ImproperlyConfigured(
                f"DATABASE_URL names the scheme {parsed.scheme!r}, but this "
                f"project supports only {' and '.join(_POSTGRES_SCHEMES)}. "
                "The scheme used to be ignored entirely -- any URL produced a "
                "PostgreSQL configuration -- so a mistake here surfaced as a "
                "connection error at the first query instead of a "
                "configuration error at boot."
            )
        return {
            "ENGINE": _POSTGRES_ENGINE,
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username or "",
            "PASSWORD": parsed.password or "",
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port) if parsed.port else "",
        }

    if debug or running_tests or allow_sqlite:
        return {"ENGINE": _SQLITE_ENGINE, "NAME": base_dir / "db.sqlite3"}

    raise ImproperlyConfigured(
        "DATABASE_URL is not set and DEBUG is off. This process would have "
        f"fallen back to SQLite at {base_dir / 'db.sqlite3'}.\n"
        "\n"
        "That fallback is refused here because it is silent: the service "
        "starts, reports healthy, serves traffic, and writes every row to a "
        "file that no volume is mounted over -- so the next image rebuild or "
        "container recreate discards all of it, with no error at any point.\n"
        "\n"
        "Set DATABASE_URL to the PostgreSQL instance. To run on SQLite "
        "deliberately (a local check, a one-off management command), set "
        "ALLOW_SQLITE=1."
    )
