"""Background dispatch for POST /sync-api-content/ and its polling endpoint.

A full import runs one LLM call per record, which outlives any gateway timeout,
so in production the request must hand the work to a worker and answer 202. The
in-request path is kept for INGESTION_MODE=sync and is covered elsewhere; here
we pin the handover, the run bookkeeping, and the tenant isolation on polling.
"""

from __future__ import annotations

from unittest import mock

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import APIIngestionRun, IngestionStage
from knowledge.services import api_sync as sync_mod
from knowledge.views import api_content as view_mod

from .factories import make_tenant


def _page(payload):
    fake = mock.Mock()
    fake.json.return_value = payload
    fake.raise_for_status.return_value = None
    fake.is_redirect = False
    fake.is_permanent_redirect = False
    fake.headers = {}
    fake.iter_content.return_value = [b"{}"]
    fake.links = {}
    return fake


class DispatchTests(APITestCase):
    def setUp(self):
        # safe_get validates every hop through services.net; stub it so these
        # tests never resolve a real hostname.
        patcher = mock.patch("knowledge.services.net.validate_public_url", lambda u: u)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("sync-api-content")
        self.admin, self.admin_key = make_tenant("async_admin", is_staff=True)

    def _post(self, body=None, key=None):
        return self.client.post(
            self.url,
            body or {"api_url": "https://api.x.com/items"},
            format="json",
            headers={"x-api-key": key or self.admin_key},
        )

    @override_settings(INGESTION_MODE="celery")
    def test_celery_mode_answers_202_without_running_the_pipeline(self):
        enqueued = []
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch("knowledge.tasks.sync_api_content_task.delay", lambda *a: enqueued.append(a)),
            mock.patch.object(sync_mod, "run_api_sync", side_effect=AssertionError("must not run in-request")),
        ):
            res = self._post()

        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["status"], "queued")
        self.assertTrue(res.data["run_id"])
        self.assertIn(res.data["run_id"], res.data["status_url"])
        self.assertEqual(len(enqueued), 1)

    @override_settings(INGESTION_MODE="celery")
    def test_the_queued_run_is_recorded_before_the_worker_starts(self):
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch("knowledge.tasks.sync_api_content_task.delay", lambda *a: None),
        ):
            res = self._post({"api_url": "https://api.x.com/items?token=secret"})

        run = APIIngestionRun.objects.get(pk=res.data["run_id"])
        self.assertEqual(run.stage, IngestionStage.QUEUED)
        self.assertEqual(run.user, self.admin)
        # The query string is where API keys travel; it must not be stored.
        self.assertNotIn("secret", run.endpoint)

    @override_settings(INGESTION_MODE="celery")
    def test_a_broker_that_refuses_the_job_is_a_503_not_a_run_stuck_queued(self):
        def refuse(*_a):
            raise OSError("connection refused")

        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch("knowledge.tasks.sync_api_content_task.delay", refuse),
        ):
            res = self._post()

        self.assertEqual(res.status_code, 503)
        run = APIIngestionRun.objects.latest("started_at")
        self.assertEqual(run.stage, IngestionStage.FAILED)

    @override_settings(INGESTION_MODE="sync")
    def test_sync_mode_still_completes_in_request_and_returns_200(self):
        with (
            mock.patch.object(view_mod, "validate_public_url", lambda u: u),
            mock.patch.object(view_mod.requests, "get", return_value=_page({"results": [{"id": 1}]})),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={"processed": 1}),
        ):
            res = self._post()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "success")
        self.assertTrue(res.data["run_id"])


class WorkerTaskTests(APITestCase):
    def setUp(self):
        self.admin, _key = make_tenant("worker_admin", is_staff=True)

    def test_the_task_advances_the_run_the_view_opened(self):
        from knowledge.tasks import sync_api_content_task

        run = APIIngestionRun.objects.create(
            user=self.admin, endpoint="https://api.x.com/items", stage=IngestionStage.QUEUED
        )
        params = sync_mod.SyncParams(api_url="https://api.x.com/items").as_dict()

        with (
            mock.patch.object(view_mod.requests, "get", return_value=_page({"results": [{"id": 1}]})),
            mock.patch("knowledge.services.net.validate_public_url", lambda u, **k: u),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={"processed": 1}),
        ):
            out = sync_api_content_task(str(run.pk), str(self.admin.pk), params)

        run.refresh_from_db()
        self.assertEqual(out["status"], "success")
        self.assertEqual(run.stage, IngestionStage.COMPLETED)
        # No second row: the worker picked up the one the view opened.
        self.assertEqual(APIIngestionRun.objects.count(), 1)

    def test_a_failing_sync_is_recorded_and_never_re_raised_at_the_worker(self):
        from knowledge.tasks import sync_api_content_task

        run = APIIngestionRun.objects.create(
            user=self.admin, endpoint="https://api.x.com/items", stage=IngestionStage.QUEUED
        )
        params = sync_mod.SyncParams(api_url="https://api.x.com/items").as_dict()

        with (
            mock.patch("knowledge.services.net.validate_public_url", lambda u, **k: u),
            mock.patch.object(view_mod.requests, "get", side_effect=RuntimeError("boom")),
        ):
            out = sync_api_content_task(str(run.pk), str(self.admin.pk), params)

        run.refresh_from_db()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(run.stage, IngestionStage.FAILED)

    def test_a_deleted_run_row_does_not_cost_the_tenant_the_sync(self):
        from knowledge.tasks import sync_api_content_task

        params = sync_mod.SyncParams(api_url="https://api.x.com/items").as_dict()
        missing = "00000000-0000-0000-0000-000000000000"

        with (
            mock.patch("knowledge.services.net.validate_public_url", lambda u, **k: u),
            mock.patch.object(view_mod.requests, "get", return_value=_page({"results": [{"id": 1}]})),
            mock.patch.object(sync_mod.APIContentRAGProcessor, "process_items", return_value={"processed": 1}) as proc,
        ):
            out = sync_api_content_task(missing, str(self.admin.pk), params)

        self.assertEqual(out["status"], "success")
        proc.assert_called_once()


class PollingTests(APITestCase):
    def setUp(self):
        self.admin, self.admin_key = make_tenant("poll_admin", is_staff=True)
        self.other, self.other_key = make_tenant("poll_other", is_staff=True)

    def _get(self, run_id, key=None):
        return self.client.get(
            reverse("api-ingestion-run", args=[str(run_id)]),
            headers={"x-api-key": key or self.admin_key},
        )

    def test_a_queued_run_reports_not_done(self):
        run = APIIngestionRun.objects.create(user=self.admin, stage=IngestionStage.QUEUED)
        res = self._get(run.pk)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "queued")
        self.assertFalse(res.data["done"])

    def test_a_completed_run_reports_done_with_its_stats(self):
        run = APIIngestionRun.objects.create(
            user=self.admin,
            stage=IngestionStage.COMPLETED,
            records_processed=7,
            chunks_created=7,
            result_stats={"processed": 7},
        )
        res = self._get(run.pk)
        self.assertTrue(res.data["done"])
        self.assertEqual(res.data["records_processed"], 7)
        self.assertEqual(res.data["result"], {"processed": 7})

    def test_a_blocked_run_says_the_index_is_unchanged(self):
        run = APIIngestionRun.objects.create(
            user=self.admin,
            stage=IngestionStage.BLOCKED,
            blocked_reason="coverage too low",
            result_stats={"validation": {"field_coverage": 0.1}},
        )
        res = self._get(run.pk)
        self.assertTrue(res.data["done"])
        self.assertIn("unchanged", res.data["detail"])
        self.assertEqual(res.data["reason"], "coverage too low")

    def test_another_tenants_run_is_not_readable(self):
        run = APIIngestionRun.objects.create(user=self.other, stage=IngestionStage.COMPLETED)
        self.assertEqual(self._get(run.pk).status_code, 404)

    def test_polling_requires_auth(self):
        run = APIIngestionRun.objects.create(user=self.admin, stage=IngestionStage.QUEUED)
        res = self.client.get(reverse("api-ingestion-run", args=[str(run.pk)]))
        self.assertEqual(res.status_code, 401)
