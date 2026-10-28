"""Knowledge-gap capture: normalisation, dedup, chat wiring (gap-only), review API."""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import (
    DocumentChunk,
    UnansweredQuestion,
    UnansweredStatus,
    UploadedDocument,
)
from knowledge.services.unanswered import (
    QA_DOC_FILENAME,
    capture_unanswered,
    normalize_question,
    resolve_to_knowledge,
)
from knowledge.views import chat as chat_view

from .factories import make_tenant

# Patch target for embeddings so resolve tests never call OpenAI.
_EMBED = "knowledge.services.unanswered.embed_one"


def _fake_answer(**kw):
    """An AnswerResult. `answer_status` drives capture: only 'gap' is captured."""
    from knowledge.services.rag import AnswerResult

    base = {"answer": "sorry", "source": "rag", "confident": False, "model": "gpt-4o"}
    base.update(kw)
    return AnswerResult(**base)


class NormalizeTests(APITestCase):
    def test_strips_punctuation_and_case_and_whitespace(self):
        self.assertEqual(normalize_question("  What are your HOURS?? "), "what are your hours")

    def test_variants_collapse_to_same_key(self):
        self.assertEqual(
            normalize_question("Do you ship to Egypt!"),
            normalize_question("do you ship to egypt"),
        )

    def test_empty_is_empty(self):
        self.assertEqual(normalize_question("   "), "")


class CaptureServiceTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")

    def test_question_is_recorded(self):
        obj = capture_unanswered(user=self.user, question="Do you offer refunds?", language="en", reason="in-domain")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.occurrences, 1)
        self.assertEqual(obj.status, UnansweredStatus.NEW)
        self.assertEqual(obj.reason, "in-domain")

    def test_duplicate_bumps_occurrences_not_rows(self):
        capture_unanswered(user=self.user, question="Do you ship to Egypt?")
        capture_unanswered(user=self.user, question="do you ship to egypt")
        self.assertEqual(UnansweredQuestion.objects.count(), 1)
        self.assertEqual(UnansweredQuestion.objects.get().occurrences, 2)

    def test_same_question_isolated_per_tenant(self):
        bob, _ = make_tenant("bob")
        capture_unanswered(user=self.user, question="What is the price?")
        capture_unanswered(user=bob, question="What is the price?")
        self.assertEqual(UnansweredQuestion.objects.filter(user=self.user).count(), 1)
        self.assertEqual(UnansweredQuestion.objects.filter(user=bob).count(), 1)

    def test_blank_question_is_ignored(self):
        obj = capture_unanswered(user=self.user, question="   ")
        self.assertIsNone(obj)


class ResolveToKnowledgeTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")
        self.obj = capture_unanswered(user=self.user, question="Do you ship to Egypt?")

    def test_creates_embedded_chunk_and_marks_answered(self):
        with mock.patch(_EMBED, return_value=([1.0, 0.0], "test-embed")):
            chunk = resolve_to_knowledge(unanswered=self.obj, answer="Yes, 3-5 days.", user=self.user)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, UnansweredStatus.ANSWERED)
        self.assertEqual(chunk.embedding, [1.0, 0.0])
        self.assertEqual(chunk.source_id, f"unanswered:{self.obj.pk}")

    def test_re_resolve_updates_in_place_no_duplicate(self):
        with mock.patch(_EMBED, return_value=([1.0, 0.0], "test-embed")):
            resolve_to_knowledge(unanswered=self.obj, answer="First answer.", user=self.user)
            resolve_to_knowledge(unanswered=self.obj, answer="Better answer.", user=self.user)
        doc = UploadedDocument.objects.get(uploaded_by=self.user, filename=QA_DOC_FILENAME)
        self.assertEqual(doc.chunks.count(), 1)
        self.assertIn("Better answer.", doc.chunks.get().content)

    def test_blank_answer_rejected(self):
        with self.assertRaises(ValueError):
            resolve_to_knowledge(unanswered=self.obj, answer="   ", user=self.user)


class ChatWiringTests(APITestCase):
    """Only an in-domain, unanswered turn (answer_status == 'gap') is captured."""

    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.url = reverse("chat")

    def _post(self, body):
        return self.client.post(self.url, body, format="json", headers={"x-api-key": self.key})

    def test_gap_status_triggers_capture(self):
        answer = _fake_answer(confident=False, answer_status="gap")
        with (
            mock.patch.object(chat_view, "answer_question", return_value=answer),
            mock.patch.object(chat_view, "dispatch_capture") as cap,
        ):
            res = self._post({"question": "an in-domain question we can't answer"})
        self.assertEqual(res.status_code, 200)
        cap.assert_called_once()
        self.assertEqual(cap.call_args.kwargs["question"], "an in-domain question we can't answer")

    def test_answered_does_not_capture(self):
        answer = _fake_answer(confident=True, answer_status="answered")
        with (
            mock.patch.object(chat_view, "answer_question", return_value=answer),
            mock.patch.object(chat_view, "dispatch_capture") as cap,
        ):
            self._post({"question": "known question"})
        cap.assert_not_called()

    def test_offtopic_does_not_capture(self):
        # Not confident, but off-topic (a greeting / unrelated) → NOT a gap.
        answer = _fake_answer(confident=False, answer_status="offtopic")
        with (
            mock.patch.object(chat_view, "answer_question", return_value=answer),
            mock.patch.object(chat_view, "dispatch_capture") as cap,
        ):
            self._post({"question": "hello there"})
        cap.assert_not_called()


class ReviewApiTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.obj = capture_unanswered(user=self.user, question="Do you offer refunds?")

    def _auth(self):
        self.client.credentials(HTTP_X_API_KEY=self.key)

    def test_list_scoped_to_tenant(self):
        bob, bob_key = make_tenant("bob")
        capture_unanswered(user=bob, question="bob only question")
        self._auth()
        res = self.client.get(reverse("unanswered-list"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 1)
        self.assertEqual(res.data["results"][0]["question"], "Do you offer refunds?")

    def test_resolve_action_marks_answered(self):
        self._auth()
        res = self.client.post(reverse("unanswered-resolve", args=[self.obj.pk]))
        self.assertEqual(res.status_code, 200)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, UnansweredStatus.ANSWERED)

    def test_resolve_with_answer_writes_knowledge(self):
        self._auth()
        with mock.patch(_EMBED, return_value=([0.1, 0.2, 0.3], "test-embed")):
            res = self.client.post(
                reverse("unanswered-resolve", args=[self.obj.pk]),
                {"answer": "Yes, refunds within 14 days."},
                format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, UnansweredStatus.ANSWERED)
        doc = UploadedDocument.objects.get(uploaded_by=self.user, filename=QA_DOC_FILENAME)
        chunk = DocumentChunk.objects.get(document=doc)
        self.assertIn("Yes, refunds within 14 days.", chunk.content)
        self.assertIn("Do you offer refunds?", chunk.content)
        self.assertEqual(chunk.embedding, [0.1, 0.2, 0.3])

    def test_dismiss_action_marks_dismissed(self):
        self._auth()
        res = self.client.post(reverse("unanswered-dismiss", args=[self.obj.pk]))
        self.assertEqual(res.status_code, 200)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, UnansweredStatus.DISMISSED)

    def test_requires_auth(self):
        res = self.client.get(reverse("unanswered-list"))
        self.assertEqual(res.status_code, 401)
