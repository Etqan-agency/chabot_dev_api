"""Phone/voice ordering channel.

A hosted voice agent (ElevenLabs Agents) does the speech and the phone line;
this app is the business side it talks to: the tenant's menu, the orders the
agent places, and the call log it reports after each call.

Prices never come from the model. The agent sends item ids and quantities;
the server looks the prices up here and computes every total.
"""

import hashlib
import secrets
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

TOKEN_PREFIX = "vt_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class VoiceAgent(models.Model):
    """Links a tenant to its hosted voice agent and holds its ordering settings.

    The agent authenticates its tool calls with ``Authorization: Bearer vt_...``.
    Only the SHA-256 of that token is stored; the plaintext is shown once, when
    it is issued.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="voice_agent",
    )
    # ElevenLabs agent id. Post-call webhooks are matched to the tenant by it.
    agent_id = models.CharField(max_length=128, blank=True, default="")
    display_name = models.CharField(max_length=120, blank=True, default="")
    token_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    token_prefix = models.CharField(max_length=12, blank=True, default="")
    delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0"), validators=[MinValueValidator(0)]
    )
    currency = models.CharField(max_length=8, default="EGP")

    class Persona(models.TextChoices):
        FEMALE = "female", "Female"
        MALE = "male", "Male"

    # The assistant's grammatical gender in the Arabic prompt; must match the voice.
    persona = models.CharField(max_length=8, choices=Persona, default=Persona.FEMALE)
    # ElevenLabs voice the agent speaks with (blank = leave the agent's voice as is).
    voice_id = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "voice_agents"
        constraints = [
            models.UniqueConstraint(
                fields=["agent_id"], condition=~Q(agent_id=""), name="voice_agent_unique_agent_id"
            ),
        ]

    def __str__(self):
        return f"{self.display_name or self.agent_id or self.pk} -> {self.tenant}"

    def issue_token(self) -> str:
        """Replace the tool token and return the new plaintext (shown once)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.token_hash = hash_token(token)
        self.token_prefix = token[:10]
        self.save(update_fields=["token_hash", "token_prefix", "updated_at"])
        return token


class MenuItem(models.Model):
    tenant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="menu_items")
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    is_available = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "voice_menu_items"
        ordering = ["category", "sort_order", "name"]
        indexes = [models.Index(fields=["tenant", "is_available"])]

    def __str__(self):
        return f"{self.name} ({self.price})"


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        PREPARING = "preparing", "Preparing"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    class Fulfilment(models.TextChoices):
        DELIVERY = "delivery", "Delivery"
        PICKUP = "pickup", "Pickup"

    class Source(models.TextChoices):
        VOICE = "voice", "Phone call"
        WHATSAPP = "whatsapp", "WhatsApp"
        MANUAL = "manual", "Entered manually"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders")
    # Per-tenant running number read out to the customer ("order 17").
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status, default=Status.NEW, db_index=True)
    source = models.CharField(max_length=16, choices=Source, default=Source.VOICE)
    fulfilment = models.CharField(max_length=16, choices=Fulfilment, default=Fulfilment.DELIVERY)
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_phone = models.CharField(max_length=32, blank=True, default="")
    address = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=8, default="EGP")
    # The voice conversation that placed it. Unique per tenant, so a retried
    # tool call returns the same order instead of creating a second one.
    conversation_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "voice_orders"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="voice_order_unique_number"),
            models.UniqueConstraint(
                fields=["tenant", "conversation_id"],
                condition=~Q(conversation_id=""),
                name="voice_order_unique_conversation",
            ),
        ]

    def __str__(self):
        return f"#{self.number} ({self.status})"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.SET_NULL, null=True, blank=True)
    # Snapshot at order time: later menu edits must not rewrite past orders.
    name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    notes = models.CharField(max_length=300, blank=True, default="")
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "voice_order_items"
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity} x {self.name}"


class VoiceCall(models.Model):
    """One finished call, as reported by the provider's post-call webhook."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="voice_calls")
    conversation_id = models.CharField(max_length=128, unique=True)
    agent_id = models.CharField(max_length=128, blank=True, default="")
    caller_number = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    duration_secs = models.PositiveIntegerField(default=0)
    termination_reason = models.CharField(max_length=255, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    # [{"role": "agent"|"user", "message": str, "time_in_call_secs": int}]
    transcript = models.JSONField(default=list, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "voice_calls"
        ordering = ["-created_at"]

    def __str__(self):
        return self.conversation_id
