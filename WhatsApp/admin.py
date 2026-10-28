from django.contrib import admin

from .models import (
    WhatsAppAccount,
    WhatsAppAnalytics,
    WhatsAppMessage,
    WhatsAppSession,
    WhatsAppUser,
)


@admin.register(WhatsAppAccount)
class WhatsAppAccountAdmin(admin.ModelAdmin):
    list_display = ("display_name", "phone_number_id", "tenant", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("phone_number_id", "display_name", "tenant__username")
    raw_id_fields = ("tenant",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(WhatsAppUser)
class WhatsAppUserAdmin(admin.ModelAdmin):
    list_display = (
        "phone_number",
        "profile_name",
        "language_preference",
        "is_active",
        "message_count",
        "last_message_at",
    )
    list_filter = ("is_active", "language_preference")
    search_fields = ("phone_number", "profile_name")
    readonly_fields = ("id", "created_at", "updated_at", "last_message_at", "message_count")


@admin.register(WhatsAppSession)
class WhatsAppSessionAdmin(admin.ModelAdmin):
    list_display = ("user", "session_type", "is_active", "expires_at", "message_count")
    list_filter = ("session_type", "is_active")
    search_fields = ("user__phone_number",)
    readonly_fields = ("id", "created_at", "updated_at", "message_count")


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("user", "message_type", "status", "response_time_ms", "created_at")
    list_filter = ("message_type", "status")
    search_fields = ("user__phone_number", "message_text")
    readonly_fields = ("id", "created_at")


@admin.register(WhatsAppAnalytics)
class WhatsAppAnalyticsAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "total_messages",
        "incoming_messages",
        "outgoing_messages",
        "unique_users",
        "error_count",
    )
    readonly_fields = ("created_at", "updated_at")
