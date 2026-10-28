"""fetch_all_pages() — a paginated source is followed to the end, safely."""

from __future__ import annotations

from unittest import mock

import requests
from django.test import SimpleTestCase

from knowledge.services import api_fetch, net
from knowledge.services.api_fetch import fetch_all_pages
from knowledge.services.net import UnsafeURLError


def _page(body: dict | list, *, link_next: str | None = None):
    resp = mock.Mock()
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    resp.links = {"next": {"url": link_next}} if link_next else {}
    # safe_get follows redirects itself, so a page response must say it is not
    # one, and must stream a body it can size-check.
    resp.is_redirect = False
    resp.is_permanent_redirect = False
    resp.headers = {}
    resp.iter_content.return_value = [b"{}"]
    return resp


def _redirect(location: str, *, permanent: bool = False):
    resp = mock.Mock()
    resp.is_redirect = not permanent
    resp.is_permanent_redirect = permanent
    resp.headers = {"Location": location}
    return resp


class _FakeHTTP:
    """Stands in for `requests`, serving a canned page per URL."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, timeout=None, allow_redirects=True, stream=False):  # noqa: ARG002
        self.calls.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected URL: {url}")
        return self.pages[url]


class FetchAllPagesTests(SimpleTestCase):
    def setUp(self):
        # The next-page URL is server-supplied, so it is re-validated for SSRF.
        # Stub the check so these tests never touch DNS.
        for target in (api_fetch, net):
            patcher = mock.patch.object(target, "validate_public_url", lambda u: u)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_follows_next_until_the_source_runs_out(self):
        http = _FakeHTTP(
            {
                "https://x.com/i/": _page({"next": "https://x.com/i/?p=2", "results": [{"id": 1}]}),
                "https://x.com/i/?p=2": _page({"next": "https://x.com/i/?p=3", "results": [{"id": 2}]}),
                "https://x.com/i/?p=3": _page({"next": None, "results": [{"id": 3}]}),
            }
        )
        pages, truncated = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 3)
        self.assertFalse(truncated)
        self.assertEqual([p["results"][0]["id"] for p in pages], [1, 2, 3])

    def test_single_page_source_makes_one_request(self):
        http = _FakeHTTP({"https://x.com/i/": _page({"results": [{"id": 1}]})})
        pages, truncated = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 1)
        self.assertFalse(truncated)
        self.assertEqual(http.calls, ["https://x.com/i/"])

    def test_relative_next_is_resolved_against_the_current_url(self):
        http = _FakeHTTP(
            {
                "https://x.com/api/items/": _page({"next": "?page=2", "results": [{"id": 1}]}),
                "https://x.com/api/items/?page=2": _page({"results": [{"id": 2}]}),
            }
        )
        pages, _ = fetch_all_pages("https://x.com/api/items/", session=http)
        self.assertEqual(len(pages), 2)

    def test_link_header_pagination(self):
        http = _FakeHTTP(
            {
                "https://x.com/i/": _page({"data": [{"id": 1}]}, link_next="https://x.com/i/?p=2"),
                "https://x.com/i/?p=2": _page({"data": [{"id": 2}]}),
            }
        )
        pages, _ = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 2)

    def test_nested_links_next_object(self):
        http = _FakeHTTP(
            {
                "https://x.com/i/": _page({"links": {"next": {"href": "https://x.com/i/?p=2"}}, "data": [{"id": 1}]}),
                "https://x.com/i/?p=2": _page({"links": {"next": None}, "data": [{"id": 2}]}),
            }
        )
        pages, _ = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 2)

    def test_a_cyclic_paginator_cannot_loop_forever(self):
        # Page 2 points back at page 1.
        http = _FakeHTTP(
            {
                "https://x.com/i/": _page({"next": "https://x.com/i/?p=2", "results": [{"id": 1}]}),
                "https://x.com/i/?p=2": _page({"next": "https://x.com/i/", "results": [{"id": 2}]}),
            }
        )
        pages, _ = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 2)

    def test_page_ceiling_stops_the_walk_and_reports_truncation(self):
        http = _FakeHTTP(
            {
                "https://x.com/i/": _page({"next": "https://x.com/i/?p=2", "results": [{"id": 1}]}),
                "https://x.com/i/?p=2": _page({"next": "https://x.com/i/?p=3", "results": [{"id": 2}]}),
            }
        )
        pages, truncated = fetch_all_pages("https://x.com/i/", max_pages=2, session=http)
        self.assertEqual(len(pages), 2)
        self.assertTrue(truncated)

    def test_ceiling_reached_on_the_last_page_is_not_truncation(self):
        http = _FakeHTTP({"https://x.com/i/": _page({"results": [{"id": 1}]})})
        pages, truncated = fetch_all_pages("https://x.com/i/", max_pages=1, session=http)
        self.assertEqual(len(pages), 1)
        self.assertFalse(truncated)

    def test_unsafe_next_url_stops_the_walk_without_fetching_it(self):
        http = _FakeHTTP(
            {"https://x.com/i/": _page({"next": "http://169.254.169.254/latest/", "results": [{"id": 1}]})}
        )
        with mock.patch.object(api_fetch, "validate_public_url", side_effect=UnsafeURLError("nope")):
            pages, _ = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 1)
        self.assertEqual(http.calls, ["https://x.com/i/"])

    def test_a_failing_later_page_keeps_the_pages_already_fetched(self):
        http = _FakeHTTP(
            {
                "https://x.com/i/": _page({"next": "https://x.com/i/?p=2", "results": [{"id": 1}]}),
                "https://x.com/i/?p=2": mock.Mock(
                    raise_for_status=mock.Mock(side_effect=requests.RequestException("down")),
                    links={},
                    is_redirect=False,
                    is_permanent_redirect=False,
                    headers={},
                    iter_content=mock.Mock(return_value=[b"{}"]),
                ),
            }
        )
        pages, truncated = fetch_all_pages("https://x.com/i/", session=http)
        self.assertEqual(len(pages), 1)  # page 1 survives
        self.assertTrue(truncated)

    def test_a_failing_first_page_is_a_real_failure(self):
        http = _FakeHTTP(
            {
                "https://x.com/i/": mock.Mock(
                    raise_for_status=mock.Mock(side_effect=requests.RequestException("down")),
                    links={},
                    is_redirect=False,
                    is_permanent_redirect=False,
                    headers={},
                    iter_content=mock.Mock(return_value=[b"{}"]),
                )
            }
        )
        with self.assertRaises(requests.RequestException):
            fetch_all_pages("https://x.com/i/", session=http)


class RedirectSSRFTests(SimpleTestCase):
    """A redirect target is chosen by the remote server, so it is not trusted.

    Validating only the URL the tenant typed left the whole guard bypassable:
    a public host that answers `302 Location: http://169.254.169.254/...` had
    its metadata-service reply ingested into the knowledge base, where chat
    would read it back out.
    """

    def test_a_redirect_to_a_private_address_is_refused(self):
        http = _FakeHTTP(
            {
                "https://evil.example/api": _redirect("http://169.254.169.254/latest/meta-data/"),
            }
        )

        # Only the attacker's public host validates; the metadata address does not.
        def only_public(url):
            if "169.254" in url:
                raise UnsafeURLError("private")
            return url

        with mock.patch.object(net, "validate_public_url", side_effect=only_public):
            with self.assertRaises(UnsafeURLError):
                fetch_all_pages("https://evil.example/api", session=http)

        # The request to the internal address was never made.
        self.assertNotIn("http://169.254.169.254/latest/meta-data/", http.calls)

    def test_a_redirect_to_a_public_address_is_still_followed(self):
        http = _FakeHTTP(
            {
                "https://a.example/api": _redirect("https://b.example/api"),
                "https://b.example/api": _page({"results": [{"id": 1}]}),
            }
        )
        with mock.patch.object(net, "validate_public_url", lambda u: u):
            pages, _ = fetch_all_pages("https://a.example/api", session=http)
        self.assertEqual(len(pages), 1)

    def test_a_redirect_loop_terminates(self):
        http = _FakeHTTP({"https://a.example/x": _redirect("https://a.example/x")})
        with mock.patch.object(net, "validate_public_url", lambda u: u):
            with self.assertRaises(UnsafeURLError):
                fetch_all_pages("https://a.example/x", session=http)
        self.assertLessEqual(len(http.calls), net.MAX_REDIRECTS + 1)

    def test_an_oversized_body_is_refused(self):
        big = _page({"results": []})
        big.headers = {}
        big.iter_content.return_value = [b"x" * (1024 * 1024)] * 40  # 40 MB
        http = _FakeHTTP({"https://a.example/big": big})
        with mock.patch.object(net, "validate_public_url", lambda u: u):
            with self.assertRaises(net.ResponseTooLarge):
                fetch_all_pages("https://a.example/big", session=http)

    def test_a_declared_oversized_length_is_refused_before_reading(self):
        big = _page({"results": []})
        big.headers = {"Content-Length": str(100 * 1024 * 1024)}
        http = _FakeHTTP({"https://a.example/big": big})
        with mock.patch.object(net, "validate_public_url", lambda u: u):
            with self.assertRaises(net.ResponseTooLarge):
                fetch_all_pages("https://a.example/big", session=http)
        big.iter_content.assert_not_called()
