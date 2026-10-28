import time

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication
from subscriptions.services import is_points_exempt, points_balance

from ..models import UsageKind
from ..serializers import (
    ChatFeedbackSerializer,
    ChatRequestSerializer,
    ChatResponseSerializer,
)
from ..services import billing, quota
from ..services.rag import RagUnavailable, answer_question
from ..services.unanswered import dispatch_capture

# Chat is multi-tenant: authenticate with the tenant's API key (or a JWT) so the
# answer is scoped to that user's own knowledge and chatbot config.
CHAT_AUTH = [APIKeyAuthentication, JWTAuthentication]


def detect_language(text: str) -> str:
    """Best-effort language detection from the question itself.

    The LLM also matches the user's language via the system prompt, so this is
    mainly a hint (e.g. for Arabic dialect handling). Arabic script is detected
    with no dependency; otherwise langdetect refines it, falling back to English.
    """
    # Any Arabic-script character → Arabic (covers Arabic + common dialects).
    if any("؀" <= ch <= "ۿ" for ch in text):
        return "ar"
    try:
        from langdetect import detect

        detected = detect(text)
        lang_map = {"ar": "ar", "en": "en", "es": "es", "fr": "fr", "de": "de", "pt": "pt", "ur": "ur"}
        return lang_map.get(detected, detected)
    except Exception:
        return "en"


class ChatAPIView(APIView):
    """Answer a question using the RAG pipeline, scoped to the caller's data."""

    authentication_classes = CHAT_AUTH
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "chat"

    @extend_schema(request=ChatRequestSerializer, responses={200: ChatResponseSerializer})
    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        question = data["question"]
        history = data.get("history") or []
        language = data.get("language") or detect_language(question)

        # Per-tenant gate: suspension, rate limit, monthly question/token budget,
        # and a points pre-check (friendly early 402).
        try:
            quota.check_chat_allowed(request.user)
        except quota.QuotaError as exc:
            return Response({"detail": exc.message}, status=exc.status_code)

        # Airtight enforcement: ATOMICALLY hold this answer's points BEFORE
        # answering. Concurrent requests can't overspend — only as many as the
        # balance covers get through; the rest get 402. The hold is settled to
        # the answer's real cost, or released in full if the answer fails, so a
        # user is never charged for a reply they didn't receive.
        reserved = quota.reserve_chat_points(request.user)
        if reserved is None:
            return Response({"detail": quota.POINTS_EXHAUSTED_MESSAGE}, status=status.HTTP_402_PAYMENT_REQUIRED)

        started = time.monotonic()
        try:
            with billing.charge_to(request.user, UsageKind.CHAT, reserved=reserved) as scope:
                result = answer_question(
                    question,
                    history=history,
                    language=language,
                    user=request.user,
                )
                elapsed = int((time.monotonic() - started) * 1000)
                # Recorded with the tokens, cost and points when the scope closes.
                scope.record.update(
                    response_time_ms=elapsed, confident=result.confident, chunk_count=len(result.sources)
                )
        except RagUnavailable as exc:
            # Log the real cause; return a generic message (never leak internals).
            import logging

            logging.getLogger("knowledge").error("RAG unavailable: %s", exc)
            return Response(
                {"detail": "The assistant is temporarily unavailable. Please try again."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Capture ONLY an in-domain question the bot couldn't answer from the data
        # ("gap") — the model classified it, so greetings/off-topic are excluded.
        # De-duplicated + off the request path; never blocks the reply.
        if result.answer_status == "gap":
            dispatch_capture(user=request.user, question=question, language=language)

        cost = quota.estimate_cost(result.model, result.prompt_tokens, result.completion_tokens)
        exempt = is_points_exempt(request.user)
        return Response(
            ChatResponseSerializer(
                {
                    "answer": result.answer,
                    "source": result.source,
                    "source_id": result.source_id,
                    "sources": result.sources,
                    "confident": result.confident,
                    "response_time_ms": elapsed,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "cost_usd": round(cost, 6),
                    # What this answer took from the wallet (its LLM call plus
                    # the question's embedding), and what is left.
                    "points_used": 0 if exempt else scope.points,
                    "points_balance": None if exempt else points_balance(request.user),
                }
            ).data
        )


class ChatFeedbackAPIView(APIView):
    """Record feedback on a chat answer."""

    authentication_classes = CHAT_AUTH
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=ChatFeedbackSerializer, responses={201: ChatFeedbackSerializer})
    def post(self, request):
        serializer = ChatFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        feedback = serializer.save(user=request.user)
        return Response(ChatFeedbackSerializer(feedback).data, status=status.HTTP_201_CREATED)
