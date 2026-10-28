import logging

from django.contrib.auth import update_session_auth_hash
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import filters, permissions, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.views import TokenObtainPairView

from .authentication import APIKeyAuthentication
from .models import APIKey, User
from .serializers import (
    AdminSetPasswordSerializer,
    APIKeyAdminSerializer,
    APIKeySerializer,
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    ProfileUpdateSerializer,
    PublicRegistrationSerializer,
    UserRegistrationSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)
from .services import (
    ActivationError,
    EmailChangeError,
    PasswordChangeError,
    activate_user_by_token,
    confirm_email_change,
    send_activation_email,
    start_email_change,
    start_password_change,
    verify_password_change_code,
)

logger = logging.getLogger(__name__)

# Auth accepted on admin/account endpoints: API key, JWT, or session.
ACCOUNT_AUTH = [APIKeyAuthentication, JWTAuthentication, SessionAuthentication]

# UserViewSet actions a non-admin may call on their OWN account. Note: users can
# VIEW their API key (api_key) but never rotate it — key rotation is admin-only.
SELF_SERVICE_ACTIONS = {
    "me",
    "change_password",
    "request_password_code",
    "change_email",
    "verify_email",
    "api_key",
}


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    # Rate-limit login attempts per IP to deter brute force / credential stuffing.
    throttle_scope = "login"


class UserViewSet(viewsets.ModelViewSet):
    # select_related the api_key so the serializer doesn't issue an extra query
    # per user when listing.
    queryset = User.objects.select_related("api_key").all()
    serializer_class = UserSerializer
    authentication_classes = ACCOUNT_AUTH
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering_fields = ["username", "email", "date_joined"]
    ordering = ["username"]

    # The actions that EMAIL a code count against the `email_code` rate (inbox
    # flooding, SMTP cost). Defined in settings all along, but no view named it.
    # Checking a code needs no limit here: each code is attempt-limited itself.
    _EMAIL_CODE_ACTIONS = {"change_email", "request_password_code"}

    @property
    def throttle_scope(self):
        return "email_code" if getattr(self, "action", None) in self._EMAIL_CODE_ACTIONS else None

    def get_permissions(self):
        """Self-service actions need only authentication; everything else
        (list/create/update/delete users, key & password admin) is admin-only."""
        if self.action in SELF_SERVICE_ACTIONS:
            return [permissions.IsAuthenticated()]
        return [permissions.IsAdminUser()]

    def get_serializer_class(self):
        if self.action == "create":
            return UserRegistrationSerializer
        return UserSerializer

    def _refuse_managing_another_admin(self, target):
        """An admin account is administered by its owner and by nobody else.

        "Admin" here is a single flat role, not a hierarchy: every staff account
        can already reach every admin endpoint. So without this, any admin can
        take over any other one — set their password and log in as them, read
        their API key (which authenticates as them outright), rotate it out from
        under them, or change the email their password resets go to. Being able
        to administer *tenants* must not mean being able to seize a colleague's
        account.

        Returns a 403 response to return, or ``None`` when the action may go
        ahead. Acting on your own account is always allowed; an admin who has to
        be changed by someone else is changed from the Django admin, where the
        act is deliberate and logged.
        """
        if (target.is_staff or target.is_superuser) and target.pk != self.request.user.pk:
            logger.warning("Admin %s tried to manage admin %s via %s", self.request.user.pk, target.pk, self.action)
            return Response(
                {"detail": "Admin accounts can only be managed by their owner."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def update(self, request, *args, **kwargs):
        """Edit a tenant — but never another admin's account."""
        refused = self._refuse_managing_another_admin(self.get_object())
        return refused or super().update(request, *args, **kwargs)

    @extend_schema(responses={201: UserSerializer})
    def create(self, request, *args, **kwargs):
        """Admin only: create a user, issue an API key, and optionally put them
        on a subscription plan.

        Send an optional ``plan`` (slug or id) and ``plan_duration_days``
        (default 30) to assign a subscription at registration. This same flow is
        reusable for a future self-serve signup endpoint.
        """
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Resolve the plan (if any) BEFORE creating the user, so an invalid plan
        # fails cleanly without leaving an orphaned account.
        plan_ref = request.data.get("plan")
        plan = None
        if plan_ref:
            from subscriptions.services import resolve_plan

            plan = resolve_plan(plan_ref)
            if plan is None:
                return Response(
                    {"plan": "Unknown plan (use a valid plan slug or id)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        user = serializer.save()
        api_key, _ = APIKey.objects.get_or_create(user=user)

        # Block the account until the email is verified: create it INACTIVE and
        # email an activation link. The user can't log in (JWT and API-key auth
        # both reject inactive users) until they open the link, which flips
        # is_active + email_verified via the public /auth/verify-email/ endpoint.
        # A user with no email is left active (there's nothing to verify).
        email_verification = "not_sent"
        if user.email:
            user.is_active = False
            user.save(update_fields=["is_active"])
            try:
                send_activation_email(user)
                email_verification = "activation_sent"
            except Exception:
                logger.exception("Activation email failed for new user %s", user.pk)
                email_verification = "send_failed"

        data = {
            **UserSerializer(user).data,
            "api_key": api_key.key,
            "email_verification": email_verification,
        }
        if plan is not None:
            from subscriptions.services import assign_plan

            sub = assign_plan(user, plan, duration_days=request.data.get("plan_duration_days", 30))
            data["subscription"] = {
                "plan": sub.plan.name,
                "status": sub.status,
                "current_period_end": sub.current_period_end,
            }
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(
        responses={
            204: None,
            400: {"type": "object", "properties": {"detail": {"type": "string"}}},
            403: {"type": "object", "properties": {"detail": {"type": "string"}}},
        },
        description=(
            "Admin only: delete a tenant and everything they own. Refuses your own "
            "account (400) and any other admin/staff account (403)."
        ),
    )
    def destroy(self, request, *args, **kwargs):
        """Delete a tenant — but never an admin, and never yourself.

        Deleting a user cascades: their documents, knowledge, API key and
        subscription all go with them, and there is no undo. The two cases this
        refuses are the ones that cannot be repaired from inside the product:

        * **Your own account.** The request would succeed, the response would
          come back 204, and the token that just made it would belong to a user
          that no longer exists — an admin deleting themselves out of their own
          system by misreading one row of a table.
        * **Another admin.** The same reasoning as refusing to *deactivate* one
          (``UserSerializer.validate_is_active``): the last remaining admin can
          otherwise be removed by a second one, and nobody can grant the role
          back through the API.

        Neither is a lock-out that a password reset fixes, so both are refused
        here rather than confirmed in the UI. An account that genuinely must go
        is removed from the Django admin, where the person doing it can see the
        full blast radius first.
        """
        target = self.get_object()

        if target.pk == request.user.pk:
            return Response(
                {"detail": "You cannot delete your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        refused = self._refuse_managing_another_admin(target)
        if refused is not None:
            return refused

        logger.info("Admin %s deleted user %s", request.user.pk, target.pk)
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        request=UserRegistrationSerializer,
        responses={201: UserSerializer},
        description="Admin only: Create a new user account with API key.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="create",
    )
    def create_user(self, request):
        """Admin only: Create new user + API key (+ optional plan). Alias of create."""
        return self.create(request)

    @extend_schema(
        request=UserRegistrationSerializer,
        responses={201: UserSerializer},
        description="Admin only: register a new user (alias of create) with an API key.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="register",
    )
    def register(self, request):
        """Admin only: register (create) a new user and generate an API key."""
        return self.create(request)

    @extend_schema(methods=["GET"], responses={200: UserSerializer})
    @extend_schema(
        methods=["PATCH"],
        request=ProfileUpdateSerializer,
        responses={200: UserSerializer},
        description="Update your own profile: username, first name, last name. "
        "Email changes go through change-email/verify-email; the API key is not editable.",
    )
    @action(
        detail=False,
        methods=["get", "patch"],
        permission_classes=[permissions.IsAuthenticated],
    )
    def me(self, request):
        if request.method == "PATCH":
            serializer = ProfileUpdateSerializer(
                request.user, data=request.data, partial=True, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return Response(UserSerializer(request.user).data)

    @extend_schema(
        request=ChangeEmailSerializer,
        responses={200: None},
        description="Start a verified email change: emails a 6-digit code to the new address. "
        "The email is only updated after confirming with verify-email.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAuthenticated],
        url_path="change-email",
    )
    def change_email(self, request):
        serializer = ChangeEmailSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        start_email_change(request.user, serializer.validated_data["new_email"])
        return Response({"detail": "A verification code was sent to the new email address."})

    @extend_schema(
        request=VerifyEmailSerializer,
        responses={200: UserSerializer},
        description="Confirm a pending email change with the 6-digit code.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAuthenticated],
        url_path="verify-email",
    )
    def verify_email(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            confirm_email_change(request.user, serializer.validated_data["code"])
        except EmailChangeError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response(UserSerializer(request.user).data)

    @extend_schema(
        request=None,
        responses={200: None},
        description="Step 1 of a password change: email a 6-digit code to the user. "
        "Submit it (with old/new passwords) to change-password.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAuthenticated],
        url_path="request-password-code",
    )
    def request_password_code(self, request):
        try:
            start_password_change(request.user)
        except PasswordChangeError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "A verification code was sent to your email."})

    @extend_schema(
        request=ChangePasswordSerializer,
        responses={200: None},
        description="Change your own password. Requires the current password AND the "
        "verification code emailed by request-password-code.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAuthenticated],
        url_path="change-password",
    )
    def change_password(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["old_password"]):
            return Response(
                {"old_password": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Second factor: a code proving control of the user's email.
        try:
            verify_password_change_code(user, serializer.validated_data["code"])
        except PasswordChangeError as exc:
            return Response({"code": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        # Keep the current session valid after the password change.
        update_session_auth_hash(request, user)
        return Response({"detail": "Password updated successfully."})

    @extend_schema(
        responses={200: None},
        description="Admin only: re-send the account-activation email to an inactive user.",
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="resend-activation",
    )
    def resend_activation(self, request, pk=None):
        user = self.get_object()
        if user.is_active and user.email_verified:
            return Response({"detail": "This account is already verified."})
        if not user.email:
            return Response(
                {"detail": "User has no email to send an activation link to."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            send_activation_email(user)
        except Exception:
            logger.exception("Resend activation failed for %s", user.pk)
            return Response(
                {"detail": "Could not send the activation email. Try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"detail": f"Activation email re-sent to {user.email}."})

    @extend_schema(
        request=AdminSetPasswordSerializer,
        responses={200: None},
        description=(
            "Admin only: set a tenant's password (no old password needed). Refuses "
            "another admin's account (403) — that would be taking it over."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="set-password",
    )
    def set_password(self, request, pk=None):
        target = self.get_object()
        refused = self._refuse_managing_another_admin(target)
        if refused is not None:
            return refused
        serializer = AdminSetPasswordSerializer(data=request.data, context={"target_user": target})
        serializer.is_valid(raise_exception=True)
        target.set_password(serializer.validated_data["new_password"])
        target.save(update_fields=["password"])
        return Response({"detail": f"Password updated for {target.username}."})

    @extend_schema(responses={200: APIKeySerializer})
    @action(
        detail=False,
        methods=["get"],
        permission_classes=[permissions.IsAuthenticated],
        url_path="api-key",
    )
    def api_key(self, request):
        """View the current user's API key (read-only — users cannot rotate it).

        NOT available on the free tier: API-key access is a paid feature, so a
        free tenant gets 402 and never sees the key.
        """
        try:
            from subscriptions.features import has_feature

            allowed = has_feature(request.user, "api_key")
        except Exception:
            allowed = bool(request.user.is_staff)  # fail closed: never leak a key
        if not allowed:
            return Response(
                {"detail": "API key access is available on paid plans. Upgrade to enable it."},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        api_key, _ = APIKey.objects.get_or_create(user=request.user)
        return Response(APIKeySerializer(api_key).data)

    @extend_schema(responses={200: APIKeySerializer})
    @action(
        detail=True,
        methods=["get"],
        permission_classes=[permissions.IsAdminUser],
        url_path="api-key",
    )
    def get_user_api_key(self, request, pk=None):
        """Admin only: Get a user's API key.

        Never another admin's: the key authenticates as its owner, so handing it
        over is handing over the account.
        """
        user = self.get_object()
        refused = self._refuse_managing_another_admin(user)
        if refused is not None:
            return refused
        api_key, _ = APIKey.objects.get_or_create(user=user)
        return Response(APIKeySerializer(api_key).data)

    @extend_schema(responses={200: APIKeySerializer})
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="regenerate-api-key",
    )
    def admin_regenerate_api_key(self, request, pk=None):
        """Admin only: Regenerate a user's API key (not another admin's)."""
        user = self.get_object()
        refused = self._refuse_managing_another_admin(user)
        if refused is not None:
            return refused
        api_key, _ = APIKey.objects.get_or_create(user=user)
        api_key.key = APIKey.generate_key()
        api_key.save()
        return Response(APIKeySerializer(api_key).data)


class APIKeyViewSet(viewsets.ModelViewSet):
    """Admin-only control plane for API keys across all users.

    list/retrieve/create/delete plus actions to revoke, activate, and
    regenerate (rotate) any user's key.
    """

    queryset = APIKey.objects.select_related("user").all()
    serializer_class = APIKeyAdminSerializer
    authentication_classes = ACCOUNT_AUTH
    permission_classes = [permissions.IsAdminUser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["user__username", "user__email"]
    ordering_fields = ["created_at", "last_used_at", "is_active"]
    ordering = ["-created_at"]

    def get_queryset(self):
        """Every tenant's key, and your own — but never another admin's.

        A key authenticates as its owner, so listing or rotating a colleague's
        key here would be the same account takeover that ``UserViewSet`` refuses
        on the user routes. Hiding them rather than refusing per-action keeps a
        foreign key indistinguishable from one that does not exist (404).
        """
        others = Q(user__is_staff=True) | Q(user__is_superuser=True)
        return super().get_queryset().exclude(others & ~Q(user=self.request.user))

    @extend_schema(responses={200: APIKeyAdminSerializer})
    @action(detail=True, methods=["post"], url_path="regenerate")
    def regenerate(self, request, pk=None):
        """Rotate the key (old key stops working immediately)."""
        api_key = self.get_object()
        api_key.key = APIKey.generate_key()
        api_key.save(update_fields=["key", "updated_at"])
        return Response(APIKeyAdminSerializer(api_key).data)

    @extend_schema(responses={200: APIKeyAdminSerializer})
    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        """Disable the key without deleting it."""
        api_key = self.get_object()
        api_key.is_active = False
        api_key.save(update_fields=["is_active", "updated_at"])
        return Response(APIKeyAdminSerializer(api_key).data)

    @extend_schema(responses={200: APIKeyAdminSerializer})
    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        """Re-enable a revoked key."""
        api_key = self.get_object()
        api_key.is_active = True
        api_key.save(update_fields=["is_active", "updated_at"])
        return Response(APIKeyAdminSerializer(api_key).data)


class RegisterView(APIView):
    """Public self-signup — anyone can create an account (no admin needed).

    Same email-verification gate as admin-created accounts: the account is created
    INACTIVE and an activation link is emailed; it cannot log in until the link is
    opened (POST /auth/verify-email/). Rate-limited per IP against spam.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []
    throttle_scope = "register"

    @extend_schema(
        request=PublicRegistrationSerializer,
        responses={201: None},
        description="Register a new account. Creates it inactive and emails an "
        "activation link; the user verifies to activate and log in.",
    )
    def post(self, request):
        serializer = PublicRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        APIKey.objects.get_or_create(user=user)

        # Block until verified: inactive + activation link (email is required here).
        user.is_active = False
        user.save(update_fields=["is_active"])
        email_verification = "activation_sent"
        try:
            send_activation_email(user)
        except Exception:
            logger.exception("Activation email failed for new signup %s", user.pk)
            email_verification = "send_failed"

        return Response(
            {
                "detail": "Account created. Check your email for a link to verify and activate it before logging in.",
                "username": user.username,
                "email": user.email,
                "email_verification": email_verification,
            },
            status=status.HTTP_201_CREATED,
        )


class EmailVerifyView(APIView):
    """Public: activate a newly created account from its email-verification link.

    The link points at the frontend, which reads ``uid`` + ``token`` from the
    query string and POSTs them here (GET is also accepted for a direct click).
    No auth — the account is inactive and cannot log in until this succeeds.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "properties": {"uid": {"type": "string"}, "token": {"type": "string"}},
                "required": ["uid", "token"],
            }
        },
        responses={200: None, 400: None},
    )
    def post(self, request):
        return self._verify(request.data.get("uid"), request.data.get("token"))

    def get(self, request):
        return self._verify(request.query_params.get("uid"), request.query_params.get("token"))

    def _verify(self, uid, token):
        if not uid or not token:
            return Response(
                {"detail": "uid and token are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            activate_user_by_token(uid, token)
        except ActivationError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Email verified. Your account is now active.", "is_active": True})
