"""Read-only tool results shared by every ordering channel (phone and WhatsApp)."""

from __future__ import annotations

import logging

from knowledge.services.retrieval import search_chunks

from ..models import MenuItem, VoiceAgent

logger = logging.getLogger(__name__)

KNOWLEDGE_RESULTS = 4
KNOWLEDGE_CHARS = 1200


def menu_payload(agent: VoiceAgent) -> dict:
    """The tenant's available menu, as the agent should quote it."""
    items = MenuItem.objects.filter(tenant=agent.tenant, is_available=True)
    return {
        "ok": True,
        "currency": agent.currency,
        "delivery_fee": str(agent.delivery_fee),
        "items": [
            {
                "item_id": item.pk,
                "name": item.name,
                "category": item.category,
                "description": item.description,
                "price": str(item.price),
            }
            for item in items
        ],
    }


def search_knowledge(tenant, query: str) -> dict:
    """Raw passages from the tenant's knowledge base; the agent phrases the answer."""
    try:
        hits = search_chunks(query, top_k=KNOWLEDGE_RESULTS, user=tenant)
        if not hits:
            # Same fallback as the chat pipeline: a short question often scores
            # under the threshold against a long passage. The agent judges
            # relevance itself and is told to say when unsure.
            hits = search_chunks(query, top_k=KNOWLEDGE_RESULTS, user=tenant, threshold=0.0)
    except Exception:
        logger.exception("Knowledge search failed for tenant %s", tenant.pk)
        return {"ok": False, "error": "Knowledge search is unavailable right now."}
    return {"ok": True, "results": [hit.content[:KNOWLEDGE_CHARS] for hit in hits]}
