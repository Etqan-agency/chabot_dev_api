"""Deterministic structuring of one API record into a RAG-ready knowledge object.

This module is the *backbone* of API ingestion, and it deliberately contains no
AI at all. Everything here is a pure function of the source record, which buys
three properties the rest of the pipeline depends on:

* **Nothing is lost.** Every leaf of the source JSON is walked and assigned
  exactly one role (see :class:`FieldRole`). A field is never dropped -- a field
  that does not belong in the embedded text is still preserved, verbatim, in the
  structured representation and in the chunk metadata.
* **Nothing is invented.** Every value in the rendered text is copied from the
  source. The AI layer (``api_analysis``) may *add* prose on top, but it can
  never substitute a value here, and ``api_validation`` re-checks that.
* **It is reproducible.** The same record always renders to the same text, so
  content hashes are stable and unchanged records are never re-embedded.

The rendered text is what gets embedded. It is written for a retrieval model,
not for a human reader: short labelled lines, the entity name and identifiers
repeated in every chunk, and no URL/opaque-blob noise diluting the vector.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

# Bumped whenever the rendering changes in a way that should force a re-embed.
# It feeds the chunk content hash, so raising it re-indexes every record on the
# next sync without anyone needing to remember --full-refresh.
SCHEMA_VERSION = 1
PROCESSING_VERSION = "api-structured-v1"


class FieldRole(StrEnum):
    """What a source field is *for*. Every leaf gets exactly one of these."""

    SEARCHABLE = "searchable"  # goes into the embedded text
    RELATIONSHIP = "relationship"  # a link to another entity; text + metadata
    METADATA = "metadata"  # preserved and filterable, kept out of the vector
    SYSTEM = "system"  # ids/slugs/skus: rendered compactly, kept in metadata
    SECRET = "secret"  # redacted before the AI and before the text
    PRESERVED = "preserved"  # unclassifiable; preserved verbatim, still rendered


# ---------------------------------------------------------------------------
# Secrets. These never reach the embedded text, the chunk metadata, the AI
# prompt or the logs -- only the raw archive, which is tenant-scoped storage.
# ---------------------------------------------------------------------------
_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|authorization|"
    r"access[_-]?key|private[_-]?key|credential|client[_-]?secret|"
    r"refresh[_-]?token|session[_-]?id|cookie|signature|salt|otp|pin)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"

_SYSTEM_KEY = re.compile(
    r"^(id|pk|uuid|guid|_id|slug|sku|code|reference|ref|[A-Za-z0-9_]+_id|[A-Za-z0-9_]+_uuid)$",
    re.IGNORECASE,
)
_DATE_KEY = re.compile(
    r"(_at$|_on$|date|time|timestamp|created|updated|modified|published|expires)",
    re.IGNORECASE,
)
_URL_VALUE = re.compile(r"^(https?://|//|data:|s3://)", re.IGNORECASE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")

# Keys whose value names the entity, best first.
_TITLE_KEYS = ("title", "name", "label", "heading", "subject", "question", "headline")
_TITLE_VARIANTS = frozenset(f"{key}{suffix}" for key in _TITLE_KEYS for suffix in ("", "_en", "_ar"))

# Keys that read as free prose -- worth their own block in the rendered text.
_PROSE_KEY = re.compile(
    r"(description|content|body|summary|details|notes|about|bio|text|excerpt)",
    re.IGNORECASE,
)


def is_secret_path(path: str) -> bool:
    """True when any segment of a dotted path names a credential."""
    return any(_SECRET_KEY.search(seg) for seg in re.split(r"[.\[\]]+", path) if seg)


# ---------------------------------------------------------------------------
# Walking the record
# ---------------------------------------------------------------------------
def iter_leaves(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(dotted_path, value)`` for every leaf of ``obj``.

    An empty dict/list is itself a leaf: its emptiness is information (a variant
    list with nothing in it, a product with no images) and the completeness
    check would otherwise never notice it disappearing.
    """
    if isinstance(obj, dict):
        if not obj:
            yield path, {}
            return
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield from iter_leaves(value, child)
    elif isinstance(obj, list):
        if not obj:
            yield path, []
            return
        for index, value in enumerate(obj):
            yield from iter_leaves(value, f"{path}[{index}]")
    else:
        yield path, obj


def leaf_paths(obj: Any) -> set[str]:
    """Every leaf path in ``obj`` -- the completeness check's ground truth."""
    return {path for path, _ in iter_leaves(obj)}


def sanitize(obj: Any) -> tuple[Any, list[str]]:
    """Return a deep copy with credential-bearing leaves replaced by ``REDACTED``.

    Returns ``(clean, redacted_paths)``. Applied before the AI prompt and before
    anything is rendered, so a secret in the source can never be embedded,
    logged, or read back to a customer.
    """
    redacted: list[str] = []

    def walk(node: Any, node_path: str) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                child = f"{node_path}.{key}" if node_path else str(key)
                if _SECRET_KEY.search(str(key)):
                    redacted.append(child)
                    out[key] = REDACTED
                else:
                    out[key] = walk(value, child)
            return out
        if isinstance(node, list):
            return [walk(value, f"{node_path}[{index}]") for index, value in enumerate(node)]
        return node

    return walk(obj, ""), redacted


# ---------------------------------------------------------------------------
# Normalization -- shape only, never meaning
# ---------------------------------------------------------------------------
def normalize_scalar(value: Any) -> Any:
    """Tidy a scalar's *formatting*. Its factual content is never changed.

    Whitespace is collapsed and ISO-ish timestamps get one consistent spelling.
    Numbers keep their exact source spelling (``"140000.00"`` stays
    ``"140000.00"``): re-formatting a price is exactly the kind of "harmless"
    tidy-up that turns into a wrong answer to a customer.
    """
    if isinstance(value, str):
        text = re.sub("[ \t\u00a0]+", " ", value).strip()
        if _ISO_DATE.match(text):
            return normalize_timestamp(text)
        return text
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def normalize_timestamp(text: str) -> str:
    """One internal spelling for a timestamp; unparsable input is kept verbatim."""
    raw = text.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.isoformat()


def humanize(path: str) -> str:
    """``brand.name_ar`` -> ``brand name ar``. A label, not a translation."""
    spaced = re.sub(r"\[(\d+)\]", r" \1", path).replace(".", " ")
    return re.sub(r"[_\-]+", " ", spaced).strip()


def render_value(value: Any) -> str:
    """One-line display of a leaf value, keeping empties as explicit facts."""
    if value is None:
        return "(null)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value == "":
        return "(empty)"
    if value == {} or value == []:
        return "(none)"
    return str(value)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(path: str, value: Any) -> FieldRole:
    """Assign one role to one leaf. Deterministic, and never returns "discard"."""
    if is_secret_path(path):
        return FieldRole.SECRET

    segments = [seg for seg in re.split(r"[.\[\]]+", path) if seg and not seg.isdigit()]
    leaf = segments[-1] if segments else path
    nested = len(segments) > 1

    if isinstance(value, str) and _URL_VALUE.match(value.strip()):
        return FieldRole.METADATA
    if _DATE_KEY.search(leaf) or (isinstance(value, str) and _ISO_DATE.match(value.strip())):
        return FieldRole.METADATA
    if _SYSTEM_KEY.match(leaf):
        # A nested object's id is what makes its link resolvable, so it belongs
        # to the relationship rather than to this record's own identifiers.
        return FieldRole.RELATIONSHIP if nested else FieldRole.SYSTEM
    if nested:
        return FieldRole.RELATIONSHIP
    if isinstance(value, str | int | float | bool) or value is None or value in ({}, []):
        return FieldRole.SEARCHABLE
    return FieldRole.PRESERVED


# ---------------------------------------------------------------------------
# The structured knowledge object
# ---------------------------------------------------------------------------
@dataclass
class StructuredRecord:
    """One source record, structured and ready to render, embed and trace."""

    source_id: str
    entity_type: str = "record"
    title: str = ""
    fields: dict[str, Any] = field(default_factory=dict)  # path -> normalized value
    roles: dict[str, str] = field(default_factory=dict)  # path -> FieldRole value
    relationships: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    identifiers: dict[str, Any] = field(default_factory=dict)
    redacted: list[str] = field(default_factory=list)
    leaf_count: int = 0

    def paths_with_role(self, *roles: FieldRole) -> list[str]:
        wanted = {role.value for role in roles}
        return [path for path, role in self.roles.items() if role in wanted]

    def as_dict(self) -> dict:
        """A JSON-storable view of this record.

        Values a JSON column cannot hold (a set, a Decimal, a datetime that
        slipped through a non-JSON source) are stringified rather than allowed
        to fail the write -- losing the exact type of an odd field is a far
        smaller cost than losing the record's structured form entirely, and the
        raw archive still holds the original.
        """
        return {
            "source_id": self.source_id,
            "entity_type": self.entity_type,
            "title": self.title,
            "identifiers": json_safe(self.identifiers),
            "fields": json_safe(self.fields),
            "roles": self.roles,
            "relationships": json_safe(self.relationships),
            "attachments": json_safe(self.attachments),
            "redacted": self.redacted,
            "leaf_count": self.leaf_count,
            "schema_version": SCHEMA_VERSION,
            "processing_version": PROCESSING_VERSION,
        }


def json_safe(value: Any) -> Any:
    """Recursively coerce a value into something a JSON column accepts."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _guess_entity_type(record: Any, hint: str = "") -> str:
    """Name what this record *is*, preferring the caller's hint.

    ``type`` is deliberately not consulted: on real catalogues it usually holds
    a delivery/format flag (``"virtual"``, ``"physical"``) rather than the kind
    of thing the record is, and using it produced headers like ``VIRTUAL:``. The
    endpoint-derived hint (``/catalog/products/`` -> ``product``) is far more
    reliable, and the AI layer can override it with a better one.
    """
    if hint:
        return hint
    if isinstance(record, dict):
        if "response_metadata" in record and len(record) == 1:
            return "response_metadata"
        for key in ("entity_type", "object", "resource", "kind"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()[:64]
    return "record"


def entity_type_from_url(url: str) -> str:
    """Derive a singular entity name from an endpoint path.

    ``https://api.example.com/api/v1/catalog/products/?limit=100`` -> ``product``.
    Purely lexical and best-effort: a bad guess costs a slightly worse header,
    never a wrong fact.
    """
    if not url:
        return ""
    path = re.sub(r"[?#].*$", "", str(url))
    segments = [seg for seg in path.split("/") if seg and not re.fullmatch(r"v\d+|api|\d+", seg, re.IGNORECASE)]
    if not segments:
        return ""
    last = re.sub(r"[^A-Za-z0-9_-]+", "", segments[-1]).lower()
    if not last or "." in last:
        return ""
    if last.endswith("ies"):
        return f"{last[:-3]}y"
    if last.endswith("ses") or last.endswith("xes") or last.endswith("ches"):
        return last[:-2]
    if last.endswith("s") and not last.endswith("ss"):
        return last[:-1]
    return last


def _pick_title(record: Any) -> str:
    """The record's own name, taken verbatim from a top-level title-ish field."""
    if not isinstance(record, dict):
        return ""
    for key in _TITLE_KEYS:
        for candidate in (key, f"{key}_en", f"{key}_ar"):
            value = record.get(candidate)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def structure_record(record: Any, source_id: str, *, entity_type_hint: str = "") -> StructuredRecord:
    """Turn one raw source record into a :class:`StructuredRecord`.

    Every leaf is walked, normalized and classified; none is discarded. The
    result is a complete, role-annotated view that the renderer, the validator
    and the metadata builder all read from.
    """
    clean, redacted = sanitize(record)

    out = StructuredRecord(
        source_id=source_id,
        entity_type=_guess_entity_type(clean, entity_type_hint),
        title=_pick_title(clean),
        redacted=redacted,
    )

    for path, value in iter_leaves(clean):
        role = classify(path, value)
        normalized = normalize_scalar(value)
        out.fields[path] = normalized
        out.roles[path] = role.value
        out.leaf_count += 1
        if role is FieldRole.SYSTEM and normalized not in (None, "", {}, []):
            out.identifiers[path] = normalized

    out.relationships, out.attachments = _build_relationships(out)
    return out


def _build_relationships(rec: StructuredRecord) -> tuple[list[dict], list[dict]]:
    """Group RELATIONSHIP leaves by parent object into explicit links.

    ``brand.id`` / ``brand.name`` become one link: *this record* -> ``brand`` ->
    *Microsoft*. That is the spec's "Product A -> belongs to -> Electronics"
    edge, derived from the source's own nesting rather than guessed.

    Returns ``(links, attachments)``. An *attachment* is a nested object that
    carries a URL and no name of its own -- an image or file entry. Rendering
    those as edges produced four near-identical lines of empty ``alt_text`` per
    product, which is the same vector noise this pipeline exists to remove, so
    they are counted in the text and preserved in full in the metadata instead.
    """
    url_parents = {
        path.rpartition(".")[0] for path in rec.paths_with_role(FieldRole.METADATA) if _is_url(rec.fields[path])
    }

    groups: dict[str, dict[str, Any]] = {}
    for path in rec.paths_with_role(FieldRole.RELATIONSHIP):
        parent, _, leaf = path.rpartition(".")
        if not parent:
            continue
        groups.setdefault(parent, {})[leaf] = rec.fields[path]

    links: list[dict] = []
    attachments: list[dict] = []
    for parent, attributes in sorted(groups.items()):
        label = ""
        for key in _TITLE_KEYS:
            for candidate in (key, f"{key}_en", f"{key}_ar"):
                value = attributes.get(candidate)
                if isinstance(value, str) and value.strip():
                    label = value.strip()
                    break
            if label:
                break
        related_id = attributes.get("id") or attributes.get("pk") or attributes.get("uuid")
        entry = {
            "path": parent,
            "type": re.sub(r"\[\d+\]", "", parent).split(".")[-1],
            "label": label,
            "related_id": None if related_id is None else str(related_id),
            "attributes": attributes,
        }
        if not label and parent in url_parents:
            attachments.append(entry)
        else:
            links.append(entry)
    return links, attachments


# ---------------------------------------------------------------------------
# Rendering -- the text that actually gets embedded
# ---------------------------------------------------------------------------
ANALYSIS_HEADING = "--- AI analysis ---"


def render_rag_text(rec: StructuredRecord, *, include_urls: bool = False) -> str:
    """Render a structured record as retrieval-optimized text.

    The layout is deliberate: entity type and name first (so the vector is
    dominated by what the record *is*), then identifiers and attributes as short
    ``Label: value`` lines, then relationships spelled out as edges. URL noise is
    summarised rather than inlined -- the full values live in the chunk metadata
    and the raw archive, so nothing is lost, but they stop diluting the vector.
    """
    lines: list[str] = []
    header = rec.entity_type.replace("_", " ").upper()
    lines.append(f"{header}: {rec.title}" if rec.title else header)

    # Every title variant, so an Arabic query matches an Arabic title.
    aliases = [
        rec.fields[path].strip()
        for path in sorted(rec.fields)
        if "." not in path
        and path.split("[")[0] in _TITLE_VARIANTS
        and isinstance(rec.fields[path], str)
        and rec.fields[path].strip()
        and rec.fields[path].strip() != rec.title
    ]
    for alias in dict.fromkeys(aliases):
        lines.append(f"Also known as: {alias}")

    if rec.identifiers:
        rendered = " | ".join(f"{humanize(p)}={render_value(v)}" for p, v in sorted(rec.identifiers.items()))
        lines.append(f"Identifiers: {rendered}")

    prose: list[tuple[str, str]] = []
    attributes: list[str] = []
    for path in sorted(rec.paths_with_role(FieldRole.SEARCHABLE, FieldRole.PRESERVED)):
        value = rec.fields[path]
        if _PROSE_KEY.search(path) and isinstance(value, str) and len(value) > 80:
            prose.append((humanize(path), value))
        else:
            attributes.append(f"- {humanize(path)}: {render_value(value)}")

    if attributes:
        lines.extend(["", "Attributes:", *attributes])

    if rec.relationships:
        lines.extend(["", "Related:"])
        subject = rec.title or rec.source_id
        for link in rec.relationships:
            label = link["label"] or render_value(link["related_id"])
            extra = ", ".join(
                f"{key}={render_value(value)}"
                for key, value in sorted(link["attributes"].items())
                if key not in _TITLE_VARIANTS
            )
            edge = f"- {subject} -> {link['type']} -> {label}"
            lines.append(f"{edge} ({extra})" if extra else edge)

    if rec.attachments:
        by_type: dict[str, int] = {}
        for item in rec.attachments:
            by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in sorted(by_type.items()))
        lines.extend(["", f"Attachments: {summary} (full details in this record's metadata)."])

    metadata_paths = rec.paths_with_role(FieldRole.METADATA)
    dates = {path: rec.fields[path] for path in metadata_paths if not _is_url(rec.fields[path])}
    if dates:
        rendered = " | ".join(f"{humanize(p)}={render_value(v)}" for p, v in sorted(dates.items()))
        lines.extend(["", f"Dates: {rendered}"])

    urls = {path: rec.fields[path] for path in metadata_paths if _is_url(rec.fields[path])}
    if urls:
        lines.append("")
        if include_urls:
            lines.append("Links:")
            lines.extend(f"- {humanize(p)}: {v}" for p, v in sorted(urls.items()))
        else:
            # Kept out of the vector on purpose: on a media-heavy catalogue these
            # are ~30% of the text and carry no retrievable meaning. They stay in
            # the chunk metadata and the raw archive, so nothing is lost.
            lines.append(f"Links: {len(urls)} URL(s) recorded in this record's metadata.")

    if rec.redacted:
        lines.extend(["", f"Redacted: {len(rec.redacted)} credential field(s) withheld."])

    for label, text in prose:
        lines.extend(["", f"{label}:", text])

    return "\n".join(lines).strip()


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and bool(_URL_VALUE.match(value.strip()))


def with_analysis(text: str, analysis: str) -> str:
    """Append the AI reading below the structured facts, never in place of them."""
    analysis = (analysis or "").strip()
    if not analysis:
        return text
    return f"{text}\n\n{ANALYSIS_HEADING}\n{analysis}"


def identity_header(rec: StructuredRecord) -> str:
    """The one or two lines that say which entity a chunk belongs to."""
    header = rec.entity_type.replace("_", " ").upper()
    lines = [f"{header}: {rec.title}" if rec.title else f"{header} {rec.source_id}"]
    if rec.identifiers:
        rendered = " | ".join(f"{humanize(p)}={render_value(v)}" for p, v in sorted(rec.identifiers.items()))
        lines.append(f"Identifiers: {rendered}")
    return "\n".join(lines)


def semantic_chunks(text: str, *, header: str = "", size: int = 1200) -> list[str]:
    """Split rendered text on section boundaries, never mid-fact.

    A record that fits returns as a single chunk -- which is the common case and
    the point: a product retrieved as one unit answers a question, whereas a
    product split at character 1200 yields one chunk of prices with no name and
    one chunk of name with no prices.

    When a record genuinely is too large, it is divided at blank-line section
    boundaries (``Attributes:``, ``Related:``, a description block), and every
    chunk after the first is prefixed with ``header`` so it still says which
    entity it describes when retrieved on its own.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    header = (header or "").strip()
    body = text
    if header and body.startswith(header):
        # The identity lines are about to be prepended to every chunk; leaving
        # them at the top of the body as well would duplicate them in the first.
        body = body[len(header) :].lstrip("\n")

    blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
    prefix = f"{header}\n\n" if header else ""
    # Every chunk carries the prefix, including the first: a chunk that does not
    # say which entity it describes is worse than useless when retrieved alone,
    # so the identity is charged against the budget rather than assumed.
    budget = max(size - len(prefix), size // 2)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        chunks.append(prefix + "\n\n".join(current))
        current, current_len = [], 0

    for block in blocks:
        if len(block) > budget:
            # A single oversized block (a long description) still has to be cut;
            # do it on line boundaries so no labelled line is torn in half.
            flush()
            chunks.extend(f"{prefix}{piece}" for piece in _split_long_block(block, budget))
            continue
        if current and current_len + len(block) + 2 > budget:
            flush()
        current.append(block)
        current_len += len(block) + 2

    flush()
    return chunks


def _split_long_block(block: str, budget: int) -> list[str]:
    """Cut one oversized block on line boundaries, falling back to hard slices."""
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in block.split("\n"):
        if len(line) > budget:
            if current:
                pieces.append("\n".join(current))
                current, current_len = [], 0
            pieces.extend(line[i : i + budget] for i in range(0, len(line), budget))
            continue
        if current and current_len + len(line) + 1 > budget:
            pieces.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return pieces
