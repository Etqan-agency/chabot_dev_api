"""Website crawl: link-following/same-site scoping (mocked network) + the
paid-only feature gate on the endpoint."""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import DocumentStatus, SourceType, UploadedDocument
from knowledge.services import web_crawler

from .factories import make_tenant


def _resp(url, html, *, status=200, ctype="text/html; charset=utf-8"):
    r = mock.Mock()
    r.url = url
    r.status_code = status
    r.headers = {"content-type": ctype}
    r.text = html
    return r


PAGES = {
    "https://ex.com": (
        "<html><head><title>Home</title></head><body>"
        "<a href='/about'>about</a> <a href='/contact'>contact</a> "
        "<a href='https://other.com/x'>ext</a></body></html>"
    ),
    "https://ex.com/about": "<html><head><title>About</title></head><body><a href='/'>home</a></body></html>",
    "https://ex.com/contact": "<html><head><title>Contact</title></head><body>reach us</body></html>",
}


class CrawlSiteLogicTests(APITestCase):
    def test_stays_on_site_and_follows_internal_links(self):
        def fake_get(url, **kw):
            return _resp(url, PAGES.get(url.rstrip("/"), "<html><body>x</body></html>"))

        session = mock.Mock()
        session.get.side_effect = fake_get
        session.headers = {}

        with (
            mock.patch.object(web_crawler, "validate_public_url", lambda u: u),
            mock.patch.object(web_crawler, "_load_robots", return_value=None),
            mock.patch.object(web_crawler, "_discover_sitemap_urls", return_value=[]),
            mock.patch.object(web_crawler, "_extract_main_text", lambda html, url: f"body of {url}"),
            mock.patch.object(web_crawler.requests, "Session", return_value=session),
        ):
            pages = web_crawler.crawl_site("https://ex.com", max_pages=50, timeout=5)

        urls = {p["url"] for p in pages}
        self.assertEqual(urls, {"https://ex.com", "https://ex.com/about", "https://ex.com/contact"})
        self.assertNotIn("https://other.com/x", urls)
        # Titles captured from <title>.
        self.assertTrue(any(p["title"] == "Home" for p in pages))

    def test_respects_max_pages(self):
        def fake_get(url, **kw):
            return _resp(url, PAGES.get(url.rstrip("/"), "<html><body>x</body></html>"))

        session = mock.Mock()
        session.get.side_effect = fake_get
        session.headers = {}

        with (
            mock.patch.object(web_crawler, "validate_public_url", lambda u: u),
            mock.patch.object(web_crawler, "_load_robots", return_value=None),
            mock.patch.object(web_crawler, "_discover_sitemap_urls", return_value=[]),
            mock.patch.object(web_crawler, "_extract_main_text", lambda html, url: f"body of {url}"),
            mock.patch.object(web_crawler.requests, "Session", return_value=session),
        ):
            pages = web_crawler.crawl_site("https://ex.com", max_pages=1, timeout=5)

        self.assertEqual(len(pages), 1)


class CrawlEndpointTests(APITestCase):
    def setUp(self):
        self.url = reverse("crawl-website")

    def test_requires_auth(self):
        self.assertEqual(self.client.post(self.url, {"url": "https://ex.com"}, format="json").status_code, 401)

    def test_free_tier_blocked_with_402(self):
        _user, key = make_tenant("freebie")  # not staff, no paid plan
        res = self.client.post(self.url, {"url": "https://ex.com"}, format="json", headers={"x-api-key": key})
        self.assertEqual(res.status_code, 402)

    def test_staff_starts_crawl_and_gets_document_id(self):
        _user, key = make_tenant("boss", is_staff=True)  # exempt → allowed
        from knowledge.views import web_crawl as web_crawl_view

        doc = UploadedDocument.objects.create(
            uploaded_by=_user,
            filename="Website: ex.com",
            source_type=SourceType.WEBSITE,
            processing_status=DocumentStatus.PROCESSING,
        )
        with (
            mock.patch.object(web_crawl_view, "dispatch_site_crawl", return_value=doc) as dispatch,
            mock.patch.object(web_crawl_view, "validate_public_url", lambda u: u),
        ):
            res = self.client.post(self.url, {"url": "ex.com"}, format="json", headers={"x-api-key": key})
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["document_id"], str(doc.pk))
        # Bare host was normalized to https://.
        self.assertEqual(dispatch.call_args.kwargs["start_url"], "https://ex.com")

    def test_rejects_internal_url(self):
        _user, key = make_tenant("boss2", is_staff=True)
        res = self.client.post(self.url, {"url": "http://127.0.0.1/admin"}, format="json", headers={"x-api-key": key})
        self.assertEqual(res.status_code, 400)

    def test_missing_url_returns_400(self):
        _user, key = make_tenant("boss3", is_staff=True)
        res = self.client.post(self.url, {}, format="json", headers={"x-api-key": key})
        self.assertEqual(res.status_code, 400)

    def test_gate_fails_open_when_billing_layer_errors(self):
        from knowledge.views import web_crawl as web_crawl_view

        _user, key = make_tenant("failopen_crawl")  # non-staff, no paid plan
        doc = UploadedDocument.objects.create(
            uploaded_by=_user,
            filename="Website: ex.com",
            source_type=SourceType.WEBSITE,
            processing_status=DocumentStatus.PROCESSING,
        )
        with (
            mock.patch("subscriptions.features.has_feature", side_effect=RuntimeError),
            mock.patch.object(web_crawl_view, "validate_public_url", lambda u: u),
            mock.patch.object(web_crawl_view, "dispatch_site_crawl", return_value=doc),
        ):
            res = self.client.post(self.url, {"url": "https://ex.com"}, format="json", headers={"x-api-key": key})
        self.assertEqual(res.status_code, 202)

    def test_invalid_max_pages_falls_back_to_default(self):
        from knowledge.views import web_crawl as web_crawl_view

        _user, key = make_tenant("boss5", is_staff=True)
        doc = UploadedDocument.objects.create(
            uploaded_by=_user,
            filename="Website: ex.com",
            source_type=SourceType.WEBSITE,
            processing_status=DocumentStatus.PROCESSING,
        )
        with (
            mock.patch.object(web_crawl_view, "validate_public_url", lambda u: u),
            mock.patch.object(web_crawl_view, "dispatch_site_crawl", return_value=doc) as dispatch,
        ):
            res = self.client.post(
                self.url, {"url": "https://ex.com", "max_pages": "lots"}, format="json", headers={"x-api-key": key}
            )
        self.assertEqual(res.status_code, 202)
        # Non-integer max_pages -> server default (100), not a crash.
        self.assertEqual(dispatch.call_args.kwargs["max_pages"], 100)

    def test_dispatch_failure_returns_500(self):
        from knowledge.views import web_crawl as web_crawl_view

        _user, key = make_tenant("boss4", is_staff=True)
        with (
            mock.patch.object(web_crawl_view, "validate_public_url", lambda u: u),
            mock.patch.object(web_crawl_view, "dispatch_site_crawl", side_effect=RuntimeError("boom")),
        ):
            res = self.client.post(self.url, {"url": "https://ex.com"}, format="json", headers={"x-api-key": key})
        self.assertEqual(res.status_code, 500)
