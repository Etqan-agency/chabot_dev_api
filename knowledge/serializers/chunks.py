"""Serializers for knowledge content — the chunks the bot actually answers from."""

from rest_framework import serializers

from ..models import DocumentChunk, UploadedDocument


class KnowledgeChunkSerializer(serializers.ModelSerializer):
    """One piece of learned knowledge, with enough context to review it."""

    document_name = serializers.CharField(source="document.filename", read_only=True)
    source_type = serializers.CharField(source="document.source_type", read_only=True)
    api_url = serializers.CharField(source="document.api_url", read_only=True)
    is_active = serializers.BooleanField(source="document.is_active", read_only=True)
    char_count = serializers.SerializerMethodField()
    is_embedded = serializers.SerializerMethodField()
    was_edited = serializers.SerializerMethodField()

    class Meta:
        model = DocumentChunk
        fields = [
            "id",
            "document",
            "document_name",
            "source_type",
            "api_url",
            "is_active",
            "position",
            "content",
            "char_count",
            "metadata",
            "source_id",
            "embedding_model",
            "is_embedded",
            "was_edited",
            "edited_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_char_count(self, obj) -> int:
        return len(obj.content or "")

    def get_is_embedded(self, obj) -> bool:
        return bool(obj.embedding)

    def get_was_edited(self, obj) -> bool:
        return obj.edited_at is not None


class KnowledgeChunkWriteSerializer(serializers.Serializer):
    """Create/replace the text of one piece of knowledge.

    ``document`` is required on create (which document the knowledge belongs to)
    and ignored on update — a chunk never moves between documents.
    """

    document = serializers.PrimaryKeyRelatedField(queryset=UploadedDocument.objects.none(), required=False)
    content = serializers.CharField()
    metadata = serializers.DictField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            # Scoped to the caller: you cannot attach knowledge to someone
            # else's document by guessing its id.
            self.fields["document"].queryset = UploadedDocument.objects.filter(uploaded_by=request.user)

    def validate_content(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Content cannot be empty.")
        return value


class SourceContentWriteSerializer(serializers.Serializer):
    """Replace a whole source's knowledge with one block of text.

    Whitespace inside the text is the author's, so it is not trimmed here (the
    service strips the ends and re-chunks the rest).
    """

    content = serializers.CharField(trim_whitespace=False)

    def validate_content(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("Content cannot be empty.")
        return value


class BulkDeleteSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False, max_length=500)
