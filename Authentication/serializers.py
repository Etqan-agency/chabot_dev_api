from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import APIKey, User


def run_password_validation(password, *, user=None, field="password"):
    """Validate a password and surface every failure reason under ``field``.

    Passing ``user`` enables the similarity check (password vs username/email/name).
    """
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({field: list(exc.messages)}) from exc


def _is_free_tier(user) -> bool:
    """Non-staff tenant with no active paid plan. Lazy import; on any failure we
    treat the user as free tier so a billing-layer hiccup never leaks a key."""
    try:
        from knowledge.services.quota import is_free_tier

        return is_free_tier(user)
    except Exception:
        return not getattr(user, "is_staff", False)


class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    api_key = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "email_verified",
            "first_name",
            "last_name",
            "role",
            "api_key",
            "is_active",
            "date_joined",
        ]
        read_only_fields = ["id", "role", "api_key", "email_verified", "date_joined"]

    def validate_is_active(self, value):
        """Admin/staff accounts can never be deactivated — that would lock the
        owner out of the whole system (no login, no API key). Guards the admin
        PATCH /users/{id}/ path against an accidental self-lockout."""
        if (
            value is False
            and self.instance
            and (getattr(self.instance, "is_staff", False) or getattr(self.instance, "is_superuser", False))
        ):
            raise serializers.ValidationError("Admin/staff accounts cannot be deactivated.")
        return value

    def get_role(self, obj):
        return "admin" if obj.is_staff else "user"

    def get_api_key(self, obj):
        """The user's API key string (null if not issued yet).

        HIDDEN (null) for free-tier tenants — API-key access is a paid feature.
        Admin viewers (e.g. the admin user list) see every *tenant's* key.

        Never another admin's, though. The key authenticates as its owner, so a
        user list that printed a colleague's key would hand out their account —
        the same takeover the user routes refuse (``_refuse_managing_another_admin``),
        through a field instead of an endpoint.
        """
        request = self.context.get("request")
        viewer = getattr(request, "user", None) if request is not None else None
        viewer_is_staff = bool(viewer is not None and getattr(viewer, "is_staff", False))
        if (obj.is_staff or obj.is_superuser) and getattr(viewer, "pk", None) != obj.pk:
            return None
        if not viewer_is_staff and _is_free_tier(obj):
            return None
        try:
            return obj.api_key.key
        except APIKey.DoesNotExist:
            return None


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    password_confirm = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "password_confirm",
        ]

    def validate_email(self, value):
        """One account per address, compared the way the database compares it.

        This is the friendly half of the rule -- a 400 instead of the 500 an
        IntegrityError would produce. The database constraint is the half that
        actually holds under concurrency; this check cannot, because two
        simultaneous signups both pass it before either writes.
        """
        value = (value or "").strip().lower()
        if value and User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_username(self, value):
        """Usernames differing only by case are the same username.

        Without this, `Admin` could be registered alongside `admin`: two
        accounts that are indistinguishable wherever a name is displayed.
        """
        value = (value or "").strip()
        if value and User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("An account with this username already exists.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match"})
        # Validate against an unsaved user so the similarity check applies
        # (rejects a password too close to the username/email/name).
        candidate = User(
            username=attrs.get("username", ""),
            email=attrs.get("email", ""),
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
        )
        run_password_validation(attrs["password"], user=candidate)
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        return User.objects.create_user(**validated_data)


class PublicRegistrationSerializer(UserRegistrationSerializer):
    """Public self-signup: same as admin registration, but email is REQUIRED and
    must be unique (it's the address we send the activation link to). Only the
    safe fields are accepted — never is_staff/is_superuser."""

    # Uniqueness and normalisation are inherited; only the requirement differs.
    email = serializers.EmailField(required=True)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.username
        token["is_staff"] = user.is_staff
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class APIKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = APIKey
        fields = ["id", "key", "is_active", "last_used_at", "created_at"]
        read_only_fields = ["id", "key", "last_used_at", "created_at"]


class APIKeyAdminSerializer(serializers.ModelSerializer):
    """Admin view of API keys — can issue a key for a user and toggle it."""

    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = APIKey
        fields = ["id", "user", "username", "key", "is_active", "last_used_at", "created_at"]
        read_only_fields = ["id", "key", "username", "last_used_at", "created_at"]


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """Self-service profile edit: name + username. Email is NOT here — it changes
    only through the verified flow; the API key is never editable."""

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name"]

    def validate_username(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Username cannot be blank.")
        qs = User.objects.filter(username__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That username is already taken.")
        return value


class ChangeEmailSerializer(serializers.Serializer):
    """Step 1 of the verified email change — request a code for a new address."""

    new_email = serializers.EmailField()

    def validate_new_email(self, value):
        value = value.strip().lower()
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and user.email and user.email.lower() == value:
            raise serializers.ValidationError("That's already your current email.")
        qs = User.objects.filter(email__iexact=value)
        if user is not None:
            qs = qs.exclude(pk=user.pk)
        if qs.exists():
            raise serializers.ValidationError("That email address is already in use.")
        return value


class VerifyEmailSerializer(serializers.Serializer):
    """Step 2 of the verified email change — confirm with the emailed code."""

    code = serializers.CharField(min_length=4, max_length=8)


class ChangePasswordSerializer(serializers.Serializer):
    """Self-service password change — requires the current password AND a
    verification code emailed to the user (request it via request-password-code)."""

    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)
    new_password_confirm = serializers.CharField(write_only=True)
    code = serializers.CharField(min_length=4, max_length=8)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError({"new_password_confirm": "Passwords do not match"})
        request = self.context.get("request")
        user = getattr(request, "user", None)
        run_password_validation(attrs["new_password"], user=user, field="new_password")
        return attrs


class AdminSetPasswordSerializer(serializers.Serializer):
    """Admin password reset for another user — no old password required."""

    new_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        run_password_validation(
            attrs["new_password"],
            user=self.context.get("target_user"),
            field="new_password",
        )
        return attrs
