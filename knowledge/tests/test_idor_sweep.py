"""Systematic IDOR/BOLA sweep: every object endpoint, as the wrong tenant.

Spot-checking a few endpoints proves nothing about the ones nobody thought to
check. This builds one object of each kind owned by Alice, then asks for it as
Bob -- authenticated, valid session, just not the owner -- across every detail
route the API exposes.

A pass is 404 or 403. A 200 is a cross-tenant read; anything 5xx means the view
reached code it should never have reached.

The route list is deliberately explicit rather than derived from the URLconf:
a new endpoint should fail this file by being absent from it, which is a
conversation, rather than be silently skipped by a clever loop.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from knowledge.models import (
    ChatbotConfig,
    DocumentChunk,
    QuestionTreeNode,
    UnansweredQuestion,
    UploadedDocument,
)
from knowledge.services.api_ingest_run import IngestionRunRecorder

from .factories import make_tenant

OK_DENIED = {403, 404}


class CrossTenantObjectAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice, cls.alice_key = make_tenant("idor_alice")
        cls.bob, cls.bob_key = make_tenant("idor_bob")

        cls.document = UploadedDocument.objects.create(
            uploaded_by=cls.alice,
            filename="alice.txt",
            file=SimpleUploadedFile("alice.txt", b"alice private", content_type="text/plain"),
        )
        cls.chunk = DocumentChunk.objects.create(
            document=cls.document,
            position=0,
            content="alice's private knowledge",
        )
        cls.unanswered = UnansweredQuestion.objects.create(user=cls.alice, question="alice's gap")
        cls.tree_node = QuestionTreeNode.objects.create(owner=cls.alice, title="alice's tree")
        cls.config, _ = ChatbotConfig.objects.get_or_create(user=cls.alice)
        cls.run_id = IngestionRunRecorder(user=cls.alice, endpoint="https://api.alice/x").run_id

    def setUp(self):
        self.client = APIClient()

    def _as_bob(self, method, url, **kwargs):
        return getattr(self.client, method)(url, headers={"x-api-key": self.bob_key}, **kwargs)

    def _assert_denied(self, method, url, label):
        res = self._as_bob(method, url)
        self.assertIn(
            res.status_code,
            OK_DENIED,
            f"{label}: {method.upper()} {url} returned {res.status_code}, expected 403/404",
        )

    # -- reads ---------------------------------------------------------------
    def test_document_detail(self):
        self._assert_denied("get", reverse("document-detail", args=[self.document.pk]), "document")

    def test_document_download(self):
        self._assert_denied("get", reverse("document-download", args=[self.document.pk]), "download")

    def test_knowledge_chunk_detail(self):
        self._assert_denied("get", reverse("knowledge-detail", args=[self.chunk.pk]), "chunk")

    def test_unanswered_detail(self):
        self._assert_denied("get", reverse("unanswered-detail", args=[self.unanswered.pk]), "unanswered")

    def test_ingestion_run_detail(self):
        self._assert_denied("get", reverse("api-ingestion-run", args=[self.run_id]), "ingestion run")

    def test_guided_tree_node_detail(self):
        self._assert_denied("get", reverse("guided-tree-detail", args=[self.tree_node.pk]), "tree node")

    # -- writes: the ones that matter most, since a 200 here is data loss ----
    def test_document_cannot_be_deleted_by_another_tenant(self):
        self._assert_denied("delete", reverse("document-detail", args=[self.document.pk]), "document delete")
        self.assertTrue(UploadedDocument.objects.filter(pk=self.document.pk).exists())

    def test_chunk_cannot_be_edited_by_another_tenant(self):
        res = self.client.patch(
            reverse("knowledge-detail", args=[self.chunk.pk]),
            {"content": "vandalised"},
            format="json",
            headers={"x-api-key": self.bob_key},
        )
        self.assertIn(res.status_code, OK_DENIED)
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.content, "alice's private knowledge")

    def test_chunk_cannot_be_deleted_by_another_tenant(self):
        self._assert_denied("delete", reverse("knowledge-detail", args=[self.chunk.pk]), "chunk delete")
        self.assertTrue(DocumentChunk.objects.filter(pk=self.chunk.pk).exists())

    def test_document_cannot_be_reindexed_by_another_tenant(self):
        # Reindex spends embedding budget; doing it to someone else's document
        # is both a write and a cost transfer.
        self._assert_denied("post", reverse("document-reindex", args=[self.document.pk]), "reindex")

    def test_unanswered_cannot_be_deleted_by_another_tenant(self):
        self._assert_denied("delete", reverse("unanswered-detail", args=[self.unanswered.pk]), "unanswered delete")

    def test_tree_node_cannot_be_deleted_by_another_tenant(self):
        self._assert_denied("delete", reverse("guided-tree-detail", args=[self.tree_node.pk]), "tree delete")
        self.assertTrue(QuestionTreeNode.objects.filter(pk=self.tree_node.pk).exists())

    # -- listings must not include the other tenant's rows -------------------
    def test_listings_are_empty_for_a_tenant_with_no_data(self):
        for name in ("document-list", "knowledge-list", "unanswered-list"):
            res = self._as_bob("get", reverse(name))
            self.assertEqual(res.status_code, 200, name)
            count = res.data.get("count", len(res.data.get("results", [])))
            self.assertEqual(count, 0, f"{name} leaked rows to a non-owner")

    def test_a_tenants_own_objects_remain_reachable(self):
        # The sweep must be proving isolation, not that everything 404s.
        res = self.client.get(
            reverse("document-detail", args=[self.document.pk]),
            headers={"x-api-key": self.alice_key},
        )
        self.assertEqual(res.status_code, 200)


class AdminOnlySurfaceTests(TestCase):
    """Endpoints that manage other people's accounts must reject a plain user."""

    @classmethod
    def setUpTestData(cls):
        cls.victim, _ = make_tenant("idor_victim")
        cls.nobody, cls.nobody_key = make_tenant("idor_nobody")

    def setUp(self):
        self.client = APIClient()

    def _denied(self, method, url, label):
        res = getattr(self.client, method)(url, headers={"x-api-key": self.nobody_key})
        self.assertIn(res.status_code, OK_DENIED, f"{label} returned {res.status_code}")

    def test_another_users_api_key_is_not_readable(self):
        # The single worst one: a plaintext credential for another account.
        self._denied("get", reverse("user-get-user-api-key", args=[self.victim.pk]), "read api key")

    def test_another_users_api_key_cannot_be_rotated(self):
        self._denied("post", reverse("user-admin-regenerate-api-key", args=[self.victim.pk]), "rotate api key")

    def test_another_users_password_cannot_be_set(self):
        self._denied("post", reverse("user-set-password", args=[self.victim.pk]), "set password")

    def test_another_user_record_is_not_readable(self):
        self._denied("get", reverse("user-detail", args=[self.victim.pk]), "user detail")

    def test_the_user_list_is_not_readable(self):
        self._denied("get", reverse("user-list"), "user list")

    def test_another_users_features_are_not_readable(self):
        self._denied("get", reverse("user-features", args=[str(self.victim.pk)]), "user features")
