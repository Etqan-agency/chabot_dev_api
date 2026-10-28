"""rebuild_api_chunks — the archive can rebuild the index with no source call."""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from knowledge.models import APISourceRecord, DocumentChunk, SourceType, UploadedDocument
from knowledge.services import api_content_processor as proc_mod
from knowledge.services.api_content_processor import APIContentRAGProcessor

FAKE_VECTOR = [0.1, 0.2, 0.3]


def _embeds(texts):
    return [FAKE_VECTOR for _ in texts], "fake-embed"


class RebuildApiChunksTests(TestCase):
    def _sync(self, items):
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            APIContentRAGProcessor(
                document_name="API Content",
                api_url="https://x.com/i/",
            ).process_items(items, ai_analysis=False)

    def test_rebuilds_chunks_from_the_archive_without_refetching(self):
        items = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
        self._sync(items)

        # Simulate losing the index only — the archive stays.
        DocumentChunk.objects.all().delete()
        self.assertEqual(APISourceRecord.objects.count(), 2)

        out = StringIO()
        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            call_command("rebuild_api_chunks", "API Content", stdout=out)

        self.assertEqual(DocumentChunk.objects.count(), 2)
        self.assertIn("no source API call", out.getvalue())
        # The rebuilt chunks carry the original records' ids and content.
        rebuilt = {c.source_id: c.content for c in DocumentChunk.objects.all()}
        self.assertEqual(set(rebuilt), {"1", "2"})
        # Rebuilt through the same structuring pass as a live sync, so the chunk
        # is labelled text rather than raw JSON -- with the value still intact.
        self.assertIn("title: A", rebuilt["1"])

    def test_accepts_a_document_uuid(self):
        self._sync([{"id": 1, "title": "A"}])
        doc = UploadedDocument.objects.get(filename="API Content")
        DocumentChunk.objects.all().delete()

        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            call_command("rebuild_api_chunks", str(doc.id), stdout=StringIO())
        self.assertEqual(DocumentChunk.objects.count(), 1)

    def test_unknown_document_is_reported(self):
        with self.assertRaises(CommandError):
            call_command("rebuild_api_chunks", "Nope", stdout=StringIO())

    def test_document_with_no_archive_is_reported(self):
        UploadedDocument.objects.create(filename="Legacy API", source_type=SourceType.API)
        with self.assertRaises(CommandError) as ctx:
            call_command("rebuild_api_chunks", "Legacy API", stdout=StringIO())
        self.assertIn("no archived records", str(ctx.exception))
