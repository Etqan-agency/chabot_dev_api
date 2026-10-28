import uuid

from django.conf import settings
from django.core.validators import MaxLengthValidator, MinLengthValidator
from django.db import models
from django.utils import timezone

from .fields import EncryptedTextField


class WhatsAppAccount(models.Model):
    """Links a tenant (knowledge owner) to a WhatsApp Business number.

    Incoming webhook messages carry the business ``phone_number_id``; we map it
    to a tenant so the bot answers from THAT tenant's knowledge, and we send
    replies using this account's number/token. This is what makes WhatsApp
    multi-tenant: any user can have their own WhatsApp bot.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="whatsapp_account",
        help_text="The tenant whose knowledge answers on this number.",
    )
    phone_number_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Meta WhatsApp Business phone-number id that receives messages.",
    )
    access_token = EncryptedTextField(
        blank=True,
        default="",
        help_text="Per-number send token (encrypted); blank falls back to WHATSAPP_ACCESS_TOKEN.",
    )
    display_name = models.CharField(max_length=120, blank=True, default="")

    class Connection(models.TextChoices):
        MANUAL = "manual", "Linked by an admin"
        EMBEDDED_SIGNUP = "embedded_signup", "Connected by the tenant (Embedded Signup)"

    connection = models.CharField(max_length=20, choices=Connection, default=Connection.MANUAL)
    # Set by Embedded Signup: the tenant's WhatsApp Business Account, the number
    # as Meta shows it, and the two-step PIN the number was registered with.
    waba_id = models.CharField(max_length=64, blank=True, default="")
    display_phone_number = models.CharField(max_length=32, blank=True, default="")
    registration_pin = EncryptedTextField(blank=True, default="")
    # The last Meta error while connecting (empty = none).
    last_error = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "whatsapp_accounts"
        verbose_name = "WhatsApp Account"
        verbose_name_plural = "WhatsApp Accounts"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.display_name or self.phone_number_id} -> {self.tenant}"


class WhatsAppUser(models.Model):
    """
    Model to store WhatsApp user information and session data
    """

    class LanguagePreference(models.TextChoices):
        ARABIC = "ar", "Arabic"
        ENGLISH = "en", "English"
        FRENCH = "fr", "French"
        SPANISH = "es", "Spanish"
        GERMAN = "de", "German"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The tenant boundary. Without it every WhatsApp contact, session and
    # message was one global pool: the viewsets had nothing to scope by, so any
    # authenticated tenant could read every other tenant's conversations. It is
    # nullable only because rows created before this column existed cannot be
    # attributed after the fact; those are visible to staff alone.
    account = models.ForeignKey(
        "WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="contacts",
        null=True,
        blank=True,
        db_index=True,
        help_text="The business number -- and so the tenant -- this contact wrote to.",
    )
    phone_number = models.CharField(
        max_length=20,
        validators=[MinLengthValidator(10), MaxLengthValidator(20)],
        help_text="WhatsApp phone number without whatsapp: prefix",
    )
    profile_name = models.CharField(max_length=255, blank=True, null=True, help_text="User's WhatsApp profile name")
    language_preference = models.CharField(
        max_length=10,
        choices=LanguagePreference.choices,
        default=LanguagePreference.ARABIC,
        help_text="User's preferred language",
    )
    is_active = models.BooleanField(default=True, help_text="Whether this user is active")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    last_message_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of last message received")
    message_count = models.PositiveIntegerField(default=0, help_text="Total number of messages exchanged")
    metadata = models.JSONField(default=dict, blank=True, help_text="Additional user metadata")

    class Meta:
        db_table = "whatsapp_users"
        verbose_name = "WhatsApp User"
        verbose_name_plural = "WhatsApp Users"
        ordering = ["-last_message_at", "-created_at"]
        indexes = [
            models.Index(fields=["phone_number"]),
            models.Index(fields=["account", "phone_number"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["language_preference"]),
            models.Index(fields=["last_message_at"]),
        ]
        constraints = [
            # One contact row per (business number, customer number). It used to
            # be phone_number alone, globally: a customer who wrote to two
            # tenants' bots got ONE row, so both tenants shared that person's
            # profile, language, counters -- and, through it, each other's
            # sessions and message history.
            models.UniqueConstraint(
                fields=["account", "phone_number"],
                name="uniq_whatsapp_contact_per_account",
            ),
            # Legacy rows carry no account; keep them unique among themselves so
            # the old invariant still holds for exactly those.
            models.UniqueConstraint(
                fields=["phone_number"],
                condition=models.Q(account__isnull=True),
                name="uniq_whatsapp_contact_without_account",
            ),
        ]

    def __str__(self):
        name = self.profile_name or "Unknown"
        return f"{name} ({self.phone_number})"

    def increment_message_count(self):
        """Increment message count"""
        self.message_count += 1
        self.save(update_fields=["message_count"])

    def update_last_message_time(self):
        """Update last message timestamp"""
        self.last_message_at = timezone.now()
        self.save(update_fields=["last_message_at"])


class WhatsAppSession(models.Model):
    """
    Model to store user session context for conversations
    """

    class SessionType(models.TextChoices):
        RAG_CHAT = "rag_chat", "RAG Chat"
        SIMPLE_CHAT = "simple_chat", "Simple Chat"
        ASK_QUESTIONS = "ask_questions", "Ask Questions"
        ASK_UNA = "ask_una", "Ask UNA"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        WhatsAppUser,
        on_delete=models.CASCADE,
        related_name="sessions",
        help_text="WhatsApp user this session belongs to",
    )
    session_type = models.CharField(
        max_length=20, choices=SessionType.choices, default=SessionType.ASK_QUESTIONS, help_text="Type of chat session"
    )
    context_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Store session context like conversation history, current question tree node, etc.",
    )
    is_active = models.BooleanField(default=True, help_text="Whether this session is active")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="When this session expires")
    message_count = models.PositiveIntegerField(default=0, help_text="Number of messages in this session")

    class Meta:
        db_table = "whatsapp_sessions"
        verbose_name = "WhatsApp Session"
        verbose_name_plural = "WhatsApp Sessions"
        ordering = ["-updated_at", "-created_at"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["session_type"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"Session {self.id} - {self.user.phone_number} ({self.session_type})"

    def is_expired(self):
        """Check if session has expired"""
        if not self.expires_at:
            return False
        return timezone.now() > self.expires_at

    def extend_session(self, hours=24):
        """Extend session expiration time"""
        self.expires_at = timezone.now() + timezone.timedelta(hours=hours)
        self.save(update_fields=["expires_at", "updated_at"])

    def increment_message_count(self):
        """Increment message count in session"""
        self.message_count += 1
        self.save(update_fields=["message_count", "updated_at"])


class WhatsAppMessage(models.Model):
    """
    Model to log all WhatsApp messages for analytics and debugging
    """

    class MessageType(models.TextChoices):
        INCOMING = "incoming", "Incoming"
        OUTGOING = "outgoing", "Outgoing"

    class MessageStatus(models.TextChoices):
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        WhatsAppUser,
        on_delete=models.CASCADE,
        related_name="messages",
        help_text="WhatsApp user who sent/received this message",
    )
    session = models.ForeignKey(
        WhatsAppSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
        help_text="Session this message belongs to",
    )
    message_type = models.CharField(
        max_length=10, choices=MessageType.choices, help_text="Whether this is an incoming or outgoing message"
    )
    message_text = models.TextField(validators=[MinLengthValidator(1)], help_text="The actual message content")
    whatsapp_message_id = models.CharField(max_length=100, blank=True, null=True, help_text="Meta WhatsApp message ID")
    status = models.CharField(
        max_length=20,
        choices=MessageStatus.choices,
        default=MessageStatus.SENT,
        help_text="Delivery status of the message",
    )
    api_endpoint_used = models.CharField(
        max_length=100, blank=True, null=True, help_text="Which chat API was used to generate response"
    )
    response_time_ms = models.PositiveIntegerField(null=True, blank=True, help_text="Response time in milliseconds")
    error_message = models.TextField(blank=True, null=True, help_text="Error message if message failed")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True, help_text="Additional message metadata")

    class Meta:
        db_table = "whatsapp_messages"
        verbose_name = "WhatsApp Message"
        verbose_name_plural = "WhatsApp Messages"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["session"]),
            models.Index(fields=["message_type"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["whatsapp_message_id"]),
        ]
        constraints = [
            # Meta re-delivers a message whenever it isn't sure we got it; one
            # incoming row per Meta id is what stops a second answer (and a
            # second charge) for the same message.
            models.UniqueConstraint(
                fields=["whatsapp_message_id"],
                condition=models.Q(message_type="incoming", whatsapp_message_id__isnull=False)
                & ~models.Q(whatsapp_message_id=""),
                name="uniq_whatsapp_incoming_message_id",
            ),
        ]

    def __str__(self):
        return f"{self.message_type} - {self.user.phone_number} - {self.created_at}"


class WhatsAppAnalytics(models.Model):
    """
    Model to store aggregated analytics data
    """

    date = models.DateField(unique=True, db_index=True)
    total_messages = models.PositiveIntegerField(default=0)
    incoming_messages = models.PositiveIntegerField(default=0)
    outgoing_messages = models.PositiveIntegerField(default=0)
    unique_users = models.PositiveIntegerField(default=0)
    api_usage = models.JSONField(default=dict, help_text="Usage count per API endpoint")
    error_count = models.PositiveIntegerField(default=0)
    avg_response_time = models.FloatField(default=0.0, help_text="Average response time in milliseconds")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "whatsapp_analytics"
        verbose_name = "WhatsApp Analytics"
        verbose_name_plural = "WhatsApp Analytics"
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["date"]),
        ]

    def __str__(self):
        return f"Analytics - {self.date}"
