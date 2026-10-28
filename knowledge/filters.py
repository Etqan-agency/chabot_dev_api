from django_filters import rest_framework as filters

from .models import DocumentChunk, UploadedDocument


class UploadedDocumentFilter(filters.FilterSet):
    class Meta:
        model = UploadedDocument
        fields = ["processing_status", "is_active", "uploaded_by"]


class DocumentChunkFilter(filters.FilterSet):
    """Slice the bot's knowledge by where it came from and whether it was fixed."""

    source_type = filters.CharFilter(field_name="document__source_type")
    document_name = filters.CharFilter(field_name="document__filename", lookup_expr="icontains")
    is_active = filters.BooleanFilter(field_name="document__is_active")
    edited = filters.BooleanFilter(field_name="edited_at", lookup_expr="isnull", exclude=True)

    class Meta:
        model = DocumentChunk
        fields = ["document", "source_id", "source_type", "document_name", "is_active", "edited"]
