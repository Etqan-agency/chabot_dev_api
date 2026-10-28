"""Asking which one, instead of guessing one.

When the tenant's knowledge holds the same detail -- a general manager, a price,
an address -- for several organisations, a question that names none of them has
no single right answer. Picking the closest match reads as a confident fact and
is wrong half the time, so the bot asks which one is meant. That turn is a
handled turn: it must not be flagged uncertain, and it must not be logged as a
knowledge gap, because nothing is missing from the data.
"""

from __future__ import annotations

import re
from unittest import mock

from django.test import TestCase

from knowledge.services import rag
from knowledge.services.rag import _extract_status, answer_question

from .factories import FakeLLM, make_hit, make_tenant

# Two agencies, each with its own general manager -- the case from production.
WAS = make_hit("وكالة الأنباء السعودية (واس) — المدير العام: حسن بن محمد الأسمري")
YUNA = make_hit("اتحاد وكالات أنباء منظمة التعاون الإسلامي (يونا) — المدير العام: محمد بن عبد ربه اليامي")


class ExtractStatusTests(TestCase):
    def test_clarify_is_parsed_and_stripped(self):
        self.assertEqual(_extract_status("أي مدير عام تقصد؟\n[[STATUS:clarify]]"), ("أي مدير عام تقصد؟", "clarify"))

    def test_the_other_statuses_still_parse(self):
        self.assertEqual(_extract_status("a [[STATUS:answered]]"), ("a", "answered"))
        self.assertEqual(_extract_status("b [[STATUS:gap]]"), ("b", "gap"))
        self.assertEqual(_extract_status("c [[STATUS:offtopic]]"), ("c", "offtopic"))
        self.assertEqual(_extract_status("plain"), ("plain", None))


class ClarifyStatusTests(TestCase):
    def setUp(self):
        self.user, _ = make_tenant("disambig")

    def _run(self, llm, *, question="مين المدير العام", hits=(WAS, YUNA), history=None):
        def fake_search(q, *, top_k=None, threshold=None, user=None):
            return list(hits)

        with (
            mock.patch.object(rag, "search_chunks", side_effect=fake_search),
            mock.patch.object(rag, "get_backend", return_value=llm),
        ):
            return answer_question(question, history=history, user=self.user, language="ar")

    def test_a_clarifying_turn_is_confident_not_unsure(self):
        # The bot did the right thing with what it had. Flagging it "not sure"
        # would put an apology on a correct reply.
        res = self._run(FakeLLM(text="أي مدير عام تقصد — واس أم يونا؟\n[[STATUS:clarify]]"))
        self.assertEqual(res.answer_status, "clarify")
        self.assertTrue(res.confident)
        self.assertEqual(res.answer, "أي مدير عام تقصد — واس أم يونا؟")
        self.assertNotIn("STATUS", res.answer)

    def test_a_clarifying_turn_is_never_a_knowledge_gap(self):
        # "gap" is what feeds the unanswered-questions queue for a human to fill.
        # Nothing is missing here, so this must not land there.
        res = self._run(FakeLLM(text="أي مدير عام تقصد — واس أم يونا؟\n[[STATUS:clarify]]"))
        self.assertNotEqual(res.answer_status, "gap")

    def test_answering_one_of_them_is_still_a_normal_answered_turn(self):
        # The rule must not turn every answer into a question: when the model
        # does answer, nothing about that path changes.
        res = self._run(FakeLLM(text="المدير العام لواس هو حسن الأسمري.\n[[STATUS:answered]]"))
        self.assertEqual(res.answer_status, "answered")
        self.assertTrue(res.confident)

    def test_a_real_gap_is_still_captured_as_one(self):
        res = self._run(FakeLLM(text="لا تتوفر لدي هذه المعلومة.\n[[STATUS:gap]]"))
        self.assertEqual(res.answer_status, "gap")
        self.assertFalse(res.confident)


class PromptRuleTests(TestCase):
    """The rule has to reach the model, and has to stay conditional."""

    def setUp(self):
        self.user, _ = make_tenant("prompt_rule")

    def _prompt_for(self, question, *, history=None, hits=(WAS, YUNA)):
        captured = {}
        llm = FakeLLM(text="x\n[[STATUS:answered]]")
        original = llm.complete_with_usage

        def spy(system, user_prompt, **kw):
            captured["system"] = system
            captured["user"] = user_prompt
            return original(system, user_prompt, **kw)

        llm.complete_with_usage = spy
        with (
            mock.patch.object(rag, "search_chunks", side_effect=lambda q, **k: list(hits)),
            mock.patch.object(rag, "get_backend", return_value=llm),
        ):
            answer_question(question, history=history, user=self.user, language="ar")
        return captured["user"]

    def test_the_disambiguation_rule_is_in_the_prompt(self):
        prompt = self._prompt_for("مين المدير العام")
        self.assertIn("WHICH ONE DO THEY MEAN", prompt)
        self.assertIn("do NOT pick one", prompt)

    def test_the_rule_covers_the_first_message_not_only_follow_ups(self):
        # The production failure was on the opening question, which the old
        # wording -- scoped to follow-ups -- never covered.
        prompt = self._prompt_for("مين المدير العام")
        self.assertIn("FIRST message", prompt)

    def test_the_rule_is_conditional_on_the_data_holding_several(self):
        # Without this the bot would interrogate customers about things its
        # knowledge only covers once.
        prompt = self._prompt_for("مين المدير العام")
        self.assertIn("only ask when the text really does hold the detail for several", prompt)
        self.assertIn("if the customer already named it", prompt)

    def test_the_clarify_tag_is_offered_in_the_status_menu(self):
        prompt = self._prompt_for("مين المدير العام")
        self.assertIn("[[STATUS:clarify]]", prompt)

    def test_every_offered_tag_is_one_the_parser_accepts(self):
        # A tag the prompt offers but the regex rejects would be left visible in
        # the reply and silently misclassify the turn.
        prompt = self._prompt_for("مين المدير العام")
        offered = set(re.findall(r"\[\[STATUS:(\w+)\]\]", prompt))
        self.assertEqual(offered, {"answered", "clarify", "gap", "offtopic"})
        for tag in offered:
            self.assertEqual(_extract_status(f"reply [[STATUS:{tag}]]")[1], tag)
