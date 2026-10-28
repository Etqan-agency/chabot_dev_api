from rest_framework import serializers

from ..models import ChatbotConfig


class ChatbotConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatbotConfig
        fields = [
            "assistant_name",
            "company_name",
            "default_language",
            "tone",
            "strict_grounding",
            "no_answer_message",
            "top_k",
            "similarity_threshold",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
