"""Usage metering + the analytics endpoint."""

from __future__ import annotations

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import UsageRecord
from knowledge.services import quota

from .factories import make_tenant


class RecordUsageTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")

    def test_record_usage_computes_cost(self):
        rec = quota.record_usage(
            self.user,
            model="gpt-4o",
            tokens_in=1000,
            tokens_out=500,
            response_time_ms=1234,
            confident=True,
            chunk_count=6,
        )
        self.assertEqual(rec.tokens_in, 1000)
        self.assertEqual(rec.tokens_out, 500)
        # 1000/1e6*2.5 + 500/1e6*10 = 0.0025 + 0.005 = 0.0075
        self.assertAlmostEqual(rec.cost_usd, 0.0075, places=6)
        self.assertEqual(UsageRecord.objects.filter(user=self.user).count(), 1)


class AnalyticsEndpointTests(APITestCase):
    def setUp(self):
        self.alice, self.alice_key = make_tenant("alice")
        self.bob, self.bob_key = make_tenant("bob")
        self.admin, self.admin_key = make_tenant("root", is_staff=True)

        quota.record_usage(
            self.alice,
            model="gpt-4o",
            tokens_in=100,
            tokens_out=50,
            response_time_ms=500,
            confident=True,
            chunk_count=3,
        )
        quota.record_usage(
            self.alice,
            model="gpt-4o",
            tokens_in=200,
            tokens_out=80,
            response_time_ms=700,
            confident=False,
            chunk_count=0,
        )
        quota.record_usage(
            self.bob, model="gpt-4o", tokens_in=10, tokens_out=5, response_time_ms=100, confident=True, chunk_count=1
        )

    def _get(self, key, **params):
        return self.client.get(reverse("analytics-usage"), params, headers={"x-api-key": key})

    def test_self_rollup_is_scoped(self):
        res = self._get(self.alice_key)
        self.assertEqual(res.status_code, 200)
        totals = res.data["totals"]
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["tokens_in"], 300)
        self.assertEqual(totals["tokens_out"], 130)
        self.assertEqual(totals["total_tokens"], 430)
        self.assertAlmostEqual(totals["confident_rate"], 0.5, places=3)
        self.assertEqual(res.data["tenant"]["username"], "alice")

    def test_quota_block_present(self):
        res = self._get(self.alice_key)
        self.assertIn("quota", res.data)
        self.assertIn("max_documents", res.data["quota"])

    def test_scope_all_requires_admin(self):
        res = self._get(self.alice_key, scope="all")
        self.assertEqual(res.status_code, 403)

    def test_scope_all_for_admin(self):
        res = self._get(self.admin_key, scope="all")
        self.assertEqual(res.status_code, 200)
        usernames = {t["tenant"]["username"] for t in res.data["tenants"]}
        self.assertEqual(usernames, {"alice", "bob"})
