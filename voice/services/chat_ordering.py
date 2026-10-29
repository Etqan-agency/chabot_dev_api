"""Take orders over a text channel (WhatsApp) with Claude and tool use.

The model talks to the customer; the server owns everything that matters:

* ``prepare_order`` prices the cart against the menu and stores it on the
  conversation's session as the *pending* order -- that is the summary the
  customer is shown.
* ``confirm_order`` takes no order data from the model at all. It places the
  pending order, so what is saved is exactly what the customer approved.

Tenants with no ordering setup (no active ordering agent, or no available menu
item) never reach this module; WhatsApp falls back to plain RAG answers.
"""

# The prompt and tool descriptions are read by the model one instruction per line.
# ruff: noqa: E501

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta

from django.utils import timezone

from knowledge.models import ChatbotConfig
from knowledge.services.billing import meter
from knowledge.services.llm import AnthropicBackend

from ..models import MenuItem, Order, VoiceAgent
from . import tools as shared_tools
from .orders import LineRequest, OrderError, place_order, quote_order

logger = logging.getLogger(__name__)

MAX_ROUNDS = 8
PENDING_TTL = timedelta(hours=2)
# Ordering spans many short messages; give the model more of the thread than
# a plain Q&A reply needs.
HISTORY = {"limit": 30, "minutes": 180}

FALLBACK_REPLY = "معلش، حصلت مشكلة عندنا. ممكن حضرتك تبعت رسالتك تاني؟"

SYSTEM_PROMPT = """You are {assistant}, the WhatsApp ordering assistant for {company}.

Reply in the customer's language. For Arabic, write friendly Egyptian Arabic and address the customer as "حضرتك". Keep messages short and WhatsApp-friendly: plain text and line breaks, at most a little *bold*, no headings or tables.

Tools:
- get_menu: the only source of items, prices and availability. Call it before naming any item or price.
- search_info: the restaurant's own information (branches, phone numbers, hours, offers). If nothing relevant comes back, say the branch will confirm. Never invent facts.
- prepare_order: once you know the items and quantities, delivery or pickup, the address (for delivery) and the customer's name, call it. It prices the order on the server. Then send the customer the summary it returns - every item with its quantity and notes, the delivery fee, the total, delivery or pickup, the address and the name - and ask them to confirm.
- confirm_order: places exactly the order you prepared. Call it only when the customer's latest message clearly confirms that summary. If they change anything, call prepare_order again and get a new confirmation.

Rules:
- Only items from get_menu exist. If the customer asks for a variant or extra that is not on the menu (for example "with cheese" when there is no such item), say it is not available for online orders and suggest the closest menu item. Notes are only for requests like "no tahini" or for options an item's description asks for.
- Confirm any quantity above 10 of one item before preparing the order.
- The customer's phone number is already known from WhatsApp; never ask for it.
- Never calculate totals yourself; use the numbers prepare_order returns.
- Never say an order is placed unless confirm_order returned ok=true; then give the order number and total.
- If a tool returns ok=false, explain the problem simply and ask how to fix it.
- For questions unrelated to the restaurant, politely bring the conversation back to ordering."""

_ITEMS_SCHEMA = {
    "type": "array",
    "description": "The ordered items",
    "items": {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer", "description": "item_id from get_menu"},
            "quantity": {"type": "integer", "description": "How many"},
            "notes": {
                "type": "string",
                "description": "Requests like من غير طحينة, or options the item's description asks for. Empty if none.",
            },
        },
        "required": ["item_id", "quantity", "notes"],
        "additionalProperties": False,
    },
}

TOOLS = [
    {
        "name": "get_menu",
        "description": "Returns the available menu: item_id, name, category, description and price, plus the delivery fee and currency.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "search_info",
        "description": "Searches the restaurant's information: branches and addresses, phone numbers, opening hours and offers.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look up, in the customer's words"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "prepare_order",
        "description": (
            "Prices the order on the server and saves it as the pending order for this conversation. "
            "Returns the summary to send to the customer for confirmation. Calling it again replaces the pending order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": _ITEMS_SCHEMA,
                "fulfilment": {"type": "string", "enum": ["delivery", "pickup"]},
                "customer_name": {"type": "string", "description": "The customer's name"},
                "address": {"type": "string", "description": "Full delivery address; empty for pickup"},
                "order_notes": {"type": "string", "description": "Notes for the whole order; empty if none"},
            },
            "required": ["items", "fulfilment", "customer_name", "address", "order_notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "confirm_order",
        "description": (
            "Places the pending order exactly as prepared. Only call it after the customer explicitly "
            "confirmed the latest summary. Returns the order number and total."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
]


def ordering_agent(tenant) -> VoiceAgent | None:
    """The tenant's ordering settings, when text ordering can run for it."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    agent = VoiceAgent.objects.filter(tenant=tenant, is_active=True).select_related("tenant").first()
    if agent is None or not MenuItem.objects.filter(tenant=tenant, is_available=True).exists():
        return None
    return agent


class _Conversation:
    """One customer's thread: what the tools may touch while answering it."""

    def __init__(self, agent: VoiceAgent, session, customer_phone: str):
        self.agent = agent
        self.session = session
        self.customer_phone = customer_phone

    # -- pending order, kept on the WhatsApp session -------------------------
    def _context(self) -> dict:
        return dict(self.session.context_data or {})

    def _save_pending(self, pending: dict | None) -> None:
        context = self._context()
        if pending is None:
            context.pop("pending_order", None)
        else:
            context["pending_order"] = pending
        self.session.context_data = context
        self.session.save(update_fields=["context_data"])

    # -- tools ---------------------------------------------------------------
    def run(self, name: str, args: dict) -> dict:
        try:
            if name == "get_menu":
                return shared_tools.menu_payload(self.agent)
            if name == "search_info":
                return shared_tools.search_knowledge(self.agent.tenant, str(args.get("query", "")))
            if name == "prepare_order":
                return self._prepare(args)
            if name == "confirm_order":
                return self._confirm()
        except OrderError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": f"Unknown tool {name}."}

    def _prepare(self, args: dict) -> dict:
        lines = [
            LineRequest(item_id=int(item["item_id"]), quantity=int(item["quantity"]), notes=str(item.get("notes", "")))
            for item in args.get("items") or []
        ]
        fulfilment = str(args.get("fulfilment", ""))
        address = str(args.get("address", "")).strip()
        if fulfilment == Order.Fulfilment.DELIVERY and not address:
            raise OrderError("A delivery order needs the customer's address.")
        quote = quote_order(self.agent, lines=lines, fulfilment=fulfilment)
        pending = {
            "id": uuid.uuid4().hex,
            "prepared_at": timezone.now().isoformat(),
            "items": [{"item_id": line.item_id, "quantity": line.quantity, "notes": line.notes} for line in lines],
            "fulfilment": fulfilment,
            "customer_name": str(args.get("customer_name", "")).strip(),
            "address": address if fulfilment == Order.Fulfilment.DELIVERY else "",
            "notes": str(args.get("order_notes", "")).strip(),
        }
        self._save_pending(pending)
        return {
            "ok": True,
            "summary": {
                "items": [
                    {"name": item.name, "quantity": line.quantity, "notes": line.notes, "line_total": str(total)}
                    for item, line, total in quote.priced
                ],
                "subtotal": str(quote.subtotal),
                "delivery_fee": str(quote.delivery_fee),
                "total": str(quote.total),
                "currency": self.agent.currency,
                "fulfilment": fulfilment,
                "address": pending["address"],
                "customer_name": pending["customer_name"],
            },
            "next_step": "Send this summary to the customer and ask them to confirm.",
        }

    def _confirm(self) -> dict:
        pending = self._context().get("pending_order")
        if not pending:
            return {"ok": False, "error": "There is no prepared order. Call prepare_order first."}
        if timezone.now() - datetime.fromisoformat(pending["prepared_at"]) > PENDING_TTL:
            self._save_pending(None)
            return {"ok": False, "error": "The prepared order expired. Prepare it again and re-confirm."}
        order, _ = place_order(
            self.agent,
            lines=[LineRequest(**item) for item in pending["items"]],
            fulfilment=pending["fulfilment"],
            customer_name=pending["customer_name"],
            customer_phone=self.customer_phone,
            address=pending["address"],
            notes=pending["notes"],
            # One key per prepared order: a retried confirm returns the same order.
            conversation_id=f"wa:{self.session.pk}:{pending['id']}",
            source=Order.Source.WHATSAPP,
        )
        self._save_pending(None)
        return {
            "ok": True,
            "order_number": order.number,
            "total": str(order.total),
            "currency": order.currency,
        }


def _system_prompt(agent: VoiceAgent) -> str:
    cfg = ChatbotConfig.objects.filter(user=agent.tenant).first()
    assistant = cfg.assistant_name if cfg and cfg.assistant_name not in ("", "Assistant") else "المساعد"
    company = (cfg and cfg.company_name) or agent.display_name or agent.tenant.get_username()
    return SYSTEM_PROMPT.format(assistant=assistant, company=company)


def _model() -> str:
    for candidate in (os.getenv("ORDERING_MODEL"), os.getenv("LLM_MODEL")):
        if candidate and candidate.startswith("claude-"):
            return candidate
    return AnthropicBackend.DEFAULT_MODEL


def reply(*, agent: VoiceAgent, session, customer_phone: str, text: str, history: list[dict]) -> str:
    """Answer one incoming message, running tools until the model replies in text.

    ``history`` is the earlier turns as ``[{"role", "content"}]`` text messages.
    Every model call is metered to the tenant's current billing scope.
    """
    from anthropic import Anthropic

    conversation = _Conversation(agent, session, customer_phone)
    model = _model()
    kwargs = {"output_config": {"effort": os.getenv("ORDERING_EFFORT", "low")}}
    if model in AnthropicBackend.FALLBACK_MODELS:
        kwargs["betas"] = [AnthropicBackend.FALLBACK_BETA]
        kwargs["extra_body"] = {"fallbacks": "default"}

    turns = list(history)
    while turns and turns[0]["role"] != "user":
        turns.pop(0)  # the API requires the first message to be the customer's
    messages = [*turns, {"role": "user", "content": text}]
    system = _system_prompt(agent)
    client = Anthropic()

    for _ in range(MAX_ROUNDS):
        response = client.beta.messages.create(
            model=model, max_tokens=16000, system=system, tools=TOOLS, messages=messages, **kwargs
        )
        usage = response.usage
        meter(getattr(response, "model", None) or model, usage.input_tokens or 0, usage.output_tokens or 0)
        if response.stop_reason == "refusal":
            return FALLBACK_REPLY
        if response.stop_reason != "tool_use":
            answer = "".join(block.text for block in response.content if block.type == "text").strip()
            return answer or FALLBACK_REPLY

        # Hand back the whole assistant turn (thinking included), then every result in one message.
        # As plain dicts: this SDK version can't re-serialize its own response models.
        messages.append(
            {
                "role": "assistant",
                "content": [
                    block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in response.content
                ],
            }
        )
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = conversation.run(block.name, dict(block.input or {}))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                    "is_error": not result.get("ok", False),
                }
            )
        messages.append({"role": "user", "content": results})

    logger.warning("Ordering reply for tenant %s hit the tool round limit", agent.tenant_id)
    return FALLBACK_REPLY
