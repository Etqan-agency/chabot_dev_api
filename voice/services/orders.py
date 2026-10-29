"""Turn what the voice agent heard into a priced, persisted order.

The agent only names items and quantities. Everything that costs money -- unit
prices, the delivery fee, totals -- is read from the database here, so nothing
a caller says ("make it free") can change what the restaurant charges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max

from ..models import MenuItem, Order, OrderItem, VoiceAgent

MAX_LINES = 30
MAX_QUANTITY = 50

_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})
_DIACRITICS = re.compile(r"[ً-ْـ]")  # harakat + tatweel


def normalize_name(name: str) -> str:
    """Fold the spelling variants speech-to-text produces for the same Arabic word."""
    text = _DIACRITICS.sub("", (name or "").strip().lower()).translate(_ARABIC_FOLD)
    return " ".join(text.split())


class OrderError(ValueError):
    """A request the agent should fix with the caller (unknown item, etc.)."""


@dataclass
class LineRequest:
    quantity: int
    name: str = ""
    item_id: int | None = None
    notes: str = ""


def _resolve(tenant, line: LineRequest, menu: list[MenuItem]) -> MenuItem:
    item = None
    if line.item_id is not None:
        item = next((m for m in menu if m.pk == line.item_id), None)
    if item is None and line.name:
        wanted = normalize_name(line.name)
        item = next((m for m in menu if normalize_name(m.name) == wanted), None)
    if item is None:
        raise OrderError(f"'{line.name or line.item_id}' is not on the menu.")
    if not item.is_available:
        raise OrderError(f"'{item.name}' is not available right now.")
    return item


@dataclass
class Quote:
    priced: list[tuple[MenuItem, LineRequest, Decimal]]
    subtotal: Decimal
    delivery_fee: Decimal

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.delivery_fee


def quote_order(agent: VoiceAgent, *, lines: list[LineRequest], fulfilment: str) -> Quote:
    """Price the lines against the menu without saving anything."""
    if not lines:
        raise OrderError("The order has no items.")
    if len(lines) > MAX_LINES:
        raise OrderError("Too many items in one order.")
    if fulfilment not in Order.Fulfilment.values:
        raise OrderError("Fulfilment must be 'delivery' or 'pickup'.")

    menu = list(MenuItem.objects.filter(tenant=agent.tenant))
    priced = []
    for line in lines:
        if not 1 <= line.quantity <= MAX_QUANTITY:
            raise OrderError(f"Quantity must be between 1 and {MAX_QUANTITY}.")
        item = _resolve(agent.tenant, line, menu)
        priced.append((item, line, item.price * line.quantity))

    return Quote(
        priced=priced,
        subtotal=sum((total for _, _, total in priced), Decimal("0")),
        delivery_fee=agent.delivery_fee if fulfilment == Order.Fulfilment.DELIVERY else Decimal("0"),
    )


def place_order(
    agent: VoiceAgent,
    *,
    lines: list[LineRequest],
    fulfilment: str,
    customer_name: str = "",
    customer_phone: str = "",
    address: str = "",
    notes: str = "",
    conversation_id: str = "",
    source: str = Order.Source.VOICE,
) -> tuple[Order, bool]:
    """Create the order, or return the one this conversation already placed.

    Returns ``(order, created)``.
    """
    tenant = agent.tenant
    conversation_id = (conversation_id or "").strip()
    if conversation_id:
        existing = Order.objects.filter(tenant=tenant, conversation_id=conversation_id).first()
        if existing is not None:
            return existing, False

    if fulfilment == Order.Fulfilment.DELIVERY and not address.strip():
        raise OrderError("A delivery order needs the customer's address.")
    quote = quote_order(agent, lines=lines, fulfilment=fulfilment)
    priced, subtotal, delivery_fee = quote.priced, quote.subtotal, quote.delivery_fee

    try:
        with transaction.atomic():
            # Lock the agent row so two calls can't take the same order number.
            VoiceAgent.objects.select_for_update().filter(pk=agent.pk).first()
            last = Order.objects.filter(tenant=tenant).aggregate(n=Max("number"))["n"] or 0
            order = Order.objects.create(
                tenant=tenant,
                number=last + 1,
                source=source,
                fulfilment=fulfilment,
                customer_name=customer_name.strip()[:120],
                customer_phone=customer_phone.strip()[:32],
                address=address.strip() if fulfilment == Order.Fulfilment.DELIVERY else "",
                notes=notes.strip(),
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                total=subtotal + delivery_fee,
                currency=agent.currency,
                conversation_id=conversation_id,
            )
            OrderItem.objects.bulk_create(
                OrderItem(
                    order=order,
                    menu_item=item,
                    name=item.name,
                    unit_price=item.price,
                    quantity=line.quantity,
                    notes=line.notes.strip()[:300],
                    line_total=line_total,
                )
                for item, line, line_total in priced
            )
    except IntegrityError:
        # A concurrent retry of the same conversation won the race.
        if conversation_id:
            existing = Order.objects.filter(tenant=tenant, conversation_id=conversation_id).first()
            if existing is not None:
                return existing, False
        raise
    return order, True
