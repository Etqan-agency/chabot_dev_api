from rest_framework import serializers

from ..models import UnansweredQuestion


class UnansweredQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnansweredQuestion
        fields = [
            "id",
            "question",
            "question_key",
            "language",
            "reason",
            "status",
            "occurrences",
            "last_asked_at",
            "created_at",
            "updated_at",
        ]
        # Everything except `status` is system-managed; reviewers only re-triage.
        read_only_fields = [
            "id",
            "question",
            "question_key",
            "language",
            "reason",
            "occurrences",
            "last_asked_at",
            "created_at",
            "updated_at",
        ]
