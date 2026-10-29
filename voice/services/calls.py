"""Store the call log ElevenLabs posts after each conversation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..models import Order, VoiceAgent, VoiceCall

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_TURNS = 500


def _caller_number(data: dict) -> str:
    metadata = data.get("metadata") or {}
    phone = metadata.get("phone_call") or {}
    dynamic = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}
    for value in (phone.get("external_number"), dynamic.get("system__caller_id")):
        if value:
            return str(value)[:32]
    return ""


def _transcript(data: dict) -> list[dict]:
    turns = []
    for turn in (data.get("transcript") or [])[:MAX_TRANSCRIPT_TURNS]:
        message = turn.get("message")
        if not message:
            continue  # tool-call-only turns carry no spoken text
        turns.append(
            {
                "role": turn.get("role", ""),
                "message": str(message),
                "time_in_call_secs": turn.get("time_in_call_secs") or 0,
            }
        )
    return turns


def record_post_call(payload: dict) -> VoiceCall | None:
    """Upsert the call for a ``post_call_transcription`` event.

    Returns None for events this app doesn't store (other types, unknown agents).
    """
    if payload.get("type") != "post_call_transcription":
        return None
    data = payload.get("data") or {}
    conversation_id = str(data.get("conversation_id") or "")
    agent_id = str(data.get("agent_id") or "")
    if not conversation_id or not agent_id:
        return None
    agent = VoiceAgent.objects.filter(agent_id=agent_id).select_related("tenant").first()
    if agent is None:
        logger.warning("Post-call webhook for unknown agent %s", agent_id)
        return None

    metadata = data.get("metadata") or {}
    started = metadata.get("start_time_unix_secs")
    analysis = data.get("analysis") or {}
    call, _ = VoiceCall.objects.update_or_create(
        conversation_id=conversation_id,
        defaults={
            "tenant": agent.tenant,
            "agent_id": agent_id,
            "caller_number": _caller_number(data),
            "status": str(data.get("status") or "")[:32],
            "started_at": datetime.fromtimestamp(started, tz=UTC) if isinstance(started, int | float) else None,
            "duration_secs": max(0, int(metadata.get("call_duration_secs") or 0)),
            "termination_reason": str(metadata.get("termination_reason") or "")[:255],
            "summary": str(analysis.get("transcript_summary") or ""),
            "transcript": _transcript(data),
            "order": Order.objects.filter(tenant=agent.tenant, conversation_id=conversation_id).first(),
        },
    )
    return call
