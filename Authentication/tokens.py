"""Signed, expiring token for the account-activation email link.

Built on Django's PasswordResetTokenGenerator (HMAC of SECRET_KEY + user state,
no DB row needed). Including ``is_active``/``email_verified`` in the hash makes
the link single-use: once the account is activated those values change and the
old token stops validating. Expiry follows ``PASSWORD_RESET_TIMEOUT``.
"""

from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return f"{user.pk}{timestamp}{user.is_active}{user.email_verified}{user.email}"


email_verification_token = EmailVerificationTokenGenerator()
