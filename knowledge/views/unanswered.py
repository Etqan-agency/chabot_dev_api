"""Review knowledge gaps: questions the bot couldn't confidently answer.

Scoped to the authenticated tenant. Read + re-triage (status) + a ``resolve``
shortcut. Listing defaults to most-frequent-first so the biggest gaps surface.
"""

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..models import UnansweredQuestion, UnansweredStatus
from ..serializers import UnansweredQuestionSerializer
from ..services import quota
from ..services.embeddings import EmbeddingError
from ..services.unanswered import resolve_to_knowledge


class UnansweredQuestionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """List / inspect / re-triage / delete this tenant's captured knowledge gaps.

    No create endpoint: rows are produced by the chat pipeline, not the API.
    """

    serializer_class = UnansweredQuestionSerializer
    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "language"]
    search_fields = ["question"]
    ordering_fields = ["occurrences", "last_asked_at", "created_at"]
    ordering = ["-occurrences", "-last_asked_at"]

    def get_queryset(self):
        # Tenant isolation: a user only ever sees their own gaps.
        return UnansweredQuestion.objects.filter(user=self.request.user)

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The answer to add to knowledge. If provided, it is "
                        "embedded so the bot can retrieve it next time. If omitted, the gap "
                        "is just marked answered.",
                    }
                },
            }
        },
        responses={200: UnansweredQuestionSerializer},
    )
    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Resolve a gap. With an ``answer`` in the body, write it into the tenant's
        knowledge (embedded + retrievable) and mark answered; without one, just mark
        answered."""
        obj = self.get_object()
        answer = (request.data.get("answer") or "").strip()
        if answer:
            quota.require_points(request.user)  # 402 when out of points / lapsed
            try:
                resolve_to_knowledge(unanswered=obj, answer=answer, user=request.user)
            except EmbeddingError:
                return Response(
                    {"detail": "Could not embed the answer right now. Please try again later."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            obj.refresh_from_db()
        else:
            obj.status = UnansweredStatus.ANSWERED
            obj.save(update_fields=["status", "updated_at"])
        return Response(UnansweredQuestionSerializer(obj).data)

    @extend_schema(request=None, responses={200: UnansweredQuestionSerializer})
    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        """Mark a gap as dismissed (not worth answering)."""
        obj = self.get_object()
        obj.status = UnansweredStatus.DISMISSED
        obj.save(update_fields=["status", "updated_at"])
        return Response(UnansweredQuestionSerializer(obj).data)
