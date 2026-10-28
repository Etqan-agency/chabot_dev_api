"""One account per identity, enforced where it cannot be raced.

The application-level `if User.objects.filter(...).exists()` that used to be the
only guard is a check-then-create: two concurrent signups both read "no such
user" and both write. These tests pin the serializer's friendly rejection AND
the database constraint underneath it, because only the second one survives
concurrency.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.tests.factories import make_tenant

from .models import User

PW = "Str0ng-Passw0rd-xyz!"


class DatabaseConstraintTests(TestCase):
    """The layer that holds when the application check is bypassed or raced."""

    def setUp(self):
        User.objects.create_user(username="ahmed", email="ahmed@example.com", password=PW)

    def test_a_duplicate_email_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="other", email="ahmed@example.com", password=PW)

    def test_an_email_differing_only_by_case_is_the_same_email(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="other2", email="Ahmed@Example.COM", password=PW)

    def test_a_username_differing_only_by_case_is_the_same_username(self):
        # Login resolves the username exactly, so `Ahmed` beside `ahmed` was two
        # working accounts that look identical wherever a name is displayed.
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="AHMED", email="fresh@example.com", password=PW)

    def test_blank_emails_do_not_collide_with_each_other(self):
        # AbstractUser allows a blank email; "no address" is not an identity.
        User.objects.create_user(username="noemail1", email="", password=PW)
        User.objects.create_user(username="noemail2", email="", password=PW)
        self.assertEqual(User.objects.filter(email="").count(), 2)

    def test_addresses_are_stored_normalised(self):
        user = User.objects.create_user(username="spaced", email="  Mixed@Case.COM  ", password=PW)
        user.refresh_from_db()
        self.assertEqual(user.email, "mixed@case.com")
        self.assertEqual(user.username, "spaced")


class RegistrationEndpointTests(APITestCase):
    """Both creation paths reject a duplicate with a 400, not a 500."""

    def setUp(self):
        self.public_url = reverse("register-public")
        self.admin_url = reverse("user-list")
        User.objects.create_user(username="taken", email="taken@example.com", password=PW)
        self.admin, self.admin_key = make_tenant("id_admin", is_staff=True)

    def _public(self, **over):
        body = {
            "username": "newcomer",
            "email": "fresh@example.com",
            "password": PW,
            "password_confirm": PW,
        }
        body.update(over)
        return self.client.post(self.public_url, body, format="json")

    def _admin(self, **over):
        body = {
            "username": "adminmade",
            "email": "fresh2@example.com",
            "password": PW,
            "password_confirm": PW,
        }
        body.update(over)
        return self.client.post(self.admin_url, body, format="json", headers={"x-api-key": self.admin_key})

    def test_public_signup_rejects_a_taken_email(self):
        res = self._public(email="taken@example.com")
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data)

    def test_public_signup_rejects_a_taken_email_in_another_case(self):
        res = self._public(email="TAKEN@Example.com")
        self.assertEqual(res.status_code, 400)

    def test_public_signup_rejects_a_taken_username_in_another_case(self):
        res = self._public(username="Taken")
        self.assertEqual(res.status_code, 400)
        self.assertIn("username", res.data)

    def test_admin_creation_rejects_a_taken_email(self):
        # This path had NO uniqueness check at all: the admin endpoint was the
        # way duplicate-email accounts actually got created.
        res = self._admin(email="taken@example.com")
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data)

    def test_admin_creation_rejects_a_taken_username_in_another_case(self):
        res = self._admin(username="TAKEN")
        self.assertEqual(res.status_code, 400)

    def test_a_genuinely_new_account_is_still_created(self):
        # The constraint must not block legitimate signups.
        self.assertEqual(self._public().status_code, 201)
