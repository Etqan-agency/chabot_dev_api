"""The sync command must report an upstream failure, not crash with NameError."""

from __future__ import annotations

from unittest import mock

import requests
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase


class SyncCommandErrorTests(SimpleTestCase):
    def test_upstream_failure_is_reported_as_a_command_error(self):
        # Regression: `import requests` had been dropped from the module while
        # the handler still caught requests.RequestException -> NameError.
        with mock.patch(
            "knowledge.management.commands.sync_api_content.fetch_all_pages",
            side_effect=requests.RequestException("down"),
        ):
            with self.assertRaises(CommandError) as ctx:
                call_command("sync_api_content", "https://api.x.com/items")
        self.assertIn("API request failed", str(ctx.exception))
