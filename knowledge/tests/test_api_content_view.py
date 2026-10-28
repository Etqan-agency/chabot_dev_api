"""POST /sync-api-content/ — auth, paid-only gate, validation, fetch/process
paths, and upstream/processing error handling (network mocked)."""

from __future__ import annotations

from unittest import mock

import requests
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.services import api_fetch as fetch_mod
from knowledge.services import api_sync as sync_mod
from knowledge.services import net as net_mod
from knowledge.services.api_content_processor import APIContentProcessingError
from knowledge.views import api_content as view_mod

from .factories import make_tenant


class SyncApiContentTests(APITestCase):
    def setUp(self):
        # safe_get re-validates every redirect hop via services.net, so stub
        # that too -- otherwise these tests resolve real hostnames.
        patcher = mock.patch.object(net_mod, "validate_public_url", lambda u: u)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("sync-api-content")
        # Staff tenant is credit-exempt AND allowed to sync (bypasses the gate).
        self.admin, self.admin_key = make_tenant("api_admin", is_staff=True)

    def _post(self, body, key=None):
        return self.client.post(
            self.url,
            body,
            format="json",
            **({"HTTP_X_API_KEY": key} if key else {}),
        )

    def test_requires_auth(self):
        self.assertEqual(self._post({"api_url": "https://x.com"}).status_code, 401)

    def test_free_tier_blocked_with_402(self):
        _user, key = make_tenant("api_freebie")  # non-staff, no paid plan
        res = self._post({"api_url": "https://x.com/data"}, key=key)
        self.assertEqual(res.status_code, 402)

    def test_missing_api_url_returns_400(self):
        res = self._post({}, key=self.admin_key)
        self.assertEqual(res.status_code, 400)

    def test_unsafe_internal_url_rejected(self):
        res = self._post({"api_url": "http://127.0.0.1/admin"}, key=self.admin_key)
        self.assertEqual(res.status_code, 400)

    def test_no_items_under_key_returns_400(self):
        fake = mock.Mock()
        fake.json.return_value = {"results": []}
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        fake.links = {}
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
        ):
            res = self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)
        self.assertEqual(res.status_code, 400)

    def test_whole_payload_is_ingested_not_just_results(self):
        # count/next are part of the response too — they must reach the processor.
        fake = mock.Mock()
        fake.json.return_value = {
            "count": 2,
            "next": None,
            "results": [{"id": 1}, {"id": 2}],
        }
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        fake.links = {}
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={}) as proc,
        ):
            res = self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)
        self.assertEqual(res.status_code, 200)
        sent = proc.call_args.args[0]
        self.assertEqual(sent[:2], [{"id": 1}, {"id": 2}])
        self.assertEqual(sent[2], {"response_metadata": {"count": 2, "next": None}})

    def test_every_page_of_a_paginated_source_is_ingested(self):
        pages = {
            "https://api.x.com/items": {
                "count": 3,
                "next": "https://api.x.com/items?page=2",
                "results": [{"id": 1}, {"id": 2}],
            },
            "https://api.x.com/items?page=2": {"count": 3, "next": None, "results": [{"id": 3}]},
        }

        def fake_get(url, timeout=None, allow_redirects=True, stream=False):  # noqa: ARG001
            resp = mock.Mock()
            resp.json.return_value = pages[url]
            resp.raise_for_status.return_value = None
            resp.is_redirect = False
            resp.is_permanent_redirect = False
            resp.headers = {}
            resp.iter_content.return_value = [b"{}"]
            resp.links = {}
            return resp

        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(fetch_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", side_effect=fake_get),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={}) as proc,
        ):
            res = self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["pages_fetched"], 2)
        self.assertFalse(res.data["truncated"])
        sent = proc.call_args.args[0]
        # Page 2's record is there — the old code stopped after page 1.
        self.assertEqual([r["id"] for r in sent if "id" in r], [1, 2, 3])
        # And each page's envelope is kept apart from the other's.
        self.assertEqual([r["response_metadata"]["_page"] for r in sent if "response_metadata" in r], [1, 2])

    def test_ai_analysis_flag_is_passed_through(self):
        fake = mock.Mock()
        fake.json.return_value = {"results": [{"id": 1}]}
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        fake.links = {}
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={}) as proc,
        ):
            self._post({"api_url": "https://api.x.com/items", "ai_analysis": False}, key=self.admin_key)
        self.assertIs(proc.call_args.kwargs["ai_analysis"], False)

        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={}) as proc,
        ):
            self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)
        self.assertIs(proc.call_args.kwargs["ai_analysis"], True)  # on by default

    def test_happy_path_processes_items(self):
        fake = mock.Mock()
        fake.json.return_value = {"results": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]}
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        stats = {"processed": 2, "chunks_created": 2, "items_new": 2, "errors": 0}
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value=stats) as proc,
        ):
            res = self._post(
                {"api_url": "https://api.x.com/items", "items_key": "results"},
                key=self.admin_key,
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "success")
        self.assertEqual(res.data["processed"], 2)
        proc.assert_called_once()

    def test_upstream_failure_returns_503(self):
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", side_effect=requests.ConnectionError("down")),
        ):
            res = self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)
        self.assertEqual(res.status_code, 503)

    def test_processing_error_returns_500(self):
        fake = mock.Mock()
        fake.json.return_value = {"results": [{"id": 1}]}
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(
                sync_mod.APIContentRAGProcessor, "process_items", side_effect=APIContentProcessingError("boom")
            ),
        ):
            res = self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)
        self.assertEqual(res.status_code, 500)

    def test_unexpected_error_returns_500(self):
        fake = mock.Mock()
        fake.json.return_value = {"results": [{"id": 1}]}
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(
                sync_mod.APIContentRAGProcessor, "process_items", side_effect=RuntimeError("unexpected")
            ),
        ):
            res = self._post({"api_url": "https://api.x.com/items"}, key=self.admin_key)
        self.assertEqual(res.status_code, 500)

    def test_gate_fails_open_when_billing_layer_errors(self):
        # If can_sync_api_content raises, the endpoint must not block (fail open).
        fake = mock.Mock()
        fake.json.return_value = {"results": [{"id": 1}]}
        fake.raise_for_status.return_value = None
        fake.is_redirect = False
        fake.is_permanent_redirect = False
        fake.headers = {}
        fake.iter_content.return_value = [b"{}"]
        with (
            mock.patch("subscriptions.services.can_sync_api_content", side_effect=RuntimeError),
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=fake),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={"processed": 1}),
        ):
            # Use a normal (non-staff) user so only the fail-open path lets it through.
            _user, key = make_tenant("api_failopen")
            res = self._post({"api_url": "https://api.x.com/items"}, key=key)
        self.assertEqual(res.status_code, 200)


class SyncApiContentIngestionTests(APITestCase):
    """The stages the sync endpoint gained: census, quality gate, run record."""

    def setUp(self):
        # safe_get re-validates every redirect hop via services.net, so stub
        # that too -- otherwise these tests resolve real hostnames.
        patcher = mock.patch.object(net_mod, "validate_public_url", lambda u: u)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("sync-api-content")
        self.admin, self.admin_key = make_tenant("ingest_admin", is_staff=True)

    def _post_with_payload(self, payload, *, process=None):
        def fake_get(url, timeout=None, allow_redirects=True, stream=False):  # noqa: ARG001
            resp = mock.Mock()
            resp.json.return_value = payload
            resp.raise_for_status.return_value = None
            resp.is_redirect = False
            resp.is_permanent_redirect = False
            resp.headers = {}
            resp.iter_content.return_value = [b"{}"]
            resp.links = {}
            return resp

        from knowledge.services import api_content_processor as proc_mod

        patches = [
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(fetch_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", side_effect=fake_get),
            # No embedding provider in tests; the pipeline around it is the point.
            mock.patch.object(proc_mod, "_embed_in_batches", lambda texts: ([[0.1, 0.2, 0.3]] * len(texts), "fake")),
        ]
        if process is not None:
            patches.append(mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", process))
        with mock.patch.object(fetch_mod.requests, "get", side_effect=fake_get):
            for patcher in patches:
                patcher.start()
            try:
                return self.client.post(
                    self.url,
                    {"api_url": "https://api.x.com/products", "ai_analysis": False},
                    format="json",
                    headers={"x-api-key": self.admin_key},
                )
            finally:
                for patcher in reversed(patches):
                    patcher.stop()

    PAYLOAD = {
        "count": 1,
        "next": None,
        "results": [{"id": 1, "title": "Widget", "price": "10.00", "brand": {"id": 2, "name": "Acme"}}],
    }

    def test_response_reports_the_real_source_census_and_coverage(self):
        res = self._post_with_payload(self.PAYLOAD)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["source_stats"]["total_records"], 2)  # item + envelope
        self.assertEqual(res.data["field_coverage"], 1.0)
        self.assertGreater(res.data["fields_total"], 0)
        self.assertEqual(res.data["fields_covered"], res.data["fields_total"])
        # The pre-existing keys are still exactly where callers expect them.
        self.assertEqual(res.data["status"], "success")
        self.assertIn("processed", res.data)
        self.assertIn("chunks_created", res.data)

    def test_a_successful_sync_is_recorded_with_real_numbers(self):
        from knowledge.models import APIIngestionRun

        self._post_with_payload(self.PAYLOAD)
        run = APIIngestionRun.objects.latest("started_at")
        self.assertEqual(run.stage, "completed")
        self.assertEqual(run.records_fetched, 2)
        self.assertEqual(run.user, self.admin)
        self.assertEqual(run.field_coverage, 1.0)
        self.assertGreater(run.chunks_created, 0)

    def test_a_blocked_sync_returns_422_and_says_the_index_is_unchanged(self):
        from knowledge.models import APIIngestionRun
        from knowledge.services.api_content_processor import IngestionBlocked

        def blocked(self_, *args, **kwargs):  # noqa: ARG001
            raise IngestionBlocked("only 1/2 records could be structured", {"fields_total": 2, "fields_covered": 1})

        res = self._post_with_payload(self.PAYLOAD, process=blocked)
        self.assertEqual(res.status_code, 422)
        self.assertIn("unchanged", res.data["detail"])
        self.assertIn("1/2", res.data["reason"])
        self.assertEqual(APIIngestionRun.objects.latest("started_at").stage, "blocked")

    def test_api_credentials_in_the_url_never_reach_the_run_record(self):
        from knowledge.services.api_ingest_run import scrub_endpoint

        self.assertEqual(
            scrub_endpoint("https://api.x.com/products?api_key=sk-secret&token=abc"),
            "https://api.x.com/products",
        )
