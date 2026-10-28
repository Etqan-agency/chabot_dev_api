"""Thin client around the Meta WhatsApp Cloud API."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def graph_url(path: str) -> str:
    """``https://graph.facebook.com/<WHATSAPP_GRAPH_VERSION>/<path>``."""
    version = getattr(settings, "WHATSAPP_GRAPH_VERSION", "") or "v23.0"
    return f"https://graph.facebook.com/{version}/{path.lstrip('/')}"


class WhatsAppClientError(RuntimeError):
    """Raised when the Meta API responds with a non-200 status."""


@dataclass
class WhatsAppConfig:
    access_token: str
    phone_number_id: str
    verify_token: str

    @classmethod
    def from_env(cls) -> WhatsAppConfig:
        return cls(
            access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
            verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
        )

    @classmethod
    def for_account(cls, account) -> WhatsAppConfig:
        """Config for a specific WhatsAppAccount, falling back to env values for
        anything the account doesn't override (token, verify token)."""
        base = cls.from_env()
        return cls(
            access_token=(getattr(account, "access_token", "") or base.access_token),
            phone_number_id=(getattr(account, "phone_number_id", "") or base.phone_number_id),
            verify_token=base.verify_token,
        )

    @property
    def messages_url(self) -> str:
        return graph_url(f"{self.phone_number_id}/messages")


class MetaWhatsAppClient:
    def __init__(self, config: WhatsAppConfig | None = None, *, timeout: int = 30):
        self.config = config or WhatsAppConfig.from_env()
        self.timeout = timeout

    def send_text(self, to_number: str, text: str) -> str:
        if not self.config.access_token or not self.config.phone_number_id:
            raise WhatsAppClientError("WhatsApp credentials are not configured.")

        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": text},
        }
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }

        response = requests.post(
            self.config.messages_url,
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )

        if response.status_code != 200:
            logger.error("Meta API error %s: %s", response.status_code, response.text[:500])
            raise WhatsAppClientError(f"Meta API responded with {response.status_code}")

        try:
            return response.json()["messages"][0]["id"]
        except (KeyError, IndexError, ValueError):
            return ""

    def verify_webhook(self, mode: str | None, token: str | None) -> bool:
        return mode == "subscribe" and token == self.config.verify_token


def parse_incoming_message(payload: dict) -> dict | None:
    """Extract the first message from a Meta webhook payload, if any."""
    try:
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return None
        message = messages[0]
        contacts = value.get("contacts") or []
        contact_name = ""
        if contacts:
            contact_name = (contacts[0].get("profile") or {}).get("name", "")

        return {
            "from_number": message.get("from", ""),
            "phone_number_id": (value.get("metadata") or {}).get("phone_number_id", ""),
            "message_text": ((message.get("text") or {}).get("body") or "").strip(),
            "message_id": message.get("id", ""),
            "timestamp": message.get("timestamp", ""),
            "profile_name": contact_name,
        }
    except (AttributeError, TypeError):
        logger.exception("Could not parse incoming WhatsApp payload")
        return None


def parse_status_updates(payload: dict) -> list[dict]:
    """Extract delivery status updates from a Meta webhook payload."""
    try:
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        return list((change.get("value") or {}).get("statuses") or [])
    except (AttributeError, TypeError):
        return []
