from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..filters import SimpleQuestionTreeFilter
from ..models import SimpleQuestionTree
from ..permissions import IsOwnerOrReadOnly
from ..serializers import (
    SimpleQuestionTreeNodeSerializer,
    SimpleQuestionTreeSerializer,
    SimpleQuestionTreeWriteSerializer,
)


class SimpleQuestionTreeViewSet(viewsets.ModelViewSet):
    queryset = SimpleQuestionTree.objects.all()
    serializer_class = SimpleQuestionTreeSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = SimpleQuestionTreeFilter
    search_fields = ["title", "answer"]
    ordering_fields = ["order", "created_at", "updated_at"]
    ordering = ["order", "created_at"]

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return SimpleQuestionTreeWriteSerializer
        return SimpleQuestionTreeSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @extend_schema(
        parameters=[OpenApiParameter("language", str, default="ar")],
        responses={200: SimpleQuestionTreeNodeSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def tree(self, request):
        language = request.query_params.get("language", "ar")
        roots = SimpleQuestionTree.objects.filter(parent__isnull=True, language=language, is_active=True).order_by(
            "order", "created_at"
        )
        return Response(SimpleQuestionTreeNodeSerializer(roots, many=True).data)

    @action(detail=True, methods=["get"])
    def children(self, request, pk=None):
        node = self.get_object()
        children = node.children.filter(is_active=True).order_by("order", "created_at")
        return Response(SimpleQuestionTreeSerializer(children, many=True).data)
