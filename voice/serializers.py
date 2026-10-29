from rest_framework import serializers

from .models import MenuItem, Order, OrderItem, VoiceAgent, VoiceCall

# -- Dashboard --------------------------------------------------------------- #


class VoiceAgentSerializer(serializers.ModelSerializer):
    has_token = serializers.SerializerMethodField()

    class Meta:
        model = VoiceAgent
        fields = [
            "id",
            "agent_id",
            "display_name",
            "delivery_fee",
            "currency",
            "is_active",
            "has_token",
            "token_prefix",
            "updated_at",
        ]
        read_only_fields = ["id", "has_token", "token_prefix", "updated_at"]

    def get_has_token(self, obj) -> bool:
        return bool(obj.token_hash)

    def validate_agent_id(self, value):
        value = value.strip()
        if value and VoiceAgent.objects.filter(agent_id=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("This agent is already linked to another account.")
        return value


class MenuItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = [
            "id",
            "name",
            "category",
            "description",
            "price",
            "is_available",
            "sort_order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ["id", "menu_item", "name", "unit_price", "quantity", "notes", "line_total"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    call_id = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "number",
            "status",
            "source",
            "fulfilment",
            "customer_name",
            "customer_phone",
            "address",
            "notes",
            "subtotal",
            "delivery_fee",
            "total",
            "currency",
            "items",
            "call_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [f for f in fields if f != "status"]

    def get_call_id(self, obj):
        call = obj.calls.first()
        return str(call.pk) if call else None


class VoiceCallListSerializer(serializers.ModelSerializer):
    order_number = serializers.IntegerField(source="order.number", read_only=True, default=None)

    class Meta:
        model = VoiceCall
        fields = [
            "id",
            "conversation_id",
            "caller_number",
            "status",
            "started_at",
            "duration_secs",
            "summary",
            "order",
            "order_number",
            "created_at",
        ]


class VoiceCallSerializer(VoiceCallListSerializer):
    class Meta(VoiceCallListSerializer.Meta):
        fields = VoiceCallListSerializer.Meta.fields + ["termination_reason", "transcript"]


# -- Agent tools --------------------------------------------------------------- #


class KnowledgeToolSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=500)


class OrderLineToolSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(required=False, allow_null=True)
    name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    quantity = serializers.IntegerField(min_value=1)
    notes = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs.get("item_id") is None and not attrs.get("name"):
            raise serializers.ValidationError("Each item needs an item_id or a name.")
        return attrs


class QuoteOrderToolSerializer(serializers.Serializer):
    items = OrderLineToolSerializer(many=True)
    fulfilment = serializers.ChoiceField(choices=Order.Fulfilment.choices)


class CreateOrderToolSerializer(QuoteOrderToolSerializer):
    customer_name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    customer_phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    address = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    conversation_id = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
