from django.urls import reverse
from rest_framework import serializers

from ..models import UploadedDocument
from ..services.uploads import validate_upload_size, validate_upload_type


class UploadedDocumentSerializer(serializers.ModelSerializer):
    # Deliberately NOT the raw `file` field. That renders MEDIA_URL, which nginx
    # served straight off disk to anyone holding the link -- so the document was
    # protected only by the secrecy of a URL the API itself hands out. This is
    # the authenticated, owner-checked route instead.
    download_url = serializers.SerializerMethodField()
    file_size_mb = serializers.FloatField(read_only=True)
    chunk_count = serializers.IntegerField(source="chunks.count", read_only=True)
    uploaded_by_username = serializers.CharField(source="uploaded_by.username", read_only=True)

    class Meta:
        model = UploadedDocument
        fields = [
            "id",
            "download_url",
            "filename",
            "file_size",
            "file_size_mb",
            "chunk_count",
            "processing_status",
            "error_message",
            "is_active",
            "source_type",
            "api_url",
            "items_key",
            "uploaded_by",
            "uploaded_by_username",
            "created_at",
            "updated_at",
        ]

        read_only_fields = [
            "id",
            "download_url",
            "filename",
            "file_size",
            "processing_status",
            "error_message",
            "source_type",
            "api_url",
            "items_key",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    def get_download_url(self, obj) -> str | None:
        """Absolute URL of the protected download, or None when there is no file
        (API-synced and crawled documents have content but no uploaded file)."""
        if not obj.file:
            return None
        request = self.context.get("request")
        path = reverse("document-download", args=[str(obj.pk)])
        return request.build_absolute_uri(path) if request is not None else path


class UploadedDocumentWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = UploadedDocument
        fields = ["file", "is_active"]

    def validate_file(self, file):
        try:
            validate_upload_type(file)
            validate_upload_size(file)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return file

    def create(self, validated_data):
        file = validated_data["file"]
        validated_data["filename"] = file.name
        validated_data["file_size"] = file.size
        return super().create(validated_data)
