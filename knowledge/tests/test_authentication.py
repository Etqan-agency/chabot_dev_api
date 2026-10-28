"""API-key authentication paths (the front door of multi-tenancy)."""

from __future__ import annotations

from django.urls import reverse
from rest_framework.test import APITestCase

from Authentication.models import APIKey

from .factories import make_tenant


class ApiKeyAuthTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.url = reverse("document-list")  # any IsAuthenticated endpoint

    def test_x_api_key_header_works(self):
        res = self.client.get(self.url, headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)

    def test_authorization_apikey_scheme_works(self):
        res = self.client.get(self.url, headers={"authorization": f"ApiKey {self.key}"})
        self.assertEqual(res.status_code, 200)

    def test_invalid_key_rejected(self):
        res = self.client.get(self.url, headers={"x-api-key": "deadbeef"})
        self.assertEqual(res.status_code, 401)

    def test_missing_key_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_inactive_key_rejected(self):
        ak = APIKey.objects.get(user=self.user)
        ak.is_active = False
        ak.save(update_fields=["is_active"])
        res = self.client.get(self.url, headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 401)

    def test_inactive_user_rejected(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        res = self.client.get(self.url, headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 401)

    def test_last_used_at_is_updated(self):
        ak = APIKey.objects.get(user=self.user)
        self.assertIsNone(ak.last_used_at)
        self.client.get(self.url, headers={"x-api-key": self.key})
        ak.refresh_from_db()
        self.assertIsNotNone(ak.last_used_at)


class JwtAuthTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("alice")

    def test_login_returns_tokens(self):
        res = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "alice", "password": "Test!Pass2026"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.data)

    def test_jwt_access_grants_api(self):
        login = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "alice", "password": "Test!Pass2026"},
            format="json",
        )
        token = login.data["access"]
        res = self.client.get(reverse("document-list"), headers={"authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 200)
