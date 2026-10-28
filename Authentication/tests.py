"""User self-service settings: profile edit, verified email change, API-key rules."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APITestCase

from Authentication.models import EmailChangeRequest, User
from Authentication.tokens import email_verification_token

FIXED_CODE = "123456"
_GEN = "Authentication.models.EmailChangeRequest.generate_code"


class ProfileEditTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="Test!Pass2026", email="alice@old.com", first_name="Ali"
        )
        self.client.force_authenticate(user=self.user)

    def test_me_returns_current_user(self):
        res = self.client.get(reverse("user-me"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["username"], "alice")

    def test_update_name_and_username(self):
        res = self.client.patch(
            reverse("user-me"),
            {"first_name": "Alice", "last_name": "Smith", "username": "alice2"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Alice")
        self.assertEqual(self.user.last_name, "Smith")
        self.assertEqual(self.user.username, "alice2")

    def test_duplicate_username_rejected(self):
        User.objects.create_user(username="bob", password="x")
        res = self.client.patch(reverse("user-me"), {"username": "bob"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_patch_me_cannot_change_email_directly(self):
        # Email is not a profile field — it can only change via the verified flow.
        res = self.client.patch(reverse("user-me"), {"email": "sneaky@x.com"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@old.com")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailChangeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x", email="alice@old.com")
        self.client.force_authenticate(user=self.user)

    def _request_change(self, new_email="new@x.com"):
        return self.client.post(reverse("user-change-email"), {"new_email": new_email}, format="json")

    @mock.patch(_GEN, return_value=FIXED_CODE)
    def test_change_email_sends_code_and_defers_update(self, _):
        res = self._request_change()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new@x.com"])
        self.assertIn(FIXED_CODE, mail.outbox[0].body)
        # Email NOT changed until verified.
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@old.com")
        self.assertTrue(EmailChangeRequest.objects.filter(user=self.user, is_used=False).exists())

    @mock.patch(_GEN, return_value=FIXED_CODE)
    def test_verify_applies_the_change(self, _):
        self._request_change()
        res = self.client.post(reverse("user-verify-email"), {"code": FIXED_CODE}, format="json")
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@x.com")

    @mock.patch(_GEN, return_value=FIXED_CODE)
    def test_wrong_code_rejected_and_email_unchanged(self, _):
        self._request_change()
        res = self.client.post(reverse("user-verify-email"), {"code": "000000"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@old.com")

    @mock.patch(_GEN, return_value=FIXED_CODE)
    def test_expired_code_rejected(self, _):
        self._request_change()
        req = EmailChangeRequest.objects.get(user=self.user, is_used=False)
        req.expires_at = timezone.now() - timedelta(minutes=1)
        req.save(update_fields=["expires_at"])
        res = self.client.post(reverse("user-verify-email"), {"code": FIXED_CODE}, format="json")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@old.com")

    def test_verify_without_request_rejected(self):
        res = self.client.post(reverse("user-verify-email"), {"code": "123456"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_email_already_in_use_rejected(self):
        User.objects.create_user(username="bob", password="x", email="taken@x.com")
        res = self._request_change("taken@x.com")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PublicRegistrationTests(APITestCase):
    def setUp(self):
        # ScopedRateThrottle counts persist in the cache across tests — reset so
        # the 5/min register limit doesn't bleed between test methods.
        from django.core.cache import cache

        cache.clear()

    def _register(self, **overrides):
        body = {
            "username": "signup",
            "email": "signup@example.com",
            "first_name": "Sign",
            "last_name": "Up",
            "password": "Str0ng-Passw0rd!",
            "password_confirm": "Str0ng-Passw0rd!",
        }
        body.update(overrides)
        return self.client.post(reverse("register-public"), body, format="json")

    def test_anyone_can_register_no_auth(self):
        res = self._register()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["email_verification"], "activation_sent")
        user = User.objects.get(username="signup")
        # Created inactive + unverified, with an API key, and NOT staff.
        self.assertFalse(user.is_active)
        self.assertFalse(user.email_verified)
        self.assertFalse(user.is_staff)
        self.assertTrue(hasattr(user, "api_key"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/verify-email?uid=", mail.outbox[0].body)

    def test_cannot_log_in_until_verified(self):
        self._register()
        res = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "signup", "password": "Str0ng-Passw0rd!"},
            format="json",
        )
        self.assertEqual(res.status_code, 401)

    def test_activation_then_login_works(self):
        self._register()
        user = User.objects.get(username="signup")
        uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
        token = email_verification_token.make_token(user)
        act = self.client.post(reverse("verify-email-public"), {"uid": uid, "token": token}, format="json")
        self.assertEqual(act.status_code, 200)
        login = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "signup", "password": "Str0ng-Passw0rd!"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)

    def test_email_required(self):
        res = self._register(email="")
        self.assertEqual(res.status_code, 400)

    def test_duplicate_email_rejected(self):
        User.objects.create_user(username="other", password="x", email="taken@example.com")
        res = self._register(email="taken@example.com")
        self.assertEqual(res.status_code, 400)

    def test_cannot_self_grant_staff(self):
        # Extra privilege fields in the body must be ignored.
        res = self._register(is_staff=True, is_superuser=True)
        self.assertEqual(res.status_code, 201)
        user = User.objects.get(username="signup")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_password_mismatch_rejected(self):
        res = self._register(password_confirm="different")
        self.assertEqual(res.status_code, 400)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CreateUserBlockingVerificationTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_authenticate(user=self.admin)

    def _create(self, **overrides):
        body = {
            "username": "newbie",
            "email": "newbie@example.com",
            "first_name": "New",
            "last_name": "Bie",
            "password": "Str0ng-Passw0rd!",
            "password_confirm": "Str0ng-Passw0rd!",
        }
        body.update(overrides)
        return self.client.post(reverse("user-list"), body, format="json")

    def test_create_makes_account_inactive_and_emails_link(self):
        res = self._create()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["email_verification"], "activation_sent")
        self.assertFalse(res.data["is_active"])
        self.assertFalse(res.data["email_verified"])
        # An activation LINK (not a code) was emailed to the new address.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["newbie@example.com"])
        self.assertIn("/verify-email?uid=", mail.outbox[0].body)
        user = User.objects.get(username="newbie")
        self.assertFalse(user.is_active)
        self.assertFalse(user.email_verified)

    def test_inactive_user_cannot_log_in(self):
        self._create()
        self.client.force_authenticate(user=None)
        res = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "newbie", "password": "Str0ng-Passw0rd!"},
            format="json",
        )
        self.assertEqual(res.status_code, 401)

    def test_public_link_activates_the_account(self):
        self._create()
        user = User.objects.get(username="newbie")
        uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
        token = email_verification_token.make_token(user)
        self.client.force_authenticate(user=None)
        res = self.client.post(reverse("verify-email-public"), {"uid": uid, "token": token}, format="json")
        self.assertEqual(res.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.email_verified)
        # And now they can log in.
        login = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "newbie", "password": "Str0ng-Passw0rd!"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)

    def test_bad_token_rejected(self):
        self._create()
        user = User.objects.get(username="newbie")
        uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
        self.client.force_authenticate(user=None)
        res = self.client.post(reverse("verify-email-public"), {"uid": uid, "token": "bad-token"}, format="json")
        self.assertEqual(res.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_link_is_single_use(self):
        self._create()
        user = User.objects.get(username="newbie")
        uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
        token = email_verification_token.make_token(user)
        self.client.force_authenticate(user=None)
        first = self.client.post(reverse("verify-email-public"), {"uid": uid, "token": token}, format="json")
        self.assertEqual(first.status_code, 200)
        # Re-using the SAME token after activation: idempotent success, still active.
        again = self.client.post(reverse("verify-email-public"), {"uid": uid, "token": token}, format="json")
        self.assertEqual(again.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_resend_activation(self):
        self._create()
        user = User.objects.get(username="newbie")
        res = self.client.post(reverse("user-resend-activation", args=[user.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)  # original + resend

    def test_create_without_email_stays_active(self):
        res = self._create(email="")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["email_verification"], "not_sent")
        self.assertTrue(res.data["is_active"])
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordChangeWithCodeTests(APITestCase):
    OLD = "Old!Pass2026"
    NEW = "New!Str0ng2026"

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password=self.OLD, email="alice@example.com")
        self.client.force_authenticate(user=self.user)

    def _request_code(self):
        return self.client.post(reverse("user-request-password-code"))

    def _change(self, code, old=None, new=None):
        return self.client.post(
            reverse("user-change-password"),
            {
                "old_password": old or self.OLD,
                "new_password": new or self.NEW,
                "new_password_confirm": new or self.NEW,
                "code": code,
            },
            format="json",
        )

    @mock.patch("Authentication.models.PasswordChangeCode.generate_code", return_value=FIXED_CODE)
    def test_request_code_emails_it(self, _):
        res = self._request_code()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.com"])
        self.assertIn(FIXED_CODE, mail.outbox[0].body)

    @mock.patch("Authentication.models.PasswordChangeCode.generate_code", return_value=FIXED_CODE)
    def test_full_flow_changes_password(self, _):
        self._request_code()
        res = self._change(FIXED_CODE)
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.NEW))

    @mock.patch("Authentication.models.PasswordChangeCode.generate_code", return_value=FIXED_CODE)
    def test_wrong_code_rejected_password_unchanged(self, _):
        self._request_code()
        res = self._change("000000")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.OLD))

    def test_change_without_requesting_code_rejected(self):
        res = self._change("123456")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.OLD))

    @mock.patch("Authentication.models.PasswordChangeCode.generate_code", return_value=FIXED_CODE)
    def test_wrong_old_password_rejected(self, _):
        self._request_code()
        res = self._change(FIXED_CODE, old="not-the-old-password")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.OLD))

    def test_code_field_is_required(self):
        res = self.client.post(
            reverse("user-change-password"),
            {"old_password": self.OLD, "new_password": self.NEW, "new_password_confirm": self.NEW},
            format="json",
        )
        self.assertEqual(res.status_code, 400)


class ApiKeyImmutabilityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")
        self.client.force_authenticate(user=self.user)

    def _give_plan(self, user):
        from datetime import timedelta

        from django.utils import timezone as tz

        from subscriptions.models import Plan, Subscription

        plan = Plan.objects.create(name="Paid", slug="paid")
        now = tz.now()
        Subscription.objects.create(
            user=user,
            plan=plan,
            status="active",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )

    def test_free_tier_cannot_view_key(self):
        # API-key access is a paid feature: free tier gets 402, never the key.
        res = self.client.get(reverse("user-api-key"))
        self.assertEqual(res.status_code, 402)
        self.assertNotIn("key", res.data)

    def test_paid_user_can_view_own_key(self):
        self._give_plan(self.user)
        res = self.client.get(reverse("user-api-key"))
        self.assertEqual(res.status_code, 200)
        self.assertIn("key", res.data)

    def test_free_tier_me_hides_api_key(self):
        from Authentication.models import APIKey

        APIKey.objects.get_or_create(user=self.user)  # key exists...
        res = self.client.get(reverse("user-me"))
        self.assertIsNone(res.data["api_key"])  # ...but is never shown

    def test_paid_me_shows_api_key(self):
        from Authentication.models import APIKey

        self._give_plan(self.user)
        APIKey.objects.get_or_create(user=self.user)
        res = self.client.get(reverse("user-me"))
        self.assertIsNotNone(res.data["api_key"])

    def test_admin_list_still_shows_free_tier_keys(self):
        from Authentication.models import APIKey

        APIKey.objects.get_or_create(user=self.user)
        admin = User.objects.create_user(username="root2", password="x", is_staff=True, is_superuser=True)
        self.client.force_authenticate(user=admin)
        res = self.client.get(reverse("user-list"))
        row = next(u for u in res.data["results"] if u["username"] == "alice")
        self.assertIsNotNone(row["api_key"])  # admins manage keys for everyone

    def test_self_service_key_rotation_removed(self):
        # Users must NOT be able to rotate their key. The self-service endpoint is
        # gone, so a non-admin request is denied (never 2xx) and the key is unchanged.
        from Authentication.models import APIKey

        key_before = APIKey.objects.get_or_create(user=self.user)[0].key
        res = self.client.post("/api/v1/users/regenerate-api-key/")
        self.assertGreaterEqual(res.status_code, 400)  # denied, not performed
        self.assertEqual(APIKey.objects.get(user=self.user).key, key_before)
