"""The database configuration must never silently fall back to SQLite.

`DATABASE_URL` unset used to mean "use SQLite at BASE_DIR/db.sqlite3", with no
warning of any kind. A production host that lost the variable therefore came up
healthy, answered `/health/`, and wrote everything to a file inside the
container's writable layer -- which the next rebuild discards. Nothing
observable from outside distinguishes that from a correctly configured host,
which is why the deployment spec carried "production DB engine unconfirmed" as
an open risk rather than a settled fact.

These tests pin the boundary: the fallback stays available where it is useful
(local development, the test suite, an explicit opt-in) and is refused where it
is dangerous.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from Conf.db import database_config

BASE = Path("/srv/app")


class DatabaseUrlTests(SimpleTestCase):
    def test_database_url_produces_a_postgres_config(self):
        config = database_config(
            "postgres://alice:s3cret@db.internal:5433/chatbot",
            debug=False,
            base_dir=BASE,
        )

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "chatbot")
        self.assertEqual(config["USER"], "alice")
        self.assertEqual(config["PASSWORD"], "s3cret")
        self.assertEqual(config["HOST"], "db.internal")
        self.assertEqual(config["PORT"], "5433")

    def test_postgresql_scheme_is_accepted_too(self):
        config = database_config("postgresql://u:p@h/db", debug=False, base_dir=BASE)

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")

    def test_a_url_naming_another_engine_is_refused(self):
        # Previously this produced ENGINE=postgresql pointed at a MySQL DSN --
        # a connection error at first query rather than a configuration error
        # at boot.
        with self.assertRaises(ImproperlyConfigured) as caught:
            database_config("mysql://u:p@h/db", debug=False, base_dir=BASE)

        self.assertIn("mysql", str(caught.exception))


class SqliteFallbackTests(SimpleTestCase):
    def test_missing_url_is_refused_outside_debug(self):
        with self.assertRaises(ImproperlyConfigured):
            database_config(None, debug=False, base_dir=BASE)

    def test_an_empty_url_counts_as_missing(self):
        with self.assertRaises(ImproperlyConfigured):
            database_config("", debug=False, base_dir=BASE)

    def test_the_refusal_names_the_variable_and_the_hazard(self):
        # The message is the entire value of this guard to whoever hits it at
        # 3am: it has to say which variable is missing and why booting anyway
        # would be worse than not booting.
        with self.assertRaises(ImproperlyConfigured) as caught:
            database_config(None, debug=False, base_dir=BASE)

        message = str(caught.exception)
        self.assertIn("DATABASE_URL", message)
        self.assertIn("SQLite", message)

    def test_debug_still_gets_sqlite(self):
        config = database_config(None, debug=True, base_dir=BASE)

        self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(config["NAME"], BASE / "db.sqlite3")

    def test_the_test_suite_still_gets_sqlite(self):
        # `manage.py test` runs with DEBUG forced to False, so without this the
        # guard would refuse to let the suite start on any machine that has no
        # Postgres.
        config = database_config(None, debug=False, base_dir=BASE, running_tests=True)

        self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")

    def test_an_explicit_opt_in_still_gets_sqlite(self):
        config = database_config(None, debug=False, base_dir=BASE, allow_sqlite=True)

        self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")


class SettingsBootTests(SimpleTestCase):
    """The guard is only worth anything if it fires at process start.

    A check that happened at first query would let a misconfigured container
    pass its healthcheck, join the load balancer, and fail per-request -- which
    is the failure mode this exists to prevent, not a milder version of it. So
    these run `manage.py check` in a subprocess: nothing else exercises
    settings.py's own import-time behaviour, because settings are imported once
    per process and cannot be re-imported meaningfully in-place.
    """

    def _run_check(self, **overrides):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DATABASE_URL", "DEBUG", "ALLOW_SQLITE", "SECRET_KEY"}
        }
        # settings.py refuses to boot without a SECRET_KEY when DEBUG is off.
        # That is a different guard with its own tests below; supply one here so
        # it cannot mask the DATABASE_URL behaviour these cases are about.
        env.setdefault("SECRET_KEY", "test-only-key-for-subprocess-boot-checks")
        env.update(overrides)
        return subprocess.run(
            [sys.executable, "manage.py", "check"],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def setUp(self):
        # settings.py calls load_dotenv(), which fills in variables that are
        # absent from the environment -- so a developer .env carrying
        # DATABASE_URL would defeat the unset-variable case below. Skip rather
        # than assert something the environment has already decided. ALLOW_SQLITE
        # decides it just as thoroughly: it is the documented opt-in that makes a
        # missing DATABASE_URL legal, so a .env with it set turns the refusal
        # this asserts into a correct boot.
        dotenv = Path(settings.BASE_DIR) / ".env"
        if dotenv.exists():
            contents = dotenv.read_text()
            for variable in ("DATABASE_URL", "ALLOW_SQLITE"):
                if variable in contents:
                    self.skipTest(f"a local .env defines {variable}; cannot test a missing DATABASE_URL")

    def test_boot_is_refused_when_database_url_is_missing(self):
        result = self._run_check(DEBUG="False")

        self.assertNotEqual(result.returncode, 0, "expected the boot to be refused")
        self.assertIn("DATABASE_URL", result.stderr)

    def test_the_explicit_opt_in_still_boots(self):
        result = self._run_check(DEBUG="False", ALLOW_SQLITE="1")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_debug_still_boots(self):
        result = self._run_check(DEBUG="True")

        self.assertEqual(result.returncode, 0, result.stderr)


class SecretKeyBootTests(SimpleTestCase):
    """SECRET_KEY is SIMPLE_JWT's signing key, so the dev default is not a weak
    secret -- it is the ability to mint a valid token for any account. Like the
    database guard, it has to stop the process at import rather than let a
    container pass its healthcheck and serve forged tokens.
    """

    def _run_check(self, **overrides):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DATABASE_URL", "DEBUG", "ALLOW_SQLITE", "SECRET_KEY"}
        }
        env.update({"ALLOW_SQLITE": "1", **overrides})
        return subprocess.run(
            [sys.executable, "manage.py", "check"],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def setUp(self):
        dotenv = Path(settings.BASE_DIR) / ".env"
        if dotenv.exists() and "SECRET_KEY" in dotenv.read_text():
            self.skipTest("a local .env defines SECRET_KEY; cannot test it unset")

    def test_boot_is_refused_on_the_development_key(self):
        result = self._run_check(DEBUG="False")
        self.assertNotEqual(result.returncode, 0, "expected the boot to be refused")
        self.assertIn("SECRET_KEY", result.stderr)
        # The message has to say why it matters, not just that it is unset.
        self.assertIn("forge an access token", result.stderr)

    def test_a_real_secret_key_boots(self):
        result = self._run_check(DEBUG="False", SECRET_KEY="a-real-and-sufficiently-long-secret-value")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_is_unaffected(self):
        result = self._run_check(DEBUG="True")
        self.assertEqual(result.returncode, 0, result.stderr)
