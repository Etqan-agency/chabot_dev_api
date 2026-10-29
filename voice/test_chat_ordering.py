"""WhatsApp ordering: the model proposes, the server prices and places (no network)."""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from knowledge.tests.factories import make_tenant
from WhatsApp.models import WhatsAppAccount, WhatsAppSession
from WhatsApp.services import conversation

from .models import MenuItem, Order, VoiceAgent
from .services import chat_ordering


class Block(SimpleNamespace):
    """A response content block, as the SDK returns it."""

    def model_dump(self, **_):
        return dict(vars(self))


ENV = {"ANTHROPIC_API_KEY": "test-key", "ORDERING_MODEL": "", "LLM_MODEL": "claude-opus-5-5"}


def text(answer):
    return SimpleNamespace(
        stop_reason="end_turn",
        model="claude-opus-5-5",
        content=[Block(type="text", text=answer)],
        usage=SimpleNamespace(input_tokens=500, output_tokens=60),
    )


def tool(name, args, call_id="tu_1"):
    return SimpleNamespace(
        stop_reason="tool_use",
        model="claude-opus-5-5",
        content=[Block(type="tool_use", id=call_id, name=name, input=args)],
        usage=SimpleNamespace(input_tokens=400, output_tokens=40),
    )


def msg(body, *, message_id):
    return {
        "message_text": body,
        "from_number": "201234567890",
        "phone_number_id": "PN_AWAD",
        "profile_name": "Islam",
        "message_id": message_id,
    }


@mock.patch.dict("os.environ", ENV)
class WhatsAppOrderingTests(TestCase):
    def setUp(self):
        self.tenant, _ = make_tenant("awad")
        WhatsAppAccount.objects.create(tenant=self.tenant, phone_number_id="PN_AWAD")
        self.agent = VoiceAgent.objects.create(tenant=self.tenant, delivery_fee=Decimal("15"))
        self.kofta = MenuItem.objects.create(tenant=self.tenant, name="ساندوتش كفتة", price=Decimal("48"))
        self.hawawshi = MenuItem.objects.create(tenant=self.tenant, name="ساندوتش حواوشي", price=Decimal("38"))
        self.client_mock = mock.Mock()
        patcher = mock.patch("anthropic.Anthropic", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.meter = mock.patch("voice.services.chat_ordering.meter").start()
        self.addCleanup(mock.patch.stopall)

    def script(self, *responses):
        self.client_mock.beta.messages.create.side_effect = list(responses)

    def prepare_args(self, **overrides):
        args = {
            "items": [
                {"item_id": self.kofta.pk, "quantity": 2, "notes": "من غير طحينة"},
                {"item_id": self.hawawshi.pk, "quantity": 1, "notes": ""},
            ],
            "fulfilment": "delivery",
            "customer_name": "إسلام",
            "address": "طنطا، شارع البحر 12",
            "order_notes": "",
        }
        args.update(overrides)
        return args

    def session(self):
        return WhatsAppSession.objects.get()

    def test_order_is_prepared_then_placed_exactly_as_confirmed(self):
        self.script(
            tool("prepare_order", self.prepare_args()),
            text("ده ملخص طلب حضرتك… الإجمالي 149 جنيه. أأكد؟"),
            tool("confirm_order", {}),
            text("تم! رقم الأوردر 1"),
        )
        first = conversation.handle_incoming_message(msg("عايز اتنين كفتة وحواوشي", message_id="m1"))
        self.assertIn("149", first.reply)
        self.assertEqual(Order.objects.count(), 0)  # nothing placed before confirmation
        pending = self.session().context_data["pending_order"]
        self.assertEqual(pending["customer_name"], "إسلام")

        second = conversation.handle_incoming_message(msg("تمام أكد", message_id="m2"))
        self.assertEqual(second.reply, "تم! رقم الأوردر 1")
        order = Order.objects.get()
        self.assertEqual(order.source, Order.Source.WHATSAPP)
        self.assertEqual(order.customer_phone, "201234567890")  # from WhatsApp, not the model
        self.assertEqual(
            (order.subtotal, order.delivery_fee, order.total), (Decimal("134"), Decimal("15"), Decimal("149"))
        )
        self.assertEqual(order.items.get(name="ساندوتش كفتة").notes, "من غير طحينة")
        self.assertNotIn("pending_order", self.session().context_data)

        # The prepare_order result the model saw carries the server's prices.
        tool_result = self.client_mock.beta.messages.create.call_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertIn('"total": "149.00"', tool_result["content"])
        # The second message was answered with the first exchange as history.
        history = self.client_mock.beta.messages.create.call_args_list[2].kwargs["messages"]
        self.assertEqual(history[0], {"role": "user", "content": "عايز اتنين كفتة وحواوشي"})

    def test_confirm_without_a_prepared_order_places_nothing(self):
        self.script(tool("confirm_order", {}), text("محتاج أعرف الطلب الأول"))
        conversation.handle_incoming_message(msg("أكد", message_id="m1"))
        self.assertEqual(Order.objects.count(), 0)
        result = self.client_mock.beta.messages.create.call_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])

    def test_off_menu_and_other_tenant_items_are_refused(self):
        other, _ = make_tenant("other")
        foreign = MenuItem.objects.create(tenant=other, name="بيتزا", price=Decimal("1"))
        self.script(
            tool("prepare_order", self.prepare_args(items=[{"item_id": foreign.pk, "quantity": 1, "notes": ""}])),
            text("الصنف ده مش عندنا"),
        )
        conversation.handle_incoming_message(msg("عايز بيتزا", message_id="m1"))
        self.assertNotIn("pending_order", self.session().context_data or {})

    def test_delivery_needs_an_address(self):
        self.script(tool("prepare_order", self.prepare_args(address="")), text("العنوان إيه؟"))
        conversation.handle_incoming_message(msg("توصيل", message_id="m1"))
        self.assertNotIn("pending_order", self.session().context_data or {})

    def test_expired_pending_order_is_not_placed(self):
        self.script(tool("prepare_order", self.prepare_args()), text("الملخص…"))
        conversation.handle_incoming_message(msg("اطلب", message_id="m1"))
        session = self.session()
        session.context_data["pending_order"]["prepared_at"] = (
            timezone.now() - timezone.timedelta(hours=3)
        ).isoformat()
        session.save()
        self.script(tool("confirm_order", {}), text("محتاجين نراجع الطلب تاني"))
        conversation.handle_incoming_message(msg("أكد", message_id="m2"))
        self.assertEqual(Order.objects.count(), 0)

    def test_retried_confirm_does_not_duplicate(self):
        self.script(tool("prepare_order", self.prepare_args()), text("الملخص…"))
        conversation.handle_incoming_message(msg("اطلب", message_id="m1"))
        pending = self.session().context_data["pending_order"]
        ordering = chat_ordering._Conversation(self.agent, self.session(), "201234567890")
        first = ordering.run("confirm_order", {})
        # Same prepared order confirmed again (e.g. a retry before the pending was cleared).
        session = self.session()
        session.context_data = {"pending_order": pending}
        session.save()
        second = chat_ordering._Conversation(self.agent, session, "201234567890").run("confirm_order", {})
        self.assertEqual(first["order_number"], second["order_number"])
        self.assertEqual(Order.objects.count(), 1)

    def test_every_model_call_is_metered(self):
        self.script(tool("get_menu", {}), text("المنيو…"))
        conversation.handle_incoming_message(msg("المنيو؟", message_id="m1"))
        self.assertEqual(self.meter.call_count, 2)

    def test_tenant_without_menu_keeps_plain_answers(self):
        MenuItem.objects.filter(tenant=self.tenant).update(is_available=False)
        self.assertIsNone(chat_ordering.ordering_agent(self.tenant))
        with mock.patch.object(conversation, "answer_question") as answer:
            answer.return_value = SimpleNamespace(answer="rag", confident=True, sources=[])
            result = conversation.handle_incoming_message(msg("سؤال", message_id="m1"))
        self.assertEqual(result.reply, "rag")
        self.client_mock.beta.messages.create.assert_not_called()

    def test_refusal_gets_a_polite_reply(self):
        refusal = text("")
        refusal.stop_reason = "refusal"
        self.script(refusal)
        result = conversation.handle_incoming_message(msg("…", message_id="m1"))
        self.assertEqual(result.reply, chat_ordering.FALLBACK_REPLY)
