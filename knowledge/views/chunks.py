"""Knowledge content API — review and fix exactly what the bot learned.

Documents are what you *uploaded*; chunks are what the bot actually answers
from. This is CRUD over the chunks, across every source (file upload, API sync,
website crawl), so a wrong or stale answer can be corrected in place instead of
re-uploading the whole source.
"""

import logging
import uuid

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..filters import DocumentChunkFilter
from ..models import DocumentChunk, SourceType, UploadedDocument
from ..serializers.chunks import (
    BulkDeleteSerializer,
    KnowledgeChunkSerializer,
    KnowledgeChunkWriteSerializer,
    SourceContentWriteSerializer,
)
from ..services import quota
from ..services.knowledge_assembly import assemble
from ..services.knowledge_edit import KnowledgeEditError, create_chunk, update_chunk
from ..services.knowledge_full import ContentError, ContentUnavailable, replace_content

logger = logging.getLogger(__name__)


@extend_schema(tags=["Knowledge content"])
class KnowledgeContentViewSet(viewsets.ModelViewSet):
    """Full CRUD over the bot's learned knowledge.

    * ``GET    /knowledge/``           — list/search every chunk the bot knows
    * ``POST   /knowledge/``           — teach it one new piece
    * ``GET    /knowledge/{id}/``      — read one
    * ``PATCH  /knowledge/{id}/``      — fix the wording (re-embedded on save)
    * ``DELETE /knowledge/{id}/``      — make it forget one
    * ``POST   /knowledge/bulk-delete/`` — forget many at once
    * ``GET    /knowledge/stats/``     — counts per source, edited count

    And, for working on a whole source as a page rather than chunk by chunk:

    * ``GET    /knowledge/sources/``   — the sources, grouped file / api / website
    * ``GET    /knowledge/sources/{document_id}/`` — one source as continuous text
    * ``PUT    /knowledge/sources/{document_id}/`` — rewrite that whole text
    * ``DELETE /knowledge/sources/{document_id}/`` — forget the source entirely
    """

    serializer_class = KnowledgeChunkSerializer
    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = DocumentChunkFilter
    search_fields = ["content"]
    ordering_fields = ["created_at", "updated_at", "position", "edited_at"]
    ordering = ["document_id", "position"]

    def get_queryset(self):
        # Tenant isolation: only chunks of the caller's own documents.
        return DocumentChunk.objects.filter(document__uploaded_by=self.request.user).select_related("document")

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return KnowledgeChunkWriteSerializer
        return KnowledgeChunkSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter("document", str, description="Only this document's knowledge"),
            OpenApiParameter("source_type", str, description="file | api | website"),
            OpenApiParameter("edited", bool, description="Only hand-edited chunks (or only untouched ones)"),
            OpenApiParameter("search", str, description="Substring match on the content"),
        ],
        responses={200: KnowledgeChunkSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=KnowledgeChunkWriteSerializer, responses={201: KnowledgeChunkSerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = serializer.validated_data.get("document")
        if document is None:
            return Response(
                {"detail": "A 'document' is required when adding knowledge."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        quota.require_points(request.user)  # 402 when out of points / lapsed
        try:
            chunk = create_chunk(
                document,
                serializer.validated_data["content"],
                metadata=serializer.validated_data.get("metadata"),
            )
        except KnowledgeEditError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(KnowledgeChunkSerializer(chunk).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=KnowledgeChunkWriteSerializer, responses={200: KnowledgeChunkSerializer})
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        chunk = self.get_object()
        serializer = self.get_serializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        content = serializer.validated_data.get("content", chunk.content)
        if content != chunk.content:  # only a text change is re-embedded
            quota.require_points(request.user)  # 402 when out of points / lapsed
        try:
            chunk = update_chunk(chunk, content, metadata=serializer.validated_data.get("metadata"))
        except KnowledgeEditError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(KnowledgeChunkSerializer(chunk).data)

    @extend_schema(
        request=BulkDeleteSerializer,
        responses={200: {"type": "object", "properties": {"deleted": {"type": "integer"}}}},
    )
    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        """Forget several pieces of knowledge in one call."""
        serializer = BulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Scoped to the caller's own queryset, so unknown/foreign ids delete nothing.
        deleted, _ = self.get_queryset().filter(id__in=serializer.validated_data["ids"]).delete()
        return Response({"deleted": deleted}, status=status.HTTP_200_OK)

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "properties": {
                    "total": {"type": "integer"},
                    "edited": {"type": "integer"},
                    "by_source": {"type": "object"},
                    "documents": {"type": "integer"},
                },
            }
        }
    )
    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """A quick read on what the bot knows and how much of it was corrected."""
        from django.db.models import Count

        qs = self.get_queryset()
        by_source = dict(
            qs.values_list("document__source_type").annotate(n=Count("id")).values_list("document__source_type", "n")
        )
        return Response(
            {
                "total": qs.count(),
                "edited": qs.filter(edited_at__isnull=False).count(),
                "by_source": by_source,
                "documents": qs.values("document_id").distinct().count(),
            }
        )

    # ---------------------------------------------------------------- reading
    #
    # The endpoints above are built for auditing: every chunk is addressable and
    # filterable. These two are built for *reading* -- what did this source
    # actually teach the bot, in the order a person would read it. Same rows,
    # same permissions, different unit.

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "properties": {
                    "sources": {"type": "object", "description": "Keyed by file | api | website"},
                    "totals": {"type": "object"},
                },
            }
        }
    )
    @action(detail=False, methods=["get"], url_path="sources")
    def sources(self, request):
        """The tenant's sources, grouped by where their knowledge came from."""
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Length

        documents = (
            UploadedDocument.objects.filter(uploaded_by=request.user)
            .annotate(
                section_count=Count("chunks", distinct=True),
                edited_count=Count("chunks", filter=Q(chunks__edited_at__isnull=False), distinct=True),
                char_count=Sum(Length("chunks__content")),
            )
            .order_by("-created_at")
        )

        grouped: dict[str, list] = {choice: [] for choice in SourceType.values}
        for document in documents:
            # A source with nothing indexed yet would render as an empty page,
            # so it is listed with its status rather than silently dropped.
            grouped.setdefault(document.source_type, []).append(
                {
                    "id": str(document.id),
                    "filename": document.filename,
                    "source_type": document.source_type,
                    "api_url": document.api_url,
                    "is_active": document.is_active,
                    "processing_status": document.processing_status,
                    "section_count": document.section_count,
                    "edited_count": document.edited_count,
                    "char_count": document.char_count or 0,
                    "created_at": document.created_at,
                    "updated_at": document.updated_at,
                }
            )

        return Response(
            {
                "sources": grouped,
                "totals": {
                    source_type: {
                        "documents": len(rows),
                        "sections": sum(r["section_count"] for r in rows),
                        "chars": sum(r["char_count"] for r in rows),
                    }
                    for source_type, rows in grouped.items()
                },
            }
        )

    @extend_schema(
        request=SourceContentWriteSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "document": {"type": "object"},
                    "content": {"type": "string"},
                    "sections": {"type": "array", "items": {"type": "object"}},
                    "char_count": {"type": "integer"},
                },
            }
        },
    )
    @action(
        detail=False,
        methods=["get", "put", "delete"],
        url_path=r"sources/(?P<document_id>[^/.]+)",
    )
    def source_content(self, request, document_id=None):
        """One source as a page: read its whole text, rewrite it, or delete it.

        * ``GET``    — the source's knowledge as one continuous ``content``
          block (plus the sections it was built from, each with its chunk id,
          for anyone who wants to audit or edit a single piece).
        * ``PUT``    — make the submitted text the source's knowledge: it is
          re-chunked and re-embedded, so retrieval follows the correction.
        * ``DELETE`` — forget the source entirely (its sections cascade).
        """
        document = (
            UploadedDocument.objects.filter(uploaded_by=request.user, id=document_id).first()
            if _looks_like_uuid(document_id)
            else None
        )
        if document is None:
            # 404 rather than 403: a foreign id must not be distinguishable
            # from one that does not exist.
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if request.method == "DELETE":
            document.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        if request.method == "PUT":
            serializer = SourceContentWriteSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            quota.require_points(request.user)  # 402 when out of points / lapsed
            try:
                replace_content(document, serializer.validated_data["content"])
            except ContentError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            except ContentUnavailable as exc:
                # The text is fine; the embedding provider is not. The previous
                # content is still in place and still being answered from.
                return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            document.refresh_from_db()

        return Response(assemble(document))


def _looks_like_uuid(value: str | None) -> bool:
    """Guard the lookup, because a malformed id raises rather than missing."""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
