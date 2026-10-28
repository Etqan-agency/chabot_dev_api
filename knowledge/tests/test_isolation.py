"""Tenant isolation: a tenant must only ever see its own data.

These tests lock in the core multi-tenancy guarantee at two layers:
  1. the vector search backend (retrieval), and
  2. the documents REST API (CRUD scoping).
"""

from __future__ import annotations

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.services.vector_store import search_candidates

from .factories import make_chunk, make_document, make_tenant


class VectorIsolationTests(APITestCase):
    def setUp(self):
        self.alice, self.alice_key = make_tenant("alice")
        self.bob, self.bob_key = make_tenant("bob")

        a_doc = make_document(self.alice, filename="alice.txt")
        b_doc = make_document(self.bob, filename="bob.txt")
        # Both chunks share the same embedding, so similarity alone can't separate
        # them — only the per-tenant filter can.
        self.a_chunk = make_chunk(a_doc, content="alice secret", embedding=[1, 0, 0, 0])
        self.b_chunk = make_chunk(b_doc, content="bob secret", embedding=[1, 0, 0, 0])

    def test_search_returns_only_callers_chunks(self):
        query = [1.0, 0.0, 0.0, 0.0]

        a_hits = search_candidates(query, fetch_k=10, threshold=0.0, user=self.alice)
        b_hits = search_candidates(query, fetch_k=10, threshold=0.0, user=self.bob)

        self.assertEqual({h.chunk_id for h in a_hits}, {str(self.a_chunk.id)})
        self.assertEqual({h.chunk_id for h in b_hits}, {str(self.b_chunk.id)})

    def test_inactive_documents_are_excluded(self):
        doc = make_document(self.alice, filename="archived.txt", is_active=False)
        make_chunk(doc, content="archived", embedding=[1, 0, 0, 0])
        hits = search_candidates([1.0, 0, 0, 0], fetch_k=10, threshold=0.0, user=self.alice)
        # Only the active chunk, not the archived one.
        self.assertEqual({h.chunk_id for h in hits}, {str(self.a_chunk.id)})


class DocumentApiIsolationTests(APITestCase):
    def setUp(self):
        self.alice, self.alice_key = make_tenant("alice")
        self.bob, self.bob_key = make_tenant("bob")
        self.a_doc = make_document(self.alice, filename="alice.txt")
        self.b_doc = make_document(self.bob, filename="bob.txt")

    def _list(self, key):
        return self.client.get(reverse("document-list"), headers={"x-api-key": key})

    def test_list_is_scoped_to_caller(self):
        res = self._list(self.alice_key)
        self.assertEqual(res.status_code, 200)
        ids = {row["id"] for row in res.data["results"]}
        self.assertEqual(ids, {str(self.a_doc.id)})

    def test_cannot_retrieve_other_tenants_document(self):
        url = reverse("document-detail", args=[self.b_doc.id])
        res = self.client.get(url, headers={"x-api-key": self.alice_key})
        self.assertEqual(res.status_code, 404)

    def test_cannot_delete_other_tenants_document(self):
        url = reverse("document-detail", args=[self.b_doc.id])
        res = self.client.delete(url, headers={"x-api-key": self.alice_key})
        self.assertEqual(res.status_code, 404)
        self.assertTrue(type(self.b_doc).objects.filter(id=self.b_doc.id).exists())

    def test_unauthenticated_is_rejected(self):
        res = self.client.get(reverse("document-list"))
        self.assertEqual(res.status_code, 401)
