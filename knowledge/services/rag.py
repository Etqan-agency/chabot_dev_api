"""RAG-only answer pipeline with multilingual and multi-dialect support.

Retrieves relevant document chunks and passes them to an LLM
for question answering in the user's language and dialect.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import untrusted
from .chatbot_config import (
    build_no_data_prompt,
    build_system_prompt,
    resolve_config,
    resolve_threshold,
    resolve_top_k,
)
from .llm import LLMError, get_backend
from .retrieval import ChunkHit, search_chunks

logger = logging.getLogger(__name__)


# The system prompt is assembled per-user from ChatbotConfig — see
# services/chatbot_config.py::build_system_prompt.


class RagUnavailable(RuntimeError):
    """Raised when RAG pipeline cannot run."""


@dataclass
class AnswerResult:
    answer: str
    source: str  # "rag"
    source_id: str = ""
    sources: list[dict] = field(default_factory=list)
    chunk_hits: list[ChunkHit] = field(default_factory=list)
    confident: bool = True
    # How the model classified this turn (drives the "not sure" hint + gap capture):
    #   "answered"  – the specific answer was in the tenant's data.
    #   "clarify"   – the data holds the asked-for fact for SEVERAL entities and the
    #                 question named none of them, so the bot asked which one. A
    #                 correct turn, not a failure: confident, and never a gap.
    #   "gap"       – in-domain question, but the answer is NOT in the data → capture.
    #   "offtopic"  – greeting / unrelated question → do not capture.
    answer_status: str = "answered"
    # Token usage of the LLM call that produced this answer (for metering).
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


_STATUS_TAG = re.compile(r"\[\[\s*STATUS\s*:\s*(answered|clarify|gap|offtopic)\s*\]\]", re.IGNORECASE)
# The same tag, but only where the prompt actually asks for it: the final line.
_TRAILING_STATUS_TAG = re.compile(
    r"\[\[\s*STATUS\s*:\s*(answered|clarify|gap|offtopic)\s*\]\]\s*\Z",
    re.IGNORECASE,
)


def _extract_status(text: str) -> tuple[str, str | None]:
    """Split the model's classification tag off the answer.

    Returns ``(clean_answer, status)`` where status is "answered" | "clarify" |
    "gap" | "offtopic" from the ``[[STATUS:...]]`` tag, or ``None`` if the model
    omitted it. The tag is always stripped from the visible answer.
    """
    raw = (text or "").strip()

    # Only a tag at the very END counts -- that is where the prompt asks for it.
    # It used to be matched anywhere in the completion and then stripped from
    # the visible reply, so a tag the model emitted mid-answer (which retrieved
    # content can talk it into doing) silently set the turn's status and left no
    # trace for the reader: a fabricated answer could be marked "answered" and
    # kept out of the knowledge-gap queue.
    status: str | None = None
    trailing = _TRAILING_STATUS_TAG.search(raw)
    if trailing:
        status = trailing.group(1).lower()
        raw = raw[: trailing.start()].rstrip()

    # Any remaining tags are text, not control: neutralise them so they neither
    # reach the customer looking like machinery nor get mistaken for one later.
    clean = _STATUS_TAG.sub("", raw).strip()
    return clean, status


def detect_dialect(text: str, language: str = "ar") -> str:
    """Detect Arabic dialect or language variant from text.

    Returns: en, ar-eg, ar-iq, ar-sa, ar-ae, ar-sy, ar-ma, etc.
    """
    if language != "ar":
        return language

    # Arabic dialect indicators (simplified heuristics)
    dialect_markers = {
        "ar-iq": ["يعني", "شنو", "صح", "خلاص", "كريت", "مسدس", "اني"],
        "ar-eg": ["يا جماعة", "يارب", "انت", "إني", "قول", "فين", "ممكن", "يعم"],
        "ar-sa": ["إن شاء الله", "والله", "إي", "لا", "عسى", "أجل", "صادي"],
        "ar-ae": ["خلاص", "شنو", "إي", "إله", "شوية", "شوف"],
        "ar-sy": ["يا", "إرجع", "شوف", "طول", "أشي", "خلاص"],
        "ar-ma": ["واخا", "فالقيت", "كاع", "شنو", "أشي", "كيفاش"],
    }

    text_lower = text.lower()
    dialect_scores = {}

    for dialect, markers in dialect_markers.items():
        score = sum(1 for marker in markers if marker in text_lower)
        if score > 0:
            dialect_scores[dialect] = score

    return max(dialect_scores, key=dialect_scores.get) if dialect_scores else "ar"


def answer_question(
    question: str,
    *,
    history: Iterable[dict] | None = None,
    language: str = "ar",
    rag_threshold: float | None = None,
    user=None,
) -> AnswerResult:
    # Per-user chatbot config: identity + grounding + retrieval overrides.
    cfg = resolve_config(user)
    system_prompt = build_system_prompt(cfg)
    llm_model = _resolve_llm_model(user)  # per-plan model, or None for default

    base_threshold = (
        rag_threshold if rag_threshold is not None else float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.45"))
    )
    threshold = resolve_threshold(cfg, base_threshold)

    # Detect dialect for more natural responses. Result is currently discarded --
    # dialect detection has no effect downstream. This is a latent behaviour bug,
    # tracked for the audit sub-project to decide whether/how it should be used.
    dialect = detect_dialect(question, language)  # noqa: F841

    # Tenant isolation guard: retrieval is ALWAYS scoped to one tenant. Without
    # an authenticated user we must never search the shared chunk table (that
    # would leak other tenants' data), so we skip retrieval entirely.
    retrieval_query = question
    if user is None or not getattr(user, "is_authenticated", False):
        chunks = []
    else:
        # Follow-ups ("مين قبله") carry no retrievable signal alone → enrich the
        # search query with recent conversation context (deterministic, no LLM).
        if history:
            retrieval_query = build_retrieval_query(question, history)
        try:
            chunks = search_chunks(
                retrieval_query,
                top_k=resolve_top_k(cfg),
                threshold=threshold,
                user=user,
            )
        except Exception as exc:
            logger.exception("Chunk search failed")
            raise RagUnavailable(str(exc)) from exc

    # Confident only when the strict (above-threshold) search found matches.
    confident = bool(chunks)

    if not chunks and user is not None and getattr(user, "is_authenticated", False):
        # Broad / meta questions ("what do you sell?", "who are you?", "tell me
        # about you") often score below the threshold. Fall back to the nearest
        # chunks (no threshold) so the bot can still answer from its knowledge.
        # The grounded prompt keeps it honest: it declines if truly off-topic.
        try:
            chunks = search_chunks(retrieval_query, top_k=resolve_top_k(cfg), threshold=0.0, user=user)
        except Exception:
            chunks = []

    if not chunks:
        # The tenant's knowledge base is empty → identity / greeting handoff
        # (the bot may still introduce itself; it never invents facts).
        if cfg.no_answer_message:
            return AnswerResult(
                answer=cfg.no_answer_message,
                source="rag",
                confident=False,
                answer_status="offtopic",
            )
        try:
            llm = get_backend(llm_model)
            no_data_prompt = build_no_data_prompt(question, cfg) + "\n\n" + _language_directive(language)
            res = llm.complete_with_usage(system_prompt, no_data_prompt)
            return AnswerResult(
                answer=res.text,
                source="rag",
                confident=False,
                answer_status="offtopic",
                model=res.model,
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
            )
        except Exception:
            return AnswerResult(
                answer="I'm sorry, I don't have information about that. Can I help you with something else?",
                source="rag",
                confident=False,
                answer_status="offtopic",
            )

    # Retrieved text is untrusted: it comes from synced APIs, crawled sites and
    # uploaded files, any of which an attacker may control. Fence it behind a
    # per-turn nonce and strip harness control sequences out of it first.
    marker = untrusted.new_fence()
    knowledge = untrusted.fence([hit.content for hit in chunks], marker=marker)
    history_text = _render_history(list(history or []))

    user_prompt = (
        f"{untrusted.directive(marker)}\n"
        f"Conversation so far (context ONLY — to understand what the customer refers to, "
        f"NOT a source of facts):\n{history_text or '(None)'}\n\n"
        f"What you know:\n{knowledge}\n\n"
        f"Customer's message: {question}\n\n"
        f"Reply naturally as part of the team, following your rules. Answer directly — do NOT "
        f"open with a greeting (مرحبا/أهلاً/hello) or re-introduce yourself unless the customer's "
        f"message above is itself a greeting.\n"
        f'CRITICAL GROUNDING: "What you know" above is your ONLY source of truth. You have NO '
        f"outside or general knowledge — not about real people, organisations, or world facts, "
        f"even ones you are sure about. Every specific fact — names, titles, who-came-before, "
        f'dates, numbers, prices — must be written EXPLICITLY in "What you know". If it isn\'t '
        f"there, you do NOT know it: kindly say you don't have that information. Never pull a "
        f"fact from outside this text, and never give one person another person's title. BUT "
        f"reading and comparing facts that ARE in the text is your job, not guessing: for "
        f"'who was before / after X' in a role, if the text lists that role's holders with "
        f"their DATES, ORDER them by date and name the one whose term ended right before X "
        f"began (or started right after) — the dates are in the text, so this is grounded, "
        f"answer it confidently. Only decline when the role's holders or their dates are truly "
        f"not in the text.\n"
        f"WHICH ONE DO THEY MEAN: before answering, check whether the customer named the "
        f'thing they are asking about. If "What you know" gives the asked-for detail (a role '
        f"like the general manager / director / head, a price, an address, a phone number, a "
        f"date) for MORE THAN ONE distinct organisation, product, branch or person, and the "
        f"customer's message does not say which of them they mean — and the conversation above "
        f"does not make it clear either — then do NOT pick one and do NOT answer for the "
        f"closest match. Ask one short question naming the actual candidates you found in "
        f'"What you know" (e.g. "Which one do you mean — X or Y?"), listing at most four and '
        f"using each one's name exactly as the text writes it. Ask nothing else in that reply. "
        f"This applies to the customer's FIRST message just as much as to a follow-up. "
        f"But only ask when the text really does hold the detail for several of them: if just "
        f"one of them has it, answer for that one normally; if the customer already named it, "
        f"answer; and never ask the customer to choose between things the text does not "
        f"actually cover.\n"
        f"ABOUT THE CONVERSATION: use it ONLY to resolve what the customer means (pronouns like "
        f"'him', 'the current one', 'before that'). It is NOT evidence. If an earlier reply in "
        f'the conversation stated something that is not in "What you know", it was a MISTAKE — '
        f"do not repeat or confirm it; correct it and say you don't have that information. If a "
        f"follow-up is ambiguous about which role or person it means, ask a short clarifying "
        f"question instead of guessing, exactly as described above. Do not mention these "
        f"notes or say 'based on the "
        f"information'.\n\n"
        f"{_language_directive(language)}\n\n"
        f"SYSTEM (do NOT show the customer): after your full reply, on a new final line, output "
        f"exactly ONE classification tag:\n"
        f'[[STATUS:answered]] — you answered it and every specific fact came from "What you '
        f'know".\n'
        f"[[STATUS:clarify]] — you did not answer yet because the question did not say which "
        f'organisation/product/person it meant, and "What you know" holds the answer for '
        f"several of them, so your reply is the short question asking which one. (Use this "
        f"ONLY for that; it is not a failure and not a knowledge gap.)\n"
        f"[[STATUS:gap]] — the question is about US / our subject area (the same domain as "
        f'"What you know"), but the specific answer is NOT written there, so you declined. '
        f"(This is a real knowledge gap a colleague should fill.)\n"
        f"[[STATUS:offtopic]] — a greeting, thanks, small talk, or a question unrelated to us "
        f"and our knowledge (general world facts, other organisations, chit-chat).\n"
        f"Choose 'gap' ONLY when the missing answer is something we should plausibly know about "
        f"ourselves. Never mention this tag."
    )

    try:
        llm = get_backend(llm_model)
        res = llm.complete_with_usage(system_prompt, user_prompt)
    except LLMError as exc:
        raise RagUnavailable(str(exc)) from exc

    # The model classifies the turn (answered / gap / offtopic). This is the
    # authoritative signal, overriding the retrieval heuristic in both directions
    # (a below-threshold match that was still answered stays confident; an
    # on-topic-but-unanswered turn becomes a gap). Fall back to retrieval only
    # when the model omitted the tag.
    answer_text, status = _extract_status(res.text)
    if status is not None:
        # "clarify" counts as handled: the bot did the right thing with what it
        # had, so it must not be flagged uncertain nor logged as a knowledge gap.
        confident = status in {"answered", "clarify"}
    # Only an in-domain, unanswered turn ("gap") should be captured for review.
    answer_status = status or ("answered" if confident else "offtopic")

    return AnswerResult(
        answer=answer_text,
        source="rag",
        confident=confident,
        answer_status=answer_status,
        model=res.model,
        prompt_tokens=res.prompt_tokens,
        completion_tokens=res.completion_tokens,
        sources=[
            {
                "filename": hit.filename,
                "document_id": hit.document_id,
                "chunk_id": hit.chunk_id,
                "position": hit.position,
                "score": hit.score,
            }
            for hit in chunks
        ],
        chunk_hits=chunks,
    )


_LANG_NAMES = {
    "en": "English",
    "ar": "Arabic",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "tr": "Turkish",
    "ur": "Urdu",
    "fa": "Persian",
    "ru": "Russian",
    "pt": "Portuguese",
    "it": "Italian",
    "hi": "Hindi",
}


def _language_directive(language: str) -> str:
    """A hard, explicit reply-language instruction based on the detected language.

    Injected as the LAST line of the prompt (recency) so even weaker models obey
    it instead of defaulting to the language of the knowledge base.
    """
    code = (language or "").split("-")[0].lower()
    if code == "ar":
        return (
            "CRITICAL: Write your ENTIRE reply in ARABIC, matching the user's dialect. Do not use any other language."
        )
    name = _LANG_NAMES.get(code)
    if name and name != "Arabic":
        return (
            f"CRITICAL: The customer's message is in {name}. Write your ENTIRE reply in "
            f"{name}. Do NOT reply in Arabic or any other language, even though your "
            f"knowledge/notes may be in a different language."
        )
    return (
        "CRITICAL: Write your ENTIRE reply in the SAME language as the customer's "
        "message above, even if your knowledge is in a different language."
    )


def _resolve_llm_model(user) -> str | None:
    """The LLM model for this user's subscription plan, or None for the default.
    Lazy import so RAG doesn't hard-depend on the subscriptions app."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        from subscriptions.services import resolve_model

        return resolve_model(user)
    except Exception:
        return None


def _render_history(history: list[dict]) -> str:
    out: list[str] = []
    for msg in history:
        role = msg.get("role", "user")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        label = "User" if role == "user" else "Assistant"
        out.append(f"{label}: {content}")
    return "\n".join(out)


def build_retrieval_query(question: str, history) -> str:
    """Enrich a bare follow-up ("مين قبله") with recent conversation context so
    the vector search has something to match — DETERMINISTICALLY.

    We append the last couple of conversation turns (the entities the follow-up
    refers to, e.g. "...اليامي") in FRONT of the original question, keeping the
    user's exact words at the end. No LLM is involved, so it can never invent a
    wrong entity (an earlier LLM-rewrite approach hallucinated names like turning
    "اليامي" into "Mohammed bin Salman", which destroyed retrieval). The original
    question terms are preserved, so a query that already retrieved well is not
    weakened. Returns the plain question when there's no history.
    """
    hist = list(history or [])
    if not hist:
        return question
    # Last two non-empty turns carry the referent (the person/role in play).
    recent = [(m.get("content") or "").strip() for m in hist[-2:]]
    recent = [c for c in recent if c]
    if not recent:
        return question
    context = " ".join(recent)
    return f"{context} {question}".strip()[:1200]
