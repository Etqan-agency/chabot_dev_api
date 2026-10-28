"""analyze_records() — enrichment that can fail without ever costing a record."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, override_settings

from knowledge.services import api_analysis
from knowledge.services.api_analysis import MAX_PROMPT_CHARS, analyze_records
from knowledge.services.llm import LLMError, LLMResult


class _FakeBackend:
    model = "fake-llm"

    def __init__(self, text="described", fail_for=()):
        self.text = text
        self.fail_for = fail_for
        self.seen: list[str] = []

    def complete_with_usage(self, system, user, *, temperature=0):  # noqa: ARG002
        self.seen.append(user)
        for needle in self.fail_for:
            if needle in user:
                raise RuntimeError("provider blew up")
        return LLMResult(text=self.text, model=self.model, prompt_tokens=7, completion_tokens=3)


def _patch_backend(backend):
    return mock.patch("knowledge.services.llm.get_backend", return_value=backend)


@override_settings(API_SYNC_AI_ANALYSIS=True, API_SYNC_ANALYSIS_WORKERS=1)
class AnalyzeRecordsTests(SimpleTestCase):
    def test_describes_each_record(self):
        backend = _FakeBackend()
        with _patch_backend(backend):
            out = analyze_records({"1": {"id": 1}, "2": {"id": 2}})
        self.assertEqual(set(out), {"1", "2"})
        self.assertEqual(out["1"].text, "described")
        self.assertEqual(out["1"].model, "fake-llm")
        self.assertEqual(out["1"].prompt_tokens, 7)

    def test_no_llm_configured_is_a_quiet_skip_not_a_failure(self):
        with mock.patch("knowledge.services.llm.get_backend", side_effect=LLMError("no key")):
            self.assertEqual(analyze_records({"1": {"id": 1}}), {})

    def test_one_bad_record_does_not_take_down_the_others(self):
        backend = _FakeBackend(fail_for=('"id": 2',))
        with _patch_backend(backend):
            out = analyze_records({"1": {"id": 1}, "2": {"id": 2}, "3": {"id": 3}})
        self.assertEqual(set(out), {"1", "3"})

    def test_blank_completion_is_treated_as_no_analysis(self):
        with _patch_backend(_FakeBackend(text="   ")):
            self.assertEqual(analyze_records({"1": {"id": 1}}), {})

    def test_oversized_record_is_truncated_in_the_prompt_only(self):
        backend = _FakeBackend()
        big = {"id": 1, "blob": "x" * (MAX_PROMPT_CHARS * 2)}
        with _patch_backend(backend):
            analyze_records({"1": big})
        prompt = backend.seen[0]
        self.assertLess(len(prompt), MAX_PROMPT_CHARS + 200)
        self.assertIn("truncated for analysis", prompt)
        # The record itself is untouched — truncation is prompt-side only.
        self.assertEqual(len(big["blob"]), MAX_PROMPT_CHARS * 2)

    def test_empty_input_never_calls_the_provider(self):
        with mock.patch("knowledge.services.llm.get_backend", side_effect=AssertionError("must not be called")):
            self.assertEqual(analyze_records({}), {})

    @override_settings(API_SYNC_AI_ANALYSIS=False)
    def test_disabled_by_setting(self):
        with mock.patch("knowledge.services.llm.get_backend", side_effect=AssertionError("must not be called")):
            self.assertEqual(analyze_records({"1": {"id": 1}}), {})
        self.assertFalse(api_analysis.is_enabled())

    @override_settings(API_SYNC_ANALYSIS_WORKERS=4)
    def test_records_are_analysed_concurrently(self):
        backend = _FakeBackend()
        with _patch_backend(backend):
            out = analyze_records({str(i): {"id": i} for i in range(8)})
        self.assertEqual(len(out), 8)

    def test_unserialisable_record_still_gets_a_prompt(self):
        backend = _FakeBackend()
        with _patch_backend(backend):
            out = analyze_records({"1": {"id": 1, "when": object()}})
        self.assertEqual(set(out), {"1"})
