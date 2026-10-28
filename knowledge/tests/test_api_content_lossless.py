"""The sync must never lose a record — not to an embedding outage, not to a
failed AI call, not to a record that renders as empty text.

These tests pin the guarantee: every fetched record lands in ``APISourceRecord``
verbatim, before anything that can fail, and the AI analysis is only ever added
next to that raw copy.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, TestCase

from knowledge.models import APISourceRecord, DocumentChunk
from knowledge.services import api_content_processor as proc_mod
from knowledge.services.api_content_processor import (
    ANALYSIS_HEADING,
    APIContentProcessingError,
    APIContentRAGProcessor,
    _with_analysis,
    extract_all,
)

FAKE_VECTOR = [0.1, 0.2, 0.3]


def _embeds(texts):
    return [FAKE_VECTOR for _ in texts], "fake-embed"


class ExtractAllTests(SimpleTestCase):
    def test_every_page_contributes_its_items_and_its_envelope(self):
        pages = [
            {"count": 3, "next": "https://x.com/i/?p=2", "results": [{"id": 1}, {"id": 2}]},
            {"count": 3, "next": None, "results": [{"id": 3}]},
        ]
        records, page_of = extract_all(pages)
        ids = [r.get("id") for r in records if "id" in r]
        self.assertEqual(ids, [1, 2, 3])

        # One metadata record per page, each tagged so page 2 cannot overwrite page 1.
        metas = [r["response_metadata"] for r in records if "response_metadata" in r]
        self.assertEqual([m["_page"] for m in metas], [1, 2])
        self.assertEqual(page_of["3"], 2)

    def test_single_page_metadata_is_left_untagged(self):
        records, _ = extract_all([{"count": 1, "results": [{"id": 1}]}])
        self.assertEqual(records[1], {"response_metadata": {"count": 1}})


class WithAnalysisTests(SimpleTestCase):
    def test_analysis_is_appended_never_substituted(self):
        out = _with_analysis('{"id": 1}', "A product record.")
        self.assertIn('{"id": 1}', out)
        self.assertIn(ANALYSIS_HEADING, out)
        self.assertIn("A product record.", out)

    def test_no_analysis_leaves_the_raw_text_exactly_as_is(self):
        self.assertEqual(_with_analysis('{"id": 1}', ""), '{"id": 1}')


class LosslessSyncTests(TestCase):
    def _processor(self):
        return APIContentRAGProcessor(document_name="API Content", api_url="https://x.com/i/")

    def test_every_record_is_archived_verbatim(self):
        items = [
            {"id": 1, "title": "A", "nested": {"deep": [1, 2, {"x": None}]}},
            {"id": 2, "title": "B", "price": 0},
            {"response_metadata": {"count": 2, "_page": 1}},
        ]
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            stats = self._processor().process_items(items, ai_analysis=False)

        self.assertEqual(stats["items_stored"], 3)
        stored = {r.source_id: r.raw for r in APISourceRecord.objects.all()}
        self.assertEqual(len(stored), 3)
        # Byte-for-byte the objects we were handed, nesting and nulls intact.
        self.assertEqual(stored["1"], items[0])
        self.assertEqual(stored["2"], items[1])

    def test_embedding_failure_still_leaves_the_records_stored(self):
        items = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

        def boom(texts):
            raise RuntimeError("embedding provider down")

        with mock.patch.object(proc_mod, "_embed_in_batches", boom):
            with self.assertRaises(APIContentProcessingError):
                self._processor().process_items(items, ai_analysis=False)

        # The whole point: the sync failed, the data did not vanish.
        self.assertEqual(APISourceRecord.objects.count(), 2)
        self.assertEqual(DocumentChunk.objects.count(), 0)

        # And a later, healthy sync backfills the chunks from the same records.
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            stats = self._processor().process_items(items, ai_analysis=False)
        self.assertEqual(stats["chunks_created"], 2)
        self.assertEqual(APISourceRecord.objects.count(), 2)

    def test_a_record_with_no_text_is_still_archived(self):
        items = [{"id": 1, "title": "A"}, {"id": 2}]
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            self._processor().process_items(
                items,
                extract_text_fn=lambda i: i.get("title", ""),  # item 2 renders empty
                ai_analysis=False,
            )
        self.assertEqual(set(APISourceRecord.objects.values_list("source_id", flat=True)), {"1", "2"})
        self.assertEqual(DocumentChunk.objects.count(), 1)  # only item 1 is indexable

    def test_records_gone_from_the_source_are_dropped_from_the_archive(self):
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            self._processor().process_items([{"id": 1}, {"id": 2}], ai_analysis=False)
            self._processor().process_items([{"id": 1}], ai_analysis=False)
        self.assertEqual(list(APISourceRecord.objects.values_list("source_id", flat=True)), ["1"])

    def test_unchanged_records_are_not_rearchived(self):
        items = [{"id": 1, "title": "A"}]
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            self._processor().process_items(items, ai_analysis=False)
            stats = self._processor().process_items(items, ai_analysis=False)
        self.assertEqual(stats["items_stored"], 0)
        self.assertEqual(stats["items_unchanged"], 1)

    def test_a_changed_record_replaces_its_archived_copy(self):
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            self._processor().process_items([{"id": 1, "title": "A"}], ai_analysis=False)
            self._processor().process_items([{"id": 1, "title": "B"}], ai_analysis=False)
        record = APISourceRecord.objects.get(source_id="1")
        self.assertEqual(record.raw["title"], "B")
        self.assertEqual(APISourceRecord.objects.count(), 1)


class AnalysisSyncTests(TestCase):
    def _processor(self):
        return APIContentRAGProcessor(document_name="API Content")

    @staticmethod
    def _fake_analysis(text="Product A, priced 10."):
        def run(raws, model=None):  # noqa: ARG001
            from knowledge.services.api_analysis import AnalysisResult

            return {
                sid: AnalysisResult(text=text, model="fake-llm", prompt_tokens=10, completion_tokens=5) for sid in raws
            }

        return run

    def test_analysis_is_stored_and_indexed_alongside_the_raw_record(self):
        items = [{"id": 1, "name": "A", "price": 10}]
        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", self._fake_analysis()),
        ):
            stats = self._processor().process_items(items, ai_analysis=True)

        self.assertEqual(stats["items_analyzed"], 1)
        record = APISourceRecord.objects.get(source_id="1")
        self.assertEqual(record.analysis, "Product A, priced 10.")
        self.assertEqual(record.analysis_model, "fake-llm")
        self.assertIsNotNone(record.analyzed_at)
        self.assertEqual(record.raw, items[0])  # raw untouched by the AI pass

        chunk_text = "".join(DocumentChunk.objects.values_list("content", flat=True))
        # The record is indexed as labelled text rather than as raw JSON, but
        # every field it carried is still retrievable verbatim from the chunk.
        self.assertIn("price: 10", chunk_text)  # exact field still retrievable
        self.assertIn("name: A", chunk_text)
        self.assertIn("Product A, priced 10.", chunk_text)  # prose too

    def test_a_failed_analysis_never_costs_the_record(self):
        items = [{"id": 1, "name": "A"}]
        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", lambda raws, model=None: {}),
        ):
            stats = self._processor().process_items(items, ai_analysis=True)

        self.assertEqual(stats["items_analyzed"], 0)
        self.assertEqual(stats["analysis_errors"], 1)
        self.assertEqual(APISourceRecord.objects.get(source_id="1").raw, items[0])
        self.assertEqual(DocumentChunk.objects.count(), 1)  # still indexed, raw only

    def test_a_record_left_unanalysed_is_retried_on_the_next_sync(self):
        items = [{"id": 1, "name": "A"}]
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            # First sync: the LLM is down.
            with mock.patch.object(proc_mod.api_analysis, "analyze_records", lambda raws, model=None: {}):
                self._processor().process_items(items, ai_analysis=True)
            self.assertEqual(APISourceRecord.objects.get(source_id="1").analysis, "")

            # Second sync: same unchanged data, LLM back — it gets analysed.
            with mock.patch.object(proc_mod.api_analysis, "analyze_records", self._fake_analysis("Now described.")):
                stats = self._processor().process_items(items, ai_analysis=True)

        self.assertEqual(stats["items_analyzed"], 1)
        self.assertEqual(APISourceRecord.objects.get(source_id="1").analysis, "Now described.")
        # The new prose changed the indexed text, so it was re-embedded.
        self.assertEqual(stats["items_updated"], 1)

    def test_an_analysed_record_is_not_reanalysed_while_it_is_unchanged(self):
        items = [{"id": 1, "name": "A"}]
        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", self._fake_analysis()) as _,
        ):
            self._processor().process_items(items, ai_analysis=True)

        calls = []

        def counting(raws, model=None):  # noqa: ARG001
            calls.append(set(raws))
            return {}

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", counting),
        ):
            stats = self._processor().process_items(items, ai_analysis=True)

        self.assertEqual(calls, [])  # the LLM is not called at all
        self.assertEqual(stats["items_unchanged"], 1)

    def test_stale_analysis_is_dropped_when_the_record_changes(self):
        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", self._fake_analysis("Describes A.")),
        ):
            self._processor().process_items([{"id": 1, "name": "A"}], ai_analysis=True)

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", self._fake_analysis("Describes B.")),
        ):
            self._processor().process_items([{"id": 1, "name": "B"}], ai_analysis=True)

        record = APISourceRecord.objects.get(source_id="1")
        self.assertEqual(record.raw["name"], "B")
        self.assertEqual(record.analysis, "Describes B.")

    def test_ai_analysis_can_be_turned_off(self):
        def explode(raws, model=None):  # noqa: ARG001
            raise AssertionError("analysis must not run when disabled")

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", explode),
        ):
            stats = self._processor().process_items([{"id": 1}], ai_analysis=False)
        self.assertEqual(stats["items_analyzed"], 0)


class FullRefreshTests(TestCase):
    """A re-index must never throw away what the archive already paid for."""

    def _processor(self):
        return APIContentRAGProcessor(document_name="API Content")

    def test_full_refresh_reindexes_without_discarding_stored_analyses(self):
        from knowledge.services.api_analysis import AnalysisResult

        items = [{"id": 1, "name": "A"}]

        def analysed(raws, model=None):  # noqa: ARG001
            return {sid: AnalysisResult(text="Describes A.", model="fake-llm") for sid in raws}

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", analysed),
        ):
            self._processor().process_items(items, ai_analysis=True)

        def explode(raws, model=None):  # noqa: ARG001
            raise AssertionError("must not re-analyse an unchanged record")

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", explode),
        ):
            stats = self._processor().process_items(items, full_refresh=True, ai_analysis=True)

        record = APISourceRecord.objects.get(source_id="1")
        self.assertEqual(record.analysis, "Describes A.")  # survived the refresh
        self.assertEqual(stats["chunks_created"], 1)  # but the index was rebuilt
        self.assertIn("Describes A.", DocumentChunk.objects.first().content)

    def test_full_refresh_still_drops_records_gone_from_the_source(self):
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            self._processor().process_items([{"id": 1}, {"id": 2}], ai_analysis=False)
            self._processor().process_items([{"id": 1}], full_refresh=True, ai_analysis=False)
        self.assertEqual(list(APISourceRecord.objects.values_list("source_id", flat=True)), ["1"])


class PageTrackingTests(TestCase):
    def test_each_record_is_archived_with_the_page_it_came_from(self):
        pages = [
            {"count": 3, "next": "https://x.com/i/?p=2", "results": [{"id": 1}, {"id": 2}]},
            {"count": 3, "next": None, "results": [{"id": 3}]},
        ]
        records, page_of = extract_all(pages)
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            APIContentRAGProcessor(document_name="API Content").process_items(
                records, ai_analysis=False, pages=page_of
            )

        stored = {r.source_id: r.page for r in APISourceRecord.objects.all()}
        self.assertEqual(stored["1"], 1)
        self.assertEqual(stored["3"], 2)
        # Both envelope records are archived, each on its own page — page 2's
        # metadata does not overwrite page 1's.
        meta_pages = sorted(page for sid, page in stored.items() if sid not in {"1", "2", "3"})
        self.assertEqual(meta_pages, [1, 2])
