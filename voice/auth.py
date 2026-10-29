"""Authentication for the two things that call this app from outside.

* Tool calls from the voice agent carry ``Authorization: Bearer vt_...``, a
  per-tenant token. The token -- never anything in the request body -- decides
  which tenant's menu and orders the call touches.
* Post-call webhooks are signed by ElevenLabs with ``ElevenLabs-Signature:
  t=<unix>,v0=<hex>``, an HMAC-SHA256 of ``"<t>.<raw body>"`` under the
  workspace webhook secret.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from rest_framework import authentication, exceptions

from .models import TOKEN_PREFIX, VoiceAgent, hash_token

SIGNATURE_HEADER = "ElevenLabs-Signature"
SIGNATURE_TOLERANCE_SECS = 30 * 60


class VoiceToolAuthentication(authentication.BaseAuthentication):
    """``request.user`` = the tenant, ``request.auth`` = its VoiceAgent."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword or not parts[1].startswith(TOKEN_PREFIX):
            return None
        agent = VoiceAgent.objects.select_related("tenant").filter(token_hash=hash_token(parts[1])).first()
        if agent is None or not agent.is_active or not agent.tenant.is_active:
            raise exceptions.AuthenticationFailed("Invalid voice agent token.")
        return agent.tenant, agent

    def authenticate_header(self, request):
        return self.keyword


def webhook_secret() -> str:
    return os.getenv("ELEVENLABS_WEBHOOK_SECRET", "").strip()


def verify_signature(raw_body: bytes, header: str | None, *, now: float | None = None) -> bool:
    secret = webhook_secret()
    if not secret or not header:
        return False
    fields = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp, received = fields.get("t", ""), fields.get("v0", "")
    if not timestamp.isdigit() or not received:
        return False
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > SIGNATURE_TOLERANCE_SECS:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
