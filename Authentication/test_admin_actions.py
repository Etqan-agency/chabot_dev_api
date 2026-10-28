"""Admin-only user/API-key management actions and their error branches, plus
public register/verify edge cases — closing the endpoint coverage gaps."""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APITestCase

from Authentication.models import APIKey, User
from Authentication.tokens import email_verification_token

_SEND = "Authentication.views.send_activation_email"


class AdminUserActionTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", password="x", email="admin@x.com", is_staff=True, is_superuser=True
        )
        self.client.force_authenticate(self.admin)

    def _body(self, **over):
        b = {
            "username": "newbie",
            "email": "newbie@example.com",
            "password": "Str0ng-Passw0rd!",
            "password_confirm": "Str0ng-Passw0rd!",
        }
        b.update(over)
        return b

    def test_create_user_alias_creates_account(self):
        res = self.client.post(reverse("user-create-user"), self._body(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(User.objects.filter(username="newbie").exists())

    def test_register_alias_creates_account(self):
        res = self.client.post(reverse("user-register"), self._body(username="reg", email="reg@x.com"), format="json")
        self.assertEqual(res.status_code, 201)

    def test_create_with_unknown_plan_rejected(self):
        res = self.client.post(reverse("user-list"), self._body(plan="no-such-plan"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.filter(username="newbie").exists())

    def test_create_reports_send_failed_when_email_errors(self):
        with mock.patch(_SEND, side_effect=RuntimeError("smtp down")):
            res = self.client.post(reverse("user-list"), self._body(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["email_verification"], "send_failed")

    def test_non_admin_cannot_create(self):
        plain = User.objects.create_user(username="plain", password="x")
        self.client.force_authenticate(plain)
        res = self.client.post(reverse("user-list"), self._body(), format="json")
        self.assertEqual(res.status_code, 403)


class ResendActivationTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_authenticate(self.admin)

    def test_already_verified_is_noop_ok(self):
        u = User.objects.create_user(username="done", email="d@x.com", password="x")
        u.is_active = True
        u.email_verified = True
        u.save()
        res = self.client.post(reverse("user-resend-activation", args=[u.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertIn("already verified", res.data["detail"])

    def test_no_email_returns_400(self):
        u = User.objects.create_user(username="noemail", password="x")
        u.is_active = False
        u.save()
        res = self.client.post(reverse("user-resend-activation", args=[u.pk]))
        self.assertEqual(res.status_code, 400)

    def test_send_failure_returns_503(self):
        u = User.objects.create_user(username="failmail", email="f@x.com", password="x")
        u.is_active = False
        u.save()
        with mock.patch(_SEND, side_effect=RuntimeError("down")):
            res = self.client.post(reverse("user-resend-activation", args=[u.pk]))
        self.assertEqual(res.status_code, 503)


class AdminPasswordAndKeyTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", is_staff=True, is_superuser=True)
        self.target = User.objects.create_user(username="target", email="t@x.com", password="OldPass!123")
        APIKey.objects.get_or_create(user=self.target)
        self.client.force_authenticate(self.admin)

    def test_admin_set_password(self):
        res = self.client.post(
            reverse("user-set-password", args=[self.target.pk]),
            {"new_password": "Br4nd-New-Pass!"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("Br4nd-New-Pass!"))

    def test_non_admin_cannot_set_password(self):
        self.client.force_authenticate(self.target)
        res = self.client.post(
            reverse("user-set-password", args=[self.target.pk]),
            {"new_password": "whatever-Pass!1"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_admin_reads_user_api_key(self):
        res = self.client.get(reverse("user-get-user-api-key", args=[self.target.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertIn("key", res.data)

    def test_admin_regenerates_user_api_key(self):
        old = self.target.api_key.key
        res = self.client.post(reverse("user-admin-regenerate-api-key", args=[self.target.pk]))
        self.assertEqual(res.status_code, 200)
        self.target.api_key.refresh_from_db()
        self.assertNotEqual(self.target.api_key.key, old)


class DeactivationGuardTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_authenticate(self.admin)

    def test_admin_cannot_deactivate_own_account(self):
        res = self.client.patch(reverse("user-detail", args=[self.admin.pk]), {"is_active": False}, format="json")
        self.assertEqual(res.status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_admin_cannot_deactivate_another_staff(self):
        """Refused as managing someone else's admin account (403), before the
        is_active rule that catches deactivating your own (400) is even reached."""
        staff = User.objects.create_user(username="staff2", password="x", is_staff=True)
        res = self.client.patch(reverse("user-detail", args=[staff.pk]), {"is_active": False}, format="json")
        self.assertEqual(res.status_code, 403)
        staff.refresh_from_db()
        self.assertTrue(staff.is_active)

    def test_normal_user_can_still_be_deactivated(self):
        normal = User.objects.create_user(username="normal", password="x")
        res = self.client.patch(reverse("user-detail", args=[normal.pk]), {"is_active": False}, format="json")
        self.assertEqual(res.status_code, 200)
        normal.refresh_from_db()
        self.assertFalse(normal.is_active)


class ReactivateAdminCommandTests(APITestCase):
    def test_command_reactivates_locked_superuser(self):
        from io import StringIO

        from django.core.management import call_command

        u = User.objects.create_user(username="locked", password="x", is_staff=True, is_superuser=True)
        u.is_active = False
        u.save(update_fields=["is_active"])

        call_command("reactivate_admin", username="locked", stdout=StringIO())
        u.refresh_from_db()
        self.assertTrue(u.is_active)
        self.assertTrue(u.is_superuser)


class RequestPasswordCodeErrorTests(APITestCase):
    def test_no_email_user_gets_400(self):
        # start_password_change raises when the user has no email to send to.
        u = User.objects.create_user(username="noemail_pw", password="x")  # no email
        self.client.force_authenticate(u)
        res = self.client.post(reverse("user-request-password-code"))
        self.assertEqual(res.status_code, 400)


class ApiKeyViewSetTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", is_staff=True, is_superuser=True)
        self.user = User.objects.create_user(username="owner", password="x")
        self.key, _ = APIKey.objects.get_or_create(user=self.user)
        self.client.force_authenticate(self.admin)

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(reverse("api-key-list")).status_code, 403)

    def test_regenerate_rotates_key(self):
        old = self.key.key
        res = self.client.post(reverse("api-key-regenerate", args=[self.key.pk]))
        self.assertEqual(res.status_code, 200)
        self.key.refresh_from_db()
        self.assertNotEqual(self.key.key, old)

    def test_revoke_then_activate(self):
        res = self.client.post(reverse("api-key-revoke", args=[self.key.pk]))
        self.assertEqual(res.status_code, 200)
        self.key.refresh_from_db()
        self.assertFalse(self.key.is_active)

        res = self.client.post(reverse("api-key-activate", args=[self.key.pk]))
        self.assertEqual(res.status_code, 200)
        self.key.refresh_from_db()
        self.assertTrue(self.key.is_active)


class PublicRegisterVerifyEdgeTests(APITestCase):
    def test_register_reports_send_failed(self):
        with mock.patch(_SEND, side_effect=RuntimeError("down")):
            res = self.client.post(
                reverse("register-public"),
                {
                    "username": "sig",
                    "email": "sig@x.com",
                    "password": "Str0ng-Passw0rd!",
                    "password_confirm": "Str0ng-Passw0rd!",
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["email_verification"], "send_failed")

    def test_verify_email_via_get_activates(self):
        u = User.objects.create_user(username="ver", email="v@x.com", password="x")
        u.is_active = False
        u.save()
        uid = urlsafe_base64_encode(force_bytes(str(u.pk)))
        token = email_verification_token.make_token(u)
        res = self.client.get(reverse("verify-email-public"), {"uid": uid, "token": token})
        self.assertEqual(res.status_code, 200)
        u.refresh_from_db()
        self.assertTrue(u.is_active)

    def test_verify_email_missing_params_returns_400(self):
        self.assertEqual(self.client.post(reverse("verify-email-public"), {}, format="json").status_code, 400)


class AdminDeleteGuardTests(APITestCase):
    """Deleting a user cascades and cannot be undone, so the two irreparable
    cases -- an admin deleting themselves, or deleting the other admin -- are
    refused rather than confirmed in the UI."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss", password="x", email="boss@x.com", is_staff=True, is_superuser=True
        )
        self.other_admin = User.objects.create_user(
            username="colleague", password="x", email="colleague@x.com", is_staff=True
        )
        self.tenant = User.objects.create_user(username="tenant", password="x", email="tenant@x.com")
        self.client.force_authenticate(self.admin)

    def _url(self, user):
        return reverse("user-detail", args=[user.pk])

    def test_deletes_an_ordinary_tenant(self):
        self.assertEqual(self.client.delete(self._url(self.tenant)).status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.tenant.pk).exists())

    def test_cannot_delete_own_account(self):
        response = self.client.delete(self._url(self.admin))
        self.assertEqual(response.status_code, 400)
        self.assertIn("your own account", response.json()["detail"])
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_cannot_delete_another_admin(self):
        response = self.client.delete(self._url(self.other_admin))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.other_admin.pk).exists())

    def test_cannot_delete_a_staff_account_that_is_not_superuser(self):
        """`is_staff` alone is what grants admin access to this API."""
        staff = User.objects.create_user(username="helper", password="x", is_staff=True)
        self.assertEqual(self.client.delete(self._url(staff)).status_code, 403)
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())

    def test_a_tenant_still_cannot_delete_anyone(self):
        """The guard is on top of the admin-only permission, not instead of it."""
        self.client.force_authenticate(self.tenant)
        response = self.client.delete(self._url(self.other_admin))
        self.assertIn(response.status_code, (401, 403))
        self.assertTrue(User.objects.filter(pk=self.other_admin.pk).exists())


class AdminCannotSeizeAnotherAdminTests(APITestCase):
    """An admin account is administered by its owner and by nobody else.

    "Admin" is one flat role here -- every staff account reaches every admin
    endpoint -- so without these guards any admin can take over any other one:
    set their password and log in as them, read the API key that authenticates
    as them, or change the email their resets go to.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss", password="x", email="boss@x.com", is_staff=True, is_superuser=True
        )
        self.other_admin = User.objects.create_user(
            username="colleague", password="OldPass!2026", email="colleague@x.com", is_staff=True
        )
        self.tenant = User.objects.create_user(username="tenant", password="x", email="tenant@x.com")
        for user in (self.admin, self.other_admin, self.tenant):
            APIKey.objects.get_or_create(user=user)
        self.client.force_authenticate(self.admin)

    # --- passwords ---------------------------------------------------------
    def test_cannot_set_another_admins_password(self):
        response = self.client.post(
            reverse("user-set-password", args=[self.other_admin.pk]),
            {"new_password": "Seized!Pass2026"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.other_admin.refresh_from_db()
        self.assertTrue(self.other_admin.check_password("OldPass!2026"), "the password was changed anyway")

    def test_can_still_set_a_tenants_password(self):
        response = self.client.post(
            reverse("user-set-password", args=[self.tenant.pk]),
            {"new_password": "Fresh!Pass2026"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.check_password("Fresh!Pass2026"))

    def test_can_set_own_password(self):
        """The guard is about other people's accounts, not your own."""
        response = self.client.post(
            reverse("user-set-password", args=[self.admin.pk]),
            {"new_password": "MyOwn!Pass2026"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    # --- API keys (a key authenticates as its owner) ------------------------
    def test_cannot_read_another_admins_api_key(self):
        response = self.client.get(reverse("user-get-user-api-key", args=[self.other_admin.pk]))
        self.assertEqual(response.status_code, 403)

    def test_cannot_rotate_another_admins_api_key(self):
        before = APIKey.objects.get(user=self.other_admin).key
        response = self.client.post(reverse("user-admin-regenerate-api-key", args=[self.other_admin.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(APIKey.objects.get(user=self.other_admin).key, before)

    def test_the_user_list_does_not_print_another_admins_key(self):
        """The list embeds each user's key -- that must not include colleagues."""
        rows = {u["username"]: u for u in self.client.get(reverse("user-list")).json()["results"]}
        self.assertIsNone(rows["colleague"]["api_key"])
        self.assertIsNotNone(rows["tenant"]["api_key"], "a tenant's key is still shown to an admin")
        self.assertIsNotNone(rows["boss"]["api_key"], "your own key is still yours to see")

    def test_the_key_control_plane_hides_another_admins_key(self):
        keys = self.client.get(reverse("api-key-list")).json()["results"]
        owners = {k["username"] for k in keys}
        self.assertNotIn("colleague", owners)
        self.assertIn("tenant", owners)

    def test_cannot_revoke_another_admins_key_through_the_key_routes(self):
        key = APIKey.objects.get(user=self.other_admin)
        response = self.client.post(reverse("api-key-revoke", args=[key.pk]))
        self.assertEqual(response.status_code, 404)
        key.refresh_from_db()
        self.assertTrue(key.is_active)

    # --- profile -----------------------------------------------------------
    def test_cannot_change_another_admins_email(self):
        """Their email is where a password reset would be sent."""
        response = self.client.patch(
            reverse("user-detail", args=[self.other_admin.pk]),
            {"email": "attacker@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.other_admin.refresh_from_db()
        self.assertEqual(self.other_admin.email, "colleague@x.com")

    def test_can_still_edit_a_tenant(self):
        response = self.client.patch(
            reverse("user-detail", args=[self.tenant.pk]),
            {"first_name": "Renamed"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
