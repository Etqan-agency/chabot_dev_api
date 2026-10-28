"""Subscription plans: limit resolution, points, renewal, model routing, API."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from knowledge.models import UsageKind, UsageRecord
from knowledge.services import quota
from knowledge.tests.factories import make_tenant
from subscriptions import services
from subscriptions.models import Plan, Subscription


def make_plan(slug="test", **kw):
    defaults = {
        "name": slug.title(),
        "slug": slug,
        "monthly_questions": 0,
        "monthly_token_cap": 0,
        "max_documents": 100,
        "max_total_mb": 200,
        "max_requests_per_min": 60,
        "llm_model": "gpt-4o",
    }
    defaults.update(kw)
    return Plan.objects.create(**defaults)


def subscribe(user, plan, *, days=30, status="active"):
    now = timezone.now()
    return Subscription.objects.create(
        user=user,
        plan=plan,
        status=status,
        current_period_start=now,
        current_period_end=now + timedelta(days=days),
    )


def chat_records(user, n):
    for _ in range(n):
        UsageRecord.objects.create(user=user, kind=UsageKind.CHAT, tokens_in=10, tokens_out=5)


class LimitResolutionTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")

    def test_no_subscription_is_free_tier(self):
        # A non-staff tenant with no plan is on the free tier: tiny doc limits.
        limits = quota.effective_limits(self.user)
        self.assertEqual(limits.max_documents, 1)  # FREE_TIER_MAX_DOCUMENTS
        self.assertEqual(limits.max_total_mb, 0.5)  # FREE_TIER_MAX_TOTAL_MB
        self.assertEqual(limits.monthly_questions, 0)  # unlimited (no plan cap)
        quota.check_chat_allowed(self.user)  # no raise (free points)

    def test_staff_gets_generous_defaults(self):
        admin, _ = make_tenant("owner_defaults", is_staff=True)
        limits = quota.effective_limits(admin)
        self.assertEqual(limits.max_documents, 100)  # global default, not free tier

    def test_plan_limits_override_defaults(self):
        subscribe(self.user, make_plan("starter", max_documents=25, monthly_questions=1000))
        limits = quota.effective_limits(self.user)
        self.assertEqual(limits.max_documents, 25)
        self.assertEqual(limits.monthly_questions, 1000)

    def test_resolve_model_from_plan(self):
        subscribe(self.user, make_plan("mini", llm_model="gpt-4o-mini"))
        self.assertEqual(services.resolve_model(self.user), "gpt-4o-mini")

    def test_no_plan_model_is_none(self):
        self.assertIsNone(services.resolve_model(self.user))


class QuestionQuotaTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("bob")

    def test_under_quota_ok(self):
        subscribe(self.user, make_plan(monthly_questions=2))
        chat_records(self.user, 1)
        quota.check_chat_allowed(self.user)  # no raise

    def test_over_quota_raises(self):
        subscribe(self.user, make_plan(monthly_questions=2))
        chat_records(self.user, 2)
        with self.assertRaises(quota.QuestionQuotaExceeded):
            quota.check_chat_allowed(self.user)

    def test_token_cap_still_enforced(self):
        subscribe(self.user, make_plan(monthly_questions=0, monthly_token_cap=100))
        UsageRecord.objects.create(user=self.user, kind=UsageKind.CHAT, tokens_in=80, tokens_out=40)
        with self.assertRaises(quota.TokenBudgetExceeded):
            quota.check_chat_allowed(self.user)

    def test_expired_subscription_blocks(self):
        subscribe(self.user, make_plan(monthly_questions=1000), days=-1)  # ended yesterday
        with self.assertRaises(quota.SubscriptionInactive):
            quota.check_chat_allowed(self.user)


class SubscriptionApiTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.admin, self.admin_key = make_tenant("root", is_staff=True)
        self.plan = make_plan("starter", monthly_questions=1000, price_usd=39)

    def test_plans_list_for_authenticated(self):
        res = self.client.get(reverse("plan-list"), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        slugs = {p["slug"] for p in res.data["results"]}
        self.assertIn("starter", slugs)

    def test_plan_create_requires_admin(self):
        body = {"name": "Hacker", "slug": "hacker", "monthly_questions": 999999}
        res = self.client.post(reverse("plan-list"), body, format="json", headers={"x-api-key": self.key})
        self.assertIn(res.status_code, (403, 401))

    def test_admin_creates_plan_without_slug(self):
        body = {"name": "Pro Plus", "monthly_questions": 20000, "price_usd": "299.00"}
        res = self.client.post(reverse("plan-list"), body, format="json", headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["slug"], "pro-plus")  # auto-derived from name

    def test_admin_assigns_subscription(self):
        body = {"user": str(self.user.id), "plan": str(self.plan.id), "duration_days": 30}
        res = self.client.post(
            reverse("subscription-list"), body, format="json", headers={"x-api-key": self.admin_key}
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(Subscription.objects.filter(user=self.user, plan=self.plan).exists())

    def test_register_with_plan_creates_subscription(self):
        from django.contrib.auth import get_user_model

        body = {
            "username": "newco",
            "email": "newco@x.com",
            "password": "S0meStr0ng!Pass",
            "password_confirm": "S0meStr0ng!Pass",
            "plan": "starter",
            "plan_duration_days": 30,
        }
        res = self.client.post(reverse("user-list"), body, format="json", headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 201)
        self.assertIn("subscription", res.data)
        self.assertEqual(res.data["subscription"]["plan"], "Starter")
        u = get_user_model().objects.get(username="newco")
        self.assertTrue(Subscription.objects.filter(user=u, plan=self.plan).exists())

    def test_register_with_invalid_plan_returns_400(self):
        body = {
            "username": "newco2",
            "email": "newco2@x.com",
            "password": "S0meStr0ng!Pass",
            "password_confirm": "S0meStr0ng!Pass",
            "plan": "does-not-exist",
        }
        res = self.client.post(reverse("user-list"), body, format="json", headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 400)

    def test_register_without_plan_still_works(self):

        body = {
            "username": "noplan",
            "email": "noplan@x.com",
            "password": "S0meStr0ng!Pass",
            "password_confirm": "S0meStr0ng!Pass",
        }
        res = self.client.post(reverse("user-list"), body, format="json", headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 201)
        self.assertNotIn("subscription", res.data)

    def test_my_subscription_free_tier(self):
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["on_free_tier"])
        self.assertIn("usage", res.data)

    def test_my_subscription_with_plan(self):
        subscribe(self.user, self.plan)
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.key})
        self.assertFalse(res.data["on_free_tier"])
        usage = res.data["usage"]
        self.assertEqual(usage["questions_limit"], 1000)
        self.assertEqual(usage["questions_used"], 0)
        self.assertEqual(usage["questions_remaining"], 1000)
        # Documents/storage usage + remaining are reported too.
        self.assertIn("documents_remaining", usage)
        self.assertIn("storage_mb_remaining", usage)


class PointsTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("wallet_user")

    def test_free_wallet_starts_at_free_tier_points(self):
        self.assertEqual(services.points_balance(self.user), 300)  # FREE_TIER_POINTS
        self.assertTrue(services.has_points(self.user))

    def test_charge_takes_milli_points_exactly(self):
        services.charge_points(self.user, 40)  # a tiny embedding: 0.04 points
        self.assertEqual(services.points_milli(self.user), 300_000 - 40)

    def test_charge_floors_at_zero(self):
        services.charge_points(self.user, 10_000_000)
        self.assertEqual(services.points_milli(self.user), 0)
        self.assertFalse(services.has_points(self.user))

    def test_below_one_point_is_exhausted(self):
        services.set_points(self.user, 0)
        services.add_points(self.user, 1)
        self.assertTrue(services.has_points(self.user))
        services.charge_points(self.user, 1)  # 0.999 left
        self.assertFalse(services.has_points(self.user))

    def test_check_chat_allowed_blocks_when_out_of_points(self):
        services.set_points(self.user, 0)
        with self.assertRaises(quota.PointsExhausted) as ctx:
            quota.check_chat_allowed(self.user)
        self.assertEqual(ctx.exception.status_code, 402)

    def test_check_chat_allowed_passes_with_points(self):
        quota.check_chat_allowed(self.user)  # 300 points → fine, no raise

    def test_assign_plan_resets_wallet_to_plan_points(self):
        # Assignment RELOADS the wallet to the plan's points (not 300 + 5000).
        plan = make_plan(slug="pro", points=5000)
        services.assign_plan(self.user, plan)
        self.assertEqual(services.points_balance(self.user), 5000)

    def test_downgrade_resets_points_down_to_new_plan(self):
        big = make_plan(slug="big", points=5000)
        small = make_plan(slug="small", points=500)
        services.assign_plan(self.user, big)
        services.add_points(self.user, 500)  # admin correction: 5000 + 500
        self.assertEqual(services.points_balance(self.user), 5500)
        services.assign_plan(self.user, small)  # downgrade → reload to 500
        self.assertEqual(services.points_balance(self.user), 500)

    def test_limits_only_plan_change_does_not_wipe_points(self):
        # A plan with no points leaves the balance untouched.
        limits_plan = make_plan(slug="limitsonly", points=0, max_documents=50)
        services.assign_plan(self.user, limits_plan)
        self.assertEqual(services.points_balance(self.user), 300)  # unchanged free grant

    def test_admin_add_points_endpoint(self):
        admin, admin_key = make_tenant("points_admin", is_staff=True)
        res = self.client.post(
            reverse("subscription-add-points"),
            {"user": str(self.user.pk), "amount": 50},
            format="json",
            headers={"x-api-key": admin_key},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["points_balance"], 350)

    def test_my_subscription_reports_points(self):
        services.charge_points(self.user, 12_345)
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        points = res.data["points"]
        self.assertEqual(points["balance"], 287.655)
        self.assertEqual(points["allowance"], 300)
        self.assertEqual(points["used"], 12.345)
        self.assertFalse(points["exhausted"])
        self.assertFalse(points["exempt"])
        self.assertNotIn("credits", res.data)

    def test_top_up_enters_the_total(self):
        # Free tier spent to zero, then the admin adds 1000: "used / total" is
        # counted against 1300, not the free tier's 300.
        services.charge_points(self.user, 300_000)
        services.add_points(self.user, 1000)
        services.charge_points(self.user, 342_000)
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.key})
        points = res.data["points"]
        self.assertEqual(points["balance"], 658)
        self.assertEqual(points["allowance"], 1300)
        self.assertEqual(points["used"], 642)

    def test_new_period_resets_the_total(self):
        services.add_points(self.user, 1000)
        services.set_points(self.user, 5000)  # plan assigned / renewed
        self.assertEqual(services.points_allowance(self.user), 5000)

    def test_my_subscription_reports_exhausted(self):
        services.set_points(self.user, 0)
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.key})
        self.assertTrue(res.data["points"]["exhausted"])


class FreeTierLimitTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("free_user")  # non-staff, no plan
        self.admin, self.admin_key = make_tenant("owner", is_staff=True)

    def test_free_tier_doc_and_storage_limits(self):
        limits = quota.effective_limits(self.user)
        self.assertEqual(limits.max_documents, 1)  # FREE_TIER_MAX_DOCUMENTS
        self.assertEqual(limits.max_total_mb, 0.5)  # FREE_TIER_MAX_TOTAL_MB

    def test_staff_is_not_free_tier(self):
        limits = quota.effective_limits(self.admin)
        self.assertEqual(limits.max_documents, 100)  # generous global default
        self.assertFalse(quota.is_free_tier(self.admin))

    def test_paid_plan_overrides_free_tier(self):
        plan = make_plan(slug="biz", max_documents=50, max_total_mb=100, points=0)
        subscribe(self.user, plan)
        limits = quota.effective_limits(self.user)
        self.assertEqual(limits.max_documents, 50)
        self.assertFalse(quota.is_free_tier(self.user))

    def test_free_tier_second_upload_blocked(self):
        # First tiny doc ok; a second one breaches the 1-document free limit.
        quota.check_document_quota(self.user, 100_000)  # ~0.1 MB, allowed
        from knowledge.tests.factories import make_document

        make_document(self.user, size=100_000)
        with self.assertRaises(quota.DocumentQuotaExceeded):
            quota.check_document_quota(self.user, 100_000)

    def test_free_tier_cannot_sync_api_content(self):
        self.assertFalse(services.can_sync_api_content(self.user))
        res = self.client.post(
            reverse("sync-api-content"),
            {"api_url": "https://example.com/api/x"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 402)

    def test_admin_can_sync_api_content(self):
        self.assertTrue(services.can_sync_api_content(self.admin))

    def test_paid_plan_can_sync_when_enabled(self):
        plan = make_plan(slug="apiplan", allow_api_sync=True, points=0)
        subscribe(self.user, plan)
        self.assertTrue(services.can_sync_api_content(self.user))


_WH_SECRET = "pdl_ntfset_testsecret"


def _sign(raw: bytes, secret=_WH_SECRET, ts="1700000000"):
    import hashlib
    import hmac

    h1 = hmac.new(secret.encode(), ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


@override_settings(PADDLE_WEBHOOK_SECRET=_WH_SECRET)
class PaddleWebhookTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("payer")
        self.plan = make_plan(slug="pro", points=2000, paddle_price_id="pri_123")
        self.url = reverse("paddle-webhook")

    def _post(self, payload, *, secret=_WH_SECRET, sign=True):
        import json

        raw = json.dumps(payload).encode()
        headers = {}
        if sign:
            headers["HTTP_PADDLE_SIGNATURE"] = _sign(raw, secret)
        return self.client.post(self.url, data=raw, content_type="application/json", **headers)

    def _txn(self, event_id="evt_1"):
        return {
            "event_id": event_id,
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_1",
                "custom_data": {"user_id": str(self.user.pk)},
                "items": [{"price": {"id": "pri_123"}}],
            },
        }

    def test_bad_signature_rejected(self):
        res = self._post(self._txn(), secret="wrong-secret")
        self.assertEqual(res.status_code, 403)

    def test_missing_signature_rejected(self):
        res = self._post(self._txn(), sign=False)
        self.assertEqual(res.status_code, 403)

    def test_transaction_completed_grants_points(self):
        res = self._post(self._txn())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(services.points_balance(self.user), 2000)  # reset, no rollover

    def test_idempotent_on_repeated_event_id(self):
        self._post(self._txn(event_id="evt_dup"))
        services.charge_points(self.user, 100_000)  # spent 100
        self._post(self._txn(event_id="evt_dup"))  # same id → no second grant
        self.assertEqual(services.points_balance(self.user), 1900)

    def test_subscription_created_sets_plan_without_granting(self):
        before = services.points_balance(self.user)
        payload = {
            "event_id": "evt_sub",
            "event_type": "subscription.created",
            "data": {
                "id": "sub_1",
                "customer_id": "ctm_1",
                "custom_data": {"user_id": str(self.user.pk)},
                "items": [{"price": {"id": "pri_123"}}],
                "next_billed_at": "2026-12-31T00:00:00Z",
            },
        }
        res = self._post(payload)
        self.assertEqual(res.status_code, 200)
        sub = Subscription.objects.get(user=self.user)
        self.assertEqual(sub.plan, self.plan)
        self.assertEqual(sub.paddle_subscription_id, "sub_1")
        # subscription.* does NOT grant points (transactions do).
        self.assertEqual(services.points_balance(self.user), before)

    def test_subscription_canceled_marks_status(self):
        from subscriptions.models import SubscriptionStatus

        subscribe(self.user, self.plan)
        Subscription.objects.filter(user=self.user).update(paddle_subscription_id="sub_9")
        payload = {
            "event_id": "evt_cancel",
            "event_type": "subscription.canceled",
            "data": {"id": "sub_9"},
        }
        self._post(payload)
        sub = Subscription.objects.get(user=self.user)
        self.assertEqual(sub.status, SubscriptionStatus.CANCELED)

    def test_unknown_price_is_ignored_gracefully(self):
        payload = self._txn(event_id="evt_x")
        payload["data"]["items"] = [{"price": {"id": "pri_unknown"}}]
        res = self._post(payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "unmatched")


class AtomicReserveTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("spender")
        services.set_points(self.user, 0)

    def test_full_hold_when_the_wallet_covers_it(self):
        services.set_points(self.user, 50)
        self.assertEqual(services.reserve_points(self.user, 20_000), 20_000)
        self.assertEqual(services.points_milli(self.user), 30_000)

    def test_hold_shrinks_to_what_is_left(self):
        services.set_points(self.user, 5)
        self.assertEqual(services.reserve_points(self.user, 20_000), 5_000)
        self.assertEqual(services.points_milli(self.user), 0)
        self.assertIsNone(services.reserve_points(self.user, 20_000))  # nothing left

    def test_below_one_point_holds_nothing(self):
        from subscriptions.models import CreditWallet

        CreditWallet.objects.filter(user=self.user).update(milli_points=999)
        self.assertIsNone(services.reserve_points(self.user, 20_000))
        self.assertEqual(services.points_milli(self.user), 999)

    def test_settle_returns_the_unused_hold(self):
        services.set_points(self.user, 50)
        held = services.reserve_points(self.user, 20_000)
        services.settle_points(self.user, held, 12_000)
        self.assertEqual(services.points_milli(self.user), 38_000)

    def test_settle_takes_the_excess_but_never_below_zero(self):
        services.set_points(self.user, 5)
        held = services.reserve_points(self.user, 20_000)  # 5000 held, 0 left
        services.settle_points(self.user, held, 9_000)
        self.assertEqual(services.points_milli(self.user), 0)

    def test_concurrent_holds_never_exceed_the_wallet(self):
        # Sequential stand-in for a race: each hold is one conditional UPDATE.
        services.set_points(self.user, 45)
        held = [services.reserve_points(self.user, 20_000) for _ in range(4)]
        self.assertEqual(held, [20_000, 20_000, 5_000, None])
        self.assertEqual(services.points_milli(self.user), 0)


class AdminPointsExemptionTests(APITestCase):
    """Staff/superusers never spend points — their usage is free testing."""

    def setUp(self):
        self.admin, self.admin_key = make_tenant("boss", is_staff=True)

    def test_admin_is_exempt(self):
        self.assertTrue(services.is_points_exempt(self.admin))
        self.assertTrue(services.has_points(self.admin))

    def test_hold_and_charge_do_not_touch_admin_wallet(self):
        before = services.points_milli(self.admin)
        self.assertEqual(services.reserve_points(self.admin, 20_000), 0)
        services.charge_points(self.admin, 5_000)
        services.settle_points(self.admin, 0, 7_000)
        self.assertEqual(services.points_milli(self.admin), before)

    def test_admin_never_blocked_even_with_empty_wallet(self):
        services.set_points(self.admin, 0)
        self.assertTrue(services.has_points(self.admin))
        quota.check_chat_allowed(self.admin)  # no 402 raise
        quota.check_points(self.admin)

    def test_my_subscription_shows_exempt(self):
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["points"]["exempt"])
        self.assertFalse(res.data["points"]["exhausted"])


class AddPointsValidationTests(APITestCase):
    """POST /subscriptions/add-points/ input validation branches (admin only)."""

    def setUp(self):
        self.admin, self.admin_key = make_tenant("points_boss", is_staff=True)
        self.user, _ = make_tenant("points_target")
        self.url = reverse("subscription-add-points")

    def _post(self, body):
        return self.client.post(self.url, body, format="json", headers={"x-api-key": self.admin_key})

    def test_non_admin_forbidden(self):
        _u, key = make_tenant("not_admin")
        res = self.client.post(
            self.url, {"user": str(self.user.pk), "amount": 10}, format="json", headers={"x-api-key": key}
        )
        self.assertEqual(res.status_code, 403)

    def test_non_integer_amount_returns_400(self):
        self.assertEqual(self._post({"user": str(self.user.pk), "amount": "lots"}).status_code, 400)

    def test_non_positive_amount_returns_400(self):
        self.assertEqual(self._post({"user": str(self.user.pk), "amount": 0}).status_code, 400)

    def test_unknown_user_returns_404(self):
        res = self._post({"user": "00000000-0000-0000-0000-000000000000", "amount": 10})
        self.assertEqual(res.status_code, 404)


@override_settings(PADDLE_WEBHOOK_SECRET=_WH_SECRET)
class PaddleWebhookEdgeTests(APITestCase):
    """Signature-verified but malformed / failing payloads."""

    def setUp(self):
        self.url = reverse("paddle-webhook")

    def test_invalid_json_body_returns_400(self):
        raw = b"{not valid json"
        sig = _sign(raw)
        res = self.client.post(self.url, data=raw, content_type="application/json", headers={"paddle-signature": sig})
        self.assertEqual(res.status_code, 400)

    def test_processing_error_is_logged_and_200(self):
        import json as _json

        payload = {"event_id": "evt_boom", "event_type": "transaction.completed", "data": {}}
        raw = _json.dumps(payload).encode()
        sig = _sign(raw)
        with mock.patch("subscriptions.paddle.process_event", side_effect=RuntimeError("kaboom")):
            res = self.client.post(
                self.url, data=raw, content_type="application/json", headers={"paddle-signature": sig}
            )
        # 200 so Paddle doesn't hammer retries on a bug we've already logged.
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "error-logged")


class PlanVisibilityTests(APITestCase):
    """Non-admins see only active plans; admins see all and can write."""

    def setUp(self):
        self.user, self.key = make_tenant("browser")
        self.admin, self.admin_key = make_tenant("plan_boss", is_staff=True)
        make_plan(slug="live", is_active=True)
        make_plan(slug="hidden", is_active=False)

    @staticmethod
    def _slugs(res):
        rows = res.data["results"] if isinstance(res.data, dict) and "results" in res.data else res.data
        return {p["slug"] for p in rows}

    def test_non_admin_sees_only_active(self):
        res = self.client.get(reverse("plan-list"), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        slugs = self._slugs(res)
        self.assertIn("live", slugs)
        self.assertNotIn("hidden", slugs)

    def test_admin_sees_all(self):
        res = self.client.get(reverse("plan-list"), headers={"x-api-key": self.admin_key})
        self.assertIn("hidden", self._slugs(res))

    def test_non_admin_cannot_create_plan(self):
        res = self.client.post(
            reverse("plan-list"),
            {"name": "X", "slug": "x", "points": 10},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 403)


class MonthlyFreeRenewalTests(APITestCase):
    """Free-tier points reset each calendar month (no rollover)."""

    def setUp(self):
        self.user, _ = make_tenant("monthly")
        self.wallet = services.get_wallet(self.user)  # created with 300

    def _backdate_grant(self):
        """Pretend the last grant happened last month."""
        from subscriptions.models import CreditWallet

        last_month = timezone.now().replace(day=1) - timedelta(days=1)
        CreditWallet.objects.filter(pk=self.wallet.pk).update(last_free_grant_at=last_month)

    def test_new_month_resets_to_free_points(self):
        services.set_points(self.user, 0)  # spent everything
        self._backdate_grant()
        # First access in the new month renews the grant.
        self.assertEqual(services.points_balance(self.user), 300)
        self.assertTrue(services.has_points(self.user))

    def test_no_double_grant_in_same_month(self):
        services.set_points(self.user, 0)
        self._backdate_grant()
        self.assertEqual(services.points_balance(self.user), 300)  # renewed
        services.charge_points(self.user, 40_000)  # 300 -> 260
        self.assertEqual(services.points_balance(self.user), 260)  # NOT re-topped

    def test_unused_points_do_not_roll_over(self):
        services.add_points(self.user, 500)  # 800
        self._backdate_grant()
        self.assertEqual(services.points_balance(self.user), 300)

    def test_subscribed_user_gets_no_free_renewal(self):
        subscribe(self.user, make_plan(slug="paidplan", points=0))
        services.set_points(self.user, 0)
        self._backdate_grant()
        self.assertEqual(services.points_balance(self.user), 0)

    def test_lapsed_subscription_gets_no_free_renewal(self):
        # A lapsed plan is not the free tier: it waits for the admin to renew.
        subscribe(self.user, make_plan(slug="lapsed", points=1000), days=-1)
        services.set_points(self.user, 0)
        self._backdate_grant()
        self.assertEqual(services.points_balance(self.user), 0)


class PlanPointsApiTests(APITestCase):
    """Admins define a plan by its points (no questions/credits derivation)."""

    def setUp(self):
        self.admin, self.admin_key = make_tenant("plan_admin", is_staff=True)

    def _create(self, body):
        return self.client.post(reverse("plan-list"), body, format="json", headers={"x-api-key": self.admin_key})

    def test_create_with_points(self):
        res = self._create(
            {
                "name": "Pro 20",
                "price_usd": "20.00",
                "points": 5000,
                "max_documents": 10,
                "max_total_mb": 20,
                "is_active": True,
                "allow_api_sync": False,
            }
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["points"], 5000)
        self.assertEqual(res.data["monthly_questions"], 0)  # optional extra cap, off
        self.assertEqual(res.data["monthly_token_cap"], 0)
        self.assertNotIn("included_credits", res.data)
        self.assertNotIn("questions", res.data)

    def test_update_points(self):
        plan = make_plan(slug="resize", points=1000)
        res = self.client.patch(
            reverse("plan-detail", args=[plan.id]),
            {"points": 2500},
            format="json",
            headers={"x-api-key": self.admin_key},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["points"], 2500)

    def test_negative_points_rejected(self):
        res = self._create({"name": "Bad", "points": -5})
        self.assertEqual(res.status_code, 400)


class RenewSubscriptionTests(APITestCase):
    """The admin renews by hand -- only once the period ended or the points ran out."""

    def setUp(self):
        self.admin, self.admin_key = make_tenant("renew_admin", is_staff=True)
        self.user, self.key = make_tenant("renew_user")
        self.plan = make_plan(slug="renewable", points=5000)
        self.sub = services.assign_plan(self.user, self.plan, duration_days=30)

    def _renew(self, body=None, key=None):
        return self.client.post(
            reverse("subscription-renew", args=[self.sub.pk]),
            body or {},
            format="json",
            headers={"x-api-key": key or self.admin_key},
        )

    def test_running_subscription_with_points_is_not_renewable(self):
        res = self._renew()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "not_renewable")
        self.assertIsNone(services.renewal_reason(self.sub))

    def test_renew_after_the_points_run_out(self):
        services.set_points(self.user, 0)
        self.assertEqual(services.renewal_reason(self.sub), "points_exhausted")
        res = self._renew()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(services.points_balance(self.user), 5000)
        self.assertTrue(res.data["is_current"])
        self.assertFalse(res.data["can_renew"])
        self.assertEqual(res.data["points_balance"], 5000)

    def test_renew_after_the_period_ends(self):
        Subscription.objects.filter(pk=self.sub.pk).update(
            current_period_start=timezone.now() - timedelta(days=31),
            current_period_end=timezone.now() - timedelta(days=1),
        )
        services.charge_points(self.user, 1_000_000)  # 4000 left, unused
        self.sub.refresh_from_db()
        self.assertEqual(services.renewal_reason(self.sub), "expired")
        res = self._renew()
        self.assertEqual(res.status_code, 200)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.is_current)
        # A new 30-day period (as long as the last one), points reset, no rollover.
        self.assertEqual((self.sub.current_period_end - self.sub.current_period_start).days, 30)
        self.assertEqual(services.points_balance(self.user), 5000)

    def test_renew_with_a_custom_duration(self):
        services.set_points(self.user, 0)
        self._renew({"duration_days": 90})
        self.sub.refresh_from_db()
        self.assertEqual((self.sub.current_period_end - self.sub.current_period_start).days, 90)

    def test_canceled_subscription_is_renewable(self):
        Subscription.objects.filter(pk=self.sub.pk).update(status="canceled")
        self.sub.refresh_from_db()
        self.assertEqual(services.renewal_reason(self.sub), "expired")
        self.assertEqual(self._renew().status_code, 200)

    def test_non_admin_cannot_renew(self):
        services.set_points(self.user, 0)
        self.assertEqual(self._renew(key=self.key).status_code, 403)
        self.assertEqual(services.points_balance(self.user), 0)

    def test_list_shows_renewal_state(self):
        services.set_points(self.user, 0)
        res = self.client.get(reverse("subscription-list"), headers={"x-api-key": self.admin_key})
        row = next(r for r in res.data["results"] if r["id"] == str(self.sub.pk))
        self.assertTrue(row["can_renew"])
        self.assertEqual(row["renewal_reason"], "points_exhausted")
        self.assertEqual(row["points_balance"], 0)
        self.assertEqual(row["points_allowance"], 5000)

    def test_expired_account_is_blocked_until_renewed(self):
        Subscription.objects.filter(pk=self.sub.pk).update(current_period_end=timezone.now() - timedelta(days=1))
        self.user.refresh_from_db()  # drop the cached subscription, as a new request would
        with self.assertRaises(quota.SubscriptionInactive):
            quota.check_points(self.user)  # points left, but the period ended
        res = self.client.get(reverse("my-subscription"), headers={"x-api-key": self.key})
        self.assertEqual(res.data["renewal_reason"], "expired")
        self._renew()
        self.user.refresh_from_db()
        quota.check_points(self.user)  # no raise


class PointsHistoryTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("history_user")
        self.other, _ = make_tenant("history_other")
        self.admin, self.admin_key = make_tenant("history_admin", is_staff=True)
        UsageRecord.objects.create(user=self.user, kind=UsageKind.CHAT, tokens_in=100, points_milli=12_345)
        UsageRecord.objects.create(user=self.user, kind=UsageKind.DOCUMENT, tokens_in=900, points_milli=117)
        UsageRecord.objects.create(user=self.other, kind=UsageKind.CHAT, points_milli=9_000)

    def _get(self, key, **params):
        return self.client.get(reverse("points-history"), params, headers={"x-api-key": key})

    def test_own_history_in_points(self):
        res = self._get(self.key)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 2)
        self.assertEqual({r["points"] for r in res.data["results"]}, {12.345, 0.117})

    def test_filter_by_kind(self):
        res = self._get(self.key, kind="document")
        self.assertEqual([r["kind"] for r in res.data["results"]], ["document"])

    def test_tenant_cannot_read_another_tenant(self):
        self.assertEqual(self._get(self.key, user=str(self.other.pk)).status_code, 403)

    def test_admin_reads_any_tenant(self):
        res = self._get(self.admin_key, user=str(self.other.pk))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 1)
