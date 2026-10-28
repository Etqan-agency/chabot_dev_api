from rest_framework import serializers

from ..models import SimpleQuestionTree


class SimpleQuestionTreeSerializer(serializers.ModelSerializer):
    has_children = serializers.SerializerMethodField()
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = SimpleQuestionTree
        fields = [
            "id",
            "title",
            "parent",
            "answer",
            "images",
            "order",
            "is_active",
            "language",
            "has_children",
            "created_by",
            "created_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_has_children(self, obj):
        return obj.children.filter(is_active=True).exists()


class SimpleQuestionTreeWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = SimpleQuestionTree
        fields = [
            "title",
            "parent",
            "answer",
            "images",
            "order",
            "is_active",
            "language",
        ]


class SimpleQuestionTreeNodeSerializer(serializers.ModelSerializer):
    """Recursive serializer used to render the full tree."""

    children = serializers.SerializerMethodField()

    class Meta:
        model = SimpleQuestionTree
        fields = [
            "id",
            "title",
            "answer",
            "images",
            "order",
            "language",
            "children",
        ]

    def get_children(self, obj):
        children = obj.children.filter(is_active=True).order_by("order", "created_at")
        return SimpleQuestionTreeNodeSerializer(children, many=True).data
