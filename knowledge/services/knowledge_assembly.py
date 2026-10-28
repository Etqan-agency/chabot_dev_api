"""Reading a source back as one continuous text.

The bot stores knowledge as chunks because that is what retrieval needs: each
one carries its own embedding. That is the wrong unit for a *person* reviewing
what the bot learned -- 1200 characters is an arbitrary place to cut a sentence,
and the reader ends up stitching the document back together in their head.

This module does that stitching: it returns the source's whole text in
``content``, ready to put in a textarea, alongside the sections it was built
from. It does not change how anything is stored, so retrieval, re-sync and
per-chunk editing all keep working exactly as before.

The one real problem is overlap. ``chunking._chunk`` deliberately repeats the
last ``CHUNK_OVERLAP`` characters of each chunk at the start of the next one, so
a passage that straddles a boundary is still wholly present in *some* embedding.
Joined naively, that repeat shows up as the reader seeing the same sentence
twice at every boundary. So each section is returned with a ``lead``: how many
of its leading characters merely restate the previous section. The content is
sent whole -- ``lead`` is a display hint, and editing still works on the real
stored text.

The repeat is not byte-exact, because ``_chunk`` strips each piece, which eats
whitespace at exactly the two edges that would otherwise line up. Matching is
therefore done on a whitespace-normalised copy and then mapped back to a raw
offset.
"""

from __future__ import annotations

import re

from ..models import DocumentChunk

# Chunking repeats at most CHUNK_OVERLAP characters, but a boundary walked back
# to a paragraph break can carry a little more. Allow slack, then stop.
MAX_OVERLAP_SCAN = 600

# Below this, a "match" is likely to be a common phrase that genuinely occurs
# twice rather than the mechanical repeat we are trying to hide.
MIN_OVERLAP = 40

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _lead_length(previous: str, current: str) -> int:
    """How many characters at the start of ``current`` just repeat ``previous``.

    Returns a raw offset into ``current``, or 0 when the two do not overlap --
    which is the normal case for API-synced knowledge, where each chunk is a
    separate record rather than a slice of one long text.
    """
    prev_norm = _normalise(previous)
    cur_norm = _normalise(current)
    limit = min(len(prev_norm), len(cur_norm), MAX_OVERLAP_SCAN)

    matched = 0
    for size in range(limit, MIN_OVERLAP - 1, -1):
        if prev_norm.endswith(cur_norm[:size]):
            matched = size
            break
    if not matched:
        return 0

    # Walk the raw string counting normalised characters (a run of whitespace
    # counts as the single space it collapses to) until `matched` are consumed.
    seen = 0
    in_whitespace = False
    for index, char in enumerate(current):
        if char.isspace():
            if in_whitespace:
                continue
            in_whitespace = True
        else:
            in_whitespace = False
        seen += 1
        if seen >= matched:
            return index + 1
    return len(current)


def _join(sections: list[dict]) -> str:
    """The sections as one continuous text, with each repeat shown only once.

    Sections that do not overlap at all -- one per API record, one per crawled
    page -- get a blank line between them, which is how they were read apart in
    the first place; a plain concatenation would run two records together into
    one unreadable sentence.
    """
    parts: list[str] = []
    for index, section in enumerate(sections):
        body = section["content"][section["lead"] :]
        if index and not section["lead"]:
            parts.append("\n\n")
        parts.append(body)
    return "".join(parts)


def assemble(document) -> dict:
    """Return ``document``'s knowledge as ordered, de-overlapped sections.

    Each section keeps its chunk id, so the page that renders this can send an
    edit or a delete straight back to ``/knowledge/{id}/`` without needing a
    second lookup.
    """
    chunks = list(
        DocumentChunk.objects.filter(document=document)
        .order_by("position")
        .only("id", "position", "content", "source_id", "edited_at", "metadata")
    )

    sections = []
    previous = ""
    for chunk in chunks:
        lead = _lead_length(previous, chunk.content) if previous else 0
        sections.append(
            {
                "id": str(chunk.id),
                "position": chunk.position,
                "content": chunk.content,
                "lead": lead,
                "char_count": len(chunk.content),
                "source_id": chunk.source_id,
                "edited_at": chunk.edited_at,
                "was_edited": chunk.edited_at is not None,
                "manually_added": bool((chunk.metadata or {}).get("manually_added")),
            }
        )
        previous = chunk.content

    content = _join(sections)

    return {
        "document": {
            "id": str(document.id),
            "filename": document.filename,
            "source_type": document.source_type,
            "api_url": document.api_url,
            "is_active": document.is_active,
            "processing_status": document.processing_status,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
        },
        # The whole source as one editable block -- what the page shows.
        "content": content,
        # The pieces it was built from, for per-section auditing and editing.
        "sections": sections,
        "section_count": len(sections),
        "edited_count": sum(1 for s in sections if s["was_edited"]),
        # What a reader would actually read, with the mechanical repeats removed.
        "char_count": len(content),
    }
