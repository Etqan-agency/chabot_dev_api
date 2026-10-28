"""WhatsApp HTTP surface: webhook verify/receive, send endpoint, and the
read-only viewsets' auth. Meta network calls are mocked."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.tests.factories import make_tenant
from WhatsApp import views as wv
from WhatsApp.models import WhatsAppAccount, WhatsAppMessage
from WhatsApp.services.meta_client import WhatsAppClientError


class WebhookVerifyTests(APITestCase):
    def setUp(self):
        self.url = reverse("whatsapp-webhook")

    def test_get_returns_challenge_when_token_valid(self):
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            Client.return_value.verify_webhook.return_value = True
            res = self.client.get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "t", "hub.challenge": "42"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode(), "42")

    def test_get_rejects_bad_token_with_403(self):
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            Client.return_value.verify_webhook.return_value = False
            res = self.client.get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "x"})
        self.assertEqual(res.status_code, 403)


APP_SECRET = "test-app-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _account(username, pnid):
    tenant, _ = make_tenant(username)
    return WhatsAppAccount.objects.create(tenant=tenant, phone_number_id=pnid)


def _msg(message_id, pnid, *, text="what do you sell?"):
    return {
        "message_text": text,
        "from_number": "20100",
        "phone_number_id": pnid,
        "profile_name": "T",
        "message_id": message_id,
    }


def _answer():
    from knowledge.services.rag import AnswerResult

    return AnswerResult(answer="we sell tea", source="rag")


@override_settings(WHATSAPP_PROCESS_MODE="sync")
@mock.patch.dict("os.environ", {"WHATSAPP_APP_SECRET": APP_SECRET}, clear=False)
class WebhookReceiveTests(APITestCase):
    """Behaviour of the webhook once a delivery is proven to be Meta's.

    Every request here is signed. Refusing an unsigned or forged one is covered
    in test_security.py; these cases are about what happens after that gate.
    """

    def setUp(self):
        self.url = reverse("whatsapp-webhook")

    def _raw(self, body: bytes):
        return self.client.post(
            self.url, data=body, content_type="application/json", headers={"x-hub-signature-256": _sign(body)}
        )

    def _post(self, payload):
        return self._raw(json.dumps(payload).encode())

    def test_invalid_json_returns_400(self):
        with mock.patch.object(wv, "MetaWhatsAppClient"):
            res = self._raw(b"{not json")
        self.assertEqual(res.status_code, 400)

    def test_no_message_returns_ok(self):
        with (
            mock.patch.object(wv, "MetaWhatsAppClient"),
            mock.patch.object(wv, "parse_status_updates", return_value=[]),
            mock.patch.object(wv, "parse_incoming_message", return_value=None),
        ):
            res = self._post({"entry": []})
        self.assertEqual(res.status_code, 200)

    def _deliver(self, msg, *, send=None, send_error=None):
        """Deliver ``msg`` through the real handler; only Meta's send is mocked."""
        with (
            mock.patch.object(wv, "MetaWhatsAppClient") as Client,
            mock.patch.object(wv, "parse_status_updates", return_value=[]),
            mock.patch.object(wv, "parse_incoming_message", return_value=msg),
            mock.patch("WhatsApp.services.conversation.answer_question", return_value=_answer()),
        ):
            Client.return_value.send_text.return_value = send or "wamid.out"
            if send_error:
                Client.return_value.send_text.side_effect = WhatsAppClientError(send_error)
            res = self._post({"entry": [{}]})
        return res, Client

    def test_incoming_message_is_answered_and_reply_sent(self):
        account = _account("wa_acme", "PNID")
        res, Client = self._deliver(_msg("wamid.in1", "PNID"), send="wamid.1")
        self.assertEqual(res.status_code, 200)
        Client.return_value.send_text.assert_called_once_with("20100", "we sell tea")
        # The reply goes FROM the number that received the message.
        self.assertEqual(Client.call_args.args[0].phone_number_id, account.phone_number_id)

    def test_each_message_is_logged_once_with_its_meta_ids(self):
        _account("wa_logs", "PNID")
        self._deliver(_msg("wamid.in2", "PNID"), send="wamid.2")
        rows = list(WhatsAppMessage.objects.order_by("created_at").values_list("message_type", "whatsapp_message_id"))
        self.assertEqual(rows, [("incoming", "wamid.in2"), ("outgoing", "wamid.2")])

    def test_a_redelivered_message_is_answered_once(self):
        _account("wa_dup", "PNID")
        msg = _msg("wamid.same", "PNID")
        _, first = self._deliver(msg)
        res, second = self._deliver(msg)
        self.assertEqual(res.status_code, 200)
        first.return_value.send_text.assert_called_once()
        second.return_value.send_text.assert_not_called()
        self.assertEqual(WhatsAppMessage.objects.filter(message_type="incoming").count(), 1)
        self.assertEqual(WhatsAppMessage.objects.filter(message_type="outgoing").count(), 1)

    def test_the_conversation_is_the_answers_context(self):
        _account("wa_history", "PNID")
        with mock.patch("WhatsApp.services.conversation.answer_question", return_value=_answer()) as aq:
            from WhatsApp.services import conversation

            conversation.handle_incoming_message(_msg("wamid.h1", "PNID", text="do you sell tea?"))
            conversation.handle_incoming_message(_msg("wamid.h2", "PNID", text="how much is it?"))
        self.assertEqual(aq.call_args_list[0].kwargs["history"], [])
        self.assertEqual(
            aq.call_args_list[1].kwargs["history"],
            [{"role": "user", "content": "do you sell tea?"}, {"role": "assistant", "content": "we sell tea"}],
        )

    @override_settings(WHATSAPP_HISTORY_MESSAGES=0)
    def test_history_can_be_turned_off(self):
        _account("wa_nohistory", "PNID")
        with mock.patch("WhatsApp.services.conversation.answer_question", return_value=_answer()) as aq:
            from WhatsApp.services import conversation

            conversation.handle_incoming_message(_msg("wamid.n1", "PNID"))
            conversation.handle_incoming_message(_msg("wamid.n2", "PNID"))
        self.assertEqual(aq.call_args.kwargs["history"], [])

    def test_handler_error_is_logged_and_acknowledged(self):
        # The delivery was accepted; a failure while answering must not make
        # Meta re-deliver (that would answer twice once it works).
        msg = _msg("wamid.err", "P")
        with (
            mock.patch.object(wv, "MetaWhatsAppClient") as Client,
            mock.patch.object(wv, "parse_status_updates", return_value=[]),
            mock.patch.object(wv, "parse_incoming_message", return_value=msg),
            mock.patch.object(wv, "handle_incoming_message", side_effect=RuntimeError("boom")),
        ):
            res = self._post({"entry": [{}]})
        self.assertEqual(res.status_code, 200)
        Client.return_value.send_text.assert_not_called()

    def test_delivery_failure_is_recorded_on_the_reply(self):
        _account("wa_fail", "PNID")
        res, _ = self._deliver(_msg("wamid.f1", "PNID"), send_error="no route")
        self.assertEqual(res.status_code, 200)
        out = WhatsAppMessage.objects.get(message_type="outgoing")
        self.assertEqual(out.status, "failed")
        self.assertIn("no route", out.error_message)

    @override_settings(WHATSAPP_PROCESS_MODE="thread")
    def test_thread_mode_acknowledges_before_answering(self):
        with (
            mock.patch.object(wv, "parse_status_updates", return_value=[]),
            mock.patch.object(wv, "parse_incoming_message", return_value=_msg("wamid.t1", "P")),
            mock.patch.object(wv.threading, "Thread") as Thread,
        ):
            res = self._post({"entry": [{}]})
        self.assertEqual(res.status_code, 200)
        Thread.assert_called_once()
        Thread.return_value.start.assert_called_once()


class SendEndpointTests(APITestCase):
    def setUp(self):
        self.url = reverse("whatsapp-send")
        self.user, _ = make_tenant("wa_sender")
        self.account = WhatsAppAccount.objects.create(tenant=self.user, phone_number_id="PN_SENDER")

    def test_requires_auth(self):
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 401)

    def test_invalid_body_returns_400(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(self.url, {"message": "hi"}, format="json").status_code, 400)

    def test_sends_from_the_callers_own_number(self):
        self.client.force_authenticate(self.user)
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            Client.return_value.send_text.return_value = "wamid.9"
            res = self.client.post(self.url, {"to_number": "20100", "message": "hi"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "sent")
        self.assertEqual(res.data["message_id"], "wamid.9")
        self.assertEqual(Client.call_args.args[0].phone_number_id, "PN_SENDER")

    def test_reply_to_a_known_contact_is_logged(self):
        from WhatsApp.services.conversation import get_or_create_session, get_or_create_user

        contact = get_or_create_user(phone_number="201000000077", account=self.account)
        get_or_create_session(contact)
        self.client.force_authenticate(self.user)
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            Client.return_value.send_text.return_value = "wamid.10"
            self.client.post(self.url, {"to_number": "201000000077", "message": "hello"}, format="json")
        out = WhatsAppMessage.objects.get(user=contact)
        self.assertEqual((out.message_type, out.whatsapp_message_id), ("outgoing", "wamid.10"))

    def test_without_a_connected_number_returns_400(self):
        other, _ = make_tenant("wa_nonumber")
        self.client.force_authenticate(other)
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            res = self.client.post(self.url, {"to_number": "20100", "message": "hi"}, format="json")
        self.assertEqual(res.status_code, 400)
        Client.return_value.send_text.assert_not_called()

    def test_a_tenant_cannot_send_from_another_account(self):
        other, _ = make_tenant("wa_other")
        other_account = WhatsAppAccount.objects.create(tenant=other, phone_number_id="PN_OTHER")
        self.client.force_authenticate(self.user)
        body = {"to_number": "20100", "message": "hi", "account": str(other_account.pk)}
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            res = self.client.post(self.url, body, format="json")
        self.assertEqual(res.status_code, 403)
        Client.return_value.send_text.assert_not_called()

    def test_staff_can_send_from_a_tenants_number(self):
        admin, _ = make_tenant("wa_admin", is_staff=True)
        self.client.force_authenticate(admin)
        body = {"to_number": "20100", "message": "hi", "account": str(self.account.pk)}
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            Client.return_value.send_text.return_value = "wamid.11"
            res = self.client.post(self.url, body, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Client.call_args.args[0].phone_number_id, "PN_SENDER")

    def test_client_error_returns_502(self):
        self.client.force_authenticate(self.user)
        with mock.patch.object(wv, "MetaWhatsAppClient") as Client:
            Client.return_value.send_text.side_effect = WhatsAppClientError("bad token")
            res = self.client.post(self.url, {"to_number": "20100", "message": "hi"}, format="json")
        self.assertEqual(res.status_code, 502)


class ReadOnlyViewsetAuthTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("wa_reader")

    def test_list_endpoints_require_auth(self):
        for name in (
            "whatsapp-user-list",
            "whatsapp-session-list",
            "whatsapp-message-list",
            "whatsapp-analytics-list",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 401, name)

    def test_tenant_listings_are_reachable_when_authenticated(self):
        # 200 with the caller's own rows -- which, for a tenant that has no
        # WhatsApp number linked, is none. Tenant scoping itself is covered in
        # test_security.py.
        self.client.force_authenticate(self.user)
        for name in (
            "whatsapp-user-list",
            "whatsapp-session-list",
            "whatsapp-message-list",
        ):
            res = self.client.get(reverse(name))
            self.assertEqual(res.status_code, 200, name)
            self.assertEqual(res.data["count"], 0, name)

    def test_platform_analytics_need_staff(self):
        # Rows are cross-tenant daily totals; a tenant must not see them.
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(reverse("whatsapp-analytics-list")).status_code, 403)
