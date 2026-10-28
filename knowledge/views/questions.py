from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..filters import QuestionAnswerFilter
from ..models import QuestionAnswer
from ..permissions import IsOwnerOrReadOnly
from ..serializers import (
    BulkQuestionUpdateSerializer,
    QuestionAnswerSerializer,
    QuestionAnswerWriteSerializer,
)


class QuestionAnswerViewSet(viewsets.ModelViewSet):
    """The Q&A bank. The chat pipeline searches these first (semantic match)."""

    queryset = QuestionAnswer.objects.all()
    serializer_class = QuestionAnswerSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = QuestionAnswerFilter
    search_fields = ["question", "answer", "overview_description"]
    ordering_fields = ["created_at", "updated_at", "count"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return QuestionAnswerWriteSerializer
        return QuestionAnswerSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"], url_path="increment-count")
    def increment_count(self, request, pk=None):
        question = self.get_object()
        question.increment_count()
        return Response(QuestionAnswerSerializer(question).data)

    @extend_schema(
        parameters=[OpenApiParameter("limit", int, description="Default 10")],
    )
    @action(detail=False, methods=["get"], url_path="most-asked")
    def most_asked(self, request):
        limit = int(request.query_params.get("limit", 10))
        qs = self.filter_queryset(self.get_queryset()).order_by("-count")[:limit]
        return Response(QuestionAnswerSerializer(qs, many=True).data)

    @extend_schema(request=BulkQuestionUpdateSerializer)
    @action(detail=False, methods=["post"], url_path="bulk-update")
    def bulk_update(self, request):
        serializer = BulkQuestionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["question_ids"]
        action_type = serializer.validated_data["action"]

        qs = self.get_queryset().filter(id__in=ids)
        if action_type == "activate":
            updated = qs.update(is_active=True)
        elif action_type == "deactivate":
            updated = qs.update(is_active=False)
        else:  # delete
            updated, _ = qs.delete()
        return Response({"action": action_type, "affected": updated})
