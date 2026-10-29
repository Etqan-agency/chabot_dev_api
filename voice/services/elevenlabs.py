"""Provision a tenant's ElevenLabs agent: its tools, prompt and greeting.

Run through ``manage.py voice_elevenlabs_sync``. Re-running it is safe: tools
already attached to the agent are updated in place (matched by name), so a new
tunnel or domain only needs one more run.
"""

# The spoken prompt below is one instruction per line; wrapping it would change what the agent reads.
# ruff: noqa: E501

from __future__ import annotations

import os

import requests

from knowledge.models import ChatbotConfig

API_BASE = "https://api.elevenlabs.io/v1/convai"
TIMEOUT = 30

DEFAULT_ASSISTANT = {"female": "ليلى", "male": "محمود"}

# One prompt per grammatical gender of the assistant: Arabic conjugates every
# instruction, and a female prompt read by a male voice is what callers noticed.
PROMPTS = {
    "female": """انتي "{assistant}"، بتردي على تليفون {company} وبتاخدي الطلبات. بتتكلمي مصري عامي، ودودة ومحترمة، جملك قصيرة، وسؤال واحد في المرة.
كلمي العميل بـ "حضرتك"، ومتخمنيش هو راجل ولا ست.

الأدوات:
- get_menu: استخدميها أول ما العميل يسأل عن المنيو أو يبدأ يطلب. الأصناف والأسعار اللي فيها بس هي الموجودة.
- search_info: لأي سؤال عن الفروع، العناوين، أرقام التليفون، أو العروض. لو مالقيتيش الإجابة قولي إن الفرع هيأكد لحضرتك.
- quote_order: بتحسب الإجمالي من السيستم. استخدميها قبل ما تقري الطلب على العميل.
- create_order: بتسجل الأوردر. بعد ما العميل يوافق صراحة على الطلب والإجمالي بس.

قواعد المنيو:
- أي صنف أو إضافة مش موجودة بالظبط في get_menu (زي "بالجبنة" أو "موزاريلا" لصنف مش مكتوب كده): متضيفيهاش ومتكتبيهاش في الملاحظات. قولي إنها مش متاحة في الطلب بالتليفون واقترحي أقرب صنف من المنيو.
- الملاحظات (notes) بس لطلبات زي "من غير طحينة"، أو للاختيارات اللي وصف الصنف بيطلبها.
- لو كمية صنف أكتر من 10، أكدي الرقم مع العميل مرة كمان قبل ما تكملي.

خطوات الطلب:
1. خدي الأصناف والكميات والملاحظات.
2. اسألي: توصيل ولا استلام من الفرع؟ لو توصيل خدي العنوان بالتفصيل.
3. خدي اسم العميل، واقريه عليه تاني عشان تتأكدي إنه صح.
4. استدعي quote_order، واقري على العميل الأصناف والإجمالي اللي رجع منها، واسألي "كده تمام؟"
5. لما يوافق، استدعي create_order بنفس الأصناف.
6. لو رجعت ok=true: قولي رقم الأوردر والإجمالي. لو ok=false: اشرحي المشكلة بالمصري واسأليه يعدّل.

ممنوع: تحسبي الإجمالي بنفسك، تخترعي أصناف أو أسعار، تقولي item_id للعميل، أو تقولي إن الأوردر اتسجل قبل ما create_order ترجع ok=true.""",
    "male": """انت "{assistant}"، بترد على تليفون {company} وبتاخد الطلبات. بتتكلم مصري عامي، ودود ومحترم، جملك قصيرة، وسؤال واحد في المرة.
كلم العميل بـ "حضرتك"، ومتخمنش هو راجل ولا ست.

الأدوات:
- get_menu: استخدمها أول ما العميل يسأل عن المنيو أو يبدأ يطلب. الأصناف والأسعار اللي فيها بس هي الموجودة.
- search_info: لأي سؤال عن الفروع، العناوين، أرقام التليفون، أو العروض. لو مالقيتش الإجابة قول إن الفرع هيأكد لحضرتك.
- quote_order: بتحسب الإجمالي من السيستم. استخدمها قبل ما تقرا الطلب على العميل.
- create_order: بتسجل الأوردر. بعد ما العميل يوافق صراحة على الطلب والإجمالي بس.

قواعد المنيو:
- أي صنف أو إضافة مش موجودة بالظبط في get_menu (زي "بالجبنة" أو "موزاريلا" لصنف مش مكتوب كده): متضيفهاش ومتكتبهاش في الملاحظات. قول إنها مش متاحة في الطلب بالتليفون واقترح أقرب صنف من المنيو.
- الملاحظات (notes) بس لطلبات زي "من غير طحينة"، أو للاختيارات اللي وصف الصنف بيطلبها.
- لو كمية صنف أكتر من 10، أكد الرقم مع العميل مرة كمان قبل ما تكمل.

خطوات الطلب:
1. خد الأصناف والكميات والملاحظات.
2. اسأل: توصيل ولا استلام من الفرع؟ لو توصيل خد العنوان بالتفصيل.
3. خد اسم العميل، واقراه عليه تاني عشان تتأكد إنه صح.
4. استدعي quote_order، واقرا على العميل الأصناف والإجمالي اللي رجع منها، واسأل "كده تمام؟"
5. لما يوافق، استدعي create_order بنفس الأصناف.
6. لو رجعت ok=true: قول رقم الأوردر والإجمالي. لو ok=false: اشرح المشكلة بالمصري واسأله يعدّل.

ممنوع: تحسب الإجمالي بنفسك، تخترع أصناف أو أسعار، تقول item_id للعميل، أو تقول إن الأوردر اتسجل قبل ما create_order ترجع ok=true.""",
}

FIRST_MESSAGE = "أهلاً بحضرتك في {company}، معاك {assistant}. حضرتك تحب تطلب إيه؟"


class ElevenLabsError(RuntimeError):
    pass


def api_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is not set.")
    return key


def _call(method: str, path: str, *, json=None) -> dict:
    res = requests.request(method, f"{API_BASE}{path}", headers={"xi-api-key": api_key()}, json=json, timeout=TIMEOUT)
    if res.status_code >= 400:
        raise ElevenLabsError(f"{method} {path} -> {res.status_code}: {res.text[:500]}")
    return res.json() if res.content else {}


def _items_schema() -> dict:
    return {
        "type": "array",
        "description": "The ordered items",
        "items": {
            "type": "object",
            "description": "One ordered item",
            "properties": {
                "item_id": {"type": "integer", "description": "The item_id from get_menu"},
                "quantity": {"type": "integer", "description": "How many"},
                "notes": {
                    "type": "string",
                    "description": (
                        "Special requests like من غير طحينة, or the options the item's description asks for. "
                        "Never an extra or variant that is not on the menu. Empty if none."
                    ),
                },
            },
            "required": ["item_id", "quantity"],
        },
    }


def _fulfilment_schema() -> dict:
    return {"type": "string", "description": "delivery or pickup", "enum": ["delivery", "pickup"]}


def tool_configs(base_url: str, token: str) -> list[dict]:
    """The webhook tools, pointed at ``base_url`` and authenticated with ``token``."""
    base = base_url.rstrip("/") + "/api/v1/voice/tools"
    headers = {"Authorization": f"Bearer {token}"}
    return [
        {
            "type": "webhook",
            "name": "get_menu",
            "description": (
                "Returns the restaurant menu with item_id, names, categories, descriptions and prices, "
                "plus the delivery fee. Call it before quoting any item or price, and before placing an order."
            ),
            "response_timeout_secs": 20,
            "api_schema": {"url": f"{base}/menu/", "method": "GET", "request_headers": headers},
        },
        {
            "type": "webhook",
            "name": "search_info",
            "description": (
                "Searches the restaurant's information: branches and addresses, hotline and delivery "
                "phone numbers, offers, and items whose price must be confirmed with the branch."
            ),
            "response_timeout_secs": 20,
            "api_schema": {
                "url": f"{base}/knowledge/",
                "method": "POST",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "description": "The search request",
                    "properties": {"query": {"type": "string", "description": "The customer's question, in Arabic"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "webhook",
            "name": "quote_order",
            "description": (
                "Calculates the order total on the server without placing it. Call it before reading the "
                "order back to the customer, and read back exactly the items and total it returns. "
                "If the response has ok=false, explain the error to the customer and ask them to adjust."
            ),
            "response_timeout_secs": 20,
            "api_schema": {
                "url": f"{base}/quote/",
                "method": "POST",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "description": "The order to price",
                    "properties": {"items": _items_schema(), "fulfilment": _fulfilment_schema()},
                    "required": ["items", "fulfilment"],
                },
            },
        },
        {
            "type": "webhook",
            "name": "create_order",
            "description": (
                "Places the order. Call ONLY after you read back the quote_order result and the customer "
                "explicitly confirmed. Send the same items. If the response has ok=false, "
                "explain the error to the customer and ask them to adjust."
            ),
            "response_timeout_secs": 20,
            "api_schema": {
                "url": f"{base}/orders/",
                "method": "POST",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "description": "The confirmed order",
                    "properties": {
                        "items": _items_schema(),
                        "fulfilment": _fulfilment_schema(),
                        "customer_name": {"type": "string", "description": "The customer's name"},
                        "address": {"type": "string", "description": "Full delivery address. Empty for pickup."},
                        "conversation_id": {"type": "string", "dynamic_variable": "system__conversation_id"},
                    },
                    "required": ["items", "fulfilment"],
                },
            },
        },
    ]


def list_agents() -> list[dict]:
    return _call("GET", "/agents").get("agents", [])


def sync_agent(agent_id: str, *, voice_agent, base_url: str, token: str) -> dict:
    """Create or update the tools, then point the agent at them. Returns ``{name: tool_id}``."""
    tenant = voice_agent.tenant
    agent = _call("GET", f"/agents/{agent_id}")
    prompt_cfg = agent.get("conversation_config", {}).get("agent", {}).get("prompt", {}) or {}
    attached = list(prompt_cfg.get("tool_ids") or [])

    configs = tool_configs(base_url, token)
    ours = {config["name"] for config in configs}
    existing, others = {}, []
    for tool_id in attached:
        try:
            tool = _call("GET", f"/tools/{tool_id}")
        except ElevenLabsError:
            continue  # a dangling id; dropped from the agent
        name = tool.get("tool_config", {}).get("name")
        if name in ours and name not in existing:
            existing[name] = tool_id
        else:
            others.append(tool_id)  # the tenant's own tools stay attached

    synced = {}
    for config in configs:
        tool_id = existing.get(config["name"])
        if tool_id:
            _call("PATCH", f"/tools/{tool_id}", json={"tool_config": config})
        else:
            tool_id = _call("POST", "/tools", json={"tool_config": config})["id"]
        synced[config["name"]] = tool_id

    persona = voice_agent.persona
    cfg = ChatbotConfig.objects.filter(user=tenant).first()
    names = {
        # "Assistant" is ChatbotConfig's untouched default, not a name to speak.
        "assistant": cfg.assistant_name
        if cfg and cfg.assistant_name not in ("", "Assistant")
        else DEFAULT_ASSISTANT[persona],
        "company": (cfg and cfg.company_name) or tenant.get_username(),
    }
    conversation_config = {
        "agent": {
            "first_message": FIRST_MESSAGE.format(**names),
            "language": "ar",
            "prompt": {"prompt": PROMPTS[persona].format(**names), "tool_ids": others + list(synced.values())},
        }
    }
    if voice_agent.voice_id:
        # One voice for the whole call: extra "supported voices" let the agent
        # switch speakers mid-conversation.
        conversation_config["tts"] = {"voice_id": voice_agent.voice_id, "supported_voices": []}
    _call("PATCH", f"/agents/{agent_id}", json={"conversation_config": conversation_config})
    return synced
