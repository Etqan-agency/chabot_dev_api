"""Document endpoints: upload quota gating, word upload, reindex, scoping.

Ingestion (which would call OpenAI) is mocked everywhere."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import TenantQuota, UploadedDocument
from knowledge.views import documents as docs_view

from .factories import make_document, make_tenant


class DocumentUploadQuotaTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.url = reverse("document-list")

    def _upload(self, *, name="note.txt", content=b"hello world"):
        f = SimpleUploadedFile(name, content, content_type="text/plain")
        return self.client.post(self.url, {"file": f}, format="multipart", headers={"x-api-key": self.key})

    def test_upload_succeeds_within_quota(self):
        with mock.patch.object(docs_view, "dispatch_ingestion") as ing:
            res = self._upload()
        self.assertEqual(res.status_code, 201)
        ing.assert_called_once()
        self.assertEqual(UploadedDocument.objects.filter(uploaded_by=self.user).count(), 1)

    def test_document_count_quota_blocks_upload(self):
        TenantQuota.objects.update_or_create(user=self.user, defaults={"max_documents": 1})
        make_document(self.user, filename="existing.txt", size=10)
        with mock.patch.object(docs_view, "dispatch_ingestion") as ing:
            res = self._upload()
        self.assertEqual(res.status_code, 413)
        ing.assert_not_called()

    def test_storage_quota_blocks_upload(self):
        TenantQuota.objects.update_or_create(
            user=self.user,
            defaults={"max_total_mb": 0.0001},  # ~100 bytes
        )
        with mock.patch.object(docs_view, "dispatch_ingestion"):
            res = self._upload(content=b"x" * 5000)
        self.assertEqual(res.status_code, 413)

    def test_suspended_blocks_upload(self):
        TenantQuota.objects.update_or_create(user=self.user, defaults={"is_suspended": True})
        with mock.patch.object(docs_view, "dispatch_ingestion"):
            res = self._upload()
        self.assertEqual(res.status_code, 403)


class WordUploadTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        # Normal (non free-tier) limits so multi-doc upload flows aren't capped.
        from knowledge.models import TenantQuota

        TenantQuota.objects.update_or_create(user=self.user, defaults={"max_documents": 100, "max_total_mb": 200})
        self.url = reverse("document-upload-word")

    def test_missing_file_returns_400(self):
        res = self.client.post(self.url, {}, format="multipart", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 400)

    def test_word_upload_succeeds(self):
        doc = make_document(self.user, filename="brief.docx")
        fake_result = SimpleNamespace(document=doc, chunks_created=7)
        f = SimpleUploadedFile("brief.docx", b"PK fake docx", content_type="application/octet-stream")
        with mock.patch.object(docs_view, "import_document_from_word", return_value=fake_result):
            res = self.client.post(self.url, {"file": f}, format="multipart", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["chunks_created"], 7)

    def test_word_upload_validation_error_returns_400(self):
        f = SimpleUploadedFile("brief.pdf", b"not docx", content_type="application/pdf")
        with mock.patch.object(docs_view, "import_document_from_word", side_effect=ValueError("Only .docx allowed")):
            res = self.client.post(self.url, {"file": f}, format="multipart", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 400)
        self.assertIn("docx", res.data["detail"])

    def test_word_upload_quota_blocks(self):
        TenantQuota.objects.update_or_create(user=self.user, defaults={"max_documents": 0})
        f = SimpleUploadedFile("brief.docx", b"x", content_type="application/octet-stream")
        with mock.patch.object(docs_view, "import_document_from_word") as imp:
            res = self.client.post(self.url, {"file": f}, format="multipart", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 413)
        imp.assert_not_called()


class ReindexTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.doc = make_document(self.user, filename="r.txt")

    def test_reindex_dispatches_ingestion(self):
        url = reverse("document-reindex", args=[self.doc.id])
        with mock.patch.object(
            docs_view,
            "dispatch_ingestion",
            return_value=SimpleNamespace(chunks_created=4),
        ) as ing:
            res = self.client.post(url, headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 202)
        ing.assert_called_once()

    def test_cannot_reindex_other_tenants_doc(self):
        other, other_key = make_tenant("bob")
        url = reverse("document-reindex", args=[self.doc.id])
        with mock.patch.object(docs_view, "dispatch_ingestion"):
            res = self.client.post(url, headers={"x-api-key": other_key})
        self.assertEqual(res.status_code, 404)

    def test_reindex_failure_returns_500(self):
        url = reverse("document-reindex", args=[self.doc.id])
        with mock.patch.object(docs_view, "dispatch_ingestion", side_effect=RuntimeError("boom")):
            res = self.client.post(url, headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 500)


class DocumentErrorBranchTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("errs")
        TenantQuota.objects.update_or_create(user=self.user, defaults={"max_documents": 100, "max_total_mb": 100})

    def test_create_swallows_ingestion_error_still_201(self):
        # A failed background ingestion must not fail the upload itself.
        with mock.patch.object(docs_view, "dispatch_ingestion", side_effect=RuntimeError("boom")):
            res = self.client.post(
                reverse("document-list"),
                {"file": SimpleUploadedFile("a.txt", b"hello world", content_type="text/plain")},
                format="multipart",
                headers={"x-api-key": self.key},
            )
        self.assertEqual(res.status_code, 201)

    def test_word_upload_unexpected_error_returns_500(self):
        with mock.patch.object(docs_view, "import_document_from_word", side_effect=RuntimeError("parser exploded")):
            res = self.client.post(
                reverse("document-upload-word"),
                {
                    "file": SimpleUploadedFile(
                        "a.docx",
                        b"PK\x03\x04stuff",
                        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                format="multipart",
                headers={"x-api-key": self.key},
            )
        self.assertEqual(res.status_code, 500)
