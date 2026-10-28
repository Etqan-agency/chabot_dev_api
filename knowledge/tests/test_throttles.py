"""The per-action DRF rates (settings.DEFAULT_THROTTLE_RATES) reach their views."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from .factories import make_tenant


class ScopedRateTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user, self.key = make_tenant("rate_tenant")

    def test_upload_rate_limited(self):
        url = reverse("document-upload-word")
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"upload": "2/min"}):
            # No file: rejected 400 before parsing, so only the throttle decides the 3rd.
            codes = [self.client.post(url, {}, headers={"x-api-key": self.key}).status_code for _ in range(3)]
        self.assertEqual(codes, [400, 400, 429])

    def test_browsing_documents_not_rate_limited(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"upload": "1/min"}):
            for _ in range(5):
                res = self.client.get(reverse("document-list"), headers={"x-api-key": self.key})
                self.assertEqual(res.status_code, 200)

    def test_email_code_rate_limited(self):
        self.client.force_authenticate(self.user)
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"email_code": "2/min"}):
            codes = [self.client.post(reverse("user-request-password-code")).status_code for _ in range(3)]
        self.assertNotEqual(codes[0], 429)
        self.assertNotEqual(codes[1], 429)
        self.assertEqual(codes[2], 429)
