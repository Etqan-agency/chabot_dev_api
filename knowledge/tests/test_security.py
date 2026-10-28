"""Security regressions: SSRF guard on outbound URL fetches."""

from __future__ import annotations

from django.test import TestCase

from knowledge.services.net import UnsafeURLError, validate_public_url


class SsrfGuardTests(TestCase):
    def test_rejects_non_http_schemes(self):
        for url in ["ftp://example.com/x", "file:///etc/passwd", "gopher://x"]:
            with self.assertRaises(UnsafeURLError):
                validate_public_url(url)

    def test_rejects_loopback(self):
        for url in ["http://127.0.0.1/", "http://localhost/x", "http://[::1]/"]:
            with self.assertRaises(UnsafeURLError):
                validate_public_url(url)

    def test_rejects_private_ranges(self):
        for url in ["http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/"]:
            with self.assertRaises(UnsafeURLError):
                validate_public_url(url)

    def test_rejects_cloud_metadata(self):
        # 169.254.169.254 — the classic SSRF target for cloud credentials.
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_missing_host(self):
        with self.assertRaises(UnsafeURLError):
            validate_public_url("http:///nohost")

    def test_allows_public_ip(self):
        # IP literal → no DNS needed; 8.8.8.8 is public, so it passes.
        self.assertEqual(validate_public_url("https://8.8.8.8/api"), "https://8.8.8.8/api")
