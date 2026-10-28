"""search_chunks orchestration + MMR re-ranking (embedding call mocked)."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from knowledge.services import retrieval
from knowledge.services.retrieval import search_chunks

from .factories import make_chunk, make_document, make_tenant


class SearchChunksTests(TestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")
        self.doc = make_document(self.user)
        self.c1 = make_chunk(self.doc, position=0, content="alpha", embedding=[1, 0, 0, 0])
        self.c2 = make_chunk(self.doc, position=1, content="beta", embedding=[0.95, 0.05, 0, 0])
        self.c3 = make_chunk(self.doc, position=2, content="gamma", embedding=[0, 0, 1, 0])

    def _patch_embed(self, vec=(1.0, 0, 0, 0)):
        return mock.patch.object(retrieval, "embed_one", return_value=(list(vec), "test"))

    def test_returns_hydrated_hits(self):
        with self._patch_embed():
            hits = search_chunks("q", user=self.user, top_k=2, use_mmr=False, threshold=0.0)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].content, "alpha")
        self.assertTrue(all(h.filename == self.doc.filename for h in hits))

    def test_threshold_excludes_orthogonal(self):
        with self._patch_embed():
            hits = search_chunks("q", user=self.user, threshold=0.5, use_mmr=False)
        contents = {h.content for h in hits}
        self.assertNotIn("gamma", contents)

    def test_mmr_default_lambda_keeps_relevance(self):
        # With the default lambda (0.6, relevance-weighted), the near-duplicate
        # "beta" still beats the orthogonal "gamma" because gamma is irrelevant.
        with self._patch_embed():
            hits = search_chunks("q", user=self.user, top_k=2, fetch_k=10, threshold=0.0, use_mmr=True)
        contents = [h.content for h in hits]
        self.assertEqual(contents[0], "alpha")
        self.assertIn("beta", contents)

    def test_mmr_low_lambda_diversifies(self):
        # Directly exercise the re-ranker: at lambda=0.0 (pure diversity) the
        # second pick avoids the near-duplicate of the first.
        import numpy as np

        from knowledge.services.vector_store import Candidate

        cands = [
            Candidate("a", 1.0, np.array([1, 0, 0, 0], dtype=np.float32)),
            Candidate("b", 0.99, np.array([0.99, 0.01, 0, 0], dtype=np.float32)),
            Candidate("c", 0.6, np.array([0, 1, 0, 0], dtype=np.float32)),
        ]
        picked = retrieval._mmr(np.array([1, 0, 0, 0], dtype=np.float32), cands, top_k=2, lambda_=0.0)
        ids = [c.chunk_id for c in picked]
        self.assertEqual(ids[0], "a")  # most relevant first
        self.assertEqual(ids[1], "c")  # diverse, not the near-duplicate "b"

    def test_empty_when_no_candidates(self):
        with self._patch_embed(vec=(0, 0, 0, 1)):
            hits = search_chunks("q", user=self.user, threshold=0.99, use_mmr=False)
        self.assertEqual(hits, [])
