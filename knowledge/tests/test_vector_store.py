"""NumpyVectorStore behaviour: scoping, threshold, fetch_k, ragged dims."""

from __future__ import annotations

from django.test import TestCase

from knowledge.services.vector_store import NumpyVectorStore, search_candidates

from .factories import make_chunk, make_document, make_tenant


class NumpyBackendTests(TestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")
        self.doc = make_document(self.user)
        # Three chunks pointing in different directions.
        self.c1 = make_chunk(self.doc, position=0, content="east", embedding=[1, 0, 0, 0])
        self.c2 = make_chunk(self.doc, position=1, content="north", embedding=[0, 1, 0, 0])
        self.c3 = make_chunk(self.doc, position=2, content="east-ish", embedding=[0.9, 0.1, 0, 0])

    def test_orders_by_similarity(self):
        hits = NumpyVectorStore().search([1.0, 0, 0, 0], fetch_k=10, threshold=0.0, user=self.user)
        ids = [h.chunk_id for h in hits]
        # c1 (exact) then c3 (close) then c2 (orthogonal).
        self.assertEqual(ids[0], str(self.c1.id))
        self.assertEqual(ids[1], str(self.c3.id))

    def test_threshold_filters(self):
        hits = NumpyVectorStore().search([1.0, 0, 0, 0], fetch_k=10, threshold=0.5, user=self.user)
        ids = {h.chunk_id for h in hits}
        # c2 is orthogonal (score 0) → excluded by threshold 0.5.
        self.assertNotIn(str(self.c2.id), ids)

    def test_fetch_k_caps_results(self):
        hits = NumpyVectorStore().search([1.0, 0, 0, 0], fetch_k=1, threshold=0.0, user=self.user)
        self.assertEqual(len(hits), 1)

    def test_document_ids_filter(self):
        other_doc = make_document(self.user, filename="other.txt")
        oc = make_chunk(other_doc, content="x", embedding=[1, 0, 0, 0])
        hits = NumpyVectorStore().search(
            [1.0, 0, 0, 0],
            fetch_k=10,
            threshold=0.0,
            user=self.user,
            document_ids=[other_doc.id],
        )
        self.assertEqual({h.chunk_id for h in hits}, {str(oc.id)})

    def test_ragged_dimensions_are_skipped(self):
        # A chunk with a different embedding dim must not crash the scan.
        make_chunk(self.doc, position=9, content="weird", embedding=[1, 2, 3])
        hits = search_candidates([1.0, 0, 0, 0], fetch_k=10, threshold=0.0, user=self.user)
        # The 3-dim chunk is silently ignored; the 4-dim ones still return.
        self.assertTrue(any(h.chunk_id == str(self.c1.id) for h in hits))

    def test_empty_for_user_with_no_chunks(self):
        stranger, _ = make_tenant("bob")
        hits = search_candidates([1.0, 0, 0, 0], fetch_k=10, threshold=0.0, user=stranger)
        self.assertEqual(hits, [])
