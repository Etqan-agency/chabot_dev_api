"""Search document chunks for RAG retrieval.

Pulls a candidate pool from the configured vector backend (numpy or pgvector),
then re-ranks with Maximal Marginal Relevance (MMR) for relevance + diversity
before returning the final ``top_k`` chunks.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from django.conf import settings

from ..models import DocumentChunk
from .embeddings import embed_one
from .vector_store import Candidate, search_candidates


@dataclass
class ChunkHit:
    chunk_id: str
    document_id: str
    filename: str
    content: str
    position: int
    score: float


def search_chunks(
    query: str,
    *,
    top_k: int | None = None,
    fetch_k: int | None = None,
    threshold: float | None = None,
    user=None,
    document_ids=None,
    use_mmr: bool | None = None,
) -> list[ChunkHit]:
    """Return the top document chunks for ``query``.

    Args:
        top_k: final chunks returned (default ``RAG_TOP_K``).
        fetch_k: candidate pool size before re-ranking (default ``RAG_FETCH_K``).
        threshold: minimum cosine similarity (default ``RAG_SIMILARITY_THRESHOLD``).
        user: restrict to this user's documents (multi-tenancy).
        document_ids: restrict to specific documents.
        use_mmr: diversify with MMR (default ``RAG_USE_MMR``).
    """
    top_k = top_k if top_k is not None else int(getattr(settings, "RAG_TOP_K", 6))
    fetch_k = fetch_k if fetch_k is not None else int(getattr(settings, "RAG_FETCH_K", 40))
    fetch_k = max(fetch_k, top_k)
    threshold = threshold if threshold is not None else float(getattr(settings, "RAG_SIMILARITY_THRESHOLD", 0.5))
    use_mmr = use_mmr if use_mmr is not None else bool(getattr(settings, "RAG_USE_MMR", True))

    query_vec, _ = embed_one(query)

    candidates = search_candidates(
        query_vec,
        fetch_k=fetch_k,
        threshold=threshold,
        user=user,
        document_ids=document_ids,
    )
    if not candidates:
        return []

    if use_mmr and len(candidates) > top_k:
        selected = _mmr(
            np.asarray(query_vec, dtype=np.float32),
            candidates,
            top_k=top_k,
            lambda_=float(getattr(settings, "RAG_MMR_LAMBDA", 0.6)),
        )
    else:
        selected = sorted(candidates, key=lambda c: -c.score)[:top_k]

    return _hydrate(selected)


def _mmr(query_vec: np.ndarray, candidates: list[Candidate], *, top_k: int, lambda_: float) -> list[Candidate]:
    """Maximal Marginal Relevance: balance relevance to the query against
    redundancy with already-selected chunks."""
    # Pre-normalise embeddings for cosine via dot product.
    mat = np.asarray([c.embedding for c in candidates], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    relevance = np.asarray([c.score for c in candidates], dtype=np.float32)

    selected_idx: list[int] = []
    remaining = set(range(len(candidates)))

    while remaining and len(selected_idx) < top_k:
        if not selected_idx:
            best = max(remaining, key=lambda i: relevance[i])
        else:
            sel = mat[selected_idx]  # (s, d)
            best, best_score = None, -np.inf
            for i in remaining:
                redundancy = float(np.max(sel @ mat[i]))
                mmr_score = lambda_ * float(relevance[i]) - (1.0 - lambda_) * redundancy
                if mmr_score > best_score:
                    best, best_score = i, mmr_score
        selected_idx.append(best)
        remaining.discard(best)

    return [candidates[i] for i in selected_idx]


def _hydrate(candidates: list[Candidate]) -> list[ChunkHit]:
    """Fetch chunk rows for the selected candidates, preserving order."""
    by_id = {c.chunk_id: c for c in candidates}
    rows = DocumentChunk.objects.select_related("document").filter(id__in=list(by_id))
    row_map = {str(r.id): r for r in rows}

    hits: list[ChunkHit] = []
    for cid, cand in by_id.items():
        chunk = row_map.get(cid)
        if chunk is None:
            continue
        hits.append(
            ChunkHit(
                chunk_id=str(chunk.id),
                document_id=str(chunk.document_id),
                filename=chunk.document.filename,
                content=chunk.content,
                position=chunk.position,
                score=cand.score,
            )
        )
    return hits
