"""Swappable vector-search backends for RAG retrieval.

Two backends:

* ``numpy``  - default; works on any database (incl. SQLite). Scans candidate
  rows in batches and keeps only the top ``fetch_k`` by cosine similarity, so
  peak memory is bounded by the batch size, not the whole table.
* ``pgvector`` - optional; uses a Postgres ``vector`` column + ANN index so the
  database does the nearest-neighbour search. Activated with
  ``RAG_VECTOR_BACKEND=pgvector`` once the column/index exist (see
  ``docs``/management command). Falls back to numpy if unavailable.

Both return ``Candidate`` objects that carry the chunk's embedding so the
retrieval layer can re-rank (e.g. MMR) without a second DB round-trip.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.db import connection, transaction

from ..models import DocumentChunk

logger = logging.getLogger(__name__)

# pgvector builds HNSW/IVFFlat indexes on a `vector` column only up to 2000
# dimensions. Above that the column still works -- `vector` stores up to 16000 --
# but every search degrades to a sequential scan over the whole chunk table,
# silently, because an unused index and an absent one look identical from the
# outside.
HNSW_VECTOR_DIM_LIMIT = 2000


def halfvec_required(dim: int) -> bool:
    """True when `dim` is too wide to index as `vector` and needs `halfvec`."""
    return dim > HNSW_VECTOR_DIM_LIMIT


def distance_expr(column: str, dim: int) -> tuple[str, str]:
    """Return the `<=>` operands for a query at `dim` dimensions.

    Above the limit both sides are cast to `halfvec(dim)`, which pgvector
    indexes up to 4000. Storage is untouched -- the column stays `vector(dim)`
    at full float32 precision, and only the index and the comparison run at
    16 bits, which is pgvector's documented route for wide embeddings.

    This must stay the single source of truth for that expression. Postgres
    will use an expression index only for an expression that matches the
    indexed one, and a mismatch is not an error: the planner quietly falls back
    to a sequential scan and the query keeps returning correct rows. That is
    exactly the failure this whole change exists to remove, so `setup_pgvector`
    builds its index from this function rather than from its own string.
    """
    if halfvec_required(dim):
        return (f"{column}::halfvec({dim})", f"%s::halfvec({dim})")
    return (column, "%s::vector")


@dataclass
class Candidate:
    chunk_id: str
    score: float
    embedding: np.ndarray


class TenantScopeMissing(RuntimeError):
    """A vector search was attempted without a tenant to scope it to."""


def require_tenant(user):
    """Fail closed when there is no tenant to scope a search to.

    Tenant isolation here used to be ``if user is not None`` -- correct for
    every current caller, and silently catastrophic for the first one that
    forgets the argument, because the fallback is *searching every tenant's
    chunks* and returning them as an answer. A missing scope is a bug, and a
    bug that leaks must stop rather than degrade.

    ``ALLOW_UNSCOPED_VECTOR_SEARCH`` exists for offline tooling that genuinely
    works across tenants (a reindex, a backfill). It is never set for the API.
    """
    if user is not None:
        return user
    from django.conf import settings

    if getattr(settings, "ALLOW_UNSCOPED_VECTOR_SEARCH", False):
        logger.warning("Vector search running UNSCOPED; every tenant's chunks are in range.")
        return None
    raise TenantScopeMissing(
        "Vector search requires a tenant. Refusing to search every tenant's "
        "chunks. Pass user=<the requesting tenant>, or set "
        "ALLOW_UNSCOPED_VECTOR_SEARCH=1 for a deliberate cross-tenant job."
    )


def _base_queryset(*, user=None, document_ids=None):
    qs = DocumentChunk.objects.filter(document__is_active=True).exclude(embedding=None)
    if user is not None:
        qs = qs.filter(document__uploaded_by=user)
    if document_ids:
        qs = qs.filter(document_id__in=document_ids)
    return qs


class NumpyVectorStore:
    """Brute-force cosine search, batched to keep memory bounded."""

    def search(self, query_vec, *, fetch_k, threshold, user=None, document_ids=None) -> list[Candidate]:
        q = np.asarray(query_vec, dtype=np.float32)
        q_norm = float(np.linalg.norm(q)) or 1.0

        batch_size = int(getattr(settings, "RAG_SCAN_BATCH", 2000)) or 2000
        qs = _base_queryset(user=user, document_ids=document_ids).order_by("id")

        kept: list[Candidate] = []
        scanned = 0
        ids: list = []
        embs: list = []

        def flush():
            nonlocal kept
            if not embs:
                return
            try:
                matrix = np.asarray(embs, dtype=np.float32)
            except ValueError:
                # Ragged embeddings (mixed dimensions) — score row by row.
                for cid, emb in zip(ids, embs, strict=True):
                    vec = np.asarray(emb, dtype=np.float32)
                    if vec.shape != q.shape:
                        continue
                    score = float(vec @ q / ((np.linalg.norm(vec) or 1.0) * q_norm))
                    if score >= threshold:
                        kept.append(Candidate(str(cid), score, vec))
            else:
                if matrix.ndim != 2 or matrix.shape[1] != q.shape[0]:
                    return
                norms = np.linalg.norm(matrix, axis=1)
                norms[norms == 0] = 1.0
                scores = (matrix @ q) / (norms * q_norm)
                for i, score in enumerate(scores):
                    if score >= threshold:
                        kept.append(Candidate(str(ids[i]), float(score), matrix[i]))
            # Keep only the global top fetch_k so far.
            kept.sort(key=lambda c: -c.score)
            del kept[fetch_k:]
            ids.clear()
            embs.clear()

        for cid, emb in qs.values_list("id", "embedding").iterator(chunk_size=batch_size):
            if not emb:
                continue
            ids.append(cid)
            embs.append(emb)
            scanned += 1
            if len(ids) >= batch_size:
                flush()
        flush()

        if scanned:
            logger.debug("numpy vector search scanned %d chunks, kept %d", scanned, len(kept))
        return kept[:fetch_k]


class PgVectorStore:
    """Nearest-neighbour search via a Postgres pgvector column + index.

    Expects a column ``embedding_vec vector(N)`` on the chunk table, populated
    from ``embedding`` (see the ``setup_pgvector`` management command). Raises
    ``VectorBackendUnavailable`` if the column/extension is missing so the
    caller can fall back to numpy.
    """

    COLUMN = "embedding_vec"

    def unavailable_reason(self) -> str | None:
        """Why this backend cannot serve a query, or None if it can.

        Deliberately cheap. A system check runs on every management command
        that touches the database, so this must answer from the catalogue
        rather than by attempting a nearest-neighbour search.

        The column is the only thing worth probing: a `vector` column cannot
        exist without the extension that defines the type, so its presence
        implies both.
        """
        if connection.vendor != "postgresql":
            return f"the pgvector backend requires PostgreSQL, but this process is connected to {connection.vendor}"

        table = DocumentChunk._meta.db_table
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
                [table, self.COLUMN],
            )
            if cur.fetchone() is None:
                return (
                    f"the {self.COLUMN} column does not exist on {table} -- "
                    "`manage.py setup_pgvector` creates it, backfills it from the "
                    "JSON embeddings and builds the index"
                )
        return None

    def unindexed_reason(self) -> str | None:
        """Why searches are not using an ANN index, or None if one exists.

        Separate from ``unavailable_reason`` because the two failures are
        different in kind. Without the column the backend cannot run at all and
        retrieval falls back to numpy. With the column but no index it runs
        perfectly and scans the whole table -- correct, unremarkable in the
        logs, and the reason this is worth a check of its own.

        Matches on the access method rather than the index name so an index
        created by hand, or under an older name, still counts.
        """
        if connection.vendor != "postgresql":
            return None
        table = DocumentChunk._meta.db_table
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM pg_class t
                JOIN pg_index ix ON ix.indrelid = t.oid
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_am am ON am.oid = i.relam
                WHERE t.relname = %s AND am.amname IN ('hnsw', 'ivfflat')
                LIMIT 1
                """,
                [table],
            )
            if cur.fetchone() is None:
                return (
                    f"{table} has no HNSW or IVFFlat index, so every search is a sequential scan over the whole table"
                )
        return None

    def search(self, query_vec, *, fetch_k, threshold, user=None, document_ids=None) -> list[Candidate]:
        if connection.vendor != "postgresql":
            raise VectorBackendUnavailable("pgvector backend requires PostgreSQL")

        table = DocumentChunk._meta.db_table
        doc_table = DocumentChunk._meta.get_field("document").related_model._meta.db_table
        vec_literal = "[" + ",".join(str(float(x)) for x in query_vec) + "]"

        where = [f"c.{self.COLUMN} IS NOT NULL", "d.is_active = TRUE"]
        params: list = [vec_literal]
        if user is not None:
            where.append("d.uploaded_by_id = %s")
            params.append(getattr(user, "pk", user))
        if document_ids:
            where.append("c.document_id = ANY(%s)")
            params.append(list(document_ids))

        # Cosine distance operator <=>; similarity = 1 - distance.
        #
        # The operands come from distance_expr so the left-hand side is
        # textually the expression setup_pgvector indexed. Hardcoding
        # `c.embedding_vec <=> %s::vector` here would still return correct
        # results against a halfvec index -- by sequential scan.
        lhs, rhs = distance_expr(f"c.{self.COLUMN}", len(query_vec))
        sql = f"""
            SELECT c.id, {lhs} <=> {rhs} AS distance, c.embedding
            FROM {table} c
            JOIN {doc_table} d ON d.id = c.document_id
            WHERE {" AND ".join(where)}
            ORDER BY distance ASC
            LIMIT %s
        """
        params.append(fetch_k)

        try:
            # Savepoint so a failure (e.g. the embedding_vec column is missing)
            # rolls back cleanly and the numpy fallback can still run — otherwise
            # the aborted transaction poisons later queries on Postgres.
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
        except Exception as exc:  # column/extension missing, etc.
            raise VectorBackendUnavailable(str(exc)) from exc

        out: list[Candidate] = []
        for cid, distance, emb in rows:
            score = 1.0 - float(distance)
            if score < threshold:
                continue
            # The JSON `embedding` column can come back from the raw cursor as a
            # string (depending on psycopg2's json typecasting) rather than a
            # parsed list — decode it before handing it to numpy.
            if isinstance(emb, str):
                emb = json.loads(emb)
            out.append(Candidate(str(cid), score, np.asarray(emb, dtype=np.float32)))
        return out


class VectorBackendUnavailable(RuntimeError):
    pass


def backfill_vectors_for_document(document_id) -> int:
    """Populate ``embedding_vec`` from the JSON ``embedding`` for one document.

    Called right after ingestion so freshly uploaded chunks are immediately
    searchable by the pgvector backend (otherwise ``embedding_vec`` stays NULL
    and the chunks are invisible until ``setup_pgvector`` is re-run).

    No-op unless the pgvector backend is active on PostgreSQL. Safe if the
    column doesn't exist yet — it just logs and skips (numpy fallback still
    serves search in that case).
    """
    if getattr(settings, "RAG_VECTOR_BACKEND", "numpy") != "pgvector":
        return 0
    if connection.vendor != "postgresql":
        return 0

    table = DocumentChunk._meta.db_table
    col = PgVectorStore.COLUMN
    sql = (
        f"UPDATE {table} SET {col} = embedding::text::vector "
        f"WHERE document_id = %s AND {col} IS NULL AND embedding IS NOT NULL"
    )
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(sql, [str(document_id)])
                updated = cur.rowcount
        logger.info("Backfilled embedding_vec for %d chunks of document %s", updated, document_id)
        return updated
    except Exception as exc:  # column/extension missing, dim mismatch, etc.
        logger.warning("embedding_vec backfill skipped for %s: %s", document_id, exc)
        return 0


def refresh_vector_for_chunk(chunk_id) -> bool:
    """Rewrite one chunk's ``embedding_vec`` from its JSON ``embedding``.

    Needed whenever a chunk's text is EDITED: ``backfill_vectors_for_document``
    only fills columns that are still NULL, so on its own it would leave the
    stale vector in place and the chunk would keep matching its old wording.

    No-op unless the pgvector backend is active on PostgreSQL.
    """
    if getattr(settings, "RAG_VECTOR_BACKEND", "numpy") != "pgvector":
        return False
    if connection.vendor != "postgresql":
        return False

    table = DocumentChunk._meta.db_table
    col = PgVectorStore.COLUMN
    sql = f"UPDATE {table} SET {col} = embedding::text::vector WHERE id = %s AND embedding IS NOT NULL"
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(sql, [str(chunk_id)])
                return cur.rowcount > 0
    except Exception as exc:  # column/extension missing, dim mismatch, etc.
        logger.warning("embedding_vec refresh skipped for chunk %s: %s", chunk_id, exc)
        return False


def get_backend():
    name = getattr(settings, "RAG_VECTOR_BACKEND", "numpy")
    if name == "pgvector":
        return PgVectorStore()
    return NumpyVectorStore()


def search_candidates(query_vec, *, fetch_k, threshold, user=None, document_ids=None) -> list[Candidate]:
    """Run the configured backend, falling back to numpy on failure."""
    user = require_tenant(user)
    backend = get_backend()
    try:
        return backend.search(
            query_vec,
            fetch_k=fetch_k,
            threshold=threshold,
            user=user,
            document_ids=document_ids,
        )
    except VectorBackendUnavailable as exc:
        logger.warning("Vector backend unavailable (%s); falling back to numpy", exc)
        return NumpyVectorStore().search(
            query_vec,
            fetch_k=fetch_k,
            threshold=threshold,
            user=user,
            document_ids=document_ids,
        )
