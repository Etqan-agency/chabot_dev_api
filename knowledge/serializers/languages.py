from rest_framework import serializers

from ..models import AvailableLanguage


class AvailableLanguageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailableLanguage
        fields = ["code", "name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class AvailableLanguageWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailableLanguage
        fields = ["code", "name", "is_active"]
