from django.contrib.auth import get_user_model
from django.utils.text import slugify
from rest_framework import serializers

from .models import Plan, Subscription

User = get_user_model()


class PlanSerializer(serializers.ModelSerializer):
    # Optional: auto-derived from `name` when omitted.
    slug = serializers.SlugField(required=False, allow_blank=True)

    class Meta:
        model = Plan
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "price_usd",
            "points",
            "monthly_questions",
            "monthly_token_cap",
            "max_documents",
            "max_total_mb",
            "max_requests_per_min",
            "llm_model",
            "allow_api_sync",
            "paddle_price_id",
            "is_active",
            "sort_order",
        ]
        read_only_fields = ["id"]
        extra_kwargs = {
            "points": {"required": False},
            "monthly_questions": {"required": False},
            "monthly_token_cap": {"required": False},
        }

    def _unique_slug(self, validated_data, instance=None) -> str:
        """Use the provided slug, else slugify the name; guarantee uniqueness."""
        base = (validated_data.get("slug") or "").strip()
        if not base:
            name = validated_data.get("name") or (instance.name if instance else "")
            base = slugify(name) or "plan"
        slug, i = base, 2
        qs = Plan.objects.all()
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        while qs.filter(slug=slug).exists():
            slug, i = f"{base}-{i}", i + 1
        return slug

    def create(self, validated_data):
        validated_data["slug"] = self._unique_slug(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "slug" in validated_data or "name" in validated_data:
            validated_data["slug"] = self._unique_slug(validated_data, instance)
        return super().update(instance, validated_data)


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    is_current = serializers.BooleanField(read_only=True)
    points_balance = serializers.SerializerMethodField()
    points_allowance = serializers.SerializerMethodField()
    # Why the admin may renew it now ("expired" / "points_exhausted"), else null.
    renewal_reason = serializers.SerializerMethodField()
    can_renew = serializers.SerializerMethodField()

    def get_points_balance(self, obj) -> float:
        from .services import points_balance

        return points_balance(obj.user)

    def get_points_allowance(self, obj) -> int:
        from .services import points_allowance

        return points_allowance(obj.user)

    def get_renewal_reason(self, obj) -> str | None:
        from .services import renewal_reason

        return renewal_reason(obj)

    def get_can_renew(self, obj) -> bool:
        return self.get_renewal_reason(obj) is not None

    class Meta:
        model = Subscription
        fields = [
            "id",
            "user",
            "username",
            "plan",
            "status",
            "current_period_start",
            "current_period_end",
            "auto_renew",
            "is_current",
            "points_balance",
            "points_allowance",
            "can_renew",
            "renewal_reason",
            "created_at",
        ]
        read_only_fields = fields


class RenewSubscriptionSerializer(serializers.Serializer):
    """Admin: renew on the same plan. Without ``duration_days`` the new period
    is as long as the previous one."""

    duration_days = serializers.IntegerField(required=False, min_value=1, max_value=3660)


class PointsHistorySerializer(serializers.Serializer):
    """One metered operation and the points it took."""

    id = serializers.UUIDField()
    kind = serializers.CharField()
    model = serializers.CharField()
    tokens_in = serializers.IntegerField()
    tokens_out = serializers.IntegerField()
    cost_usd = serializers.SerializerMethodField()
    points = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()

    def get_cost_usd(self, obj) -> float:
        return round(obj.cost_usd or 0.0, 6)

    def get_points(self, obj) -> float:
        return round((obj.points_milli or 0) / 1000, 3)


class AssignSubscriptionSerializer(serializers.Serializer):
    """Admin: put a user on a plan for a billing period."""

    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    plan = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all())
    duration_days = serializers.IntegerField(default=30, min_value=1)
    auto_renew = serializers.BooleanField(default=True)

    def save(self):
        from .services import assign_plan

        data = self.validated_data
        # assign_plan sets the period AND resets the wallet to the plan's points.
        return assign_plan(
            data["user"],
            data["plan"],
            duration_days=data["duration_days"],
            auto_renew=data["auto_renew"],
        )
