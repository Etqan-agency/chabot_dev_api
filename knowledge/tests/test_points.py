"""Points: every OpenAI call is charged to its tenant at the real cost, work is
refused (402) without points, and a renewal resumes what stopped."""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import AvailableLanguage, QuestionTreeNode, UsageKind, UsageRecord
from knowledge.services import billing
from knowledge.services import guided_tree as gt
from knowledge.services.embeddings import EmbeddingError, OpenAIEmbeddingBackend
from knowledge.services.llm import OpenAIBackend
from knowledge.services.rag import AnswerResult
from subscriptions import services
from subscriptions.models import Plan, Subscription
from WhatsApp.models import WhatsAppAccount
from WhatsApp.services import conversation

from .factories import make_document, make_tenant


class FakeEmbedder:
    """Like the OpenAI backend, meters each call: 1000 tokens of
    text-embedding-3-large = $0.00013 = 130 milli-points."""

    model = "text-embedding-3-large"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        billing.meter(self.model, 1000)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def embedder():
    return mock.patch("knowledge.services.embeddings.get_backend", return_value=FakeEmbedder())


def make_plan(slug="plan", points=1000, **kw):
    return Plan.objects.create(name=slug.title(), slug=slug, points=points, **kw)


class BackendMeteringTests(TestCase):
    """The real OpenAI backends report every call to the current scope."""

    def setUp(self):
        self.user, _ = make_tenant("metered")

    def test_llm_backend_meters_its_call(self):
        backend = OpenAIBackend.__new__(OpenAIBackend)
        backend.model = "gpt-4o"
        backend._client = mock.Mock()
        backend._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
        )
        with billing.charge_to(self.user, UsageKind.CHAT) as scope:
            backend.complete("system", "user")
        # 1000 * 2.5 / 1M + 100 * 10 / 1M = $0.0035 = 3.5 points
        self.assertEqual(scope.milli, 3500)
        self.assertEqual(services.points_milli(self.user), 300_000 - 3500)

    def test_embedding_backend_meters_its_call(self):
        backend = OpenAIEmbeddingBackend.__new__(OpenAIEmbeddingBackend)
        backend.model = "text-embedding-3-large"
        backend._client = mock.Mock()
        backend._client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1])], usage=SimpleNamespace(prompt_tokens=10_000)
        )
        with billing.charge_to(self.user, UsageKind.DOCUMENT) as scope:
            backend.embed(["text"])
        self.assertEqual(scope.milli, 1300)  # 10k * $0.13 / 1M = $0.0013

    def test_a_call_outside_any_scope_is_not_charged(self):
        billing.meter("gpt-4o", 1000, 1000)
        self.assertEqual(services.points_milli(self.user), 300_000)
        self.assertFalse(UsageRecord.objects.exists())

    def test_one_record_per_operation(self):
        with billing.charge_to(self.user, UsageKind.DOCUMENT):
            for _ in range(3):
                billing.meter("text-embedding-3-large", 1000)
        rec = UsageRecord.objects.get(user=self.user)
        self.assertEqual((rec.kind, rec.tokens_in, rec.points_milli), (UsageKind.DOCUMENT, 3000, 390))

    def test_worker_threads_charge_the_scope_without_the_database(self):
        def work():
            billing.meter("gpt-4o-mini", 1000, 1000)  # 750 milli

        with billing.charge_to(self.user, UsageKind.TRANSLATION) as scope:
            threads = [threading.Thread(target=billing.bind(work)) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(scope.pending, 3000)  # not taken yet: workers don't write
        self.assertEqual(services.points_milli(self.user), 300_000 - 3000)  # taken on exit

    def test_staff_usage_is_recorded_but_free(self):
        admin, _ = make_tenant("metered_admin", is_staff=True)
        with billing.charge_to(admin, UsageKind.CHAT):
            billing.meter("gpt-4o", 1000, 100)
        rec = UsageRecord.objects.get(user=admin)
        self.assertEqual((rec.tokens_in, rec.points_milli), (1000, 0))


@override_settings(INGESTION_MODE="sync")
class DocumentPointsTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("doc_points")

    def _upload(self):
        f = SimpleUploadedFile("note.txt", b"hello world " * 50, content_type="text/plain")
        return self.client.post(
            reverse("document-list"), {"file": f}, format="multipart", headers={"x-api-key": self.key}
        )

    def test_indexing_a_document_is_charged_to_its_owner(self):
        with embedder():
            res = self._upload()
        self.assertEqual(res.status_code, 201)
        rec = UsageRecord.objects.get(user=self.user)
        self.assertEqual((rec.kind, rec.points_milli), (UsageKind.DOCUMENT, 130))
        self.assertEqual(services.points_milli(self.user), 300_000 - 130)

    def test_upload_without_points_is_refused(self):
        services.set_points(self.user, 0)
        with embedder() as get_backend:
            res = self._upload()
        self.assertEqual(res.status_code, 402)
        self.assertIn("points", res.data["detail"])
        get_backend.assert_not_called()

    def test_upload_on_a_lapsed_plan_is_refused(self):
        plan = make_plan(points=5000, max_documents=10, max_total_mb=10)
        services.assign_plan(self.user, plan)
        Subscription.objects.filter(user=self.user).update(status="expired")
        self.user.refresh_from_db()
        with embedder():
            self.assertEqual(self._upload().status_code, 402)

    def test_reindex_without_points_is_refused(self):
        doc = make_document(self.user)
        services.set_points(self.user, 0)
        res = self.client.post(reverse("document-reindex", args=[doc.pk]), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 402)

    def test_a_document_stops_mid_way_when_the_points_run_out(self):
        from knowledge.models import DocumentStatus
        from knowledge.services import chunking

        services.set_points(self.user, 1)  # the first batch (0.13) runs; 0.87 left is not enough
        doc = make_document(self.user)
        with (
            embedder(),
            mock.patch.object(chunking, "_read_document", return_value="word " * 10_000),
            mock.patch.object(chunking, "EMBED_BATCH", 1),
        ):
            with self.assertRaises(EmbeddingError):
                chunking.ingest_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, DocumentStatus.FAILED)
        self.assertIn("Out of points", doc.error_message)
        self.assertEqual(services.points_milli(self.user), 1000 - 130)  # the batch that ran

    def test_knowledge_edit_is_charged_and_refused_without_points(self):
        doc = make_document(self.user)
        url = reverse("knowledge-list")
        with embedder():
            res = self.client.post(
                url, {"document": str(doc.pk), "content": "new fact"}, format="json", headers={"x-api-key": self.key}
            )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(UsageRecord.objects.get(user=self.user).kind, UsageKind.KNOWLEDGE)
        services.set_points(self.user, 0)
        with embedder():
            res = self.client.post(
                url, {"document": str(doc.pk), "content": "another"}, format="json", headers={"x-api-key": self.key}
            )
        self.assertEqual(res.status_code, 402)


class ApiSyncPointsTests(APITestCase):
    def test_import_without_points_is_refused(self):
        user, key = make_tenant("sync_points")
        services.assign_plan(user, make_plan(points=5000, allow_api_sync=True))
        services.set_points(user, 0)
        res = self.client.post(
            reverse("sync-api-content"),
            {"api_url": "https://example.com/api"},
            format="json",
            headers={"x-api-key": key},
        )
        self.assertEqual(res.status_code, 402)


class MeteringLLM:
    """A translation LLM that meters every call like the real backend
    (gpt-4o-mini, 1000 in + 1000 out = $0.00075 = 750 milli-points)."""

    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def complete(self, system, user, *, temperature=0):
        with self.lock:
            self.calls += 1
        billing.meter("gpt-4o-mini", 1000, 1000)
        lang = re.search(r"code '([^']+)'", system).group(1)
        if "JSON object" in system:
            return json.dumps({k: f"[{lang}] {v}" for k, v in json.loads(user).items()}, ensure_ascii=False)
        return f"[{lang}] {user}"


@override_settings(
    GUIDED_TREE_TRANSLATE_MODE="sync",
    GUIDED_TREE_CANONICAL_LANGUAGE="ar",
    GUIDED_TREE_TRANSLATE_BATCH_ITEMS=1,
    GUIDED_TREE_TRANSLATE_CONCURRENCY=1,
)
class TranslationPointsTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("tree_points")
        cache.clear()
        with mock.patch.object(gt, "schedule_sync"):
            root = gt.create_node(owner=self.user, title="root")
            for i in range(3):
                gt.create_node(owner=self.user, parent=root, title=f"child {i}")
        AvailableLanguage.objects.create(owner=self.user, code="en", name="English")

    def translated(self):
        return QuestionTreeNode.objects.filter(owner=self.user, language="en").exclude(source_hash="").count()

    def test_translation_is_charged(self):
        llm = MeteringLLM()
        with mock.patch("knowledge.services.llm.get_backend", return_value=llm):
            gt.sync_owner(self.user.pk)
        self.assertEqual(self.translated(), 4)
        rec = UsageRecord.objects.get(user=self.user)
        self.assertEqual((rec.kind, rec.points_milli), (UsageKind.TRANSLATION, 4 * 750))
        self.assertEqual(services.points_milli(self.user), 300_000 - 3000)

    def test_translation_pauses_when_the_points_run_out_and_resumes_on_renewal(self):
        services.set_points(self.user, 1)  # one batch (0.75) fits, then < 1 point is left
        llm = MeteringLLM()
        with mock.patch("knowledge.services.llm.get_backend", return_value=llm):
            gt.sync_owner(self.user.pk)
            self.assertEqual(llm.calls, 1)
            self.assertEqual(self.translated(), 1)  # the rest kept the source text, blank hash
            self.assertEqual(services.points_milli(self.user), 250)

            res = self.client.get(reverse("tree-language-list"), headers={"x-api-key": self.key})
            en = next(row for row in res.data if row["code"] == "en")
            self.assertTrue(en["paused"])

            # The admin puts the tenant on a plan: the paused nodes are translated.
            services.assign_plan(self.user, make_plan(points=5000))
        self.assertEqual(self.translated(), 4)
        self.assertEqual(services.points_milli(self.user), 5_000_000 - 3 * 750)

    def test_adding_a_language_without_points_is_refused(self):
        services.set_points(self.user, 0)
        res = self.client.post(
            reverse("tree-language-list"),
            {"code": "fr", "name": "French"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 402)
        self.assertFalse(AvailableLanguage.objects.filter(code="fr").exists())

    def test_retranslate_without_points_is_refused(self):
        services.set_points(self.user, 0)
        res = self.client.post(reverse("guided-tree-retranslate"), {}, format="json", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 402)


class WhatsAppPointsTests(TestCase):
    def setUp(self):
        self.tenant, _ = make_tenant("wa_points")
        WhatsAppAccount.objects.create(tenant=self.tenant, phone_number_id="PN1")

    def _ask(self, answer):
        msg = {"message_text": "what do you sell?", "from_number": "201000000001", "phone_number_id": "PN1"}
        with mock.patch.object(conversation, "answer_question", side_effect=answer) as aq:
            reply = conversation.handle_incoming_message(msg).reply
        return reply, aq

    @staticmethod
    def _metered_answer(*args, **kwargs):
        billing.meter("gpt-4o", 120, 30)  # 0.6 points
        return AnswerResult(answer="we sell tea", source="rag", model="gpt-4o")

    def test_a_whatsapp_answer_is_charged_like_the_chat(self):
        reply, _ = self._ask(self._metered_answer)
        self.assertEqual(reply, "we sell tea")
        self.assertEqual(services.points_milli(self.tenant), 300_000 - 600)
        rec = UsageRecord.objects.get(user=self.tenant)
        self.assertEqual((rec.kind, rec.points_milli), (UsageKind.CHAT, 600))

    def test_no_points_no_answer(self):
        services.set_points(self.tenant, 0)
        reply, aq = self._ask(self._metered_answer)
        self.assertEqual(reply, conversation.UNAVAILABLE_MESSAGE)
        aq.assert_not_called()

    def test_suspended_tenant_gets_no_answer(self):
        from knowledge.models import TenantQuota

        TenantQuota.objects.update_or_create(user=self.tenant, defaults={"is_suspended": True})
        reply, aq = self._ask(self._metered_answer)
        self.assertEqual(reply, conversation.UNAVAILABLE_MESSAGE)
        aq.assert_not_called()


class AnalyticsPointsTests(APITestCase):
    def test_only_chat_answers_count_as_requests(self):
        user, key = make_tenant("analytics_points")
        UsageRecord.objects.create(
            user=user, kind=UsageKind.CHAT, tokens_in=100, response_time_ms=1000, points_milli=12_000
        )
        UsageRecord.objects.create(user=user, kind=UsageKind.DOCUMENT, tokens_in=900, points_milli=117)
        res = self.client.get(reverse("analytics-usage"), headers={"x-api-key": key})
        totals = res.data["totals"]
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(totals["avg_response_ms"], 1000)
        self.assertEqual(totals["tokens_in"], 1000)  # tokens cover every operation
        self.assertEqual(totals["points_used"], 12.117)
