"""Prove an incoming webhook really came from Meta.

The webhook is a public, unauthenticated POST endpoint -- it has to be, Meta
calls it -- and until now it acted on whatever JSON arrived. Anyone who knew
the URL could therefore hand the platform a payload naming any tenant's
``phone_number_id`` and any ``from`` number, and the server would:

* run that tenant's RAG pipeline, spending their LLM credits;
* **send a WhatsApp message from that tenant's business number to a phone
  number of the attacker's choosing**, because the reply goes back to the
  ``from`` in the payload; and
* write rows into the contact, session and message tables.

Meta signs every delivery with ``X-Hub-Signature-256: sha256=<hmac>``, an
HMAC-SHA256 of the *raw* body under the app secret. Checking it is what turns
this endpoint from "anyone" into "Meta".

The signature is verified against the bytes exactly as received: re-serialising
the parsed JSON would change whitespace and key order and never match.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def app_secret() -> str:
    return os.getenv("WHATSAPP_APP_SECRET", "").strip()


def signature_required() -> bool:
    """Whether an unsigned delivery must be refused.

    Defaults to required. A deployment that genuinely cannot set the secret yet
    can set ``WHATSAPP_REQUIRE_SIGNATURE=0`` to keep receiving messages, which
    is a deliberate, visible choice rather than the silent default it used to
    be -- and it is logged as a warning on every request it lets through.
    """
    raw = os.getenv("WHATSAPP_REQUIRE_SIGNATURE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def verify(raw_body: bytes, signature_header: str | None) -> bool:
    """True when ``signature_header`` is Meta's HMAC over ``raw_body``."""
    secret = app_secret()
    if not secret or not signature_header:
        return False

    received = signature_header.strip()
    if not received.lower().startswith(_PREFIX):
        return False
    received = received[len(_PREFIX) :]

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Constant time: a byte-by-byte compare leaks the correct prefix length.
    return hmac.compare_digest(expected, received)


def check(raw_body: bytes, signature_header: str | None) -> tuple[bool, str]:
    """Decide whether to process a delivery. Returns ``(allowed, reason)``.

    ``reason`` is for the log, never for the response: telling a caller *why*
    their signature was rejected helps them get it right next time.
    """
    if verify(raw_body, signature_header):
        return True, "signature ok"

    if not signature_required():
        logger.warning(
            "WhatsApp webhook: accepting an UNVERIFIED delivery because "
            "WHATSAPP_REQUIRE_SIGNATURE is off. Anyone who knows this URL can "
            "spend tenant credits and send messages from tenant numbers. Set "
            "WHATSAPP_APP_SECRET and remove the override."
        )
        return True, "signature not enforced"

    if not app_secret():
        logger.error(
            "WhatsApp webhook: rejecting a delivery because WHATSAPP_APP_SECRET "
            "is not set, so no delivery can be verified. Set it to the Meta app "
            "secret (App Dashboard -> Settings -> Basic)."
        )
        return False, "no app secret configured"

    return False, "bad or missing signature"
