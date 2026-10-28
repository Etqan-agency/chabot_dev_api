"""Guided question tree with positional multi-language mirroring.

ONE canonical language (``settings.GUIDED_TREE_CANONICAL_LANGUAGE``) is authored
by hand; every other active language is a generated MIRROR of it, matched by
**position** — the node at the same ``order`` under the matching parent — never
by foreign key. Two walks power everything:

* ``position_path(node)``  — the list of ``order`` values from a root down to the
  node (its address in the tree, language-independent).
* ``node_at_path(owner, language, path)`` — walk that address down a given
  language's tree.

From those: ``canonical_of`` (any node → its canonical twin) and ``mirror_of``
(canonical node → its twin in another language).

All edits target the canonical node; mirrors are kept in line by
``sync_languages`` (translate what is missing or changed -- batched, all
languages at once -- and drop what no longer exists) in a background thread,
so writes never block. Each tenant picks its
own target languages, on top of any global ones. Rendered trees are cached per
language and invalidated on write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

from ..models import AvailableLanguage, QuestionTreeNode, UsageKind

logger = logging.getLogger(__name__)


def canonical_language() -> str:
    return getattr(settings, "GUIDED_TREE_CANONICAL_LANGUAGE", "ar")


def is_canonical(language: str) -> bool:
    return language == canonical_language()


# --------------------------------------------------------------------------- #
# Positional matching
# --------------------------------------------------------------------------- #
def position_path(node: QuestionTreeNode) -> list[int]:
    """The node's address: ``order`` values from the root down to it."""
    path: list[int] = []
    cur = node
    # Follow parents to the root. Bounded by tree depth.
    while cur is not None:
        path.append(cur.order)
        cur = cur.parent
    path.reverse()
    return path


def node_at_path(owner, language: str, path: list[int]) -> QuestionTreeNode | None:
    """Walk ``path`` (a list of orders) down ``language``'s tree for ``owner``."""
    parent = None
    node = None
    for order in path:
        node = QuestionTreeNode.objects.filter(owner=owner, language=language, parent=parent, order=order).first()
        if node is None:
            return None
        parent = node
    return node


def canonical_of(node: QuestionTreeNode) -> QuestionTreeNode | None:
    """Return the canonical-language twin of ``node`` (itself if already canonical)."""
    if is_canonical(node.language):
        return node
    return node_at_path(node.owner, canonical_language(), position_path(node))


def mirror_of(canonical_node: QuestionTreeNode, language: str) -> QuestionTreeNode | None:
    """Return the ``language`` twin of a canonical node (None if not generated yet)."""
    if language == canonical_node.language:
        return canonical_node
    return node_at_path(canonical_node.owner, language, position_path(canonical_node))


# --------------------------------------------------------------------------- #
# Ordering (unique among active siblings, per owner + language)
# --------------------------------------------------------------------------- #
def _sibling_qs(owner, language: str, parent):
    return QuestionTreeNode.objects.filter(owner=owner, language=language, parent=parent)


def next_order(owner, language: str, parent) -> int:
    from django.db.models import Max

    current = _sibling_qs(owner, language, parent).aggregate(m=Max("order")).get("m")
    return (current + 1) if current is not None else 0


def order_is_taken(owner, language: str, parent, order: int, exclude_pk=None) -> bool:
    qs = _sibling_qs(owner, language, parent).filter(order=order)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #
def _strip_quotes(text: str) -> str:
    text = text.strip()
    for q in ('"', "'", "«", "»", "“", "”"):
        if len(text) >= 2 and text[0] == q and text[-1] == q:
            text = text[1:-1].strip()
    return text


def _translation_models() -> tuple[str | None, str | None]:
    """(primary, fallback) models; None = the provider's default (LLM_MODEL)."""
    primary = getattr(settings, "GUIDED_TREE_TRANSLATE_MODEL", "") or None
    fallback = getattr(settings, "GUIDED_TREE_TRANSLATE_FALLBACK_MODEL", "") or None
    return primary, fallback


def _translate(text: str, target_lang: str, source_lang: str | None) -> str | None:
    """One text's translation, or None when every model failed."""
    src = f" from {source_lang}" if source_lang else ""
    system = (
        "You are a professional translator. Translate the user's text"
        f"{src} into the language with code '{target_lang}'. Preserve meaning, "
        "tone, names, numbers, URLs and formatting. Output ONLY the translation — "
        "no quotes, no notes, no preamble."
    )
    from .llm import get_backend

    for model in _translation_models():  # primary, then the fallback model
        try:
            out = _strip_quotes(get_backend(model).complete(system, text) or "")
            if out:
                return out
        except Exception:
            logger.warning("translate_text: model %s failed", model or "default", exc_info=True)
    return None


def translate_text(text: str | None, target_lang: str, *, source_lang: str | None = None) -> str:
    """Translate ``text`` into ``target_lang``. Never raises.

    Empty/blank text is returned as-is. If every model fails we return the
    ORIGINAL text (a readable degraded state beats a crash or an empty node).
    """
    if not text or not text.strip():
        return text or ""
    return _translate(text, target_lang, source_lang) or text


def _parse_json_object(raw: str) -> dict:
    """The JSON object in an LLM reply, tolerating code fences or stray prose."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("the reply is not a JSON object")
    return data


def _translate_chunk(chunk: dict[str, str], target_lang: str, source_lang: str | None) -> dict[str, str]:
    """Translate many short texts in ONE LLM call: ``{key: text}`` → ``{key: translation}``.

    Keys the model drops or garbles are simply missing from the result; the
    caller translates those one by one. Raises when no model answered at all.
    """
    src = f" from {source_lang}" if source_lang else ""
    system = (
        "You are a professional translator. The user sends a JSON object whose values are "
        f"texts{src}. Translate every value into the language with code '{target_lang}'. "
        "Preserve meaning, tone, names, numbers, URLs, markdown and line breaks, and use the "
        "same wording for the same term everywhere. Reply with ONLY a JSON object with exactly "
        "the same keys and the translated values — no code fences, no notes."
    )
    from .llm import get_backend

    payload = json.dumps(chunk, ensure_ascii=False)
    last_error: Exception | None = None
    for model in _translation_models():
        try:
            data = _parse_json_object(get_backend(model).complete(system, payload) or "")
        except Exception as exc:
            last_error = exc
            logger.warning("translate batch: model %s failed", model or "default", exc_info=True)
            continue
        return {
            key: _strip_quotes(value)
            for key, value in data.items()
            if key in chunk and isinstance(value, str) and value.strip()
        }
    raise RuntimeError("every translation model failed") from last_error


def _chunks(items: dict[str, str]) -> list[dict[str, str]]:
    """Split texts into LLM-sized batches (bounded by characters and by count)."""
    max_chars = int(getattr(settings, "GUIDED_TREE_TRANSLATE_BATCH_CHARS", 3000))
    max_items = int(getattr(settings, "GUIDED_TREE_TRANSLATE_BATCH_ITEMS", 40))
    out: list[dict[str, str]] = []
    current: dict[str, str] = {}
    size = 0
    for key, text in items.items():
        if current and (size + len(text) > max_chars or len(current) >= max_items):
            out.append(current)
            current, size = {}, 0
        current[key] = text
        size += len(text)
    if current:
        out.append(current)
    return out


def translate_many(jobs: dict[str, dict[str, str]], source_lang: str | None) -> dict[str, dict[str, str | None]]:
    """Translate ``{language: {key: text}}`` → ``{language: {key: translation or None}}``.

    Texts are batched (one LLM call per batch, not per text) and every batch of
    every language runs concurrently, so a whole tree in several languages takes
    about as long as its slowest batch. A batch that fails, or texts a batch
    left out, fall back to one call per text; None = that text failed too.
    """
    from concurrent.futures import ThreadPoolExecutor

    results: dict[str, dict[str, str | None]] = {lang: {} for lang in jobs}
    work = [(lang, chunk) for lang, items in jobs.items() for chunk in _chunks(items)]
    if not work:
        return results

    from .billing import bind, scope_has_points

    def run(lang: str, chunk: dict[str, str]) -> tuple[str, dict[str, str | None]]:
        if not scope_has_points():
            # Out of points: leave these untranslated (blank hash); the sync
            # after the plan is renewed picks them up.
            return lang, dict.fromkeys(chunk)
        try:
            done: dict[str, str | None] = dict(_translate_chunk(chunk, lang, source_lang))
        except Exception:
            done = {}
        for key, text in chunk.items():
            if key not in done:
                if not scope_has_points():
                    done[key] = None
                    continue
                try:
                    done[key] = _translate(text, lang, source_lang)
                except Exception:
                    logger.exception("translate: text %s -> %s failed", key, lang)
                    done[key] = None
        return lang, done

    workers = max(1, min(len(work), int(getattr(settings, "GUIDED_TREE_TRANSLATE_CONCURRENCY", 6))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for lang, done in pool.map(bind(lambda job: run(*job)), work):  # workers charge the caller
            results[lang].update(done)
    return results


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #
def languages_for(owner):
    """The languages ``owner`` sees: its own plus the global (staff-managed) ones."""
    return AvailableLanguage.objects.filter(Q(owner=owner) | Q(owner__isnull=True))


def active_target_languages(owner) -> list[str]:
    """``owner``'s active languages that are NOT the canonical one (the mirror targets)."""
    codes = languages_for(owner).filter(is_active=True).exclude(code=canonical_language())
    return sorted(set(codes.values_list("code", flat=True)))


# --------------------------------------------------------------------------- #
# Mirror sync
# --------------------------------------------------------------------------- #
def _source_hash(node: QuestionTreeNode) -> str:
    return hashlib.sha256(f"{node.title}\x00{node.answer or ''}".encode()).hexdigest()


@dataclass
class _SyncPlan:
    """What one language needs: read from the DB before any LLM call is made."""

    owner: object
    language: str
    force: bool
    by_parent: dict  # canonical parent id -> canonical children, in order
    existing: list  # every current mirror node of this language
    at: dict  # (mirror parent id, order) -> mirror node
    texts: dict  # "<canonical id>:t|a" -> canonical text that needs translating


def _plan(owner, language: str, force: bool) -> _SyncPlan:
    canon = canonical_language()
    by_parent: dict = {}
    for node in QuestionTreeNode.objects.filter(owner=owner, language=canon).order_by("order", "created_at"):
        by_parent.setdefault(node.parent_id, []).append(node)
    existing = list(QuestionTreeNode.objects.filter(owner=owner, language=language).order_by("created_at"))
    at: dict = {}
    for mirror in existing:
        at.setdefault((mirror.parent_id, mirror.order), mirror)
    texts: dict = {}

    # The same positional walk the apply step does, collecting what to translate.
    def walk(canonical_parent_id, mirror_parent_id, parent_missing) -> None:
        for source in by_parent.get(canonical_parent_id, []):
            mirror = None if parent_missing else at.get((mirror_parent_id, source.order))
            if mirror is None or force or mirror.source_hash != _source_hash(source):
                if source.title.strip():
                    texts[f"{source.id}:t"] = source.title
                if (source.answer or "").strip():
                    texts[f"{source.id}:a"] = source.answer
            walk(source.id, getattr(mirror, "id", None), mirror is None)

    walk(None, None, False)
    return _SyncPlan(owner, language, force, by_parent, existing, at, texts)


def _apply(plan: _SyncPlan, translated: dict) -> None:
    """Write one language's mirrors from ``translated`` (the plan's texts, translated).

    Walks the canonical tree and, at each position, (re)writes what is missing
    or whose canonical text changed (``source_hash``) -- or everything with
    ``force``. Mirrors left at no canonical position (deleted or moved nodes,
    duplicates) are removed at the end. Nothing is wiped first, so the language
    keeps serving its previous tree meanwhile. Failures are isolated per node; a
    node whose translation failed keeps the source text and a blank hash, so
    the next sync retries it.
    """
    owner, language = plan.owner, plan.language
    kept: set = set()

    def text_for(source, field: str, value: str | None) -> tuple[str | None, bool]:
        if not value or not value.strip():
            return value, True
        out = translated.get(f"{source.id}:{field}")
        return (out, True) if out else (value, False)

    def walk(canonical_parent_id, mirror_parent) -> None:
        for source in plan.by_parent.get(canonical_parent_id, []):
            mirror = plan.at.get((getattr(mirror_parent, "id", None), source.order))
            try:
                digest = _source_hash(source)
                if mirror is None or plan.force or mirror.source_hash != digest:
                    title, title_ok = text_for(source, "t", source.title)
                    answer, answer_ok = text_for(source, "a", source.answer)
                    fields = {
                        "title": title,
                        "answer": answer or None,
                        "is_active": source.is_active,
                        "source_hash": digest if title_ok and answer_ok else "",
                    }
                    if mirror is None:
                        mirror = QuestionTreeNode.objects.create(
                            owner=owner, language=language, parent=mirror_parent, order=source.order, **fields
                        )
                    else:
                        for key, value in fields.items():
                            setattr(mirror, key, value)
                        mirror.save(update_fields=[*fields, "updated_at"])
                elif mirror.is_active != source.is_active:
                    mirror.is_active = source.is_active
                    mirror.save(update_fields=["is_active", "updated_at"])
            except Exception:
                logger.exception("sync: node %s -> %s failed", source.pk, language)
            if mirror is not None:
                kept.add(mirror.id)
                walk(source.id, mirror)

    walk(None, None)
    stale = [m.id for m in plan.existing if m.id not in kept]
    if stale:
        QuestionTreeNode.objects.filter(id__in=stale).delete()
    invalidate_cache(owner, [language])


def sync_languages(owner, languages: list[str], *, force: bool = False) -> None:
    """Bring each of ``languages``' trees in line with the canonical tree, in place.

    Three steps so the slow part runs in parallel without touching the DB from
    worker threads: plan every language (DB), translate everything that changed
    in batched, concurrent LLM calls (no DB), then write each language (DB).
    The translation calls are charged to ``owner`` (points); when the points run
    out the remaining nodes keep the source text and are retried on a later sync.
    """
    from .billing import charge_to

    with charge_to(owner, UsageKind.TRANSLATION):
        _sync_languages(owner, languages, force=force)


def _sync_languages(owner, languages: list[str], *, force: bool = False) -> None:
    plans = []
    for language in languages:
        if is_canonical(language):
            continue
        try:
            plans.append(_plan(owner, language, force))
        except Exception:
            logger.exception("sync: planning %s/%s failed", getattr(owner, "pk", owner), language)
    translations = translate_many({p.language: p.texts for p in plans}, canonical_language())
    for plan in plans:
        try:
            _apply(plan, translations.get(plan.language, {}))
        except Exception:
            logger.exception("sync: writing %s/%s failed", getattr(owner, "pk", owner), plan.language)


def sync_language(owner, language: str, *, force: bool = False) -> None:
    """Bring one language's tree in line with the canonical tree (see ``sync_languages``)."""
    sync_languages(owner, [language], force=force)


def resync_language(owner, language: str) -> None:
    """Re-translate ``language``'s entire tree (in place) from the canonical tree."""
    sync_language(owner, language, force=True)


def _sync_keys(owner_id) -> tuple[str, str]:
    return f"guided_tree_sync:{owner_id}:lock", f"guided_tree_sync:{owner_id}:dirty"


def is_syncing(owner) -> bool:
    """True while a background sync is translating ``owner``'s tree."""
    return cache.get(_sync_keys(getattr(owner, "pk", owner))[0]) is not None


def sync_owner(owner_id, languages: list[str] | None = None, force: bool = False) -> None:
    """Sync the target languages of one tenant -- one run at a time per tenant.

    Writes arrive in bursts (a tree upload is one POST per node), so each write
    just marks the tenant dirty; whoever holds the lock keeps syncing until no
    write is pending. Ordering avoids a lost wake-up: mark dirty BEFORE trying
    the lock, and re-check dirty AFTER releasing it.
    """
    from Authentication.models import User

    lock, dirty = _sync_keys(owner_id)
    ttl = int(getattr(settings, "GUIDED_TREE_SYNC_LOCK_TTL", 1800))
    cache.set(dirty, 1, ttl)
    while cache.add(lock, 1, ttl):
        try:
            while cache.get(dirty):
                cache.delete(dirty)
                owner = User.objects.filter(pk=owner_id).first()
                if owner is None:
                    return
                try:
                    sync_languages(owner, languages or active_target_languages(owner), force=force)
                except Exception:
                    logger.exception("sync_owner: %s failed", owner_id)
                languages, force = None, False  # a re-run (more writes came in) is a plain sync
        finally:
            cache.delete(lock)
        if not cache.get(dirty):
            return


# --------------------------------------------------------------------------- #
# Async dispatch
# --------------------------------------------------------------------------- #
def _run_bg(fn, *args) -> None:
    from django.db import connection

    try:
        fn(*args)
    except Exception:
        logger.exception("guided_tree background task failed")
    finally:
        connection.close()


def _dispatch(fn, *args) -> None:
    """Run ``fn`` in a thread unless GUIDED_TREE_TRANSLATE_MODE is 'sync'."""
    mode = getattr(settings, "GUIDED_TREE_TRANSLATE_MODE", "thread")
    if mode == "sync":
        fn(*args)
        return
    threading.Thread(target=_run_bg, args=(fn, *args), daemon=True).start()


def schedule_sync(owner, languages: list[str] | None = None, *, force: bool = False) -> None:
    """Bring ``owner``'s translated trees up to date in the background."""
    _dispatch(sync_owner, getattr(owner, "pk", owner), languages, force)


# --------------------------------------------------------------------------- #
# CRUD (always targets canonical; mirrors follow asynchronously)
# --------------------------------------------------------------------------- #
def create_node(*, owner, parent=None, title, answer=None, order=None, is_active=True) -> QuestionTreeNode:
    """Create a canonical node; it is translated into every language in the background."""
    canon = canonical_language()
    parent_canonical = canonical_of(parent) if parent is not None else None
    if order is None:
        order = next_order(owner, canon, parent_canonical)
    elif order_is_taken(owner, canon, parent_canonical, order):
        raise ValueError("A sibling already uses that order (must be unique among siblings).")
    node = QuestionTreeNode.objects.create(
        owner=owner,
        parent=parent_canonical,
        title=title,
        answer=answer or None,
        order=order,
        is_active=is_active,
        language=canon,
    )
    invalidate_cache(owner, [canon])
    schedule_sync(owner)
    return node


def update_node(node: QuestionTreeNode, *, title=None, answer=None, order=None, is_active=None) -> QuestionTreeNode:
    """Edit a node (resolved to canonical); its translations follow in the background."""
    canonical = canonical_of(node)
    if canonical is None:
        raise ValueError("Could not resolve this node to its canonical version.")
    canon = canonical_language()
    if order is not None and order != canonical.order:
        if order_is_taken(
            owner=canonical.owner, language=canon, parent=canonical.parent, order=order, exclude_pk=canonical.pk
        ):
            raise ValueError("A sibling already uses that order (must be unique among siblings).")
    fields = []
    if title is not None:
        canonical.title = title
        fields.append("title")
    if answer is not None:
        canonical.answer = answer or None
        fields.append("answer")
    if order is not None:
        canonical.order = order
        fields.append("order")
    if is_active is not None:
        canonical.is_active = is_active
        fields.append("is_active")
    if fields:
        canonical.save(update_fields=fields + ["updated_at"])
    invalidate_cache(canonical.owner, [canon])
    # The sync re-translates only what changed; an order change moves the node's
    # address, which it handles as "translate at the new position, drop the old".
    schedule_sync(canonical.owner)
    return canonical


def delete_node(node: QuestionTreeNode) -> None:
    """Delete a node: resolve to canonical → drop every language mirror → drop canonical."""
    canonical = canonical_of(node)
    if canonical is None:
        node.delete()
        return
    owner = canonical.owner
    languages = active_target_languages(owner)
    for language in languages:
        mirror = mirror_of(canonical, language)
        if mirror is not None:
            mirror.delete()  # cascades its own subtree
    canonical.delete()  # cascades canonical subtree
    invalidate_cache(owner, [canonical_language(), *languages])


# --------------------------------------------------------------------------- #
# Cached reads
# --------------------------------------------------------------------------- #
def _cache_key(owner_id, language: str) -> str:
    return f"guided_tree:{owner_id}:{language}"


def invalidate_cache(owner, languages) -> None:
    owner_id = getattr(owner, "pk", owner)
    cache.delete_many([_cache_key(owner_id, lang) for lang in languages])


def _serialize(node: QuestionTreeNode, by_parent: dict) -> dict:
    return {
        "id": str(node.id),
        "title": node.title,
        "answer": node.answer,
        "order": node.order,
        "language": node.language,
        "children": [
            _serialize(child, by_parent)
            for child in sorted(by_parent.get(node.id, []), key=lambda n: (n.order, n.created_at))
        ],
    }


def build_tree(owner, language: str) -> list[dict]:
    """Full nested tree for one language — ONE query, grouped in memory (no N+1)."""
    nodes = list(QuestionTreeNode.objects.filter(owner=owner, language=language, is_active=True))
    by_parent: dict = {}
    for n in nodes:
        by_parent.setdefault(n.parent_id, []).append(n)
    roots = sorted(by_parent.get(None, []), key=lambda n: (n.order, n.created_at))
    return [_serialize(r, by_parent) for r in roots]


def get_tree(owner, language: str) -> list[dict]:
    """Cached nested tree for ``language`` (TTL = GUIDED_TREE_CACHE_TTL)."""
    ttl = int(getattr(settings, "GUIDED_TREE_CACHE_TTL", 300))
    if ttl <= 0:
        return build_tree(owner, language)
    key = _cache_key(owner.pk, language)
    cached = cache.get(key)
    if cached is not None:
        return cached
    tree = build_tree(owner, language)
    cache.set(key, tree, ttl)
    return tree
