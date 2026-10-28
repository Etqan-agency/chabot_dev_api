"""AI reading of a synced API record.

Structured JSON retrieves badly on its own: a question phrased in prose rarely
resembles ``{"sku": "A-19", "qty": 0}``. ``api_schema`` already turns the record
into labelled, retrievable text deterministically; this module adds the layer
that only a language model can supply -- naming what the record *is*, and saying
in ordinary words what its fields mean -- and that layer is *appended to* the
deterministic text rather than replacing it.

Three hard rules shape everything here:

* **The AI never becomes the source of a fact.** Its output is additive. Every
  value a customer can be told still comes from ``api_schema``'s rendering of
  the record, and ``api_validation`` rejects an analysis that introduces a
  number the record does not contain.
* **Credentials never reach the model.** The record is sanitized before it is
  serialised into a prompt, so an API key in the source is redacted on the way
  in rather than discovered in a prompt log later.
* **Failure is never fatal.** No LLM key, a provider outage, or a single bad
  record must never fail a sync or drop a record. The worst case is a record
  stored with an empty analysis, which the next sync retries.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from django.conf import settings

from .api_schema import sanitize

logger = logging.getLogger(__name__)

# Characters of one record sent to the LLM. Only the *prompt* is capped -- the
# stored record is always complete, however large it is.
MAX_PROMPT_CHARS = 20_000
DEFAULT_WORKERS = 4

SYSTEM_PROMPT = (
    "You describe ONE record from a customer's data API so that a support "
    "chatbot can retrieve and quote it. You are a reader, not a writer: your "
    "job is to make the record findable, never to improve it.\n"
    "Return ONLY a JSON object with these keys:\n"
    '  "entity_type": a short lowercase noun for what this record is '
    '(e.g. "product", "order", "article", "employee").\n'
    '  "summary": plain prose, in the same language as the record\'s own text '
    "(Arabic record -> Arabic summary), stating what the record is and what its "
    "fields mean in ordinary words -- including empty/zero/null fields when they "
    'carry meaning (e.g. "no discount is set", "out of stock").\n'
    '  "categories": a short list of topic words a customer might search by.\n'
    '  "ambiguous": a list of field names whose meaning you could NOT confidently '
    "determine. Leave it empty if there are none.\n"
    "Rules:\n"
    "- Preserve EVERY identifier, number, price, date, status, name and URL "
    "exactly as given. Do not round, convert, translate or reformat them.\n"
    "- NEVER state a number that does not appear in the record. Do not compute "
    "totals, discounts, differences or conversions -- not even obvious ones.\n"
    "- NEVER fill a missing value with a guess. If it is missing, say it is "
    'missing. If it is ambiguous, list it under "ambiguous" and describe it '
    "using the record's own wording.\n"
    "- Never invent, infer or add anything that is not in the record.\n"
    "- No preamble, no markdown fences. Just the JSON object."
)


@dataclass
class AnalysisResult:
    text: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Semantic fields the model identified. Advisory only: the deterministic
    # renderer owns every fact, so a wrong value here costs a label, not a price.
    data: dict = field(default_factory=dict)

    @property
    def entity_type(self) -> str:
        value = self.data.get("entity_type")
        return value.strip().lower()[:64] if isinstance(value, str) else ""

    @property
    def categories(self) -> list[str]:
        values = self.data.get("categories")
        if not isinstance(values, list):
            return []
        return [str(v).strip() for v in values if str(v).strip()][:20]

    @property
    def ambiguous(self) -> list[str]:
        values = self.data.get("ambiguous")
        if not isinstance(values, list):
            return []
        return [str(v).strip() for v in values if str(v).strip()][:50]


def is_enabled() -> bool:
    return bool(getattr(settings, "API_SYNC_AI_ANALYSIS", True))


def _workers() -> int:
    return max(1, int(getattr(settings, "API_SYNC_ANALYSIS_WORKERS", DEFAULT_WORKERS) or DEFAULT_WORKERS))


def _prompt_for(raw) -> str:
    """Serialise one record for the prompt, with credentials already removed."""
    try:
        clean, _ = sanitize(raw)
    except Exception:  # noqa: BLE001 - a weird record must still be describable
        clean = raw
    try:
        text = json.dumps(clean, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(clean)
    if len(text) > MAX_PROMPT_CHARS:
        # Truncation is visible to the model so it does not present a partial
        # record as a complete one.
        text = text[:MAX_PROMPT_CHARS] + "\n... [record truncated for analysis; full record is stored]"
    return text


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_completion(text: str) -> tuple[str, dict]:
    """Split a completion into ``(prose, structured_fields)``.

    The model is asked for JSON, but a model that answers with plain prose is
    still useful -- and every existing deployment behaved that way before this
    change -- so prose is accepted as the summary rather than discarded.
    """
    stripped = _FENCE.sub("", (text or "").strip())
    if not stripped:
        return "", {}
    if not stripped.startswith("{"):
        return stripped, {}
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return stripped, {}
    if not isinstance(parsed, dict):
        return stripped, {}

    summary = parsed.get("summary")
    prose = summary.strip() if isinstance(summary, str) else ""
    if not prose:
        # JSON with no usable summary is worse than the raw completion.
        prose = stripped
    return prose, parsed


def analyze_records(raws: dict[str, object], *, model: str | None = None) -> dict[str, AnalysisResult]:
    """Analyse ``{source_id: raw record}``; return ``{source_id: AnalysisResult}``.

    Records that could not be analysed are simply absent from the result. If no
    LLM backend is available at all, returns ``{}`` without attempting a single
    call (so a keyless deployment costs nothing and logs once).

    Each record is one request. That is the batching strategy, not an oversight:
    it is the only arrangement in which a single malformed record cannot poison
    its neighbours' descriptions, and in which prompt size is bounded by the
    largest record rather than by the size of the API response.
    """
    if not raws or not is_enabled():
        return {}

    from .llm import LLMError, get_backend

    try:
        backend = get_backend(model)
    except LLMError as exc:
        logger.info("API sync: skipping AI analysis (%s)", exc)
        return {}
    except Exception:
        logger.warning("API sync: skipping AI analysis (LLM backend unavailable)", exc_info=True)
        return {}

    from .billing import bind, scope_has_points

    def one(item: tuple[str, object]) -> tuple[str, AnalysisResult | None]:
        sid, raw = item
        if not scope_has_points():
            return sid, None  # the tenant ran out of points mid-import
        try:
            res = backend.complete_with_usage(SYSTEM_PROMPT, _prompt_for(raw))
            prose, data = parse_completion(res.text)
            if not prose:
                return sid, None
            return sid, AnalysisResult(
                text=prose,
                model=res.model or getattr(backend, "model", ""),
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
                data=data,
            )
        except Exception:
            logger.warning("API sync: analysis failed for record %s", sid, exc_info=True)
            return sid, None

    out: dict[str, AnalysisResult] = {}
    items = list(raws.items())
    workers = min(_workers(), len(items))
    if workers <= 1:
        results = [one(entry) for entry in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(bind(one), items))  # workers charge the caller's scope

    for sid, res in results:
        if res is not None:
            out[sid] = res
    return out
