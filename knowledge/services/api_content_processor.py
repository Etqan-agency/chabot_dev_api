"""Process API content (products, articles, rows, ...) into RAG-queryable chunks.

The pipeline, in the order the stages actually run:

    fetch -> extract -> archive raw -> structure -> AI analysis -> validate
          -> semantic chunking -> metadata -> embed -> promote

Three properties are worth stating explicitly, because each one is a deliberate
trade rather than an accident of the implementation:

**Incremental.** On re-sync only new or changed source records are re-embedded,
unchanged records are left untouched, and records that disappeared from the
source are removed. Syncing a live database stays cheap in both time and
embedding cost instead of re-embedding everything every time.

**Staged.** Nothing that is already indexed is deleted until the new version has
been structured, validated and embedded successfully. A failure at any stage --
including a validation failure -- leaves the previous, working knowledge base
exactly as it was. That is why ``full_refresh`` no longer wipes chunks up front.

**Lossless, and checked.** Every record is archived verbatim before any
transform runs, every leaf of every record is walked and assigned a role rather
than dropped, and ``api_validation`` re-derives coverage from the raw record
afterwards instead of trusting that the transform behaved.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..models import (
    APISourceRecord,
    DocumentChunk,
    DocumentStatus,
    SourceType,
    UploadedDocument,
)
from . import api_analysis, api_schema, api_validation
from .api_schema import (  # re-exported: imported from here by callers and tests
    ANALYSIS_HEADING,
    PROCESSING_VERSION,
    SCHEMA_VERSION,
)
from .api_schema import with_analysis as _with_analysis
from .chunking import _chunk, _embed_in_batches

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200

__all__ = [
    "ANALYSIS_HEADING",
    "PROCESSING_VERSION",
    "SCHEMA_VERSION",
    "APIContentProcessingError",
    "APIContentRAGProcessor",
    "IngestionBlocked",
    "SyncStats",
    "_with_analysis",  # re-exported: this module has always been its import site
    "extract_all",
    "extract_items",
    "source_statistics",
]


class APIContentProcessingError(Exception):
    """Raised when API content processing fails."""


class IngestionBlocked(APIContentProcessingError):
    """Raised when the new ingestion failed quality control and was not promoted.

    The previous knowledge base is untouched when this is raised -- that is the
    entire point of it being a distinct exception.
    """

    def __init__(self, reason: str, report: dict):
        super().__init__(reason)
        self.reason = reason
        self.report = report


@dataclass
class SyncStats:
    processed: int = 0  # source items with usable content
    items_new: int = 0  # items embedded for the first time
    items_updated: int = 0  # items whose content changed and were re-embedded
    items_unchanged: int = 0  # items skipped (no change -> no embedding cost)
    items_removed: int = 0  # items deleted because they vanished from the source
    chunks_created: int = 0  # chunks newly written this sync
    items_stored: int = 0  # records archived verbatim this sync
    items_analyzed: int = 0  # records the AI pass described this sync
    analysis_errors: int = 0  # records the AI pass could not describe
    analyses_rejected: int = 0  # AI descriptions dropped for stating absent numbers
    items_protected: int = 0  # hand-edited records the sync deliberately kept
    fields_total: int = 0  # source leaf fields seen across all records
    fields_covered: int = 0  # source leaf fields preserved in the structured form
    records_with_issues: int = 0  # records whose validation found something wrong
    errors: int = 0
    validation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "chunks_created": self.chunks_created,
            "items_new": self.items_new,
            "items_updated": self.items_updated,
            "items_unchanged": self.items_unchanged,
            "items_removed": self.items_removed,
            "items_stored": self.items_stored,
            "items_analyzed": self.items_analyzed,
            "analysis_errors": self.analysis_errors,
            "analyses_rejected": self.analyses_rejected,
            "items_protected": self.items_protected,
            "fields_total": self.fields_total,
            "fields_covered": self.fields_covered,
            "field_coverage": round(self.fields_covered / self.fields_total, 6) if self.fields_total else 1.0,
            "records_with_issues": self.records_with_issues,
            "errors": self.errors,
            "schema_version": SCHEMA_VERSION,
            "processing_version": PROCESSING_VERSION,
            "validation": self.validation,
        }


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _Incoming:
    """One source record as it arrived, plus everything derived from it."""

    raw: object
    text: str  # the record rendered as text (may be empty)
    raw_hash: str  # hash of the record itself, independent of the rendering
    page: int = 1
    structured: api_schema.StructuredRecord | None = None


def _default_text(item) -> str:
    """Fallback rendering for a record that is not being structured.

    ``default=str`` matters more than it looks: this is also what derives the
    source id of a record with no natural key, so a single value JSON cannot
    represent (a set, a datetime, a Decimal from a DB-backed source) used to
    raise here and drop the whole record. Stringifying keeps it ingestible, and
    only affects values that could not have been serialised at all.
    """
    return json.dumps(item, ensure_ascii=False, indent=2, default=str)


def _hash_raw(item) -> str:
    """Hash the record itself, so a change is detected however it renders."""
    try:
        return _hash_text(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return _hash_text(repr(item))


def _default_source_id(item: dict, content: str) -> str:
    """Pick a stable id for a source item, falling back to a content hash.

    An API-provided id is always preferred: positional indexes shift whenever
    the source reorders or paginates differently, which would make every record
    look changed and re-embed the entire catalogue.
    """
    if isinstance(item, dict):
        for key in ("id", "pk", "uuid", "slug", "_id"):
            value = item.get(key)
            if value not in (None, ""):
                return str(value)[:255]
    # No natural id -> hash of content. Stable while the content is stable.
    return _hash_text(content)[:255]


def _longest_list_key(payload: dict) -> str | None:
    """Top-level key holding the longest list, if the payload has one at all."""
    best, best_len = None, -1
    for key, value in payload.items():
        if isinstance(value, list) and len(value) > best_len:
            best, best_len = key, len(value)
    return best


def extract_items(payload, items_key: str = "results") -> list:
    """Turn a raw API payload into source records, keeping *everything* it holds.

    A paginated envelope such as ``{"count": 9, "next": ..., "results": [...]}``
    yields one record per item **plus** one record carrying the envelope's other
    fields, so top-level metadata is ingested too instead of being thrown away.

    - list payload -> its elements are the records.
    - dict payload -> the items are ``payload[items_key]`` when that is a
      non-empty list, otherwise the longest top-level list (so a source that
      names its list ``data``/``items``/... works without configuration); every
      remaining field is gathered into one ``{"response_metadata": {...}}``
      record. A dict holding no list at all is itself a single record.
    - anything else (a bare string/number) -> one ``{"value": payload}`` record.

    The metadata record has no natural id, so it is tracked by content hash: a
    sync in which only the metadata changed reports it as one removed + one new
    item rather than as an update.
    """
    if isinstance(payload, list):
        return list(payload)

    if not isinstance(payload, dict):
        return [{"value": payload}]

    explicit = payload.get(items_key)
    key = items_key if isinstance(explicit, list) and explicit else _longest_list_key(payload)

    items = list(payload.get(key) or []) if key is not None else []
    rest = {k: v for k, v in payload.items() if k != key}
    if rest:
        # No list anywhere -> the payload *is* the record, natural id and all.
        items.append({"response_metadata": rest} if key is not None else rest)
    return items


def extract_all(payloads: list, items_key: str = "results") -> tuple[list, dict[str, int]]:
    """Flatten every fetched page into records, remembering which page each came from.

    Each page contributes its items plus its own envelope metadata; the metadata
    record is tagged with its page number so page 2's ``next``/``count`` cannot
    collide with page 1's and overwrite it.
    """
    records: list = []
    pages: dict[str, int] = {}
    multi = len(payloads) > 1
    for index, payload in enumerate(payloads, start=1):
        for item in extract_items(payload, items_key):
            if multi and isinstance(item, dict) and "response_metadata" in item and len(item) == 1:
                item = {"response_metadata": {**item["response_metadata"], "_page": index}}
            records.append(item)
            # Same rendering as process_items' default extract_text_fn, so a
            # record without a natural id hashes to the same source id here.
            pages.setdefault(_default_source_id(item, _default_text(item)), index)
    return records, pages


def source_statistics(items: list, *, endpoint: str = "", pages_fetched: int = 0, truncated: bool = False) -> dict:
    """Describe the fetched payload *before* anything is done to it.

    This is the baseline every later completeness claim is measured against, so
    it is computed from the raw records and nothing else.
    """
    stats = {
        "endpoint": endpoint,
        "fetched_at": timezone.now().isoformat(),
        "pages_fetched": pages_fetched,
        "truncated": truncated,
        "total_records": len(items),
        "total_fields": 0,
        "nested_objects": 0,
        "arrays": 0,
        "null_values": 0,
        "empty_values": 0,
        "unique_ids": 0,
        "response_bytes": 0,
    }
    seen_ids: set[str] = set()
    for item in items:
        try:
            stats["response_bytes"] += len(json.dumps(item, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            stats["response_bytes"] += len(repr(item))
        for _, value in api_schema.iter_leaves(item):
            stats["total_fields"] += 1
            if value is None:
                stats["null_values"] += 1
            elif value == "":
                stats["empty_values"] += 1
            elif value == {}:
                stats["nested_objects"] += 1
                stats["empty_values"] += 1
            elif value == []:
                stats["arrays"] += 1
                stats["empty_values"] += 1
        if isinstance(item, dict):
            for key in ("id", "pk", "uuid", "slug", "_id"):
                value = item.get(key)
                if value not in (None, ""):
                    seen_ids.add(str(value))
                    break
            stats["nested_objects"] += sum(1 for v in item.values() if isinstance(v, dict))
            stats["arrays"] += sum(1 for v in item.values() if isinstance(v, list))
    stats["unique_ids"] = len(seen_ids)
    return stats


def _setting(name: str, default):
    value = getattr(settings, name, default)
    return default if value is None else value


class APIContentRAGProcessor:
    """Convert API content (products, articles, rows, ...) to RAG-queryable chunks."""

    def __init__(
        self,
        document_name: str = "API Content",
        user=None,
        api_url: str = "",
        items_key: str = "results",
        source_type: str = SourceType.API,
    ):
        self.document_name = document_name
        self.user = user
        self.api_url = api_url
        self.items_key = items_key
        self.source_type = source_type
        self._ensure_api_document()

    def _ensure_api_document(self):
        """Create (or fetch) a virtual document that owns the API content's chunks."""
        query_params = {"filename": self.document_name}
        if self.user:
            query_params["uploaded_by"] = self.user

        defaults = {
            "file_size": 0,
            "processing_status": DocumentStatus.COMPLETED,
            "source_type": self.source_type,
            "api_url": self.api_url,
            "items_key": self.items_key,
        }
        if self.user:
            defaults["uploaded_by"] = self.user

        doc, created = UploadedDocument.objects.get_or_create(
            **query_params,
            defaults=defaults,
        )
        self.api_document = doc

        if not created and self.api_url:
            doc.source_type = self.source_type
            doc.api_url = self.api_url
            doc.items_key = self.items_key
            doc.save()

        if created:
            logger.info("Created virtual document: %s", self.document_name)

    # ---------------------------------------------------------------- pipeline
    def process_items(
        self,
        items: list[dict],
        extract_text_fn: Callable[[dict], str] | None = None,
        id_fn: Callable[[dict], str] | None = None,
        *,
        full_refresh: bool = False,
        ai_analysis: bool | None = None,
        pages: dict[str, int] | None = None,
    ) -> dict:
        """Incrementally sync a list of source items into RAG chunks.

        The work runs in separately reasoned phases, deliberately ordered so that
        a failure late in the pipeline can never cost us content:

        1. **Archive** -- every record is written verbatim to ``APISourceRecord``
           before chunking or embedding, so an embedding outage leaves the data
           stored and rebuildable rather than lost.
        2. **Structure** -- each record becomes a role-annotated knowledge object
           (``api_schema``). Deterministic: no field is dropped, none invented.
        3. **Analyse** -- the AI reading of each new/changed record, appended to
           the structured text (never replacing it). Best-effort, and rejected
           outright if it states a number the record does not contain.
        4. **Validate** -- coverage is re-derived from the raw records. If too
           much was lost the sync is abandoned *without touching the index*.
        5. **Embed & promote** -- chunk, embed, then swap in one transaction.

        ``extract_text_fn`` opts out of structuring: a caller that already knows
        how to render its records (the website crawler renders pages to article
        text) keeps full control, and only the generic API path is structured.

        Args:
            items: source records (dicts) from the API/DB.
            extract_text_fn: item -> text. When given, structuring is skipped.
            id_fn: item -> stable id. Defaults to a natural id key or content hash.
            full_refresh: re-chunk and re-embed every record, changed or not.
            ai_analysis: run the AI pass (default: the API_SYNC_AI_ANALYSIS setting).
            pages: optional ``{source_id: page number}`` for paginated sources.

        Returns: stats dict (see ``SyncStats``).

        Raises:
            IngestionBlocked: quality control failed; the index was left as-is.
            APIContentProcessingError: embedding or persistence failed.
        """
        structured_mode = extract_text_fn is None
        render = extract_text_fn or _default_text
        id_fn = id_fn or (lambda item: "")
        pages = pages or {}

        stats = SyncStats()
        report = api_validation.IngestionReport(records_fetched=len(items))
        entity_hint = api_schema.entity_type_from_url(self.api_url) if structured_mode else ""
        include_urls = bool(_setting("API_RAG_INCLUDE_URLS", False))

        try:
            # --- Build the incoming view ------------------------------------
            # Every item is kept, including one whose text rendering is empty:
            # it still gets archived, it simply produces no chunks.
            incoming: dict[str, _Incoming] = {}
            validations: dict[str, api_validation.RecordValidation] = {}

            for item in items:
                try:
                    sid = (id_fn(item) or "").strip() or _default_source_id(item, _default_text(item))
                    structured = None
                    if structured_mode:
                        structured = api_schema.structure_record(item, sid, entity_type_hint=entity_hint)
                        content = api_schema.render_rag_text(structured, include_urls=include_urls)
                        report.records_structured += 1
                    else:
                        content = render(item) or ""
                    incoming[sid] = _Incoming(
                        raw=item,
                        text=content.strip(),
                        raw_hash=_hash_raw(item),
                        page=pages.get(sid, 1),
                        structured=structured,
                    )
                    stats.processed += 1
                except Exception as exc:  # noqa: BLE001 - per-item isolation
                    logger.error("Error reading API item: %s", exc)
                    report.errors.append(f"structure failed: {exc}")
                    stats.errors += 1

            # `full_refresh` resets the *index* (re-chunk and re-embed
            # everything). It deliberately does NOT delete anything yet: the old
            # chunks stay live until the new ones are validated and embedded.
            live_hashes = self._existing_hashes()
            existing_chunk_hash = {} if full_refresh else live_hashes
            stored = self._stored_records()

            incoming_ids = set(incoming)
            # What we already know about, whether it is indexed, archived, or
            # both -- so a record that vanished from the source is dropped from
            # everywhere, not just from whichever table still listed it.
            known_ids = set(live_hashes) | set(stored)
            removed_ids = known_ids - incoming_ids

            # --- Phase 1: archive the raw records (committed on its own) -----
            raw_changed = {
                sid for sid in incoming_ids if stored.get(sid, {}).get("content_hash") != incoming[sid].raw_hash
            }
            stats.items_stored = self._store_raw(incoming, raw_changed, removed_ids)

            # --- Phase 2/3: AI analysis of new/changed (and not-yet-analysed) -
            analyses = self._run_analysis(
                incoming,
                stored,
                raw_changed,
                ai_analysis=ai_analysis,
                stats=stats,
            )

            # --- Phase 4: validate what we are about to index ----------------
            final: dict[str, tuple[str, str]] = {}
            for sid, inc in incoming.items():
                analysis = analyses.get(sid, "")
                if structured_mode and inc.structured is not None:
                    validation = api_validation.validate_record(
                        inc.raw,
                        inc.structured,
                        inc.text,
                        analysis=analysis,
                    )
                    if validation.analysis_rejected:
                        # The description stated a number the record does not
                        # contain. It is dropped rather than embedded: an
                        # invented price is worse than no description at all.
                        logger.warning(
                            "API sync: rejected AI analysis for %s (numbers not in source: %s)",
                            sid,
                            ", ".join(validation.invented_numbers[:5]),
                        )
                        analyses[sid] = ""
                        analysis = ""
                        stats.analyses_rejected += 1
                    validations[sid] = validation
                    report.add(validation)

                if not inc.text:
                    continue
                content = api_schema.with_analysis(inc.text, analysis)
                # The schema version is part of the hash, so changing the
                # rendering re-indexes everything without a manual full refresh.
                final[sid] = (content, _hash_text(f"v{SCHEMA_VERSION}\n{content}"))

            stats.fields_total = report.fields_total
            stats.fields_covered = report.fields_covered
            stats.records_with_issues = report.records_with_issues
            stats.validation = report.as_dict()

            if structured_mode:
                blocking = report.blocking_reason(
                    min_field_coverage=float(_setting("API_SYNC_MIN_FIELD_COVERAGE", 1.0)),
                    min_record_coverage=float(_setting("API_SYNC_MIN_RECORD_COVERAGE", 1.0)),
                )
                if blocking:
                    # Nothing below this point runs: no chunk is deleted, no
                    # embedding is paid for, and the live index is untouched.
                    logger.error("API sync BLOCKED for %s: %s", self.document_name, blocking)
                    raise IngestionBlocked(blocking, report.as_dict())

            changed_ids = {sid for sid in final if existing_chunk_hash.get(sid) != final[sid][1]}

            # A human correction outranks a re-sync of data that did not change.
            # Without this, every sync would quietly revert the fix (the edit
            # altered the chunk hash, so the record looks "changed" forever).
            # When the SOURCE record itself changes, the source wins again.
            if not full_refresh:
                protected = self._hand_edited_ids() - raw_changed
                if protected & changed_ids:
                    stats.items_protected = len(protected & changed_ids)
                    logger.info(
                        "API sync: keeping %d hand-edited record(s) for %s",
                        stats.items_protected,
                        self.document_name,
                    )
                    changed_ids -= protected

            stats.items_unchanged = len(set(final) - changed_ids)
            stats.items_new = len([s for s in changed_ids if s not in existing_chunk_hash])
            stats.items_updated = len([s for s in changed_ids if s in existing_chunk_hash])
            stats.items_removed = len(removed_ids)

            self._store_structured(incoming, validations, changed_ids)

            if not changed_ids and not removed_ids:
                logger.info("API sync: nothing changed for %s", self.document_name)
                return stats.as_dict()

            # --- Phase 5: chunk, embed, then promote -------------------------
            pending = self._build_chunks(incoming, final, changed_ids, structured_mode=structured_mode)

            embeddings: list = []
            model = ""
            if pending:
                try:
                    embeddings, model = _embed_in_batches([piece.content for piece in pending])
                except Exception as exc:  # noqa: BLE001
                    # The records are already archived (phase 1) and the live
                    # index is still intact, so this costs searchability of the
                    # new data until the next sync -- never the content.
                    raise APIContentProcessingError(f"Embedding failed: {exc}") from exc

            stats.chunks_created = self._promote(pending, embeddings, model, changed_ids, removed_ids, full_refresh)

            if stats.chunks_created:
                from .vector_store import backfill_vectors_for_document

                backfill_vectors_for_document(self.api_document.id)

            logger.info("API sync completed for %s: %s", self.document_name, stats.as_dict())
            return stats.as_dict()

        except APIContentProcessingError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error processing API content: %s", exc)
            raise APIContentProcessingError(str(exc)) from exc

    # ------------------------------------------------------------------ stages
    def _run_analysis(self, incoming, stored, raw_changed, *, ai_analysis, stats) -> dict[str, str]:
        """Phase 2: describe new/changed records, reusing descriptions we own."""
        analyses: dict[str, str] = {
            sid: (stored.get(sid, {}).get("analysis") or "") for sid in incoming if sid not in raw_changed
        }
        if ai_analysis is None:
            ai_analysis = api_analysis.is_enabled()
        if not ai_analysis:
            return analyses

        # Retry records left unanalysed by an earlier outage.
        to_analyze = {sid for sid in raw_changed if incoming[sid].text}
        to_analyze |= {sid for sid in incoming if incoming[sid].text and not analyses.get(sid)}

        fresh = {}
        if to_analyze:
            fresh = api_analysis.analyze_records({sid: incoming[sid].raw for sid in to_analyze})
        if fresh:
            self._store_analyses(fresh)
            analyses.update({sid: res.text for sid, res in fresh.items()})
            self._name_unnamed_entities(incoming, fresh)
        stats.items_analyzed = len(fresh)
        stats.analysis_errors = len(to_analyze) - len(fresh)
        return analyses

    def _name_unnamed_entities(self, incoming, fresh) -> None:
        """Let the model name an entity only when nothing deterministic could.

        The endpoint usually says what a record is (``/catalog/products/`` ->
        ``product``), and when it does, that wins: the entity name is part of the
        embedded text, so sourcing it from an LLM would make the indexed text --
        and therefore the content hash -- vary between runs on identical input.
        Only a record that would otherwise be filed under the useless generic
        ``record`` takes the model's suggestion, and the text is re-rendered so
        the header and the metadata cannot disagree.
        """
        include_urls = bool(_setting("API_RAG_INCLUDE_URLS", False))
        for sid, res in fresh.items():
            structured = incoming[sid].structured
            if structured is None or not res.entity_type:
                continue
            if structured.entity_type not in ("", "record"):
                continue
            structured.entity_type = res.entity_type
            incoming[sid].text = api_schema.render_rag_text(structured, include_urls=include_urls).strip()

    def _build_chunks(self, incoming, final, changed_ids, *, structured_mode) -> list["_PendingChunk"]:
        """Semantic chunking plus the metadata that makes a chunk traceable."""
        fetched_at = timezone.now().isoformat()
        pending: list[_PendingChunk] = []

        for sid in sorted(changed_ids):
            content, chash = final[sid]
            inc = incoming[sid]
            structured = inc.structured

            if structured_mode and structured is not None:
                pieces = api_schema.semantic_chunks(
                    content,
                    header=api_schema.identity_header(structured),
                    size=CHUNK_SIZE,
                )
            else:
                pieces = _chunk(content, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)

            for index, piece in enumerate(pieces):
                pending.append(
                    _PendingChunk(
                        source_id=sid,
                        content_hash=chash,
                        content=piece,
                        metadata=self._chunk_metadata(
                            sid,
                            inc,
                            structured,
                            chunk_index=index,
                            chunk_count=len(pieces),
                            content_hash=chash,
                            fetched_at=fetched_at,
                        ),
                    )
                )
        return pending

    def _chunk_metadata(
        self,
        sid: str,
        inc: _Incoming,
        structured,
        *,
        chunk_index: int,
        chunk_count: int,
        content_hash: str,
        fetched_at: str,
    ) -> dict:
        """Everything needed to filter a chunk and to attribute an answer to it.

        Full field values are **not** duplicated here -- they live once, in this
        record's ``APISourceRecord`` (raw plus structured), which every chunk
        points at through ``source_id``. Copying them into each chunk would grow
        the index without making anything more traceable.
        """
        meta = {
            "source": "api",
            "source_id": sid,
            "record_id": sid,
            "tenant_id": str(getattr(self.user, "id", "")) if self.user else "",
            "source_type": self.source_type,
            "document_id": str(self.api_document.id),
            "endpoint": self.api_url,
            "source_url": self.api_url,
            "page": inc.page,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "parent_id": sid,
            "content_hash": content_hash,
            "fetched_at": fetched_at,
            "schema_version": SCHEMA_VERSION,
            "processing_version": PROCESSING_VERSION,
        }
        if structured is not None:
            meta.update(
                {
                    "entity_type": structured.entity_type,
                    "entity_id": str(structured.identifiers.get("id", sid)),
                    "title": structured.title,
                    "field_count": structured.leaf_count,
                    "relationship_count": len(structured.relationships),
                    "attachment_count": len(structured.attachments),
                    "redacted_count": len(structured.redacted),
                }
            )
        return meta

    def _promote(self, pending, embeddings, model, changed_ids, removed_ids, full_refresh) -> int:
        """Swap the new chunks in for the old ones, atomically.

        This is the only place chunks are deleted, and it runs after structuring,
        validation and embedding have all succeeded. If the transaction fails the
        previous index is still there, in full.
        """
        with transaction.atomic():
            if full_refresh:
                DocumentChunk.objects.filter(document=self.api_document).delete()
            else:
                stale_ids = changed_ids | removed_ids
                if stale_ids:
                    DocumentChunk.objects.filter(document=self.api_document, source_id__in=stale_ids).delete()

            next_pos = self._next_position()
            rows = [
                DocumentChunk(
                    document=self.api_document,
                    position=next_pos + index,
                    content=piece.content,
                    embedding=vector,
                    embedding_model=model,
                    source_id=piece.source_id,
                    content_hash=piece.content_hash,
                    metadata=piece.metadata,
                )
                for index, (piece, vector) in enumerate(zip(pending, embeddings, strict=True))
            ]
            if rows:
                DocumentChunk.objects.bulk_create(rows)
            return len(rows)

    # ------------------------------------------------------------------ helpers
    def _existing_hashes(self) -> dict[str, str]:
        """Map each existing source_id to its stored content hash."""
        rows = (
            DocumentChunk.objects.filter(document=self.api_document)
            .exclude(source_id="")
            .values_list("source_id", "content_hash")
        )
        # All chunks of one item share the same hash; last write wins.
        return dict(rows)

    def _next_position(self) -> int:
        current_max = DocumentChunk.objects.filter(document=self.api_document).aggregate(m=Max("position")).get("m")
        return (current_max + 1) if current_max is not None else 0

    def _stored_records(self) -> dict[str, dict]:
        """Map each archived source_id to its raw hash and existing analysis."""
        rows = APISourceRecord.objects.filter(document=self.api_document).values(
            "source_id", "content_hash", "analysis"
        )
        return {row["source_id"]: row for row in rows}

    def _hand_edited_ids(self) -> set[str]:
        """Source ids whose chunks a human corrected through the knowledge API."""
        return set(
            DocumentChunk.objects.filter(document=self.api_document, edited_at__isnull=False)
            .exclude(source_id="")
            .values_list("source_id", flat=True)
        )

    def _store_raw(self, incoming: dict[str, _Incoming], changed: set[str], removed: set[str]) -> int:
        """Archive the raw records verbatim. Committed before embedding runs.

        Returns the number of records written this sync. Nothing here depends on
        the LLM or the embedding provider, so this is the step that guarantees a
        synced record is never lost.
        """
        if not changed and not removed:
            return 0

        rows = [
            APISourceRecord(
                document=self.api_document,
                source_id=sid,
                # Verbatim for any JSON source -- ``json_safe`` is an identity
                # transform on values JSON can already express. It only bites for
                # a non-JSON source (a DB row carrying a Decimal or a datetime),
                # where stringifying the odd value is what makes the record
                # storable at all instead of failing the whole archive write.
                raw=api_schema.json_safe(incoming[sid].raw),
                content_hash=incoming[sid].raw_hash,
                page=incoming[sid].page,
            )
            for sid in changed
        ]
        with transaction.atomic():
            if removed:
                APISourceRecord.objects.filter(document=self.api_document, source_id__in=removed).delete()
            if rows:
                # A changed record replaces its archived version; its stale
                # analysis goes with it (it described the old data).
                APISourceRecord.objects.filter(document=self.api_document, source_id__in=changed).delete()
                APISourceRecord.objects.bulk_create(rows)
        return len(rows)

    def _store_structured(self, incoming, validations, changed_ids) -> None:
        """Persist the structured view and its verdict next to the raw record."""
        if not changed_ids:
            return
        rows = APISourceRecord.objects.filter(document=self.api_document, source_id__in=list(changed_ids))
        updates = []
        for row in rows:
            inc = incoming.get(row.source_id)
            if inc is None or inc.structured is None:
                continue
            row.structured = inc.structured.as_dict()
            row.rag_text = inc.text
            row.entity_type = (inc.structured.entity_type or "")[:64]
            row.title = (inc.structured.title or "")[:255]
            validation = validations.get(row.source_id)
            if validation is not None:
                row.coverage = validation.coverage
                row.validation = validation.as_dict()
            updates.append(row)
        if updates:
            APISourceRecord.objects.bulk_update(
                updates,
                ["structured", "rag_text", "entity_type", "title", "coverage", "validation"],
            )

    def _store_analyses(self, fresh: dict) -> None:
        """Persist the AI reading onto the archived records."""
        now = timezone.now()
        rows = APISourceRecord.objects.filter(document=self.api_document, source_id__in=list(fresh))
        updates = []
        for row in rows:
            res = fresh.get(row.source_id)
            if res is None:
                continue
            row.analysis = res.text
            row.analysis_model = res.model[:64]
            row.analyzed_at = now
            updates.append(row)
        if updates:
            APISourceRecord.objects.bulk_update(updates, ["analysis", "analysis_model", "analyzed_at"])


@dataclass
class _PendingChunk:
    """One chunk waiting to be embedded and promoted."""

    source_id: str
    content_hash: str
    content: str
    metadata: dict
