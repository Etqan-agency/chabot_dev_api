from rest_framework import serializers

from ..models import AvailableLanguage, QuestionTreeNode


class AvailableLanguageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailableLanguage
        fields = ["id", "code", "name", "is_active"]
        read_only_fields = ["id"]


class QuestionTreeNodeSerializer(serializers.ModelSerializer):
    """Flat node view (admin dumps / single-node responses)."""

    has_children = serializers.SerializerMethodField()

    class Meta:
        model = QuestionTreeNode
        fields = [
            "id",
            "title",
            "answer",
            "parent",
            "order",
            "is_active",
            "language",
            "has_children",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "language", "created_at", "updated_at"]

    def get_has_children(self, obj):
        return obj.has_children()


class QuestionTreeNodeWriteSerializer(serializers.Serializer):
    """Create/edit a node — always applied to the canonical language."""

    parent = serializers.PrimaryKeyRelatedField(
        queryset=QuestionTreeNode.objects.all(), required=False, allow_null=True
    )
    title = serializers.CharField(max_length=500)
    answer = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    order = serializers.IntegerField(required=False, min_value=0)
    is_active = serializers.BooleanField(required=False, default=True)


class QuestionTreeNodeUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=500, required=False)
    answer = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    order = serializers.IntegerField(required=False, min_value=0)
    is_active = serializers.BooleanField(required=False)


class TreeNodeOutSerializer(serializers.Serializer):
    """Read-only nested tree node (matches services.guided_tree.build_tree output)."""

    id = serializers.UUIDField()
    title = serializers.CharField()
    answer = serializers.CharField(allow_null=True)
    order = serializers.IntegerField()
    language = serializers.CharField()
    children = serializers.ListField()
