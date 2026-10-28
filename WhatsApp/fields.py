"""A text field stored encrypted at rest (Fernet), for per-tenant Meta tokens.

Every connected number carries its own long-lived Meta token, and a database
dump must not hand out the ability to send from every customer's number. The
key is ``WHATSAPP_TOKEN_KEY`` (a Fernet key) or, when unset, one derived from
``SECRET_KEY``. Values written before encryption (no ``enc:`` prefix) are
read as they are and encrypted on their next save.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

PREFIX = "enc:"


def _fernet() -> Fernet:
    key = os.getenv("WHATSAPP_TOKEN_KEY", "").strip()
    if not key:
        digest = hashlib.sha256(f"whatsapp-token:{settings.SECRET_KEY}".encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value or ""
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value or ""
    try:
        return _fernet().decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        # The key changed (SECRET_KEY / WHATSAPP_TOKEN_KEY rotated): the number
        # must be reconnected. Never return the ciphertext as a token.
        logger.error("A stored WhatsApp token could not be decrypted; reconnect the number.")
        return ""


class EncryptedTextField(models.TextField):
    """Plain text in Python, ``enc:<fernet>`` in the database."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value) if value is not None else value

    def to_python(self, value):
        return decrypt(value) if isinstance(value, str) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value) if value else value
