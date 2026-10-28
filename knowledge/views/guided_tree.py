"""Guided question tree API — cached multilingual reads + canonical-only writes.

Reads (public to any authenticated tenant): pick a language → get the nested
tree (cached). Writes (owner/admin): always applied to the canonical language;
mirrors regenerate in the background.
"""

import logging

from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication
from subscriptions.services import has_points

from ..models import AvailableLanguage, QuestionTreeNode
from ..serializers import (
    AvailableLanguageSerializer,
    QuestionTreeNodeSerializer,
    QuestionTreeNodeUpdateSerializer,
    QuestionTreeNodeWriteSerializer,
)
from ..services import guided_tree as gt
from ..services import quota

logger = logging.getLogger(__name__)

AUTH = [APIKeyAuthentication, JWTAuthentication]


def feature_blocked(request):
    """None if allowed to author the tree, else a 402 Response."""
    try:
        from subscriptions.features import has_feature

        allowed = has_feature(request.user, "guided_tree")
    except Exception:
        allowed = True  # fail open on a billing-layer hiccup
    if allowed:
        return None
    return Response(
        {"detail": "The guided question tree isn't enabled for your account."},
        status=status.HTTP_402_PAYMENT_REQUIRED,
    )


class GuidedTreeViewSet(ViewSet):
    """Multilingual guided question tree, scoped to the caller (tenant)."""

    authentication_classes = AUTH
    permission_classes = [permissions.IsAuthenticated]

    def _owned(self, request, pk):
        return QuestionTreeNode.objects.filter(owner=request.user, pk=pk).first()

    # -- Reads ------------------------------------------------------------- #
    @extend_schema(
        parameters=[OpenApiParameter("language", str, description="Language code (default: canonical)")],
        responses={200: dict},
    )
    def list(self, request):
        """Nested tree for a language (cached). Defaults to the canonical language."""
        language = request.query_params.get("language") or gt.canonical_language()
        return Response(
            {
                "language": language,
                "canonical_language": gt.canonical_language(),
                "tree": gt.get_tree(request.user, language),
            }
        )

    @extend_schema(request=QuestionTreeNodeWriteSerializer, responses={201: QuestionTreeNodeSerializer})
    def create(self, request):
        """Add a node (applied to the canonical language; mirrors follow async)."""
        blocked = feature_blocked(request)
        if blocked is not None:
            return blocked
        ser = QuestionTreeNodeWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        parent = data.get("parent")
        if parent is not None and parent.owner_id != request.user.id:
            return Response({"detail": "Unknown parent."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            node = gt.create_node(
                owner=request.user,
                parent=parent,
                title=data["title"],
                answer=data.get("answer"),
                order=data.get("order"),
                is_active=data.get("is_active", True),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(QuestionTreeNodeSerializer(node).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        node = self._owned(request, pk)
        if node is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(QuestionTreeNodeSerializer(node).data)

    @extend_schema(request=QuestionTreeNodeUpdateSerializer, responses={200: QuestionTreeNodeSerializer})
    def partial_update(self, request, pk=None):
        node = self._owned(request, pk)
        if node is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = QuestionTreeNodeUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        try:
            updated = gt.update_node(node, **ser.validated_data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(QuestionTreeNodeSerializer(updated).data)

    def destroy(self, request, pk=None):
        node = self._owned(request, pk)
        if node is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        gt.delete_node(node)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # -- Extra actions ----------------------------------------------------- #
    @extend_schema(
        parameters=[OpenApiParameter("language", str, description="Language code (default: canonical)")],
        responses={200: QuestionTreeNodeSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="flat")
    def flat(self, request):
        """Flat dump of every node in a language (admin/debug)."""
        language = request.query_params.get("language") or gt.canonical_language()
        qs = QuestionTreeNode.objects.filter(owner=request.user, language=language).order_by(
            "parent_id", "order", "created_at"
        )
        return Response(QuestionTreeNodeSerializer(qs, many=True).data)

    @extend_schema(responses={202: dict})
    @action(detail=False, methods=["post"], url_path="retranslate")
    def retranslate(self, request):
        """Re-translate one (or every) language's whole tree from the canonical tree.

        In place: the language keeps serving its current tree until each node is
        replaced, so the bot never shows an empty tree mid-way.
        """
        blocked = feature_blocked(request)
        if blocked is not None:
            return blocked
        language = request.data.get("language")
        targets = [language] if language else gt.active_target_languages(request.user)
        targets = [lang for lang in targets if not gt.is_canonical(lang)]
        if targets:
            quota.require_points(request.user)  # 402 when out of points / lapsed
            gt.schedule_sync(request.user, targets, force=True)
        return Response({"status": "retranslating", "languages": targets}, status=status.HTTP_202_ACCEPTED)


class AvailableLanguageViewSet(ViewSet):
    """The languages a tenant's tree is translated into.

    Any tenant adds/removes its OWN languages (adding one translates the whole
    tree into it, and every later question follows automatically). Global
    languages (no owner) apply to every tenant and stay staff-managed.
    """

    authentication_classes = AUTH
    permission_classes = [permissions.IsAuthenticated]

    def _is_staff(self, request):
        return request.user.is_staff or request.user.is_superuser

    def list(self, request):
        """The caller's languages + global ones, with each one's translation progress."""
        active_only = request.query_params.get("active") in ("1", "true", "yes")
        qs = gt.languages_for(request.user)
        if active_only:
            qs = qs.filter(is_active=True)
        # A mirror counts as translated once its translation succeeded (a failed
        # one keeps the source text with a blank hash until the next sync).
        counts = dict(
            QuestionTreeNode.objects.filter(owner=request.user)
            .values_list("language")
            .annotate(n=Count("id", filter=~Q(source_hash="")))
            .values_list("language", "n")
        )
        total = QuestionTreeNode.objects.filter(owner=request.user, language=gt.canonical_language()).count()
        syncing = gt.is_syncing(request.user)
        out_of_points = not has_points(request.user)
        out = []
        for lang in qs.order_by("name"):
            row = AvailableLanguageSerializer(lang).data
            row.update(
                {
                    "scope": "global" if lang.owner_id is None else "account",
                    "translated_nodes": total if gt.is_canonical(lang.code) else counts.get(lang.code, 0),
                    "total_nodes": total,
                    "translating": syncing and not gt.is_canonical(lang.code),
                    # Untranslated nodes wait for points (the plan's renewal).
                    "paused": out_of_points and not gt.is_canonical(lang.code) and counts.get(lang.code, 0) < total,
                }
            )
            out.append(row)
        return Response(out)

    @extend_schema(request=AvailableLanguageSerializer, responses={201: AvailableLanguageSerializer})
    def create(self, request):
        """Add a language and translate the caller's whole tree into it (background).

        Staff can pass ``"global": true`` to add it for every tenant instead.
        """
        blocked = feature_blocked(request)
        if blocked is not None:
            return blocked
        ser = AvailableLanguageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        code = ser.validated_data["code"].strip().lower()
        if code == gt.canonical_language():
            return Response(
                {"detail": "That is the canonical language; it already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        make_global = str(request.data.get("global", "")).lower() in ("1", "true", "yes")
        if make_global and not self._is_staff(request):
            return Response({"detail": "Only admins can add a global language."}, status=status.HTTP_403_FORBIDDEN)
        quota.require_points(request.user)  # 402 when out of points / lapsed
        if not make_global and AvailableLanguage.objects.filter(owner__isnull=True, code=code).exists():
            return Response(
                {"detail": "That language is already available to every account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        lang, _ = AvailableLanguage.objects.update_or_create(
            owner=None if make_global else request.user,
            code=code,
            defaults={"name": ser.validated_data["name"], "is_active": ser.validated_data.get("is_active", True)},
        )
        gt.schedule_sync(request.user, [lang.code])
        return Response(AvailableLanguageSerializer(lang).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        """Remove a language (never the canonical one) and its translated tree."""
        if pk == gt.canonical_language():
            return Response(
                {"detail": "The canonical language cannot be removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        own = AvailableLanguage.objects.filter(owner=request.user, code=pk).first()
        if own is not None:
            QuestionTreeNode.objects.filter(owner=request.user, language=pk).delete()
            own.delete()
            gt.invalidate_cache(request.user, [pk])
            return Response(status=status.HTTP_204_NO_CONTENT)
        lang = AvailableLanguage.objects.filter(owner__isnull=True, code=pk).first()
        if lang is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not self._is_staff(request):
            return Response({"detail": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        # A global language spans every tenant, so removing it drops that
        # language's mirror nodes for every tenant that doesn't also have it as
        # its own (leaving them would orphan untranslatable content).
        keep = AvailableLanguage.objects.filter(code=pk, owner__isnull=False).values_list("owner_id", flat=True)
        doomed = QuestionTreeNode.objects.filter(language=pk).exclude(owner_id__in=keep)
        owner_ids = list(doomed.values_list("owner_id", flat=True).distinct())
        doomed.delete()
        lang.delete()
        for owner_id in owner_ids:
            gt.invalidate_cache(owner_id, [pk])
        return Response(status=status.HTTP_204_NO_CONTENT)
