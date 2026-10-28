"""Multi-tenant WhatsApp routing: business number -> tenant -> knowledge."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.services.rag import AnswerResult
from knowledge.tests.factories import make_tenant
from WhatsApp.models import WhatsAppAccount
from WhatsApp.services import conversation


def _msg(text="hello", number="201000000000", pnid="", name="Tester"):
    return {
        "message_text": text,
        "from_number": number,
        "phone_number_id": pnid,
        "profile_name": name,
    }


class TenantResolutionTests(TestCase):
    def test_resolves_tenant_by_phone_number_id(self):
        tenant, _ = make_tenant("acme")
        WhatsAppAccount.objects.create(tenant=tenant, phone_number_id="PNID123")
        account, resolved = conversation.resolve_account_and_tenant("PNID123")
        self.assertEqual(resolved, tenant)
        self.assertEqual(account.tenant, tenant)

    def test_unknown_number_resolves_to_none(self):
        account, resolved = conversation.resolve_account_and_tenant("NOPE")
        self.assertIsNone(account)
        self.assertIsNone(resolved)

    def test_inactive_account_is_ignored(self):
        tenant, _ = make_tenant("acme")
        WhatsAppAccount.objects.create(tenant=tenant, phone_number_id="PNID123", is_active=False)
        _, resolved = conversation.resolve_account_and_tenant("PNID123")
        self.assertIsNone(resolved)


class IncomingMessageRoutingTests(TestCase):
    def test_answers_from_the_mapped_tenant(self):
        tenant, _ = make_tenant("acme")
        WhatsAppAccount.objects.create(tenant=tenant, phone_number_id="PNID123")

        fake = AnswerResult(answer="answer from acme", source="rag")
        with mock.patch.object(conversation, "answer_question", return_value=fake) as aq:
            result = conversation.handle_incoming_message(_msg(text="what do you offer?", pnid="PNID123"))
        reply, account = result.reply, result.account

        self.assertEqual(reply, "answer from acme")
        self.assertEqual(account.tenant, tenant)
        # The tenant was passed to the RAG call → answer scoped to their data.
        self.assertEqual(aq.call_args.kwargs["user"], tenant)

    def test_unconnected_number_returns_friendly_message(self):
        result = conversation.handle_incoming_message(_msg(text="hi", pnid="UNCONNECTED"))
        reply, account = result.reply, result.account
        self.assertIsNone(account)
        self.assertIn("isn't connected", reply)

    def test_help_command_works_without_tenant(self):
        reply = conversation.handle_incoming_message(_msg(text="/help", pnid="X")).reply
        self.assertIn("Commands", reply)


class AccountAdminApiTests(APITestCase):
    def setUp(self):
        self.tenant, _ = make_tenant("acme")
        self.user, _ = make_tenant("bob")
        self.admin, _ = make_tenant("root", is_staff=True)

    def test_admin_can_link_account(self):
        self.client.force_authenticate(self.admin)
        res = self.client.post(
            reverse("whatsapp-account-list"),
            {"tenant": str(self.tenant.id), "phone_number_id": "PNID999", "display_name": "Acme"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(WhatsAppAccount.objects.filter(phone_number_id="PNID999").exists())
        self.assertNotIn("access_token", res.data)  # token is write-only

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(self.user)
        res = self.client.get(reverse("whatsapp-account-list"))
        self.assertEqual(res.status_code, 403)
