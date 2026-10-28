from rest_framework import serializers

from ..models import QuestionAnswer


class QuestionAnswerSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = QuestionAnswer
        fields = [
            "id",
            "question",
            "answer",
            "answer_type",
            "overview_description",
            "count",
            "is_active",
            "created_by",
            "created_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "count", "created_by", "created_at", "updated_at"]


class QuestionAnswerWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestionAnswer
        fields = [
            "question",
            "answer",
            "answer_type",
            "overview_description",
            "is_active",
        ]
