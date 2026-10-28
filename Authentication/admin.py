from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import APIKey, EmailChangeRequest, PasswordChangeCode, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("username",)
    list_display = ("username", "email", "is_staff", "is_superuser", "is_active")


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ("user", "key", "is_active", "last_used_at", "created_at")
    list_filter = ("is_active",)
    search_fields = ("user__username", "key")
    readonly_fields = ("id", "key", "last_used_at", "created_at", "updated_at")


@admin.register(EmailChangeRequest)
class EmailChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "new_email", "is_used", "attempts", "expires_at", "created_at")
    list_filter = ("is_used",)
    search_fields = ("user__username", "new_email")
    readonly_fields = (
        "id",
        "user",
        "new_email",
        "code_hash",
        "expires_at",
        "attempts",
        "created_at",
        "updated_at",
    )


@admin.register(PasswordChangeCode)
class PasswordChangeCodeAdmin(admin.ModelAdmin):
    list_display = ("user", "is_used", "attempts", "expires_at", "created_at")
    list_filter = ("is_used",)
    search_fields = ("user__username",)
    readonly_fields = (
        "id",
        "user",
        "code_hash",
        "expires_at",
        "attempts",
        "created_at",
        "updated_at",
    )
