"""Health probe: drain behaviour, database reporting, and logging discipline."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest import mock

from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse


class HealthProbeTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.drain_file = Path(self._tmp.name) / "drain"

    def test_returns_ok_when_not_draining(self):
        with override_settings(DRAIN_FILE=str(self.drain_file)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_returns_503_draining_when_drain_file_exists(self):
        self.drain_file.touch()

        with override_settings(DRAIN_FILE=str(self.drain_file)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "draining")

    def test_draining_response_still_reports_database_state(self):
        self.drain_file.touch()

        with override_settings(DRAIN_FILE=str(self.drain_file)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.json()["database"], "connected")

    def test_draining_with_unreachable_database_reports_both(self):
        """Draining takes precedence over an unhealthy database, but the
        database state is still reported so an operator can tell the two
        situations apart."""
        self.drain_file.touch()

        with (
            override_settings(DRAIN_FILE=str(self.drain_file)),
            mock.patch(
                "Conf.views.connection.ensure_connection",
                side_effect=OperationalError("database is down"),
            ),
        ):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "draining")
        self.assertEqual(response.json()["database"], "disconnected")

    def test_reports_unhealthy_when_database_is_unreachable(self):
        with (
            override_settings(DRAIN_FILE=str(self.drain_file)),
            mock.patch(
                "Conf.views.connection.ensure_connection",
                side_effect=OperationalError("database is down"),
            ),
        ):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unhealthy")
        self.assertEqual(response.json()["database"], "disconnected")

    def test_draining_503_is_not_logged_to_django_request(self):
        """Probe 503s must stay off the `django.request` logger.

        Traefik probes this endpoint every few seconds per replica, so a
        rolling deploy's drain window -- a deliberate 503, not a fault --
        would otherwise emit an ERROR per probe for the whole rollout and
        bury real failures.
        """
        self.drain_file.touch()

        with override_settings(DRAIN_FILE=str(self.drain_file)):
            with self.assertNoLogs("django.request", level=logging.DEBUG):
                response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)

    def test_unhealthy_503_is_not_logged_to_django_request(self):
        with (
            override_settings(DRAIN_FILE=str(self.drain_file)),
            mock.patch(
                "Conf.views.connection.ensure_connection",
                side_effect=OperationalError("database is down"),
            ),
        ):
            with self.assertNoLogs("django.request", level=logging.DEBUG):
                response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)


class DevCorsOriginTests(TestCase):
    """Local dev origins must never be trusted outside DEBUG.

    CORS_ALLOW_CREDENTIALS is True, so a trusted origin can read authenticated
    responses. Before this change the localhost origins were appended
    unconditionally, including in production.
    """

    def test_dev_origins_are_not_added_when_debug_is_off(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins(["https://app.example.com"], debug=False)

        self.assertEqual(result, ["https://app.example.com"])

    def test_dev_origins_are_added_when_debug_is_on(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins([], debug=True)

        self.assertIn("http://localhost:5173", result)
        self.assertIn("http://localhost:3000", result)

    def test_configured_origins_are_preserved_and_come_first(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins(["https://app.example.com"], debug=True)

        self.assertEqual(result[0], "https://app.example.com")

    def test_does_not_duplicate_an_already_configured_dev_origin(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins(["http://localhost:5173"], debug=True)

        self.assertEqual(result.count("http://localhost:5173"), 1)

    def test_does_not_mutate_the_list_it_is_given(self):
        from Conf.settings import _with_dev_origins

        configured = ["https://app.example.com"]
        _with_dev_origins(configured, debug=True)

        self.assertEqual(configured, ["https://app.example.com"])

    def test_assembled_setting_has_no_dev_origin_in_production(self):
        """Guards the real assembled setting, not just the helper function.

        settings.py is evaluated once at import time, so DEBUG cannot be
        flipped here with override_settings and re-run through the module.
        Instead this asserts against the actual `CORS_ALLOWED_ORIGINS` the
        process is running with -- so it fails for real if the
        hardcoded-origin bug this class is named after is ever reintroduced
        below the `_with_dev_origins` call site (or the call site itself is
        deleted).

        Deliberately checks `Conf.settings.DEBUG` (the module attribute),
        not `django.conf.settings.DEBUG`: Django's test runner
        unconditionally overrides the latter to False for the duration of
        the suite (see `DiscoverRunner.setup_test_environment`), regardless
        of the real DEBUG the process booted with. `CORS_ALLOWED_ORIGINS`
        was assembled once, at import time, using that real value -- so
        checking the overridden flag here would silently assert the wrong
        branch whenever a developer runs the suite locally with DEBUG=True.
        """
        from django.conf import settings

        from Conf import settings as conf_settings

        if conf_settings.DEBUG:
            self.skipTest("DEBUG is True in this test run; dev origins are expected to be present.")

        dev_hosts = ("localhost", "127.0.0.1")
        offending = [origin for origin in settings.CORS_ALLOWED_ORIGINS if any(host in origin for host in dev_hosts)]

        self.assertEqual(offending, [])
