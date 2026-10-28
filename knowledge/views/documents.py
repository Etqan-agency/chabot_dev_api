import logging

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..filters import UploadedDocumentFilter
from ..models import DocumentStatus, UploadedDocument
from ..serializers import UploadedDocumentSerializer, UploadedDocumentWriteSerializer
from ..services import protected_media, quota
from ..services.ingestion import dispatch_ingestion
from ..services.word_import import import_document_from_word

logger = logging.getLogger(__name__)


class UploadedDocumentViewSet(viewsets.ModelViewSet):
    queryset = UploadedDocument.objects.all()
    serializer_class = UploadedDocumentSerializer
    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = UploadedDocumentFilter
    search_fields = ["filename"]
    ordering_fields = ["created_at", "updated_at", "file_size"]
    ordering = ["-created_at"]

    # The actions that parse + embed a file (real money per call) count against
    # the `upload` rate (settings.DEFAULT_THROTTLE_RATES), on top of the
    # document/storage quota. Defined in settings all along, but no view named it.
    # Browsing and downloading stay unscoped.
    _UPLOAD_ACTIONS = {"create", "upload_word", "reindex"}

    @property
    def throttle_scope(self):
        return "upload" if getattr(self, "action", None) in self._UPLOAD_ACTIONS else None

    def get_queryset(self):
        return UploadedDocument.objects.filter(uploaded_by=self.request.user)

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return UploadedDocumentWriteSerializer
        return UploadedDocumentSerializer

    def create(self, request, *args, **kwargs):
        # Enforce the tenant's document-count / total-size quota before ingesting.
        incoming = request.FILES.get("file")
        try:
            quota.check_document_quota(request.user, incoming.size if incoming else 0)
        except quota.QuotaError as exc:
            return Response({"detail": exc.message}, status=exc.status_code)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        instance = serializer.save(
            uploaded_by=self.request.user,
            processing_status=DocumentStatus.PENDING,
        )
        try:
            dispatch_ingestion(instance)
        except Exception:
            # Status/error already persisted by ingest_document.
            logger.exception("Document ingestion failed for %s", instance.id)
        return instance

    @extend_schema(
        responses={200: OpenApiTypes.BINARY},
        description=(
            "Download the original uploaded file. The only way to read it: the media directory is not publicly served."
        ),
    )
    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, pk=None):
        """Stream this document's file to its owner.

        ``get_object`` runs against ``get_queryset``, which is already filtered
        to ``uploaded_by=request.user`` -- so another tenant's id is a 404 here,
        not a download.
        """
        document = self.get_object()
        return protected_media.serve(document.file, download_name=document.filename)

    @extend_schema(responses={202: UploadedDocumentSerializer})
    @action(detail=True, methods=["post"], url_path="reindex")
    def reindex(self, request, pk=None):
        """Force re-chunking and re-embedding of an existing document."""
        instance = self.get_object()
        quota.require_points(request.user)  # 402 when out of points / lapsed
        try:
            result = dispatch_ingestion(instance)
        except Exception:
            logger.exception("Reindex failed for %s", instance.id)
            return Response(
                {"detail": "Re-indexing failed. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        instance.refresh_from_db()
        payload = UploadedDocumentSerializer(instance).data
        if result is not None:
            payload["chunks_created"] = result.chunks_created
        return Response(payload, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
                "required": ["file"],
            }
        },
        responses={201: UploadedDocumentSerializer},
    )
    @action(detail=False, methods=["post"], url_path="upload-word")
    def upload_word(self, request):
        """Add knowledge by uploading a Word (.docx) file.

        Creates a document, then parses, chunks, and embeds it so the
        content is immediately searchable by the chat pipeline.
        """
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"detail": "A 'file' field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            quota.check_document_quota(request.user, uploaded.size)
        except quota.QuotaError as exc:
            return Response({"detail": exc.message}, status=exc.status_code)
        try:
            result = import_document_from_word(uploaded, uploaded_by=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Word upload ingestion failed")
            return Response(
                {"detail": "Failed to ingest the Word file."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                **UploadedDocumentSerializer(result.document).data,
                "chunks_created": result.chunks_created,
            },
            status=status.HTTP_201_CREATED,
        )
