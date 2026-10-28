"""Embedded Signup: a tenant connects their own number. Meta is mocked."""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.tests.factories import make_tenant
from WhatsApp.models import WhatsAppAccount
from WhatsApp.services.meta_client import WhatsAppConfig

ENV = {"WHATSAPP_APP_SECRET": "app-secret"}
SETTINGS = {"WHATSAPP_APP_ID": "111", "WHATSAPP_ES_CONFIG_ID": "222", "WHATSAPP_GRAPH_VERSION": "v23.0"}
BODY = {"code": "one-time-code", "waba_id": "900", "phone_number_id": "800"}


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = str(self._data)

    def json(self):
        return self._data


def meta(overrides=None):
    """A fake Graph API: maps "METHOD path" to a FakeResponse."""
    routes = {
        "GET oauth/access_token": FakeResponse(data={"access_token": "tenant-token"}),
        "GET 900/phone_numbers": FakeResponse(
            data={"data": [{"id": "800", "display_phone_number": "+20 100 000 0000", "verified_name": "Tea Shop"}]}
        ),
        "POST 900/subscribed_apps": FakeResponse(data={"success": True}),
        "POST 800/register": FakeResponse(data={"success": True}),
        "DELETE 900/subscribed_apps": FakeResponse(data={"success": True}),
    }
    routes.update(overrides or {})
    calls = []

    def request(method, url, **kwargs):
        path = url.split("/v23.0/", 1)[1]
        calls.append((method, path, kwargs))
        return routes[f"{method} {path}"]

    return request, calls


@override_settings(**SETTINGS)
@mock.patch.dict("os.environ", ENV, clear=False)
class EmbeddedSignupTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("es_tenant")
        self.client.force_authenticate(self.user)

    def _signup(self, request, body=None):
        with mock.patch("WhatsApp.services.embedded_signup.requests.request", side_effect=request):
            return self.client.post(reverse("whatsapp-embedded-signup"), body or BODY, format="json")

    def test_connection_status_exposes_signup_config(self):
        res = self.client.get(reverse("whatsapp-connection"))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["connected"])
        self.assertIsNone(res.data["account"])
        self.assertEqual(
            res.data["signup"], {"enabled": True, "app_id": "111", "config_id": "222", "graph_version": "v23.0"}
        )

    def test_signup_connects_the_number(self):
        request, calls = meta()
        res = self._signup(request)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["connected"])
        self.assertEqual(res.data["warning"], "")
        self.assertEqual(res.data["account"]["display_phone_number"], "+20 100 000 0000")
        self.assertEqual(res.data["account"]["connection"], "embedded_signup")

        account = WhatsAppAccount.objects.get(tenant=self.user)
        self.assertEqual(account.phone_number_id, "800")
        self.assertEqual(account.waba_id, "900")
        self.assertEqual(account.display_name, "Tea Shop")
        self.assertEqual(account.access_token, "tenant-token")
        self.assertEqual(len(account.registration_pin), 6)
        # Replies go out with the tenant's own token.
        self.assertEqual(WhatsAppConfig.for_account(account).access_token, "tenant-token")

        exchange = calls[0][2]["params"]
        self.assertEqual(exchange, {"client_id": "111", "client_secret": "app-secret", "code": "one-time-code"})
        self.assertEqual(
            [f"{m} {p}" for m, p, _ in calls[1:]],
            [
                "GET 900/phone_numbers",
                "POST 900/subscribed_apps",
                "POST 800/register",
            ],
        )
        self.assertNotIn("tenant-token", str(res.data))

    def test_token_and_pin_are_encrypted_at_rest(self):
        request, _ = meta()
        self._signup(request)
        with connection.cursor() as cursor:
            cursor.execute("SELECT access_token, registration_pin FROM whatsapp_accounts")
            token, pin = cursor.fetchone()
        self.assertTrue(token.startswith("enc:"))
        self.assertNotIn("tenant-token", token)
        self.assertTrue(pin.startswith("enc:"))

    def test_number_outside_the_business_account_is_refused(self):
        request, _ = meta({"GET 900/phone_numbers": FakeResponse(data={"data": [{"id": "999"}]})})
        res = self._signup(request)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "number_not_in_waba")
        self.assertFalse(WhatsAppAccount.objects.exists())

    def test_number_of_another_tenant_is_refused_before_calling_meta(self):
        other, _ = make_tenant("es_other")
        WhatsAppAccount.objects.create(tenant=other, phone_number_id="800")
        request, calls = meta()
        res = self._signup(request)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "number_taken")
        self.assertEqual(calls, [])

    def test_meta_error_is_reported(self):
        bad = FakeResponse(400, {"error": {"message": "Invalid verification code format."}})
        request, _ = meta({"GET oauth/access_token": bad})
        res = self._signup(request)
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.data["code"], "meta_error")
        self.assertIn("Invalid verification code format.", res.data["detail"])

    def test_failed_registration_keeps_the_number_with_a_warning(self):
        bad = FakeResponse(400, {"error": {"message": "Number already registered."}})
        request, _ = meta({"POST 800/register": bad})
        res = self._signup(request)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Number already registered.", res.data["warning"])
        self.assertEqual(res.data["account"]["last_error"], res.data["warning"])
        self.assertTrue(res.data["connected"])

    def test_reconnecting_replaces_the_tenants_number(self):
        WhatsAppAccount.objects.create(tenant=self.user, phone_number_id="700", is_active=False)
        request, _ = meta()
        self._signup(request)
        account = WhatsAppAccount.objects.get(tenant=self.user)
        self.assertEqual(account.phone_number_id, "800")
        self.assertTrue(account.is_active)

    @override_settings(WHATSAPP_ES_CONFIG_ID="")
    def test_not_configured(self):
        request, calls = meta()
        res = self._signup(request)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["code"], "not_configured")
        self.assertEqual(calls, [])

    def test_feature_disabled(self):
        with mock.patch("subscriptions.features.has_feature", return_value=False):
            res = self.client.post(reverse("whatsapp-embedded-signup"), BODY, format="json")
        self.assertEqual(res.status_code, 402)

    def test_ids_must_be_numeric(self):
        res = self.client.post(
            reverse("whatsapp-embedded-signup"),
            {**BODY, "waba_id": "900/../me"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse("whatsapp-connection")).status_code, 401)
        self.assertEqual(self.client.post(reverse("whatsapp-embedded-signup"), BODY).status_code, 401)

    def test_disconnect(self):
        request, calls = meta()
        self._signup(request)
        with mock.patch("WhatsApp.services.embedded_signup.requests.request", side_effect=request):
            res = self.client.post(reverse("whatsapp-disconnect"))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["connected"])
        self.assertEqual(calls[-1][:2], ("DELETE", "900/subscribed_apps"))
        account = WhatsAppAccount.objects.get(tenant=self.user)
        self.assertFalse(account.is_active)
        self.assertEqual(account.access_token, "")

    def test_disconnect_without_a_number(self):
        res = self.client.post(reverse("whatsapp-disconnect"))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "not_connected")


class EncryptedFieldTests(TestCase):
    def test_plain_legacy_value_is_read_and_reencrypted(self):
        tenant, _ = make_tenant("es_legacy")
        account = WhatsAppAccount.objects.create(tenant=tenant, phone_number_id="555")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE whatsapp_accounts SET access_token = %s", ["plain-token"])
        account.refresh_from_db()
        self.assertEqual(account.access_token, "plain-token")
        account.save()
        with connection.cursor() as cursor:
            cursor.execute("SELECT access_token FROM whatsapp_accounts")
            self.assertTrue(cursor.fetchone()[0].startswith("enc:"))
        account.refresh_from_db()
        self.assertEqual(account.access_token, "plain-token")
