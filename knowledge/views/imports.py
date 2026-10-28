from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..services.excel_import import import_questions_from_xlsx


class ExcelImportView(APIView):
    """Bulk-import questions/answers from an .xlsx file."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
                "required": ["file"],
            }
        },
        responses={
            201: OpenApiResponse(description="Rows imported"),
            400: OpenApiResponse(description="Invalid file"),
        },
    )
    def post(self, request):
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"detail": "A 'file' field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = import_questions_from_xlsx(uploaded, created_by=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"created": result.created, "skipped": result.skipped},
            status=status.HTTP_201_CREATED,
        )
