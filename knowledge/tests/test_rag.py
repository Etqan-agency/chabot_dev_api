"""answer_question pipeline logic — every branch, no OpenAI calls."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from knowledge.models import ChatbotConfig
from knowledge.services import rag
from knowledge.services.rag import answer_question, detect_dialect

from .factories import FakeLLM, make_hit, make_tenant


class AnswerQuestionTests(TestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")

    def _run(self, *, hits, fallback_hits=None, llm=None):
        """Patch retrieval + LLM. search_chunks returns `hits` on the strict
        call and `fallback_hits` on the threshold=0 fallback call."""
        llm = llm or FakeLLM()
        results = [hits]
        if fallback_hits is not None:
            results.append(fallback_hits)

        def fake_search(question, *, top_k=None, threshold=None, user=None):
            return results.pop(0) if results else []

        with (
            mock.patch.object(rag, "search_chunks", side_effect=fake_search),
            mock.patch.object(rag, "get_backend", return_value=llm),
        ):
            return answer_question("ما هي الخدمة؟", user=self.user)

    def test_followup_retrieval_query_is_context_augmented(self):
        # A bare follow-up ("مين قبله") gets recent conversation context prepended
        # DETERMINISTICALLY (no LLM) so retrieval has the referent — while the
        # original words stay intact (never weakening a query that already worked).
        captured = {}

        def fake_search(question, *, top_k=None, threshold=None, user=None):
            captured["q"] = question
            return [make_hit("عيسى خيره روبله 2017-2019")]

        history = [
            {"role": "user", "content": "مين المدير الحالي"},
            {"role": "assistant", "content": "محمد بن عبد ربه اليامي"},
        ]
        with (
            mock.patch.object(rag, "search_chunks", side_effect=fake_search),
            mock.patch.object(rag, "get_backend", return_value=FakeLLM()),
        ):
            answer_question("مين قبله", history=history, user=self.user)
        # The referent from history ("اليامي") AND the original words are present.
        self.assertIn("اليامي", captured["q"])
        self.assertIn("مين قبله", captured["q"])

    def test_no_history_keeps_raw_query(self):
        captured = {}

        def fake_search(question, *, top_k=None, threshold=None, user=None):
            captured["q"] = question
            return [make_hit("x")]

        with (
            mock.patch.object(rag, "search_chunks", side_effect=fake_search),
            mock.patch.object(rag, "get_backend", return_value=FakeLLM()),
        ):
            answer_question("ما هي الخدمة؟", user=self.user)
        self.assertEqual(captured["q"], "ما هي الخدمة؟")  # unchanged without history

    def test_strong_query_terms_are_preserved_with_history(self):
        # Regression guard: the explicit "مين قبل اليامي" must keep its exact words
        # in the retrieval query (the old LLM-rewrite corrupted them → 0.08 scores).
        captured = {}

        def fake_search(question, *, top_k=None, threshold=None, user=None):
            captured["q"] = question
            return [make_hit("x")]

        history = [{"role": "assistant", "content": "محمد بن عبد ربه اليامي"}]
        with (
            mock.patch.object(rag, "search_chunks", side_effect=fake_search),
            mock.patch.object(rag, "get_backend", return_value=FakeLLM()),
        ):
            answer_question("مين قبل اليامي", history=history, user=self.user)
        self.assertIn("مين قبل اليامي", captured["q"])

    def test_confident_when_strict_hits(self):
        res = self._run(hits=[make_hit("we sell software")])
        self.assertTrue(res.confident)
        self.assertEqual(res.answer, "fake answer")
        self.assertEqual(res.prompt_tokens, 100)
        self.assertEqual(res.completion_tokens, 20)
        self.assertEqual(res.model, "gpt-4o")

    def test_not_confident_on_fallback(self):
        # Strict search empty, fallback finds nearest chunks.
        res = self._run(hits=[], fallback_hits=[make_hit("maybe relevant")])
        self.assertFalse(res.confident)
        self.assertEqual(res.answer, "fake answer")

    def test_no_data_handoff_when_empty(self):
        # Both strict and fallback empty → no-data prompt path, still answers.
        llm = FakeLLM(text="hi, I'm here to help")
        res = self._run(hits=[], fallback_hits=[], llm=llm)
        self.assertFalse(res.confident)
        self.assertEqual(res.answer, "hi, I'm here to help")

    def test_static_no_answer_message(self):
        ChatbotConfig.objects.update_or_create(user=self.user, defaults={"no_answer_message": "Sorry, no info."})
        # No LLM call should be needed; provide one that would fail if used.
        res = self._run(hits=[], fallback_hits=[])
        self.assertEqual(res.answer, "Sorry, no info.")
        self.assertFalse(res.confident)

    def test_sources_are_returned(self):
        res = self._run(hits=[make_hit("a", filename="x.txt"), make_hit("b")])
        self.assertEqual(len(res.sources), 2)
        self.assertIn("filename", res.sources[0])

    def test_status_gap_forces_not_confident_and_is_stripped(self):
        # Strict hits (would be confident) but the model says it's an in-domain
        # question with no answer in the data → gap: not confident, tag stripped.
        llm = FakeLLM(text="I don't have that information.\n[[STATUS:gap]]")
        res = self._run(hits=[make_hit("on-topic chunk")], llm=llm)
        self.assertFalse(res.confident)
        self.assertEqual(res.answer_status, "gap")
        self.assertNotIn("STATUS", res.answer)
        self.assertEqual(res.answer, "I don't have that information.")

    def test_status_answered_is_confident_and_stripped(self):
        llm = FakeLLM(text="We sell software.\n[[STATUS:answered]]")
        res = self._run(hits=[make_hit("we sell software")], llm=llm)
        self.assertTrue(res.confident)
        self.assertEqual(res.answer_status, "answered")
        self.assertEqual(res.answer, "We sell software.")

    def test_status_answered_on_below_threshold_fallback_is_confident(self):
        # A match below the strict threshold (fallback only), still answered from
        # the data → confident, NOT logged as a gap.
        llm = FakeLLM(text="Our CEO is X.\n[[STATUS:answered]]")
        res = self._run(hits=[], fallback_hits=[make_hit("CEO is X")], llm=llm)
        self.assertTrue(res.confident)
        self.assertEqual(res.answer_status, "answered")

    def test_status_offtopic_not_confident_not_a_gap(self):
        llm = FakeLLM(text="Hello! How can I help?\n[[STATUS:offtopic]]")
        res = self._run(hits=[make_hit("chunk")], llm=llm)
        self.assertFalse(res.confident)
        self.assertEqual(res.answer_status, "offtopic")

    def test_missing_tag_keeps_retrieval_confidence(self):
        # No tag → behavior falls back to strict retrieval.
        res = self._run(hits=[make_hit("we sell software")])
        self.assertTrue(res.confident)


class ExtractStatusTests(TestCase):
    def test_parses_and_strips(self):
        from knowledge.services.rag import _extract_status

        self.assertEqual(_extract_status("ans\n[[STATUS:gap]]"), ("ans", "gap"))
        self.assertEqual(_extract_status("ans [[STATUS:answered]]"), ("ans", "answered"))
        self.assertEqual(_extract_status("hi [[STATUS:offtopic]]"), ("hi", "offtopic"))
        self.assertEqual(_extract_status("plain answer"), ("plain answer", None))


class IsolationGuardTests(TestCase):
    """answer_question must NEVER search the shared table without a tenant."""

    def test_anonymous_user_never_searches(self):
        llm = FakeLLM(text="generic greeting")
        with (
            mock.patch.object(rag, "search_chunks") as search,
            mock.patch.object(rag, "get_backend", return_value=llm),
        ):
            res = answer_question("who are you?", user=None)
        search.assert_not_called()  # the critical assertion
        self.assertFalse(res.confident)

    def test_unauthenticated_user_never_searches(self):
        class Anon:
            is_authenticated = False

        llm = FakeLLM()
        with (
            mock.patch.object(rag, "search_chunks") as search,
            mock.patch.object(rag, "get_backend", return_value=llm),
        ):
            answer_question("hello", user=Anon())
        search.assert_not_called()


class DetectDialectTests(TestCase):
    def test_non_arabic_returns_language(self):
        self.assertEqual(detect_dialect("hello", "en"), "en")

    def test_egyptian_markers(self):
        self.assertEqual(detect_dialect("انت فين يا جماعة", "ar"), "ar-eg")

    def test_plain_arabic_defaults(self):
        self.assertEqual(detect_dialect("مرحبا", "ar"), "ar")
