"""Chat endpoint: auth, quota gating, metering, validation — mocked internals."""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import TenantQuota, UsageKind, UsageRecord
from knowledge.services import billing
from knowledge.services.rag import AnswerResult
from knowledge.views import chat as chat_view
from subscriptions.models import CreditWallet
from subscriptions.services import points_milli, set_points

from .factories import make_tenant


def _fake_answer(**kw):
    base = {
        "answer": "hello from the team",
        "source": "rag",
        "confident": True,
        "model": "gpt-4o",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "sources": [{"filename": "a.txt", "score": 0.8}],
    }
    base.update(kw)
    return AnswerResult(**base)


def _answering(**kw):
    """A stand-in for answer_question that, like the real LLM backend, meters
    its call (gpt-4o, 120 in + 30 out = $0.0006 = 0.6 points)."""
    result = _fake_answer(**kw)

    def answer(*args, **kwargs):
        billing.meter(result.model, result.prompt_tokens, result.completion_tokens)
        return result

    return answer


class ChatEndpointTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.url = reverse("chat")

    def _post(self, body, key=None):
        return self.client.post(
            self.url,
            body,
            format="json",
            **({"HTTP_X_API_KEY": key} if key else {}),
        )

    def test_requires_auth(self):
        res = self._post({"question": "hi"})
        self.assertEqual(res.status_code, 401)

    def test_happy_path_returns_answer_and_usage(self):
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()):
            res = self._post({"question": "what do you sell?"}, key=self.key)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["answer"], "hello from the team")
        self.assertEqual(res.data["prompt_tokens"], 120)
        self.assertEqual(res.data["completion_tokens"], 30)
        self.assertGreater(res.data["cost_usd"], 0)
        self.assertTrue(res.data["confident"])

    def test_usage_is_recorded(self):
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()):
            self._post({"question": "hi"}, key=self.key)
        rec = UsageRecord.objects.get(user=self.user, kind=UsageKind.CHAT)
        self.assertEqual(rec.tokens_in, 120)
        self.assertEqual(rec.tokens_out, 30)
        self.assertEqual(rec.chunk_count, 1)

    def test_points_charged_at_the_real_cost(self):
        start = points_milli(self.user)  # free grant (300 points)
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()):
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(points_milli(self.user), start - 600)  # 0.6 points, not the 20 held
        self.assertEqual(res.data["points_used"], 0.6)
        self.assertEqual(res.data["points_balance"], 299.4)
        self.assertEqual(UsageRecord.objects.get(user=self.user).points_milli, 600)

    def test_out_of_points_returns_402(self):
        set_points(self.user, 0)  # empty the wallet
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()) as aq:
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 402)
        aq.assert_not_called()  # blocked before the LLM runs

    def test_less_than_one_point_is_out_of_points(self):
        set_points(self.user, 0)  # creates the wallet
        CreditWallet.objects.filter(user=self.user).update(milli_points=999)
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()) as aq:
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 402)
        aq.assert_not_called()

    def test_a_small_balance_still_gets_an_answer(self):
        # Less than the 20-point hold left: the hold shrinks to what is left.
        set_points(self.user, 2)
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()):
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(points_milli(self.user), 2000 - 600)

    def test_an_answer_costing_more_than_the_hold_floors_at_zero(self):
        set_points(self.user, 1)  # 1 point held; the answer costs 0.6 * 10 = 6
        expensive = _answering(prompt_tokens=1200, completion_tokens=300)
        with mock.patch.object(chat_view, "answer_question", side_effect=expensive):
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(points_milli(self.user), 0)

    def test_points_released_when_answer_fails(self):
        # Points are held BEFORE answering; if the LLM fails the user gets them
        # all back (never charged for a reply they didn't get).
        from knowledge.services.rag import RagUnavailable

        start = points_milli(self.user)
        with mock.patch.object(chat_view, "answer_question", side_effect=RagUnavailable("down")):
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(points_milli(self.user), start)  # held then released
        self.assertFalse(UsageRecord.objects.filter(user=self.user).exists())

    def test_unexpected_error_releases_and_propagates_500(self):
        # A non-RagUnavailable crash must release the held points and 500.
        start = points_milli(self.user)
        self.client.raise_request_exception = False  # observe the 500 instead of re-raising
        with mock.patch.object(chat_view, "answer_question", side_effect=ValueError("boom")):
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 500)
        self.assertEqual(points_milli(self.user), start)  # held then released

    def test_staff_are_not_charged(self):
        admin, admin_key = make_tenant("chat_admin", is_staff=True)
        start = points_milli(admin)
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()):
            res = self._post({"question": "hi"}, key=admin_key)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["points_used"], 0)
        self.assertIsNone(res.data["points_balance"])
        self.assertEqual(points_milli(admin), start)
        rec = UsageRecord.objects.get(user=admin)
        self.assertEqual((rec.tokens_in, rec.points_milli), (120, 0))  # metered, not charged

    def test_atomic_reserve_race_loss_returns_402(self):
        # Pre-check passes (has points) but the atomic hold loses the race
        # (a concurrent request spent them first) -> 402, LLM never runs.
        with (
            mock.patch.object(chat_view.quota, "reserve_chat_points", return_value=None),
            mock.patch.object(chat_view, "answer_question", side_effect=_answering()) as aq,
        ):
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 402)
        aq.assert_not_called()

    def test_rate_limit_returns_429(self):
        TenantQuota.objects.update_or_create(user=self.user, defaults={"max_requests_per_min": 1})
        UsageRecord.objects.create(user=self.user, kind=UsageKind.CHAT)
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()) as aq:
            res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 429)
        aq.assert_not_called()  # gated before the LLM runs

    def test_suspended_returns_403(self):
        TenantQuota.objects.update_or_create(user=self.user, defaults={"is_suspended": True})
        res = self._post({"question": "hi"}, key=self.key)
        self.assertEqual(res.status_code, 403)

    def test_blank_question_rejected(self):
        res = self._post({"question": ""}, key=self.key)
        self.assertEqual(res.status_code, 400)

    def test_history_too_long_rejected(self):
        history = [{"role": "user", "content": "x"} for _ in range(50)]
        res = self._post({"question": "hi", "history": history}, key=self.key)
        self.assertEqual(res.status_code, 400)

    def test_valid_history_accepted(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        with mock.patch.object(chat_view, "answer_question", side_effect=_answering()):
            res = self._post({"question": "again", "history": history}, key=self.key)
        self.assertEqual(res.status_code, 200)


class DetectLanguageTests(APITestCase):
    def test_arabic_script_is_deterministic(self):
        # Any Arabic-script text is detected with no dependency on langdetect.
        self.assertEqual(chat_view.detect_language("مرحبا كيف الحال"), "ar")
        self.assertEqual(chat_view.detect_language("بتبيعو اي هنا"), "ar")

    def test_latin_text_is_not_arabic(self):
        # langdetect can mislabel short Latin text, but it must never be "ar".
        self.assertNotEqual(chat_view.detect_language("hello how are you today my friend"), "ar")

    def test_empty_falls_back_without_error(self):
        self.assertIsInstance(chat_view.detect_language(""), str)


class ChatFeedbackTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("fb_user")
        self.url = reverse("chat-feedback")

    def _post(self, body, key=None):
        return self.client.post(
            self.url,
            body,
            format="json",
            **({"HTTP_X_API_KEY": key} if key else {}),
        )

    def test_requires_auth(self):
        self.assertEqual(self._post({"question": "q", "answer": "a", "rating": "up"}).status_code, 401)

    def test_records_feedback(self):
        res = self._post(
            {"question": "do you ship?", "answer": "yes", "rating": "up", "comment": "great"},
            key=self.key,
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["rating"], "up")
        # Scoped to the caller.
        self.assertEqual(str(res.data["user"]), str(self.user.pk))

    def test_invalid_rating_rejected(self):
        res = self._post({"question": "q", "answer": "a", "rating": "meh"}, key=self.key)
        self.assertEqual(res.status_code, 400)
