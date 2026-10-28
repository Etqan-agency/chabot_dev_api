"""Document delivery and retrieval scope.

Two boundaries that used to be softer than they looked:

* Uploaded files were served straight off disk by nginx at a public URL that
  the API itself handed out. Ownership is now checked before any byte moves.
* Vector search scoped to a tenant only ``if user is not None``; a caller that
  omitted the argument searched every tenant's chunks and got them back as an
  answer. That now raises instead.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import UploadedDocument
from knowledge.services.vector_store import TenantScopeMissing, search_candidates

from .factories import make_tenant


def _document(owner, *, name="secret.txt", body=b"confidential contents"):
    return UploadedDocument.objects.create(
        uploaded_by=owner,
        filename=name,
        file=SimpleUploadedFile(name, body, content_type="text/plain"),
        file_size=len(body),
    )


class DocumentDownloadTests(APITestCase):
    def setUp(self):
        self.alice, self.alice_key = make_tenant("dl_alice")
        self.bob, self.bob_key = make_tenant("dl_bob")
        self.alice_doc = _document(self.alice)

    def _get(self, doc, key):
        return self.client.get(
            reverse("document-download", args=[str(doc.pk)]),
            headers={"x-api-key": key},
        )

    def test_the_owner_can_download_their_file(self):
        res = self._get(self.alice_doc, self.alice_key)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(b"".join(res.streaming_content), b"confidential contents")

    def test_another_tenant_cannot_download_it(self):
        self.assertEqual(self._get(self.alice_doc, self.bob_key).status_code, 404)

    def test_an_anonymous_request_cannot_download_it(self):
        res = self.client.get(reverse("document-download", args=[str(self.alice_doc.pk)]))
        self.assertEqual(res.status_code, 401)

    def test_the_file_is_never_offered_inline(self):
        # An uploaded .html or .svg rendered on this origin would run its own
        # script against the API's domain.
        res = self._get(self.alice_doc, self.alice_key)
        self.assertTrue(res["Content-Disposition"].startswith("attachment"))

    def test_the_api_no_longer_publishes_a_public_media_url(self):
        res = self.client.get(reverse("document-list"), headers={"x-api-key": self.alice_key})
        row = res.data["results"][0]
        self.assertNotIn("file", row)
        self.assertIn("download_url", row)
        self.assertIn(f"/documents/{self.alice_doc.pk}/download/", row["download_url"])

    @override_settings(USE_X_ACCEL_REDIRECT=True)
    def test_behind_nginx_the_bytes_are_handed_off_but_the_check_still_runs(self):
        allowed = self._get(self.alice_doc, self.alice_key)
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed["X-Accel-Redirect"].startswith("/protected-media/"))
        # Authorisation is unchanged by who moves the bytes.
        self.assertEqual(self._get(self.alice_doc, self.bob_key).status_code, 404)


class RetrievalScopeTests(TestCase):
    def test_a_search_without_a_tenant_is_refused(self):
        with self.assertRaises(TenantScopeMissing):
            search_candidates([1.0, 0.0, 0.0], fetch_k=5, threshold=0.0)

    @override_settings(ALLOW_UNSCOPED_VECTOR_SEARCH=True)
    def test_an_explicitly_unscoped_job_is_still_possible(self):
        # Offline tooling (reindex, backfill) legitimately crosses tenants, but
        # it has to say so.
        self.assertEqual(search_candidates([1.0, 0.0, 0.0], fetch_k=5, threshold=0.0), [])

    def test_a_scoped_search_still_works(self):
        user, _ = make_tenant("scope_ok")
        self.assertEqual(search_candidates([1.0, 0.0, 0.0], fetch_k=5, threshold=0.0, user=user), [])
