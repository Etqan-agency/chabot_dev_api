import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from knowledge.tests.factories import make_hit, make_tenant

from .models import MenuItem, Order, VoiceAgent, VoiceCall
from .services.orders import normalize_name

WEBHOOK_SECRET = "wsec_test"


def make_restaurant(username, *, delivery_fee="20", agent_id=""):
    user, _ = make_tenant(username)
    agent = VoiceAgent.objects.create(tenant=user, delivery_fee=Decimal(delivery_fee), agent_id=agent_id)
    token = agent.issue_token()
    shawarma = MenuItem.objects.create(tenant=user, name="شاورما فراخ", price=Decimal("85"))
    fries = MenuItem.objects.create(tenant=user, name="بطاطس", price=Decimal("35"))
    return user, agent, token, shawarma, fries


def tool_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def sign(body: bytes, *, secret=WEBHOOK_SECRET, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v0={digest}"


class ToolAuthTests(TestCase):
    def setUp(self):
        self.user, self.agent, self.token, *_ = make_restaurant("resto")

    def test_missing_or_wrong_token_is_rejected(self):
        self.assertEqual(APIClient().get(reverse("voice-tool-menu")).status_code, 401)
        self.assertEqual(tool_client("vt_wrong").get(reverse("voice-tool-menu")).status_code, 401)

    def test_rotated_token_stops_working(self):
        self.agent.issue_token()
        self.assertEqual(tool_client(self.token).get(reverse("voice-tool-menu")).status_code, 401)

    def test_inactive_agent_is_rejected(self):
        self.agent.is_active = False
        self.agent.save()
        self.assertEqual(tool_client(self.token).get(reverse("voice-tool-menu")).status_code, 401)

    def test_token_is_not_stored_in_plaintext(self):
        self.agent.refresh_from_db()
        self.assertNotIn(self.token, (self.agent.token_hash, self.agent.token_prefix))


class MenuToolTests(TestCase):
    def test_only_own_available_items(self):
        user, _, token, shawarma, fries = make_restaurant("a")
        fries.is_available = False
        fries.save()
        make_restaurant("b")
        res = tool_client(token).get(reverse("voice-tool-menu"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual([i["item_id"] for i in res.json()["items"]], [shawarma.pk])
        self.assertEqual(res.json()["delivery_fee"], "20.00")


class CreateOrderToolTests(TestCase):
    def setUp(self):
        self.user, self.agent, self.token, self.shawarma, self.fries = make_restaurant("resto")
        self.client = tool_client(self.token)

    def order(self, **overrides):
        body = {
            "items": [
                {"item_id": self.shawarma.pk, "quantity": 2, "notes": "سبايسي"},
                {"item_id": self.fries.pk, "quantity": 1},
            ],
            "fulfilment": "delivery",
            "customer_name": "أحمد",
            "address": "مدينة نصر، شارع عباس العقاد",
            "conversation_id": "conv_1",
        }
        body.update(overrides)
        return self.client.post(reverse("voice-tool-orders"), body, format="json")

    def test_prices_and_totals_come_from_the_menu(self):
        res = self.order(items=[{"item_id": self.shawarma.pk, "quantity": 2, "price": "1", "total": "0"}])
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["subtotal"], "170.00")
        self.assertEqual(body["delivery_fee"], "20.00")
        self.assertEqual(body["total"], "190.00")
        order = Order.objects.get()
        self.assertEqual(order.tenant, self.user)
        self.assertEqual(order.items.get().unit_price, Decimal("85"))

    def test_pickup_has_no_delivery_fee_or_address(self):
        res = self.order(fulfilment="pickup", address="ignored")
        self.assertEqual(res.json()["total"], "205.00")
        self.assertEqual(Order.objects.get().address, "")

    def test_same_conversation_never_creates_a_second_order(self):
        first = self.order()
        second = self.order()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["order_number"], second.json()["order_number"])
        self.assertEqual(Order.objects.count(), 1)

    def test_order_numbers_count_up_per_tenant(self):
        self.order(conversation_id="c1")
        res = self.order(conversation_id="c2")
        self.assertEqual(res.json()["order_number"], 2)
        _, _, other_token, other_item, _ = make_restaurant("other")
        res = tool_client(other_token).post(
            reverse("voice-tool-orders"),
            {"items": [{"item_id": other_item.pk, "quantity": 1}], "fulfilment": "pickup"},
            format="json",
        )
        self.assertEqual(res.json()["order_number"], 1)

    def test_item_matched_by_name_despite_spelling_variants(self):
        res = self.order(items=[{"name": "شاورمَا  فراخ", "quantity": 1}])
        self.assertTrue(res.json()["ok"])
        self.assertEqual(normalize_name("فرخة مشوية"), normalize_name("فرخه مشويه"))

    def test_business_errors_are_spoken_back_not_saved(self):
        self.fries.is_available = False
        self.fries.save()
        cases = [
            {"items": [{"name": "بيتزا", "quantity": 1}]},
            {"items": [{"item_id": self.fries.pk, "quantity": 1}]},
            {"address": ""},
            {"items": [{"item_id": self.shawarma.pk, "quantity": 500}]},
            {"items": []},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                res = self.order(**overrides)
                self.assertEqual(res.status_code, 200)
                self.assertFalse(res.json()["ok"])
        self.assertEqual(Order.objects.count(), 0)

    def test_another_tenants_item_id_is_unknown(self):
        _, _, _, foreign_item, _ = make_restaurant("other")
        res = self.order(items=[{"item_id": foreign_item.pk, "quantity": 1}])
        self.assertFalse(res.json()["ok"])
        self.assertEqual(Order.objects.count(), 0)


class QuoteToolTests(TestCase):
    def test_quote_prices_from_the_menu_and_saves_nothing(self):
        _, _, token, shawarma, fries = make_restaurant("resto")
        res = tool_client(token).post(
            reverse("voice-tool-quote"),
            {
                "items": [
                    {"item_id": shawarma.pk, "quantity": 2, "price": "1"},
                    {"item_id": fries.pk, "quantity": 1},
                ],
                "fulfilment": "delivery",
            },
            format="json",
        )
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual((body["subtotal"], body["delivery_fee"], body["total"]), ("205.00", "20.00", "225.00"))
        self.assertEqual(Order.objects.count(), 0)

    def test_quote_rejects_items_not_on_the_menu(self):
        _, _, token, *_ = make_restaurant("resto")
        res = tool_client(token).post(
            reverse("voice-tool-quote"),
            {"items": [{"name": "حواوشي موزاريلا", "quantity": 1}], "fulfilment": "pickup"},
            format="json",
        )
        self.assertFalse(res.json()["ok"])


class KnowledgeToolTests(TestCase):
    def test_search_is_scoped_to_the_token_tenant(self):
        user, _, token, *_ = make_restaurant("resto")
        hit = make_hit("مفتوحين لحد ٢ بالليل")
        with mock.patch("voice.services.tools.search_chunks", return_value=[hit]) as search:
            res = tool_client(token).post(reverse("voice-tool-knowledge"), {"query": "مواعيدكم؟"}, format="json")
        self.assertEqual(res.json(), {"ok": True, "results": ["مفتوحين لحد ٢ بالليل"]})
        self.assertEqual(search.call_args.kwargs["user"], user)

    def test_empty_search_retries_without_threshold(self):
        _, _, token, *_ = make_restaurant("resto")
        with mock.patch("voice.services.tools.search_chunks", side_effect=[[], [make_hit("الفروع")]]) as search:
            res = tool_client(token).post(reverse("voice-tool-knowledge"), {"query": "فين؟"}, format="json")
        self.assertEqual(res.json()["results"], ["الفروع"])
        self.assertEqual(search.call_args.kwargs["threshold"], 0.0)

    def test_search_failure_is_reported_not_raised(self):
        _, _, token, *_ = make_restaurant("resto")
        with mock.patch("voice.services.tools.search_chunks", side_effect=RuntimeError("down")):
            res = tool_client(token).post(reverse("voice-tool-knowledge"), {"query": "x"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["ok"])


class WebhookTests(TestCase):
    def setUp(self):
        self.user, self.agent, *_ = make_restaurant("resto", agent_id="agent_1")
        patcher = mock.patch.dict("os.environ", {"ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def payload(self, **data):
        base = {
            "agent_id": "agent_1",
            "conversation_id": "conv_1",
            "status": "done",
            "transcript": [
                {"role": "agent", "message": "أهلاً، تحب تطلب إيه؟", "time_in_call_secs": 0},
                {"role": "user", "message": "اتنين شاورما", "time_in_call_secs": 3},
                {"role": "agent", "message": None, "tool_calls": [{}], "time_in_call_secs": 9},
            ],
            "metadata": {"start_time_unix_secs": 1761700000, "call_duration_secs": 95},
            "analysis": {"transcript_summary": "طلب شاورما"},
        }
        base.update(data)
        return json.dumps({"type": "post_call_transcription", "event_timestamp": 1, "data": base}).encode()

    def post(self, body, signature):
        return self.client.post(
            reverse("voice-webhook-elevenlabs"),
            body,
            content_type="application/json",
            HTTP_ELEVENLABS_SIGNATURE=signature,
        )

    def test_records_call_and_links_its_order(self):
        order = Order.objects.create(tenant=self.user, number=1, conversation_id="conv_1")
        body = self.payload()
        self.assertEqual(self.post(body, sign(body)).status_code, 200)
        call = VoiceCall.objects.get()
        self.assertEqual((call.tenant, call.order, call.duration_secs), (self.user, order, 95))
        self.assertEqual(len(call.transcript), 2)
        self.assertEqual(call.summary, "طلب شاورما")

    def test_redelivery_updates_instead_of_duplicating(self):
        body = self.payload()
        self.post(body, sign(body))
        self.post(body, sign(body))
        self.assertEqual(VoiceCall.objects.count(), 1)

    def test_bad_stale_or_missing_signature_is_refused(self):
        body = self.payload()
        for signature in ["", sign(body, secret="other"), sign(body, timestamp=int(time.time()) - 3600)]:
            with self.subTest(signature=signature):
                self.assertEqual(self.post(body, signature).status_code, 401)
        self.assertEqual(VoiceCall.objects.count(), 0)

    def test_unknown_agent_is_acknowledged_but_not_stored(self):
        body = self.payload(agent_id="someone_else")
        self.assertEqual(self.post(body, sign(body)).status_code, 200)
        self.assertEqual(VoiceCall.objects.count(), 0)


class ElevenLabsSyncTests(TestCase):
    """The provisioning command talks to ElevenLabs only through ``_call``."""

    def setUp(self):
        self.user, self.agent, *_ = make_restaurant("resto")
        self.calls = []
        self.tools = {"t_menu": "get_menu", "t_other": "end_call"}

        def fake_call(method, path, *, json=None):
            self.calls.append((method, path, json))
            if path == "/agents/agent_1" and method == "GET":
                return {"conversation_config": {"agent": {"prompt": {"tool_ids": list(self.tools)}}}}
            if path.startswith("/tools/") and method == "GET":
                return {"tool_config": {"name": self.tools[path.split("/")[-1]]}}
            if path == "/tools" and method == "POST":
                return {"id": f"new_{json['tool_config']['name']}"}
            return {}

        patcher = mock.patch("voice.services.elevenlabs._call", side_effect=fake_call)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_sync(self, **options):
        from django.core.management import call_command

        call_command(
            "voice_elevenlabs_sync",
            "resto",
            base_url="https://demo.example.com",
            agent_id="agent_1",
            stdout=mock.Mock(),
            **options,
        )

    def agent_patch(self):
        return next(
            body["conversation_config"]
            for method, path, body in self.calls
            if method == "PATCH" and path == "/agents/agent_1"
        )

    def test_updates_existing_tools_creates_missing_and_keeps_others(self):
        self.run_sync()
        patched = [path for method, path, _ in self.calls if method == "PATCH" and path.startswith("/tools/")]
        created = [body["tool_config"]["name"] for method, path, body in self.calls if path == "/tools"]
        self.assertEqual(patched, ["/tools/t_menu"])
        self.assertEqual(created, ["search_info", "quote_order", "create_order"])
        self.assertEqual(
            self.agent_patch()["agent"]["prompt"]["tool_ids"],
            ["t_other", "t_menu", "new_search_info", "new_quote_order", "new_create_order"],
        )
        self.assertNotIn("tts", self.agent_patch())  # no voice chosen -> the agent's voice is left alone

    def test_persona_and_voice_are_applied_and_remembered(self):
        self.run_sync(persona="male", voice_id="voice_eg", assistant_name="محمود")
        config = self.agent_patch()
        self.assertEqual(config["tts"], {"voice_id": "voice_eg", "supported_voices": []})
        self.assertTrue(config["agent"]["prompt"]["prompt"].startswith('انت "محمود"'))
        self.assertIn("معاك محمود", config["agent"]["first_message"])

        self.calls.clear()
        self.run_sync()  # e.g. a new tunnel URL: persona and voice are kept
        self.assertEqual(self.agent_patch()["tts"]["voice_id"], "voice_eg")
        self.assertTrue(self.agent_patch()["agent"]["prompt"]["prompt"].startswith('انت "محمود"'))

    def test_tools_carry_the_new_token_and_url(self):
        self.run_sync()
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.agent_id, "agent_1")
        config = next(
            body["tool_config"] for method, path, body in self.calls if method == "PATCH" and path == "/tools/t_menu"
        )
        self.assertEqual(config["api_schema"]["url"], "https://demo.example.com/api/v1/voice/tools/menu/")
        token = config["api_schema"]["request_headers"]["Authorization"].removeprefix("Bearer ")
        self.assertEqual(tool_client(token).get(reverse("voice-tool-menu")).status_code, 200)


class DashboardTests(TestCase):
    def setUp(self):
        self.user, self.agent, _, self.shawarma, _ = make_restaurant("resto")
        self.other, _, _, self.other_item, _ = make_restaurant("other")
        self.order = Order.objects.create(tenant=self.user, number=1, total=Decimal("100"))
        self.other_order = Order.objects.create(tenant=self.other, number=1)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_orders_are_tenant_scoped(self):
        res = self.client.get(reverse("voice-order-list"))
        self.assertEqual([o["id"] for o in res.json()["results"]], [str(self.order.pk)])
        url = reverse("voice-order-detail", args=[self.other_order.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"status": "cancelled"}).status_code, 404)

    def test_dashboard_can_only_change_status(self):
        url = reverse("voice-order-detail", args=[self.order.pk])
        res = self.client.patch(url, {"status": "preparing", "total": "1"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.total), ("preparing", Decimal("100")))

    def test_menu_crud_is_tenant_scoped(self):
        res = self.client.post(reverse("voice-menu-item-list"), {"name": "كنافة", "price": "60"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(MenuItem.objects.get(pk=res.json()["id"]).tenant, self.user)
        ids = [i["id"] for i in self.client.get(reverse("voice-menu-item-list")).json()]
        self.assertEqual(len(ids), 3)
        self.assertNotIn(self.other_item.pk, ids)
        url = reverse("voice-menu-item-detail", args=[self.other_item.pk])
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_token_is_shown_once(self):
        url = reverse("voice-agent")
        issued = self.client.post(url).json()
        self.assertTrue(issued["token"].startswith("vt_"))
        shown = self.client.get(url).json()
        self.assertNotIn("token", shown)
        self.assertTrue(shown["has_token"])
        res = tool_client(issued["token"]).get(reverse("voice-tool-menu"))
        self.assertEqual(res.status_code, 200)

    def test_agent_id_cannot_be_claimed_twice(self):
        VoiceAgent.objects.filter(tenant=self.other).update(agent_id="agent_x")
        res = self.client.patch(reverse("voice-agent"), {"agent_id": "agent_x"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_calls_are_tenant_scoped(self):
        VoiceCall.objects.create(tenant=self.other, conversation_id="theirs")
        mine = VoiceCall.objects.create(tenant=self.user, conversation_id="mine", transcript=[{"role": "user"}])
        res = self.client.get(reverse("voice-call-list"))
        self.assertEqual([c["id"] for c in res.json()["results"]], [str(mine.pk)])
        detail = self.client.get(reverse("voice-call-detail", args=[mine.pk])).json()
        self.assertEqual(detail["transcript"], [{"role": "user"}])
