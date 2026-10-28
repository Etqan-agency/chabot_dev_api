"""Meta Embedded Signup: a tenant connects their own WhatsApp number.

The dashboard opens Meta's signup popup (Facebook Login for Business). When the
tenant finishes, the popup hands the dashboard a one-time ``code`` plus the
``waba_id`` and ``phone_number_id`` they picked, and the dashboard posts them
here. We then, server side:

1. exchange the code for the tenant's own long-lived business token,
2. check the number really is in that WhatsApp Business Account,
3. subscribe our app to the account, so its messages reach our webhook,
4. register the number for the Cloud API (two-step PIN),
5. save it as the tenant's ``WhatsAppAccount`` (token encrypted).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

import requests
from django.conf import settings
from django.db import transaction

from ..models import WhatsAppAccount
from .meta_client import graph_url
from .webhook_auth import app_secret

logger = logging.getLogger(__name__)

TIMEOUT = 20


class SignupError(Exception):
    """A step failed; ``status`` and ``code`` go back to the dashboard."""

    def __init__(self, detail: str, *, status: int = 400, code: str = "signup_failed"):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.code = code


@dataclass
class SignupConfig:
    app_id: str
    config_id: str
    graph_version: str

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.config_id and app_secret())


def signup_config() -> SignupConfig:
    return SignupConfig(
        app_id=getattr(settings, "WHATSAPP_APP_ID", ""),
        config_id=getattr(settings, "WHATSAPP_ES_CONFIG_ID", ""),
        graph_version=getattr(settings, "WHATSAPP_GRAPH_VERSION", "") or "v23.0",
    )


def _meta_error(response) -> str:
    try:
        error = response.json().get("error") or {}
        return error.get("error_user_msg") or error.get("message") or response.text[:300]
    except ValueError:
        return response.text[:300]


def _call(method: str, path: str, *, token: str | None = None, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = requests.request(method, graph_url(path), headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise SignupError(f"Could not reach Meta: {exc}", status=502, code="meta_unreachable") from exc
    if response.status_code >= 400:
        message = _meta_error(response)
        logger.warning("Embedded Signup: %s %s -> %s %s", method, path, response.status_code, message)
        raise SignupError(f"Meta: {message}", status=502, code="meta_error")
    try:
        return response.json()
    except ValueError:
        return {}


def exchange_code(code: str) -> str:
    """The one-time signup code -> the tenant's business token."""
    config = signup_config()
    data = _call(
        "GET",
        "oauth/access_token",
        params={"client_id": config.app_id, "client_secret": app_secret(), "code": code},
    )
    token = data.get("access_token", "")
    if not token:
        raise SignupError("Meta returned no access token for this signup.", status=502, code="meta_error")
    return token


def complete_signup(user, *, code: str, waba_id: str, phone_number_id: str) -> tuple[WhatsAppAccount, str]:
    """Connect ``phone_number_id`` to ``user``. Returns the account and a
    warning ("" when none): a failed registration still saves the number, so
    the dashboard can show what went wrong instead of losing the signup."""
    if not signup_config().enabled:
        raise SignupError(
            "Connecting a WhatsApp number isn't set up on this server yet.", status=503, code="not_configured"
        )
    taken = WhatsAppAccount.objects.filter(phone_number_id=phone_number_id).exclude(tenant=user).exists()
    if taken:
        raise SignupError(
            "This WhatsApp number is already connected to another account.", status=409, code="number_taken"
        )

    token = exchange_code(code)

    # The ids come from the browser: only trust a number the token can see in
    # that business account.
    numbers = _call(
        "GET",
        f"{waba_id}/phone_numbers",
        token=token,
        params={"fields": "id,display_phone_number,verified_name"},
    ).get("data", [])
    number = next((n for n in numbers if str(n.get("id")) == str(phone_number_id)), None)
    if number is None:
        raise SignupError(
            "That number isn't in the WhatsApp Business Account you connected.", code="number_not_in_waba"
        )

    _call("POST", f"{waba_id}/subscribed_apps", token=token)

    pin = f"{secrets.randbelow(10**6):06d}"
    warning = ""
    try:
        _call(
            "POST",
            f"{phone_number_id}/register",
            token=token,
            json={"messaging_product": "whatsapp", "pin": pin},
        )
    except SignupError as exc:
        # E.g. a number already registered with its own PIN. Messages may
        # still flow; keep the number and surface the reason.
        warning = exc.detail
        pin = ""

    with transaction.atomic():
        account, _ = WhatsAppAccount.objects.update_or_create(
            tenant=user,
            defaults={
                "phone_number_id": str(phone_number_id),
                "waba_id": str(waba_id),
                "access_token": token,
                "registration_pin": pin,
                "display_name": number.get("verified_name", "") or "",
                "display_phone_number": number.get("display_phone_number", "") or "",
                "connection": WhatsAppAccount.Connection.EMBEDDED_SIGNUP,
                "last_error": warning,
                "is_active": True,
            },
        )
    return account, warning


def disconnect(account: WhatsAppAccount) -> None:
    """Stop answering on this number: unsubscribe our app from the tenant's
    business account (best effort) and deactivate it. History is kept."""
    if account.connection == WhatsAppAccount.Connection.EMBEDDED_SIGNUP and account.waba_id and account.access_token:
        try:
            _call("DELETE", f"{account.waba_id}/subscribed_apps", token=account.access_token)
        except SignupError as exc:
            logger.warning("Embedded Signup: could not unsubscribe %s: %s", account.waba_id, exc.detail)
    account.is_active = False
    account.access_token = ""
    account.registration_pin = ""
    account.save(update_fields=["is_active", "access_token", "registration_pin", "updated_at"])
