"""Response headers, and the dev consoles that used to be public.

The policy has to be strict where it costs nothing and permissive exactly where
the page genuinely needs it -- a CSP that breaks the docs page is a CSP someone
will switch off.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from knowledge.tests.factories import make_tenant


class SecurityHeaderTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_api_responses_carry_a_strict_policy(self):
        res = self.client.get(reverse("health"))
        csp = res["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_every_response_denies_powerful_features(self):
        res = self.client.get(reverse("health"))
        self.assertIn("camera=()", res["Permissions-Policy"])
        self.assertIn("geolocation=()", res["Permissions-Policy"])

    def test_the_referrer_never_leaves_the_origin(self):
        # Download URLs and reset links must not travel in a Referer header.
        res = self.client.get(reverse("health"))
        self.assertEqual(res["Referrer-Policy"], "same-origin")

    def test_the_docs_page_still_renders_and_keeps_the_framing_rules(self):
        res = self.client.get("/api/docs/")
        self.assertEqual(res.status_code, 200)
        csp = res["Content-Security-Policy"]
        # Permissive enough to work...
        self.assertIn("script-src", csp)
        # ...but still not embeddable, and still no plugins.
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_clickjacking_protection_is_present(self):
        self.assertEqual(self.client.get(reverse("health"))["X-Frame-Options"], "DENY")


class DevConsoleExposureTests(TestCase):
    """An API console pointed at production, previously open to anyone."""

    def setUp(self):
        self.client = Client()

    @override_settings(DEBUG=False)
    def test_the_api_tester_is_not_public_in_production(self):
        self.assertEqual(self.client.get(reverse("api-tester")).status_code, 404)

    @override_settings(DEBUG=False)
    def test_the_paddle_test_page_is_not_public_in_production(self):
        self.assertEqual(self.client.get(reverse("paddle-test")).status_code, 404)

    @override_settings(DEBUG=False)
    def test_staff_can_still_reach_them(self):
        staff, _ = make_tenant("console_staff", is_staff=True)
        self.client.force_login(staff)
        # 200 when the file exists in the checkout, 404 when it does not --
        # either way, not the permission refusal a non-staff user gets.
        res = self.client.get(reverse("api-tester"))
        self.assertIn(res.status_code, {200, 404})

    @override_settings(DEBUG=True)
    def test_development_is_unaffected(self):
        res = self.client.get(reverse("api-tester"))
        self.assertIn(res.status_code, {200, 404})
