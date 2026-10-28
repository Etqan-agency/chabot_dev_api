"""Security boundaries of the WhatsApp subsystem.

Two holes are pinned here because both were open in production:

1. Every WhatsApp viewset exposed ``Model.objects.all()`` behind nothing but
   IsAuthenticated, so any tenant could read every other tenant's contacts,
   sessions and conversation text.
2. The webhook accepted any POST, so anyone knowing the URL could spend a
   tenant's credits and make their business number send a message to an
   arbitrary phone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.tests.factories import make_tenant

from .models import WhatsAppAccount, WhatsAppMessage, WhatsAppSession, WhatsAppUser

APP_SECRET = "test-app-secret"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _account(tenant, phone_number_id: str) -> WhatsAppAccount:
    return WhatsAppAccount.objects.create(tenant=tenant, phone_number_id=phone_number_id)


def _contact(account, phone: str) -> WhatsAppUser:
    return WhatsAppUser.objects.create(account=account, phone_number=phone)


class TenantIsolationTests(APITestCase):
    def setUp(self):
        self.alice, _ = make_tenant("wa_alice")
        self.bob, _ = make_tenant("wa_bob")
        self.alice_account = _account(self.alice, "pn-alice")
        self.bob_account = _account(self.bob, "pn-bob")

        self.alice_contact = _contact(self.alice_account, "1111111111")
        self.bob_contact = _contact(self.bob_account, "2222222222")

        self.bob_session = WhatsAppSession.objects.create(user=self.bob_contact)
        self.bob_message = WhatsAppMessage.objects.create(
            user=self.bob_contact,
            session=self.bob_session,
            message_type="incoming",
            message_text="bob's customer said something confidential",
        )

    def _as_alice(self):
        self.client.force_authenticate(user=self.alice)

    def test_a_tenant_cannot_list_another_tenants_contacts(self):
        self._as_alice()
        res = self.client.get(reverse("whatsapp-user-list"))
        self.assertEqual(res.status_code, 200)
        numbers = {row["phone_number"] for row in res.data["results"]}
        self.assertEqual(numbers, {"1111111111"})

    def test_a_tenant_cannot_retrieve_another_tenants_contact_by_id(self):
        self._as_alice()
        res = self.client.get(reverse("whatsapp-user-detail", args=[str(self.bob_contact.pk)]))
        self.assertEqual(res.status_code, 404)

    def test_a_tenant_cannot_read_another_tenants_conversations(self):
        self._as_alice()
        res = self.client.get(reverse("whatsapp-message-list"))
        self.assertEqual(res.data["count"], 0)
        detail = self.client.get(reverse("whatsapp-message-detail", args=[str(self.bob_message.pk)]))
        self.assertEqual(detail.status_code, 404)

    def test_the_message_search_cannot_reach_across_tenants(self):
        # search_fields includes message_text: without scoping this was a
        # full-text search over every tenant's customer conversations.
        self._as_alice()
        res = self.client.get(reverse("whatsapp-message-list"), {"search": "confidential"})
        self.assertEqual(res.data["count"], 0)

    def test_a_tenant_cannot_read_another_tenants_sessions(self):
        self._as_alice()
        res = self.client.get(reverse("whatsapp-session-detail", args=[str(self.bob_session.pk)]))
        self.assertEqual(res.status_code, 404)

    def test_a_tenant_with_no_whatsapp_number_sees_nothing(self):
        stranger, _ = make_tenant("wa_stranger")
        self.client.force_authenticate(user=stranger)
        self.assertEqual(self.client.get(reverse("whatsapp-user-list")).data["count"], 0)
        self.assertEqual(self.client.get(reverse("whatsapp-message-list")).data["count"], 0)

    def test_platform_analytics_are_not_visible_to_a_tenant(self):
        # Rows are per-day totals across ALL tenants; showing them tells a
        # tenant how much traffic everyone else handles.
        self._as_alice()
        self.assertEqual(self.client.get(reverse("whatsapp-analytics-list")).status_code, 403)

    def test_staff_keep_the_unscoped_view(self):
        staff, _ = make_tenant("wa_staff", is_staff=True)
        self.client.force_authenticate(user=staff)
        res = self.client.get(reverse("whatsapp-user-list"))
        self.assertEqual(res.data["count"], 2)


class ContactUniquenessTests(APITestCase):
    def test_one_person_writing_to_two_tenants_gets_two_contacts(self):
        alice, _ = make_tenant("u_alice")
        bob, _ = make_tenant("u_bob")
        a = _account(alice, "pn-a")
        b = _account(bob, "pn-b")

        first = _contact(a, "9999999999")
        second = _contact(b, "9999999999")

        # Same human, two tenants, two separate records -- so neither tenant's
        # session or history is reachable through the other's.
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(WhatsAppUser.objects.filter(phone_number="9999999999").count(), 2)

    def test_the_same_number_cannot_be_added_twice_to_one_account(self):
        from django.db import IntegrityError, transaction

        alice, _ = make_tenant("u_alice2")
        a = _account(alice, "pn-a2")
        _contact(a, "8888888888")
        with self.assertRaises(IntegrityError), transaction.atomic():
            _contact(a, "8888888888")


@override_settings(WHATSAPP_PROCESS_MODE="sync")
class WebhookAuthenticationTests(APITestCase):
    """The webhook must prove a delivery is Meta's before acting on it."""

    def setUp(self):
        self.url = reverse("whatsapp-webhook")
        self.payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-victim"},
                                "messages": [
                                    {
                                        "from": "15550000000",
                                        "id": "wamid.x",
                                        "type": "text",
                                        "text": {"body": "hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        self.body = json.dumps(self.payload).encode()

    def _post(self, *, signature=None, body=None):
        kwargs = {"content_type": "application/json"}
        if signature is not None:
            kwargs["HTTP_X_HUB_SIGNATURE_256"] = signature
        return self.client.post(self.url, data=body if body is not None else self.body, **kwargs)

    @mock.patch.dict("os.environ", {"WHATSAPP_APP_SECRET": APP_SECRET}, clear=False)
    def test_an_unsigned_delivery_is_refused(self):
        with mock.patch("WhatsApp.views.handle_incoming_message") as handler:
            res = self._post()
        self.assertEqual(res.status_code, 403)
        handler.assert_not_called()

    @mock.patch.dict("os.environ", {"WHATSAPP_APP_SECRET": APP_SECRET}, clear=False)
    def test_a_forged_signature_is_refused(self):
        with mock.patch("WhatsApp.views.handle_incoming_message") as handler:
            res = self._post(signature=_sign(self.body, "the-wrong-secret"))
        self.assertEqual(res.status_code, 403)
        handler.assert_not_called()

    @mock.patch.dict("os.environ", {"WHATSAPP_APP_SECRET": APP_SECRET}, clear=False)
    def test_a_tampered_body_is_refused(self):
        # Signature valid for the original bytes, body swapped afterwards.
        signature = _sign(self.body)
        tampered = json.dumps({**self.payload, "injected": True}).encode()
        with mock.patch("WhatsApp.views.handle_incoming_message") as handler:
            res = self._post(signature=signature, body=tampered)
        self.assertEqual(res.status_code, 403)
        handler.assert_not_called()

    @mock.patch.dict("os.environ", {"WHATSAPP_APP_SECRET": APP_SECRET}, clear=False)
    def test_a_correctly_signed_delivery_is_processed(self):
        with (
            mock.patch("WhatsApp.views.handle_incoming_message", return_value=None) as handler,
            mock.patch("WhatsApp.views.MetaWhatsAppClient"),
            mock.patch("WhatsApp.views.log_message"),
        ):
            res = self._post(signature=_sign(self.body))
        self.assertEqual(res.status_code, 200)
        handler.assert_called_once()

    @mock.patch.dict("os.environ", {"WHATSAPP_APP_SECRET": "", "WHATSAPP_REQUIRE_SIGNATURE": "1"}, clear=False)
    def test_with_no_secret_configured_nothing_is_accepted(self):
        # Fail closed: an unconfigured deployment must not be an open one.
        with mock.patch("WhatsApp.views.handle_incoming_message") as handler:
            res = self._post(signature=_sign(self.body))
        self.assertEqual(res.status_code, 403)
        handler.assert_not_called()

    @mock.patch.dict(
        "os.environ",
        {"WHATSAPP_APP_SECRET": "", "WHATSAPP_REQUIRE_SIGNATURE": "0"},
        clear=False,
    )
    def test_the_escape_hatch_still_works_but_is_explicit(self):
        with (
            mock.patch("WhatsApp.views.handle_incoming_message", return_value=None) as handler,
            mock.patch("WhatsApp.views.MetaWhatsAppClient"),
            mock.patch("WhatsApp.views.log_message"),
        ):
            res = self._post()
        self.assertEqual(res.status_code, 200)
        handler.assert_called_once()
