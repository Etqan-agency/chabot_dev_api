"""Structuring, validation and staged promotion for API ingestion.

The load-bearing claim of this pipeline is that a record can be reshaped for
retrieval *without* losing anything, and that the AI layer cannot turn a
reshaping into an invention. These tests are where that claim is checked --
including against the real payload shape that motivated the work.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from knowledge.models import APIIngestionRun, APISourceRecord, DocumentChunk
from knowledge.services import api_content_processor as proc_mod
from knowledge.services import api_schema, api_validation
from knowledge.services.api_analysis import AnalysisResult, parse_completion
from knowledge.services.api_content_processor import (
    APIContentRAGProcessor,
    IngestionBlocked,
    source_statistics,
)

FAKE_VECTOR = [0.1, 0.2, 0.3]


def _embeds(texts):
    return [FAKE_VECTOR for _ in texts], "fake-embed"


PRODUCT = {
    "id": 83,
    "title": "Microsoft Office 365 Personal | License",
    "title_ar": "مايكروسوفت اوفيس 365 برسونال | كود",
    "slug": "microsoft-office-365-personal-key",
    "sku": "M365_Personal",
    "base_price": "140000.00",
    "compare_at_price": None,
    "short_description": "",
    "quantity_available": 97,
    "is_active": True,
    "brand": {"id": 1, "name": "Microsoft", "slug": "microsoft"},
    "images": [
        {"id": 153, "image": "https://cdn.example.com/a.webp", "alt_text": "", "is_primary": True},
        {"id": 405, "image": "https://cdn.example.com/b.webp", "alt_text": "", "is_primary": False},
    ],
    "created_at": "2026-06-02T00:07:19.881977Z",
}


# ---------------------------------------------------------------------------
# Structuring: every field survives, and keeps its meaning
# ---------------------------------------------------------------------------
class StructureRecordTests(SimpleTestCase):
    def _structured(self, record=None, **kwargs):
        record = PRODUCT if record is None else record
        return api_schema.structure_record(record, "83", **kwargs)

    def test_every_source_leaf_is_classified_and_none_is_dropped(self):
        rec = self._structured()
        self.assertEqual(set(rec.fields), api_schema.leaf_paths(PRODUCT))
        self.assertEqual(set(rec.roles), set(rec.fields))
        self.assertEqual(rec.leaf_count, len(api_schema.leaf_paths(PRODUCT)))

    def test_nested_objects_arrays_and_scalars_all_flatten(self):
        rec = self._structured()
        self.assertEqual(rec.fields["brand.name"], "Microsoft")
        self.assertEqual(rec.fields["images[1].id"], 405)
        self.assertEqual(rec.fields["base_price"], "140000.00")

    def test_null_and_empty_values_are_preserved_not_skipped(self):
        rec = self._structured()
        self.assertIsNone(rec.fields["compare_at_price"])
        self.assertEqual(rec.fields["short_description"], "")
        text = api_schema.render_rag_text(rec)
        # The absence is stated explicitly -- this is what stops a model from
        # inferring a discount that the record never mentioned.
        self.assertIn("compare at price: (null)", text)
        self.assertIn("short description: (empty)", text)

    def test_empty_containers_are_leaves_of_their_own(self):
        rec = self._structured({"id": 1, "tags": [], "meta": {}})
        self.assertEqual(rec.fields["tags"], [])
        self.assertEqual(rec.fields["meta"], {})
        self.assertIn("(none)", api_schema.render_rag_text(rec))

    def test_prices_keep_their_exact_source_spelling(self):
        rec = self._structured()
        self.assertEqual(rec.fields["base_price"], "140000.00")
        self.assertIn("140000.00", api_schema.render_rag_text(rec))

    def test_nesting_becomes_an_explicit_relationship(self):
        rec = self._structured()
        brand = next(link for link in rec.relationships if link["path"] == "brand")
        self.assertEqual(brand["label"], "Microsoft")
        self.assertEqual(brand["related_id"], "1")
        self.assertIn("-> brand -> Microsoft", api_schema.render_rag_text(rec))

    def test_media_entries_are_summarised_rather_than_listed_as_relationships(self):
        rec = self._structured()
        self.assertEqual(len(rec.attachments), 2)
        text = api_schema.render_rag_text(rec)
        self.assertIn("Attachments: 2 images", text)
        self.assertNotIn("cdn.example.com", text)

    def test_urls_stay_out_of_the_vector_but_remain_in_the_structured_record(self):
        rec = self._structured()
        text = api_schema.render_rag_text(rec)
        self.assertIn("URL(s) recorded", text)
        self.assertEqual(rec.fields["images[0].image"], "https://cdn.example.com/a.webp")
        self.assertIn(rec.roles["images[0].image"], {api_schema.FieldRole.METADATA.value})

    def test_urls_can_be_inlined_when_the_tenant_wants_them(self):
        text = api_schema.render_rag_text(self._structured(), include_urls=True)
        self.assertIn("https://cdn.example.com/a.webp", text)

    def test_arabic_title_is_indexed_as_an_alias(self):
        text = api_schema.render_rag_text(self._structured())
        self.assertIn("مايكروسوفت اوفيس 365 برسونال", text)

    def test_entity_type_comes_from_the_endpoint_not_from_a_format_flag(self):
        self.assertEqual(api_schema.entity_type_from_url("https://a.co/api/v1/catalog/products/?limit=9"), "product")
        self.assertEqual(api_schema.entity_type_from_url("https://a.co/categories"), "category")
        rec = self._structured({"id": 1, "type": "virtual"}, entity_type_hint="product")
        self.assertEqual(rec.entity_type, "product")

    def test_timestamps_are_normalised_without_changing_the_instant(self):
        rec = self._structured()
        self.assertEqual(rec.fields["created_at"], "2026-06-02T00:07:19.881977+00:00")

    def test_whitespace_is_tidied_but_words_are_not(self):
        rec = self._structured({"id": 1, "name": "  Office   2021  "})
        self.assertEqual(rec.fields["name"], "Office 2021")


class SecretHandlingTests(SimpleTestCase):
    SECRETS = {
        "id": 1,
        "name": "Widget",
        "api_key": "sk-live-abcdef",
        "nested": {"access_token": "tok-123", "public": "fine"},
    }

    def test_credentials_are_redacted_before_anything_else_happens(self):
        clean, redacted = api_schema.sanitize(self.SECRETS)
        self.assertEqual(clean["api_key"], api_schema.REDACTED)
        self.assertEqual(clean["nested"]["access_token"], api_schema.REDACTED)
        self.assertEqual(clean["nested"]["public"], "fine")
        self.assertEqual(set(redacted), {"api_key", "nested.access_token"})

    def test_secrets_never_reach_the_embedded_text(self):
        rec = api_schema.structure_record(self.SECRETS, "1")
        text = api_schema.render_rag_text(rec)
        self.assertNotIn("sk-live-abcdef", text)
        self.assertNotIn("tok-123", text)
        self.assertIn("credential field(s) withheld", text)

    def test_secrets_never_reach_the_ai_prompt(self):
        from knowledge.services.api_analysis import _prompt_for

        prompt = _prompt_for(self.SECRETS)
        self.assertNotIn("sk-live-abcdef", prompt)
        self.assertNotIn("tok-123", prompt)
        self.assertIn("Widget", prompt)


# ---------------------------------------------------------------------------
# Semantic chunking
# ---------------------------------------------------------------------------
class SemanticChunkingTests(SimpleTestCase):
    def test_a_record_that_fits_stays_one_chunk(self):
        rec = api_schema.structure_record(PRODUCT, "83", entity_type_hint="product")
        text = api_schema.render_rag_text(rec)
        self.assertEqual(len(api_schema.semantic_chunks(text, size=4000)), 1)

    def test_a_split_record_repeats_its_identity_in_every_chunk(self):
        rec = api_schema.structure_record(PRODUCT, "83", entity_type_hint="product")
        text = api_schema.render_rag_text(rec)
        header = api_schema.identity_header(rec)
        chunks = api_schema.semantic_chunks(text, header=header, size=260)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertIn("Microsoft Office 365 Personal", chunk)
            self.assertIn("id=83", chunk)

    def test_an_oversized_single_block_is_cut_on_line_boundaries(self):
        body = "\n".join(f"- field {i}: value {i}" for i in range(200))
        chunks = api_schema.semantic_chunks(f"PRODUCT: X\n\n{body}", header="PRODUCT: X", size=400)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        rejoined = "".join(chunks)
        self.assertIn("- field 0: value 0", rejoined)
        self.assertIn("- field 199: value 199", rejoined)

    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(api_schema.semantic_chunks("   "), [])


# ---------------------------------------------------------------------------
# Validation: completeness in, no invention out
# ---------------------------------------------------------------------------
class ValidationTests(SimpleTestCase):
    def _validate(self, record, analysis=""):
        rec = api_schema.structure_record(record, str(record.get("id", "x")), entity_type_hint="product")
        text = api_schema.render_rag_text(rec)
        return api_validation.validate_record(record, rec, text, analysis=analysis)

    def test_a_faithfully_structured_record_validates_clean(self):
        result = self._validate(PRODUCT)
        self.assertTrue(result.ok)
        self.assertEqual(result.coverage, 1.0)
        self.assertEqual(result.missing_paths, [])
        self.assertEqual(result.unrendered_values, [])

    def test_a_dropped_field_is_named_not_averaged_away(self):
        rec = api_schema.structure_record(PRODUCT, "83")
        text = api_schema.render_rag_text(rec)
        del rec.fields["sku"]  # simulate a structuring bug
        result = api_validation.validate_record(PRODUCT, rec, text)
        self.assertFalse(result.ok)
        self.assertIn("sku", result.missing_paths)

    def test_an_analysis_quoting_the_record_is_accepted(self):
        result = self._validate(PRODUCT, analysis="السعر 140000.00 والكمية 97.")
        self.assertFalse(result.analysis_rejected)
        self.assertTrue(result.ok)

    def test_an_analysis_inventing_a_discount_is_rejected(self):
        result = self._validate(PRODUCT, analysis="قبل الخصم 140000.00 وبعد الخصم 39000.")
        self.assertTrue(result.analysis_rejected)
        self.assertEqual(result.invented_numbers, ["39000"])

    def test_number_formatting_differences_are_not_treated_as_invention(self):
        result = self._validate(PRODUCT, analysis="Priced at 140,000 for 97 units.")
        self.assertFalse(result.analysis_rejected)

    def test_arabic_indic_digits_match_their_ascii_source(self):
        result = self._validate(PRODUCT, analysis="الكمية ٩٧ قطعة.")
        self.assertFalse(result.analysis_rejected)

    def test_report_blocks_when_records_could_not_be_structured(self):
        report = api_validation.IngestionReport(records_fetched=10, records_structured=9)
        reason = report.blocking_reason(min_field_coverage=1.0, min_record_coverage=1.0)
        self.assertIn("9/10", reason)

    def test_report_does_not_block_a_clean_run(self):
        report = api_validation.IngestionReport(records_fetched=2, records_structured=2)
        report.add(self._validate(PRODUCT))
        self.assertEqual(report.blocking_reason(min_field_coverage=1.0, min_record_coverage=1.0), "")

    def test_clean_records_are_not_itemised_in_the_report(self):
        report = api_validation.IngestionReport(records_fetched=1, records_structured=1)
        report.add(self._validate(PRODUCT))
        self.assertEqual(report.as_dict()["issues"], [])


class ParseCompletionTests(SimpleTestCase):
    def test_structured_json_is_understood(self):
        prose, data = parse_completion('{"entity_type": "product", "summary": "A license.", "categories": ["office"]}')
        self.assertEqual(prose, "A license.")
        self.assertEqual(data["entity_type"], "product")

    def test_fenced_json_is_understood(self):
        prose, _ = parse_completion('```json\n{"summary": "Fenced."}\n```')
        self.assertEqual(prose, "Fenced.")

    def test_plain_prose_is_still_accepted(self):
        prose, data = parse_completion("Just a description.")
        self.assertEqual(prose, "Just a description.")
        self.assertEqual(data, {})

    def test_malformed_json_falls_back_to_the_raw_text(self):
        prose, data = parse_completion('{"summary": "unclosed')
        self.assertTrue(prose.startswith("{"))
        self.assertEqual(data, {})


class SourceStatisticsTests(SimpleTestCase):
    def test_census_counts_the_payload_as_it_arrived(self):
        stats = source_statistics([PRODUCT, {"id": 9}], endpoint="https://a.co/p", pages_fetched=2)
        self.assertEqual(stats["total_records"], 2)
        self.assertEqual(stats["unique_ids"], 2)
        self.assertEqual(stats["pages_fetched"], 2)
        self.assertEqual(stats["total_fields"], len(api_schema.leaf_paths(PRODUCT)) + 1)
        self.assertEqual(stats["null_values"], 1)  # compare_at_price
        self.assertEqual(stats["empty_values"], 3)  # short_description + two alt_text
        self.assertGreater(stats["response_bytes"], 0)


# ---------------------------------------------------------------------------
# End to end through the processor
# ---------------------------------------------------------------------------
@override_settings(API_SYNC_AI_ANALYSIS=False)
class ProcessorPipelineTests(TestCase):
    def _processor(self, **kwargs):
        kwargs.setdefault("document_name", "API Content")
        kwargs.setdefault("api_url", "https://api.example.com/v1/products/")
        return APIContentRAGProcessor(**kwargs)

    def _sync(self, items, processor=None, **kwargs):
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            return (processor or self._processor()).process_items(items, ai_analysis=False, **kwargs)

    def test_a_product_becomes_exactly_one_traceable_chunk(self):
        stats = self._sync([PRODUCT])
        self.assertEqual(stats["chunks_created"], 1)
        chunk = DocumentChunk.objects.get()
        self.assertEqual(chunk.source_id, "83")
        self.assertEqual(chunk.metadata["entity_type"], "product")
        self.assertEqual(chunk.metadata["entity_id"], "83")
        self.assertEqual(chunk.metadata["chunk_index"], 0)
        self.assertEqual(chunk.metadata["chunk_count"], 1)
        self.assertEqual(chunk.metadata["schema_version"], api_schema.SCHEMA_VERSION)
        self.assertEqual(chunk.metadata["endpoint"], "https://api.example.com/v1/products/")

    def test_the_structured_view_is_stored_next_to_the_raw_record(self):
        self._sync([PRODUCT])
        record = APISourceRecord.objects.get(source_id="83")
        self.assertEqual(record.raw, PRODUCT)  # untouched
        self.assertEqual(record.entity_type, "product")
        self.assertEqual(record.title, "Microsoft Office 365 Personal | License")
        self.assertEqual(record.coverage, 1.0)
        self.assertEqual(set(record.structured["fields"]), api_schema.leaf_paths(PRODUCT))
        self.assertIn("140000.00", record.rag_text)

    def test_stats_report_real_field_coverage(self):
        stats = self._sync([PRODUCT])
        self.assertEqual(stats["fields_total"], len(api_schema.leaf_paths(PRODUCT)))
        self.assertEqual(stats["fields_covered"], stats["fields_total"])
        self.assertEqual(stats["field_coverage"], 1.0)
        self.assertEqual(stats["records_with_issues"], 0)

    def test_every_source_value_is_still_findable_in_the_index(self):
        self._sync([PRODUCT])
        indexed = "\n".join(DocumentChunk.objects.values_list("content", flat=True))
        record = APISourceRecord.objects.get(source_id="83")
        blob = indexed + json.dumps(record.structured, ensure_ascii=False)
        for path, value in api_schema.iter_leaves(PRODUCT):
            if value in (None, "", {}, []) or isinstance(value, bool):
                continue
            # Timestamps are given one internal spelling; the instant is
            # unchanged, and the raw archive still holds the original text.
            expected = api_schema.normalize_scalar(value)
            self.assertIn(str(expected), blob, f"{path} was lost")

    def test_the_raw_archive_keeps_values_exactly_as_the_api_sent_them(self):
        self._sync([PRODUCT])
        record = APISourceRecord.objects.get(source_id="83")
        self.assertEqual(record.raw, PRODUCT)
        self.assertEqual(record.raw["created_at"], "2026-06-02T00:07:19.881977Z")

    # -- incremental synchronisation ------------------------------------
    def test_unchanged_records_are_not_reembedded(self):
        self._sync([PRODUCT])
        stats = self._sync([PRODUCT])
        self.assertEqual(stats["items_unchanged"], 1)
        self.assertEqual(stats["chunks_created"], 0)

    def test_a_changed_record_is_reprocessed(self):
        self._sync([PRODUCT])
        changed = {**PRODUCT, "base_price": "150000.00"}
        stats = self._sync([changed])
        self.assertEqual(stats["items_updated"], 1)
        self.assertIn("150000.00", DocumentChunk.objects.get().content)

    def test_a_new_record_is_added_without_touching_the_others(self):
        self._sync([PRODUCT])
        stats = self._sync([PRODUCT, {"id": 99, "title": "Other"}])
        self.assertEqual(stats["items_new"], 1)
        self.assertEqual(stats["items_unchanged"], 1)
        self.assertEqual(DocumentChunk.objects.count(), 2)

    def test_a_deleted_record_is_removed_from_the_index_and_archive(self):
        self._sync([PRODUCT, {"id": 99, "title": "Other"}])
        stats = self._sync([PRODUCT])
        self.assertEqual(stats["items_removed"], 1)
        self.assertEqual(set(DocumentChunk.objects.values_list("source_id", flat=True)), {"83"})
        self.assertEqual(set(APISourceRecord.objects.values_list("source_id", flat=True)), {"83"})

    def test_identity_follows_the_api_id_not_the_position(self):
        self._sync([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
        stats = self._sync([{"id": 2, "title": "B"}, {"id": 1, "title": "A"}])
        self.assertEqual(stats["items_unchanged"], 2)
        self.assertEqual(stats["chunks_created"], 0)

    def test_duplicate_ids_collapse_to_one_record(self):
        stats = self._sync([{"id": 1, "title": "A"}, {"id": 1, "title": "A"}])
        self.assertEqual(DocumentChunk.objects.filter(source_id="1").count(), 1)
        self.assertEqual(stats["items_new"], 1)

    def test_records_without_an_id_get_a_deterministic_identity(self):
        self._sync([{"title": "No id here"}])
        first = DocumentChunk.objects.get().source_id
        stats = self._sync([{"title": "No id here"}])
        self.assertEqual(stats["items_unchanged"], 1)
        self.assertEqual(DocumentChunk.objects.get().source_id, first)

    # -- failure handling ------------------------------------------------
    def test_a_blocked_ingestion_leaves_the_previous_index_intact(self):
        self._sync([PRODUCT])
        before = {c.id: c.content for c in DocumentChunk.objects.all()}

        original = api_schema.structure_record

        def lossy(record, source_id, **kwargs):
            rec = original(record, source_id, **kwargs)
            rec.fields.pop("sku", None)  # a structuring bug that loses a field
            rec.roles.pop("sku", None)
            return rec

        with (
            mock.patch.object(proc_mod.api_schema, "structure_record", lossy),
            self.assertRaises(IngestionBlocked) as caught,
        ):
            self._sync([{**PRODUCT, "base_price": "1.00"}])

        self.assertIn("source fields were preserved", caught.exception.reason)
        after = {c.id: c.content for c in DocumentChunk.objects.all()}
        self.assertEqual(before, after)  # previous knowledge base untouched

    def test_an_embedding_failure_leaves_the_previous_index_intact(self):
        self._sync([PRODUCT])
        before = set(DocumentChunk.objects.values_list("id", flat=True))

        def boom(texts):
            raise RuntimeError("embedding provider down")

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", boom),
            self.assertRaises(proc_mod.APIContentProcessingError),
        ):
            self._processor().process_items([{**PRODUCT, "base_price": "2.00"}], ai_analysis=False)

        self.assertEqual(set(DocumentChunk.objects.values_list("id", flat=True)), before)

    def test_full_refresh_does_not_delete_before_the_new_index_is_ready(self):
        self._sync([PRODUCT])
        before = set(DocumentChunk.objects.values_list("id", flat=True))

        def boom(texts):
            raise RuntimeError("down")

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", boom),
            self.assertRaises(proc_mod.APIContentProcessingError),
        ):
            self._processor().process_items([PRODUCT], ai_analysis=False, full_refresh=True)

        self.assertEqual(set(DocumentChunk.objects.values_list("id", flat=True)), before)

    def test_full_refresh_rebuilds_when_it_succeeds(self):
        self._sync([PRODUCT])
        stats = self._sync([PRODUCT], full_refresh=True)
        self.assertEqual(stats["chunks_created"], 1)
        self.assertEqual(DocumentChunk.objects.count(), 1)

    def test_a_malformed_record_does_not_take_down_the_sync(self):
        stats = self._sync([PRODUCT, {"id": 7, "weird": {1, 2}}])
        self.assertGreaterEqual(stats["processed"], 1)
        self.assertTrue(DocumentChunk.objects.filter(source_id="83").exists())

    # -- the AI layer ----------------------------------------------------
    @override_settings(API_SYNC_AI_ANALYSIS=True)
    def test_an_inventing_analysis_is_dropped_before_it_is_ever_embedded(self):
        def analyse(raws, **kwargs):
            return {sid: AnalysisResult(text="بعد الخصم يصبح 39000 د.ع.", model="fake-llm", data={}) for sid in raws}

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", analyse),
        ):
            stats = self._processor().process_items([PRODUCT], ai_analysis=True)

        self.assertEqual(stats["analyses_rejected"], 1)
        self.assertNotIn("39000", DocumentChunk.objects.get().content)
        self.assertIn("140000.00", DocumentChunk.objects.get().content)

    @override_settings(API_SYNC_AI_ANALYSIS=True)
    def test_a_grounded_analysis_is_kept_and_indexed(self):
        def analyse(raws, **kwargs):
            return {
                sid: AnalysisResult(
                    text="ترخيص أوفيس 365 برسونال بسعر 140000.00 والكمية 97.",
                    model="fake-llm",
                    data={"entity_type": "license"},
                )
                for sid in raws
            }

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", analyse),
        ):
            stats = self._processor().process_items([PRODUCT], ai_analysis=True)

        self.assertEqual(stats["analyses_rejected"], 0)
        content = DocumentChunk.objects.get().content
        self.assertIn("برسونال", content)
        self.assertIn(api_schema.ANALYSIS_HEADING, content)

    # -- tenant isolation -------------------------------------------------
    def test_two_tenants_never_share_records_chunks_or_runs(self):
        from knowledge.tests.factories import make_tenant

        alice, _ = make_tenant("alice")
        bob, _ = make_tenant("bob")

        self._sync([PRODUCT], processor=self._processor(user=alice))
        self._sync([{"id": 83, "title": "Bob's product"}], processor=self._processor(user=bob))

        alice_chunks = DocumentChunk.objects.filter(document__uploaded_by=alice)
        bob_chunks = DocumentChunk.objects.filter(document__uploaded_by=bob)
        self.assertEqual(alice_chunks.count(), 1)
        self.assertEqual(bob_chunks.count(), 1)
        self.assertIn("Microsoft", alice_chunks.get().content)
        self.assertNotIn("Microsoft", bob_chunks.get().content)
        self.assertEqual(alice_chunks.get().metadata["tenant_id"], str(alice.id))
        self.assertNotEqual(
            APISourceRecord.objects.get(document__uploaded_by=alice).raw,
            APISourceRecord.objects.get(document__uploaded_by=bob).raw,
        )


class IngestionRunTests(TestCase):
    def test_a_run_records_the_real_numbers_and_scrubs_the_query_string(self):
        from knowledge.services.api_ingest_run import IngestionRunRecorder

        recorder = IngestionRunRecorder(endpoint="https://api.example.com/p/?api_key=sk-secret")
        recorder.fetched(pages=2, records=5, truncated=False, source_stats={"total_records": 5})
        recorder.succeeded({"processed": 5, "chunks_created": 5, "fields_total": 40, "fields_covered": 40})

        run = APIIngestionRun.objects.get()
        self.assertEqual(run.endpoint, "https://api.example.com/p/")
        self.assertNotIn("sk-secret", run.endpoint)
        self.assertEqual(run.records_fetched, 5)
        self.assertEqual(run.chunks_created, 5)
        self.assertEqual(run.field_coverage, 1.0)
        self.assertEqual(run.stage, "completed")
        self.assertIsNotNone(run.finished_at)

    def test_a_blocked_run_says_why(self):
        from knowledge.services.api_ingest_run import IngestionRunRecorder

        recorder = IngestionRunRecorder(endpoint="https://api.example.com/p/")
        recorder.blocked("only 9/10 records", {"fields_total": 10, "fields_covered": 9})

        run = APIIngestionRun.objects.get()
        self.assertEqual(run.stage, "blocked")
        self.assertIn("9/10", run.blocked_reason)


@override_settings(API_SYNC_AI_ANALYSIS=True)
class EntityNamingTests(TestCase):
    """The model may name an entity, but never at the cost of reproducibility."""

    def _sync(self, items, *, api_url, entity_type):
        def analyse(raws, **kwargs):
            return {
                sid: AnalysisResult(text="A description.", model="fake-llm", data={"entity_type": entity_type})
                for sid in raws
            }

        with (
            mock.patch.object(proc_mod, "_embed_in_batches", _embeds),
            mock.patch.object(proc_mod.api_analysis, "analyze_records", analyse),
        ):
            APIContentRAGProcessor(document_name="API Content", api_url=api_url).process_items(items, ai_analysis=True)
        return DocumentChunk.objects.get()

    def test_the_endpoint_wins_over_the_model(self):
        chunk = self._sync(
            [{"id": 1, "title": "A"}],
            api_url="https://api.example.com/v1/products/",
            entity_type="gizmo",
        )
        self.assertEqual(chunk.metadata["entity_type"], "product")
        self.assertTrue(chunk.content.startswith("PRODUCT: A"))

    def test_the_model_names_what_the_endpoint_could_not(self):
        chunk = self._sync([{"id": 1, "title": "A"}], api_url="", entity_type="invoice")
        self.assertEqual(chunk.metadata["entity_type"], "invoice")
        # Header and metadata agree: the text was re-rendered, not just relabelled.
        self.assertTrue(chunk.content.startswith("INVOICE: A"))

    def test_the_same_input_produces_the_same_chunk_hash_twice(self):
        first = self._sync(
            [{"id": 1, "title": "A"}],
            api_url="https://api.example.com/v1/products/",
            entity_type="gizmo",
        ).content_hash
        DocumentChunk.objects.all().delete()
        APISourceRecord.objects.all().delete()
        second = self._sync(
            [{"id": 1, "title": "A"}],
            api_url="https://api.example.com/v1/products/",
            entity_type="something-else-entirely",
        ).content_hash
        self.assertEqual(first, second)
