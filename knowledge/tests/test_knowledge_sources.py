"""Reading knowledge back as continuous text, grouped by where it came from.

The CRUD endpoints next door are built for auditing one chunk at a time. These
two are built for reading a whole source, so what they have to get right is
different: the sections must come back in document order, the mechanical
overlap between consecutive chunks must be measured so the reader is not shown
the same sentence twice, and none of it may leak across tenants.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import DocumentChunk, SourceType
from knowledge.services.chunking import CHUNK_OVERLAP, CHUNK_SIZE, _chunk
from knowledge.services.embeddings import EmbeddingError
from knowledge.services.knowledge_assembly import _lead_length

from .factories import make_chunk, make_document, make_tenant


class SourceListTests(APITestCase):
    def setUp(self):
        self.url = reverse("knowledge-sources")
        self.alice, self.alice_key = make_tenant("s_alice")
        self.bob, self.bob_key = make_tenant("s_bob")

        self.handbook = make_document(self.alice, filename="handbook.pdf")
        make_chunk(self.handbook, position=0, content="Refunds take 30 days.")
        make_chunk(self.handbook, position=1, content="We ship worldwide.")

        self.feed = make_document(self.alice, filename="Product API")
        self.feed.source_type = SourceType.API
        self.feed.save(update_fields=["source_type"])
        make_chunk(self.feed, position=0, content="SKU 1 is a blue widget.")

        self.bob_doc = make_document(self.bob, filename="bob-secret.pdf")
        make_chunk(self.bob_doc, content="bob private")

    def _auth(self, key):
        self.client.credentials(HTTP_X_API_KEY=key)

    def test_requires_auth(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_groups_sources_by_where_the_knowledge_came_from(self):
        self._auth(self.alice_key)
        body = self.client.get(self.url).json()

        self.assertEqual([s["filename"] for s in body["sources"]["file"]], ["handbook.pdf"])
        self.assertEqual([s["filename"] for s in body["sources"]["api"]], ["Product API"])
        self.assertEqual(body["sources"]["website"], [])

    def test_every_source_type_is_present_even_when_empty(self):
        """The UI renders a tab per type, so an absent key would be a crash."""
        self._auth(self.alice_key)
        body = self.client.get(self.url).json()
        for source_type in SourceType.values:
            self.assertIn(source_type, body["sources"])
            self.assertIn(source_type, body["totals"])

    def test_counts_sections_and_characters(self):
        self._auth(self.alice_key)
        body = self.client.get(self.url).json()

        handbook = body["sources"]["file"][0]
        self.assertEqual(handbook["section_count"], 2)
        self.assertEqual(handbook["char_count"], len("Refunds take 30 days.") + len("We ship worldwide."))
        self.assertEqual(body["totals"]["file"]["documents"], 1)
        self.assertEqual(body["totals"]["file"]["sections"], 2)

    def test_a_source_with_nothing_indexed_is_still_listed(self):
        """Otherwise a failed upload vanishes with no way to see or delete it."""
        make_document(self.alice, filename="empty.pdf")
        self._auth(self.alice_key)
        body = self.client.get(self.url).json()

        empty = [s for s in body["sources"]["file"] if s["filename"] == "empty.pdf"]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["section_count"], 0)
        self.assertEqual(empty[0]["char_count"], 0)

    def test_does_not_list_another_tenants_sources(self):
        self._auth(self.alice_key)
        body = self.client.get(self.url).json()
        names = [s["filename"] for group in body["sources"].values() for s in group]
        self.assertNotIn("bob-secret.pdf", names)


class SourceContentTests(APITestCase):
    def setUp(self):
        self.alice, self.alice_key = make_tenant("c_alice")
        self.bob, self.bob_key = make_tenant("c_bob")

        self.doc = make_document(self.alice, filename="handbook.pdf")
        self.first = make_chunk(self.doc, position=0, content="Refunds take 30 days.")
        self.second = make_chunk(self.doc, position=1, content="We ship worldwide.")

        self.bob_doc = make_document(self.bob, filename="bob.pdf")
        make_chunk(self.bob_doc, content="bob private")

    def _url(self, document_id):
        return reverse("knowledge-source-content", args=[document_id])

    def _auth(self, key):
        self.client.credentials(HTTP_X_API_KEY=key)

    def test_requires_auth(self):
        self.assertEqual(self.client.get(self._url(self.doc.id)).status_code, 401)

    def test_returns_sections_in_reading_order(self):
        self._auth(self.alice_key)
        body = self.client.get(self._url(self.doc.id)).json()

        self.assertEqual([s["content"] for s in body["sections"]], ["Refunds take 30 days.", "We ship worldwide."])
        self.assertEqual(body["section_count"], 2)
        self.assertEqual(body["document"]["filename"], "handbook.pdf")

    def test_sections_carry_their_chunk_id_so_edits_have_somewhere_to_go(self):
        self._auth(self.alice_key)
        body = self.client.get(self._url(self.doc.id)).json()
        self.assertEqual(body["sections"][0]["id"], str(self.first.id))

    def test_position_order_not_creation_order(self):
        """Chunks are created out of order by some sync paths."""
        make_chunk(self.doc, position=2, content="Third.")
        late = make_chunk(self.doc, position=3, content="Fourth.")
        # (document, position) is unique, so vacate slot 0 before claiming it.
        self.first.position = 9
        self.first.save(update_fields=["position"])
        late.position = 0
        late.save(update_fields=["position"])

        self._auth(self.alice_key)
        body = self.client.get(self._url(self.doc.id)).json()
        self.assertEqual(body["sections"][0]["content"], "Fourth.")
        self.assertEqual(body["sections"][-1]["content"], "Refunds take 30 days.")

    def test_unrelated_sections_are_not_marked_as_overlapping(self):
        """API-synced chunks are separate records; nothing should be trimmed."""
        self._auth(self.alice_key)
        body = self.client.get(self._url(self.doc.id)).json()
        self.assertEqual([s["lead"] for s in body["sections"]], [0, 0])

    def test_overlap_between_real_chunks_is_measured(self):
        """The repeat that chunking deliberately adds is reported, not shown twice."""
        text = " ".join(f"Sentence number {n} about refunds and shipping." for n in range(120))
        pieces = _chunk(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        self.assertGreater(len(pieces), 1, "need a multi-chunk document for this test")

        doc = make_document(self.alice, filename="long.txt")
        for position, piece in enumerate(pieces):
            make_chunk(doc, position=position, content=piece)

        self._auth(self.alice_key)
        body = self.client.get(self._url(doc.id)).json()

        self.assertEqual(body["sections"][0]["lead"], 0)
        self.assertGreater(body["sections"][1]["lead"], 0, "the repeated opening was not detected")

        # Reading every section from its lead offset reproduces the source text
        # without the duplicated passages.
        visible = "".join(s["content"][s["lead"] :] for s in body["sections"])
        self.assertLess(len(visible), sum(len(p) for p in pieces))
        self.assertEqual(body["char_count"], sum(len(s["content"]) - s["lead"] for s in body["sections"]))

    def test_flags_hand_edited_sections(self):
        self._auth(self.alice_key)
        detail = reverse("knowledge-detail", args=[self.first.id])
        with self.settings(OPENAI_API_KEY="x"):
            from unittest import mock

            with mock.patch(
                "knowledge.services.knowledge_edit.embed_one",
                return_value=([0.0, 1.0, 0.0, 0.0], "fake"),
            ):
                self.client.patch(detail, {"content": "Refunds take 14 days."}, format="json")

        body = self.client.get(self._url(self.doc.id)).json()
        self.assertTrue(body["sections"][0]["was_edited"])
        self.assertFalse(body["sections"][1]["was_edited"])
        self.assertEqual(body["edited_count"], 1)

    # --- isolation ---------------------------------------------------------
    def test_cannot_read_another_tenants_source(self):
        self._auth(self.alice_key)
        self.assertEqual(self.client.get(self._url(self.bob_doc.id)).status_code, 404)

    def test_unknown_id_is_a_404_not_a_crash(self):
        self._auth(self.alice_key)
        self.assertEqual(self.client.get(self._url("11111111-1111-1111-1111-111111111111")).status_code, 404)

    def test_a_malformed_id_is_a_404_not_a_500(self):
        """A non-UUID would otherwise raise inside the queryset."""
        self._auth(self.alice_key)
        self.assertEqual(self.client.get(self._url("not-a-uuid")).status_code, 404)


class LeadLengthTests(APITestCase):
    """The overlap measurement itself, away from HTTP."""

    def test_no_overlap_returns_zero(self):
        self.assertEqual(_lead_length("Completely unrelated text here.", "Nothing in common at all."), 0)

    def test_short_coincidental_match_is_ignored(self):
        """A shared short phrase is not the mechanical repeat we are hiding."""
        self.assertEqual(_lead_length("...and that is the policy.", "the policy applies to all."), 0)

    def test_exact_repeat_is_measured(self):
        tail = "This sentence is repeated across the boundary between two chunks."
        self.assertEqual(_lead_length(f"Some earlier text. {tail}", f"{tail} And then more."), len(tail))

    def test_whitespace_differences_do_not_defeat_it(self):
        """Chunks are stripped on save, so the repeat is rarely byte-exact."""
        tail = "This sentence is repeated across the boundary between two chunks."
        lead = _lead_length(f"Earlier.\n\n{tail}\n", f"{tail}   And then more.")
        self.assertGreaterEqual(lead, len(tail) - 2)

    def test_never_reports_more_than_the_section_holds(self):
        text = "Identical content on both sides of the boundary, nothing else."
        self.assertLessEqual(_lead_length(text, text), len(text))


def _fake_embeddings(texts):
    """Stand in for the embedding provider: one tiny vector per piece."""
    return [[1.0, 0.0, 0.0, 0.0] for _ in texts], "fake-embeddings"


class SourceAsOneTextTests(APITestCase):
    """Reading a source as the single block of text a page would show."""

    def setUp(self):
        self.alice, self.alice_key = make_tenant("j_alice")
        self.client.credentials(HTTP_X_API_KEY=self.alice_key)

    def _url(self, document_id):
        return reverse("knowledge-source-content", args=[document_id])

    def test_separate_records_are_not_run_together(self):
        """Two API records concatenated raw would read as one broken sentence."""
        doc = make_document(self.alice, filename="handbook.pdf")
        make_chunk(doc, position=0, content="Refunds take 30 days.")
        make_chunk(doc, position=1, content="We ship worldwide.")

        body = self.client.get(self._url(doc.id)).json()
        self.assertEqual(body["content"], "Refunds take 30 days.\n\nWe ship worldwide.")
        self.assertEqual(body["char_count"], len(body["content"]))

    def test_the_repeated_passage_appears_once(self):
        text = " ".join(f"Sentence number {n} about refunds and shipping." for n in range(120))
        pieces = _chunk(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        self.assertGreater(len(pieces), 1, "need a multi-chunk document for this test")

        doc = make_document(self.alice, filename="long.txt")
        for position, piece in enumerate(pieces):
            make_chunk(doc, position=position, content=piece)

        content = self.client.get(self._url(doc.id)).json()["content"]
        self.assertLess(len(content), sum(len(p) for p in pieces), "overlap was not removed")
        # Every sentence of the original survives, and none of them twice.
        for n in (0, 60, 119):
            self.assertEqual(content.count(f"Sentence number {n} about refunds"), 1)

    def test_an_empty_source_reads_as_an_empty_page(self):
        doc = make_document(self.alice, filename="empty.pdf")
        body = self.client.get(self._url(doc.id)).json()
        self.assertEqual(body["content"], "")
        self.assertEqual(body["char_count"], 0)


class SourceRewriteTests(APITestCase):
    """PUT: the whole source, rewritten by hand and re-embedded."""

    def setUp(self):
        self.alice, self.alice_key = make_tenant("w_alice")
        self.bob, self.bob_key = make_tenant("w_bob")

        self.doc = make_document(self.alice, filename="handbook.pdf")
        self.first = make_chunk(self.doc, position=0, content="Refunds take 30 days.")
        self.second = make_chunk(self.doc, position=1, content="We ship worldwide.")

        self.bob_doc = make_document(self.bob, filename="bob.pdf")
        make_chunk(self.bob_doc, content="bob private")

        self.client.credentials(HTTP_X_API_KEY=self.alice_key)

    def _url(self, document_id):
        return reverse("knowledge-source-content", args=[document_id])

    def _put(self, content, document=None):
        with mock.patch("knowledge.services.chunking.embed_texts", side_effect=_fake_embeddings):
            return self.client.put(self._url((document or self.doc).id), {"content": content}, format="json")

    def test_requires_auth(self):
        self.client.credentials()
        self.assertEqual(self.client.put(self._url(self.doc.id), {}, format="json").status_code, 401)

    def test_replaces_the_whole_text(self):
        response = self._put("Refunds take 14 days.\n\nWe ship worldwide, tracked.")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Refunds take 14 days.", response.json()["content"])

        # Read it back the way the page would.
        body = self.client.get(self._url(self.doc.id)).json()
        self.assertIn("Refunds take 14 days.", body["content"])
        self.assertNotIn("30 days", body["content"])

    def test_the_old_sections_are_gone_not_appended_to(self):
        """A rewrite that kept the old chunks would leave the bot answering both."""
        self._put("Refunds take 14 days.")
        contents = list(DocumentChunk.objects.filter(document=self.doc).values_list("content", flat=True))
        self.assertEqual(contents, ["Refunds take 14 days."])

    def test_the_new_text_is_embedded_so_retrieval_follows_it(self):
        self._put("Refunds take 14 days.")
        chunk = DocumentChunk.objects.get(document=self.doc)
        self.assertTrue(chunk.embedding, "the rewritten text was stored without an embedding")
        self.assertEqual(chunk.embedding_model, "fake-embeddings")
        self.assertIsNotNone(chunk.edited_at)
        self.assertTrue(chunk.metadata.get("manually_edited"))

    def test_long_text_is_split_back_into_sections(self):
        text = " ".join(f"Policy point number {n} about refunds." for n in range(200))
        response = self._put(text)
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()["section_count"], 1)

    def test_a_rewrite_round_trips_without_growing(self):
        """Read, save unchanged, read again -- the overlap must not be re-inserted."""
        text = " ".join(f"Policy point number {n} about refunds." for n in range(200))
        self._put(text)
        first = self.client.get(self._url(self.doc.id)).json()["content"]
        self._put(first)
        second = self.client.get(self._url(self.doc.id)).json()["content"]
        self.assertEqual(first, second)

    def test_empty_content_is_rejected(self):
        self.assertEqual(self._put("   ").status_code, 400)

    def test_nothing_is_lost_when_the_embedding_provider_is_down(self):
        with mock.patch(
            "knowledge.services.chunking.embed_texts",
            side_effect=EmbeddingError("provider down"),
        ):
            response = self.client.put(self._url(self.doc.id), {"content": "Refunds take 14 days."}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            list(DocumentChunk.objects.filter(document=self.doc).values_list("content", flat=True)),
            ["Refunds take 30 days.", "We ship worldwide."],
        )

    def test_a_failed_upload_can_be_fixed_by_pasting_its_text(self):
        self.doc.processing_status = "failed"
        self.doc.error_message = "Unsupported file type: .pdf"
        self.doc.save(update_fields=["processing_status", "error_message"])

        body = self._put("Refunds take 14 days.").json()
        self.assertEqual(body["document"]["processing_status"], "completed")

    def test_an_api_source_is_not_rewritten_as_a_whole(self):
        """Re-chunking would drop the source ids the sync protects edits by."""
        self.doc.source_type = SourceType.API
        self.doc.save(update_fields=["source_type"])

        response = self._put("Refunds take 14 days.")
        self.assertEqual(response.status_code, 400)
        self.assertIn("PATCH /knowledge/", response.json()["detail"])
        self.assertEqual(DocumentChunk.objects.filter(document=self.doc).count(), 2)

    def test_cannot_rewrite_another_tenants_source(self):
        response = self._put("mine now", document=self.bob_doc)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(DocumentChunk.objects.get(document=self.bob_doc).content, "bob private")


class SourceDeleteTests(APITestCase):
    """DELETE: the page's own delete button, without a second endpoint."""

    def setUp(self):
        self.alice, self.alice_key = make_tenant("d_alice")
        self.bob, self.bob_key = make_tenant("d_bob")
        self.doc = make_document(self.alice, filename="handbook.pdf")
        make_chunk(self.doc, position=0, content="Refunds take 30 days.")
        self.bob_doc = make_document(self.bob, filename="bob.pdf")
        make_chunk(self.bob_doc, content="bob private")
        self.client.credentials(HTTP_X_API_KEY=self.alice_key)

    def _url(self, document_id):
        return reverse("knowledge-source-content", args=[document_id])

    def test_deletes_the_source_and_its_sections(self):
        self.assertEqual(self.client.delete(self._url(self.doc.id)).status_code, 204)
        self.assertEqual(self.client.get(self._url(self.doc.id)).status_code, 404)
        self.assertEqual(DocumentChunk.objects.filter(document=self.doc).count(), 0)

    def test_cannot_delete_another_tenants_source(self):
        self.assertEqual(self.client.delete(self._url(self.bob_doc.id)).status_code, 404)
        self.assertEqual(DocumentChunk.objects.filter(document=self.bob_doc).count(), 1)
