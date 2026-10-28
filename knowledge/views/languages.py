from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import AvailableLanguage
from ..permissions import IsStaffOrReadOnly
from ..serializers import AvailableLanguageSerializer, AvailableLanguageWriteSerializer


class AvailableLanguageViewSet(viewsets.ModelViewSet):
    queryset = AvailableLanguage.objects.all()
    serializer_class = AvailableLanguageSerializer
    permission_classes = [IsStaffOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active"]
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name", "created_at"]
    ordering = ["name"]
    lookup_field = "code"

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return AvailableLanguageWriteSerializer
        return AvailableLanguageSerializer

    @action(detail=False, methods=["get"])
    def active(self, request):
        qs = AvailableLanguage.objects.filter(is_active=True).order_by("name")
        return Response(AvailableLanguageSerializer(qs, many=True).data)
