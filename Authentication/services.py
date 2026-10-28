"""Account self-service flows that need more than a serializer — currently the
verified email change (send a code, then confirm it)."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .models import EmailChangeRequest, PasswordChangeCode, User
from .tokens import email_verification_token


class EmailChangeError(Exception):
    """Raised when confirming an email change can't proceed (bad/expired code)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ActivationError(Exception):
    """Raised when an account-activation link is invalid or expired."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class PasswordChangeError(Exception):
    """Raised when a password-change code is missing, bad, or expired."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def start_password_change(user: User) -> PasswordChangeCode:
    """Email a 6-digit code that authorizes the user's next password change.

    Invalidates any earlier unused code. Requires the user to have an email.
    """
    if not user.email:
        raise PasswordChangeError("Your account has no email to send a code to. Add one first.")

    PasswordChangeCode.objects.filter(user=user, is_used=False).update(is_used=True)

    ttl = int(getattr(settings, "EMAIL_VERIFICATION_TTL_MINUTES", 15))
    code = PasswordChangeCode.generate_code()
    obj = PasswordChangeCode(user=user, expires_at=timezone.now() + timedelta(minutes=ttl))
    obj.set_code(code)
    obj.save()

    send_mail(
        subject="Your password change code",
        message=(
            f"Hi {user.first_name or user.username},\n\n"
            f"Use this code to confirm your password change:\n\n"
            f"    {code}\n\n"
            f"It expires in {ttl} minutes. If you didn't request this, someone may "
            f"be trying to access your account — do NOT share this code."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[user.email],
        fail_silently=False,
    )
    return obj


def verify_password_change_code(user: User, code: str) -> None:
    """Consume the latest pending password-change code, or raise PasswordChangeError."""
    obj = PasswordChangeCode.objects.filter(user=user, is_used=False).order_by("-created_at").first()
    if obj is None:
        raise PasswordChangeError("No pending password change. Request a code first.")

    max_attempts = int(getattr(settings, "EMAIL_VERIFICATION_MAX_ATTEMPTS", 5))
    if obj.is_expired:
        obj.is_used = True
        obj.save(update_fields=["is_used", "updated_at"])
        raise PasswordChangeError("The code has expired. Request a new one.")
    if obj.attempts >= max_attempts:
        obj.is_used = True
        obj.save(update_fields=["is_used", "updated_at"])
        raise PasswordChangeError("Too many attempts. Request a new code.")
    if not obj.code_matches(code):
        obj.attempts += 1
        obj.save(update_fields=["attempts", "updated_at"])
        raise PasswordChangeError("Incorrect code.")

    obj.is_used = True
    obj.save(update_fields=["is_used", "updated_at"])


def build_activation_link(user: User) -> str:
    """A frontend URL carrying the uid + token for this user's activation."""
    uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
    token = email_verification_token.make_token(user)
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/verify-email?uid={uid}&token={token}"


def send_activation_email(user: User) -> str:
    """Email the account-activation link to the (as-yet inactive) user."""
    link = build_activation_link(user)
    send_mail(
        subject="Verify your email to activate your account",
        message=(
            f"Hi {user.first_name or user.username},\n\n"
            f"Your account has been created but is not active yet. Verify your "
            f"email by opening this link:\n\n{link}\n\n"
            f"If you didn't expect this, you can ignore this email."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[user.email],
        fail_silently=False,
    )
    return link


def activate_user_by_token(uidb64: str, token: str) -> User:
    """Validate an activation link and flip the account to active + verified.

    Idempotent: a link for an already-active, verified account returns the user
    without error. Raises ``ActivationError`` for a bad/expired/used link.
    """
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError) as exc:
        # Generic message for all failure modes; underlying cause is not user-facing.
        raise ActivationError("Invalid or expired verification link.") from exc

    # Already verified → treat a repeat click as success (token no longer valid).
    if user.is_active and user.email_verified:
        return user

    if not email_verification_token.check_token(user, token):
        raise ActivationError("Invalid or expired verification link.")

    user.is_active = True
    user.email_verified = True
    user.save(update_fields=["is_active", "email_verified"])
    return user


def start_email_change(user: User, new_email: str) -> EmailChangeRequest:
    """Create a pending email change and email a 6-digit code to ``new_email``.

    Any earlier unused request for this user is invalidated so only the latest
    code works.
    """
    new_email = new_email.strip().lower()

    # Invalidate previous pending requests — one active code at a time.
    EmailChangeRequest.objects.filter(user=user, is_used=False).update(is_used=True)

    ttl = int(getattr(settings, "EMAIL_VERIFICATION_TTL_MINUTES", 15))
    code = EmailChangeRequest.generate_code()
    req = EmailChangeRequest(
        user=user,
        new_email=new_email,
        expires_at=timezone.now() + timedelta(minutes=ttl),
    )
    req.set_code(code)
    req.save()

    send_mail(
        subject="Confirm your new email address",
        message=(
            f"Hi {user.first_name or user.username},\n\n"
            f"Use this code to confirm your new email address:\n\n"
            f"    {code}\n\n"
            f"It expires in {ttl} minutes. If you didn't request this, ignore this email."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[new_email],
        fail_silently=False,
    )
    return req


def confirm_email_change(user: User, code: str) -> User:
    """Validate ``code`` against the latest pending request and apply the change.

    Raises ``EmailChangeError`` on no pending request, expiry, too many attempts,
    or a wrong code.
    """
    req = EmailChangeRequest.objects.filter(user=user, is_used=False).order_by("-created_at").first()
    if req is None:
        raise EmailChangeError("No pending email change. Request a new code.")

    max_attempts = int(getattr(settings, "EMAIL_VERIFICATION_MAX_ATTEMPTS", 5))
    if req.is_expired:
        req.is_used = True
        req.save(update_fields=["is_used", "updated_at"])
        raise EmailChangeError("The code has expired. Request a new one.")
    if req.attempts >= max_attempts:
        req.is_used = True
        req.save(update_fields=["is_used", "updated_at"])
        raise EmailChangeError("Too many attempts. Request a new code.")

    if not req.code_matches(code):
        req.attempts += 1
        req.save(update_fields=["attempts", "updated_at"])
        raise EmailChangeError("Incorrect code.")

    # Guard against the address being taken since the request was made.
    if User.objects.exclude(pk=user.pk).filter(email__iexact=req.new_email).exists():
        req.is_used = True
        req.save(update_fields=["is_used", "updated_at"])
        raise EmailChangeError("That email address is already in use.")

    with transaction.atomic():
        user.email = req.new_email
        user.email_verified = True
        user.save(update_fields=["email", "email_verified"])
        req.is_used = True
        req.save(update_fields=["is_used", "updated_at"])
    return user
