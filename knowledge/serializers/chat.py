from django.conf import settings
from rest_framework import serializers

from ..models import AnswerSource, ChatFeedback


class ChatMessageSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=["user", "assistant", "system"])
    content = serializers.CharField()


class ChatRequestSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=2000)
    history = ChatMessageSerializer(many=True, required=False, default=list)
    # Optional: when omitted/blank, the server detects the language from the question.
    language = serializers.CharField(max_length=8, required=False, allow_blank=True, default="")

    def validate_history(self, value):
        # 1 turn = a user message + the assistant's reply (2 messages).
        turns = int(getattr(settings, "CHAT_MAX_HISTORY_TURNS", 10))
        max_messages = turns * 2
        if len(value) > max_messages:
            raise serializers.ValidationError(
                f"History too long: keep the last {turns} exchanges (at most {max_messages} messages)."
            )
        return value


class ChatResponseSerializer(serializers.Serializer):
    answer = serializers.CharField()
    source = serializers.ChoiceField(choices=AnswerSource.choices)
    source_id = serializers.CharField(allow_blank=True, required=False)
    sources = serializers.ListField(child=serializers.DictField(), required=False)
    confident = serializers.BooleanField(default=True)
    response_time_ms = serializers.IntegerField(required=False)
    prompt_tokens = serializers.IntegerField(required=False)
    completion_tokens = serializers.IntegerField(required=False)
    cost_usd = serializers.FloatField(required=False)
    # Points this answer took (0 for staff) and the wallet after it (null for staff).
    points_used = serializers.FloatField(required=False)
    points_balance = serializers.FloatField(required=False, allow_null=True)


class ChatFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatFeedback
        fields = [
            "id",
            "question",
            "answer",
            "source",
            "source_id",
            "rating",
            "comment",
            "user",
            "created_at",
        ]
        read_only_fields = ["id", "user", "created_at"]
