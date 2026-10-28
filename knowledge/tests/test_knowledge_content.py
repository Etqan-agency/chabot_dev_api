"""Knowledge content CRUD — review and fix what the bot learned, in place.

The load-bearing guarantee here is that a *correction actually takes effect*:
retrieval matches on the embedding, so editing the text without re-embedding
would leave the bot matching the wording you just deleted.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import DocumentChunk, SourceType
from knowledge.services import knowledge_edit
from knowledge.services.embeddings import EmbeddingError

from .factories import make_chunk, make_document, make_tenant

NEW_VECTOR = [0.0, 1.0, 0.0, 0.0]


def _fake_embed(text):
    return NEW_VECTOR, "fake-embed"


class KnowledgeContentTests(APITestCase):
    def setUp(self):
        self.list_url = reverse("knowledge-list")
        self.alice, self.alice_key = make_tenant("k_alice")
        self.bob, self.bob_key = make_tenant("k_bob")

        self.doc = make_document(self.alice, filename="handbook.txt")
        self.chunk = make_chunk(self.doc, position=0, content="Refunds take 30 days.")
        self.other = make_chunk(self.doc, position=1, content="We ship worldwide.")

        self.bob_doc = make_document(self.bob, filename="bob.txt")
        self.bob_chunk = make_chunk(self.bob_doc, content="bob private")

    def _detail(self, chunk):
        return reverse("knowledge-detail", args=[chunk.id])

    def _auth(self, key):
        self.client.credentials(HTTP_X_API_KEY=key)

    # --- auth / isolation --------------------------------------------------
    def test_requires_auth(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_lists_only_the_callers_knowledge(self):
        self._auth(self.alice_key)
        res = self.client.get(self.list_url)
        self.assertEqual(res.status_code, 200)
        contents = {row["content"] for row in res.data["results"]}
        self.assertEqual(contents, {"Refunds take 30 days.", "We ship worldwide."})
        self.assertNotIn("bob private", contents)

    def test_cannot_read_another_tenants_chunk(self):
        self._auth(self.alice_key)
        self.assertEqual(self.client.get(self._detail(self.bob_chunk)).status_code, 404)

    def test_cannot_edit_another_tenants_chunk(self):
        self._auth(self.alice_key)
        res = self.client.patch(self._detail(self.bob_chunk), {"content": "hacked"}, format="json")
        self.assertEqual(res.status_code, 404)
        self.bob_chunk.refresh_from_db()
        self.assertEqual(self.bob_chunk.content, "bob private")

    def test_cannot_delete_another_tenants_chunk(self):
        self._auth(self.alice_key)
        self.assertEqual(self.client.delete(self._detail(self.bob_chunk)).status_code, 404)
        self.assertTrue(DocumentChunk.objects.filter(id=self.bob_chunk.id).exists())

    def test_cannot_attach_knowledge_to_another_tenants_document(self):
        self._auth(self.alice_key)
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            res = self.client.post(
                self.list_url,
                {"document": str(self.bob_doc.id), "content": "injected"},
                format="json",
            )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.bob_doc.chunks.count(), 1)

    # --- read --------------------------------------------------------------
    def test_detail_shows_where_the_knowledge_came_from(self):
        self._auth(self.alice_key)
        res = self.client.get(self._detail(self.chunk))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["document_name"], "handbook.txt")
        self.assertEqual(res.data["source_type"], SourceType.FILE)
        self.assertEqual(res.data["char_count"], len("Refunds take 30 days."))
        self.assertTrue(res.data["is_embedded"])
        self.assertFalse(res.data["was_edited"])

    def test_search_filters_by_content(self):
        self._auth(self.alice_key)
        res = self.client.get(self.list_url, {"search": "ship"})
        self.assertEqual([r["content"] for r in res.data["results"]], ["We ship worldwide."])

    def test_filter_by_document_and_source_type(self):
        api_doc = make_document(self.alice, filename="API Content")
        api_doc.source_type = SourceType.API
        api_doc.save()
        make_chunk(api_doc, content="from the api")

        self._auth(self.alice_key)
        res = self.client.get(self.list_url, {"source_type": "api"})
        self.assertEqual([r["content"] for r in res.data["results"]], ["from the api"])

        res = self.client.get(self.list_url, {"document": str(self.doc.id)})
        self.assertEqual(len(res.data["results"]), 2)

    # --- update: the correction must actually take effect ------------------
    def test_editing_content_reembeds_it(self):
        self._auth(self.alice_key)
        old_vector = self.chunk.embedding
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed) as _:
            res = self.client.patch(
                self._detail(self.chunk),
                {"content": "Refunds take 14 days."},
                format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.content, "Refunds take 14 days.")
        # The embedding moved with the text — otherwise the bot would keep
        # matching (and answering with) the old wording.
        self.assertNotEqual(self.chunk.embedding, old_vector)
        self.assertEqual(self.chunk.embedding, NEW_VECTOR)
        self.assertEqual(self.chunk.embedding_model, "fake-embed")

    def test_editing_marks_the_chunk_as_hand_edited(self):
        self._auth(self.alice_key)
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            res = self.client.patch(self._detail(self.chunk), {"content": "Corrected."}, format="json")
        self.assertTrue(res.data["was_edited"])
        self.chunk.refresh_from_db()
        self.assertIsNotNone(self.chunk.edited_at)
        self.assertTrue(self.chunk.metadata.get("manually_edited"))

    def test_edit_refreshes_the_vector_mirror(self):
        # The bulk backfill only fills NULL columns, so an edit needs its own
        # per-row refresh or pgvector keeps serving the old text's vector.
        self._auth(self.alice_key)
        with (
            mock.patch.object(knowledge_edit, "embed_one", _fake_embed),
            mock.patch("knowledge.services.vector_store.refresh_vector_for_chunk") as refresh,
        ):
            self.client.patch(self._detail(self.chunk), {"content": "Corrected."}, format="json")
        refresh.assert_called_once_with(self.chunk.id)

    def test_a_failing_embedding_leaves_the_old_text_intact(self):
        self._auth(self.alice_key)

        def boom(text):
            raise EmbeddingError("provider down")

        with mock.patch.object(knowledge_edit, "embed_one", boom):
            res = self.client.patch(self._detail(self.chunk), {"content": "New text."}, format="json")
        self.assertEqual(res.status_code, 400)
        self.chunk.refresh_from_db()
        # Not half-applied: the text and its embedding still agree.
        self.assertEqual(self.chunk.content, "Refunds take 30 days.")

    def test_empty_content_is_rejected(self):
        self._auth(self.alice_key)
        res = self.client.patch(self._detail(self.chunk), {"content": "   "}, format="json")
        self.assertEqual(res.status_code, 400)
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.content, "Refunds take 30 days.")

    def test_an_unchanged_edit_does_not_pay_for_an_embedding(self):
        self._auth(self.alice_key)
        calls = []

        def counting(text):
            calls.append(text)
            return _fake_embed(text)

        with mock.patch.object(knowledge_edit, "embed_one", counting):
            res = self.client.patch(self._detail(self.chunk), {"content": self.chunk.content}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(calls, [])

    def test_put_replaces_the_content(self):
        self._auth(self.alice_key)
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            res = self.client.put(self._detail(self.chunk), {"content": "Replaced."}, format="json")
        self.assertEqual(res.status_code, 200)
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.content, "Replaced.")

    # --- create ------------------------------------------------------------
    def test_teaching_a_new_piece_of_knowledge(self):
        self._auth(self.alice_key)
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            res = self.client.post(
                self.list_url,
                {"document": str(self.doc.id), "content": "Gift cards never expire."},
                format="json",
            )
        self.assertEqual(res.status_code, 201)
        chunk = DocumentChunk.objects.get(id=res.data["id"])
        self.assertEqual(chunk.content, "Gift cards never expire.")
        self.assertEqual(chunk.embedding, NEW_VECTOR)  # searchable immediately
        self.assertEqual(chunk.position, 2)  # appended after the existing two
        self.assertTrue(chunk.metadata.get("manually_added"))

    def test_create_without_a_document_is_rejected(self):
        self._auth(self.alice_key)
        res = self.client.post(self.list_url, {"content": "orphan"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_create_with_empty_content_is_rejected(self):
        self._auth(self.alice_key)
        res = self.client.post(self.list_url, {"document": str(self.doc.id), "content": " "}, format="json")
        self.assertEqual(res.status_code, 400)

    # --- delete ------------------------------------------------------------
    def test_deleting_makes_the_bot_forget_one_piece(self):
        self._auth(self.alice_key)
        res = self.client.delete(self._detail(self.chunk))
        self.assertEqual(res.status_code, 204)
        self.assertFalse(DocumentChunk.objects.filter(id=self.chunk.id).exists())
        # The document and its other knowledge are untouched.
        self.assertTrue(DocumentChunk.objects.filter(id=self.other.id).exists())

    def test_bulk_delete(self):
        self._auth(self.alice_key)
        res = self.client.post(
            reverse("knowledge-bulk-delete"),
            {"ids": [str(self.chunk.id), str(self.other.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["deleted"], 2)
        self.assertEqual(self.doc.chunks.count(), 0)

    def test_bulk_delete_cannot_touch_another_tenant(self):
        self._auth(self.alice_key)
        res = self.client.post(
            reverse("knowledge-bulk-delete"),
            {"ids": [str(self.chunk.id), str(self.bob_chunk.id)]},
            format="json",
        )
        self.assertEqual(res.data["deleted"], 1)  # only alice's
        self.assertTrue(DocumentChunk.objects.filter(id=self.bob_chunk.id).exists())

    def test_bulk_delete_requires_ids(self):
        self._auth(self.alice_key)
        res = self.client.post(reverse("knowledge-bulk-delete"), {"ids": []}, format="json")
        self.assertEqual(res.status_code, 400)

    # --- stats -------------------------------------------------------------
    def test_stats_summarise_what_the_bot_knows(self):
        self._auth(self.alice_key)
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            self.client.patch(self._detail(self.chunk), {"content": "Corrected."}, format="json")

        res = self.client.get(reverse("knowledge-stats"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["total"], 2)
        self.assertEqual(res.data["edited"], 1)
        self.assertEqual(res.data["documents"], 1)
        self.assertEqual(res.data["by_source"], {SourceType.FILE: 2})

    def test_edited_filter(self):
        self._auth(self.alice_key)
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            self.client.patch(self._detail(self.chunk), {"content": "Corrected."}, format="json")

        res = self.client.get(self.list_url, {"edited": "true"})
        self.assertEqual([r["content"] for r in res.data["results"]], ["Corrected."])
        res = self.client.get(self.list_url, {"edited": "false"})
        self.assertEqual([r["content"] for r in res.data["results"]], ["We ship worldwide."])


class EditsSurviveResyncTests(APITestCase):
    """A correction must not be silently undone by the next API sync."""

    def setUp(self):
        self.alice, self.alice_key = make_tenant("k_sync")
        self.client.credentials(HTTP_X_API_KEY=self.alice_key)

    def _sync(self, items):
        from knowledge.services import api_content_processor as proc_mod
        from knowledge.services.api_content_processor import APIContentRAGProcessor

        def _embeds(texts):
            return [NEW_VECTOR for _ in texts], "fake-embed"

        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            return APIContentRAGProcessor(
                document_name="API Content",
                user=self.alice,
                api_url="https://x.com/i/",
            ).process_items(items, ai_analysis=False)

    def _edit(self, chunk, text):
        with mock.patch.object(knowledge_edit, "embed_one", _fake_embed):
            return self.client.patch(
                reverse("knowledge-detail", args=[chunk.id]),
                {"content": text},
                format="json",
            )

    def test_a_correction_survives_a_resync_of_unchanged_data(self):
        items = [{"id": 1, "title": "Refunds take 30 days"}]
        self._sync(items)
        chunk = DocumentChunk.objects.get(source_id="1")

        res = self._edit(chunk, "Refunds take 14 days.")
        self.assertEqual(res.status_code, 200)

        stats = self._sync(items)  # same data from the source
        chunk.refresh_from_db()
        self.assertEqual(chunk.content, "Refunds take 14 days.")  # the fix stuck
        self.assertEqual(stats["items_protected"], 1)

    def test_the_source_wins_again_once_the_record_really_changes(self):
        self._sync([{"id": 1, "title": "Refunds take 30 days"}])
        chunk = DocumentChunk.objects.get(source_id="1")
        self._edit(chunk, "Refunds take 14 days.")

        # The source now says something new — that outranks the old correction.
        stats = self._sync([{"id": 1, "title": "Refunds take 7 days"}])
        self.assertEqual(stats["items_protected"], 0)
        fresh = DocumentChunk.objects.get(source_id="1")
        self.assertIn("Refunds take 7 days", fresh.content)

    def test_a_full_refresh_rebuilds_everything_including_edits(self):
        items = [{"id": 1, "title": "Refunds take 30 days"}]
        self._sync(items)
        chunk = DocumentChunk.objects.get(source_id="1")
        self._edit(chunk, "Refunds take 14 days.")

        from knowledge.services import api_content_processor as proc_mod
        from knowledge.services.api_content_processor import APIContentRAGProcessor

        def _embeds(texts):
            return [NEW_VECTOR for _ in texts], "fake-embed"

        with mock.patch.object(proc_mod, "_embed_in_batches", _embeds):
            APIContentRAGProcessor(
                document_name="API Content", user=self.alice, api_url="https://x.com/i/"
            ).process_items(items, full_refresh=True, ai_analysis=False)

        rebuilt = DocumentChunk.objects.get(source_id="1")
        self.assertIn("Refunds take 30 days", rebuilt.content)  # back to the source
