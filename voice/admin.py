from django.contrib import admin

from .models import MenuItem, Order, OrderItem, VoiceAgent, VoiceCall


@admin.register(VoiceAgent)
class VoiceAgentAdmin(admin.ModelAdmin):
    list_display = ["tenant", "agent_id", "display_name", "token_prefix", "is_active", "updated_at"]
    search_fields = ["tenant__username", "agent_id", "display_name"]
    exclude = ["token_hash"]
    readonly_fields = ["token_prefix"]


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ["name", "tenant", "category", "price", "is_available"]
    list_filter = ["is_available"]
    search_fields = ["name", "tenant__username"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ["menu_item", "name", "unit_price", "quantity", "notes", "line_total"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["number", "tenant", "status", "fulfilment", "customer_name", "total", "created_at"]
    list_filter = ["status", "fulfilment", "source"]
    search_fields = ["customer_name", "customer_phone", "tenant__username"]
    inlines = [OrderItemInline]


@admin.register(VoiceCall)
class VoiceCallAdmin(admin.ModelAdmin):
    list_display = ["conversation_id", "tenant", "caller_number", "duration_secs", "order", "created_at"]
    search_fields = ["conversation_id", "caller_number", "tenant__username"]
    readonly_fields = ["transcript"]
