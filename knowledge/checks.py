"""Startup checks for the knowledge app.

Currently one: the retrieval backend a deployment *configured* and the backend
it actually *gets* are allowed to differ silently, and that difference is
invisible by construction. `search_candidates` catches
`VectorBackendUnavailable` and serves the query from numpy instead, so a host
with `RAG_VECTOR_BACKEND=pgvector` and no vector column returns correct results
at the wrong cost, for as long as nobody looks.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register

from .services.vector_store import PgVectorStore


@register(Tags.database)
def check_vector_backend(app_configs, databases=None, **kwargs):
    """Warn when pgvector is configured but cannot actually be used."""
    if getattr(settings, "RAG_VECTOR_BACKEND", "numpy") != "pgvector":
        # numpy is a deliberate choice, not a degraded state. Cost nothing.
        return []

    if not databases:
        # `manage.py check` runs without a database unless asked. Probing here
        # would raise rather than report.
        return []

    try:
        reason = PgVectorStore().unavailable_reason()
    except Exception:
        # These checks run as part of `migrate`. An unreachable database must
        # not abort the command this check only annotates -- and a database
        # that cannot be queried is already loud everywhere else.
        return []

    if reason is None:
        return []

    return [
        Warning(
            f"RAG_VECTOR_BACKEND is 'pgvector', but that backend is unusable: {reason}.",
            hint=(
                "Retrieval is falling back to the numpy backend, which scans "
                "candidate rows in Python instead of using an index. Nothing "
                "fails, which is why this does not surface on its own -- it "
                "surfaces as slow search. Run `manage.py setup_pgvector` once the "
                "embeddings are in place (it infers the dimension from them), or "
                "set RAG_VECTOR_BACKEND=numpy to record that the fallback is "
                "intended."
            ),
            id="knowledge.W001",
        )
    ]


@register(Tags.database)
def check_vector_index(app_configs, databases=None, **kwargs):
    """Warn when pgvector works but has no ANN index behind it.

    A distinct failure from W001 and a quieter one. W001 fires when the backend
    cannot run and retrieval silently falls back to numpy. This fires when the
    backend runs exactly as configured and Postgres sequentially scans every
    chunk for every query -- right answers, no error, no log line, and a cost
    that only shows up as the corpus grows.

    It has a specific way of happening. pgvector will not index a `vector`
    column above 2000 dimensions and the configured embedding model is 3072, so
    a `setup_pgvector` run on a pgvector older than 0.7 (no `halfvec`) creates
    the column, backfills it, and builds nothing.
    """
    if getattr(settings, "RAG_VECTOR_BACKEND", "numpy") != "pgvector":
        return []
    if not databases:
        return []

    try:
        store = PgVectorStore()
        # An unusable backend is W001's business; do not report both for one
        # cause. A missing column is not an unindexed table.
        if store.unavailable_reason() is not None:
            return []
        reason = store.unindexed_reason()
    except Exception:
        return []

    if reason is None:
        return []

    return [
        Warning(
            f"RAG_VECTOR_BACKEND is 'pgvector' and usable, but unindexed: {reason}.",
            hint=(
                "Run `manage.py setup_pgvector`, which builds the index that "
                "suits the embedding width -- an HNSW index on the column up to "
                "2000 dimensions, and on its halfvec cast above that. If it "
                "reports it could not build one, the server's pgvector predates "
                "halfvec (0.7.0) and needs upgrading."
            ),
            id="knowledge.W002",
        )
    ]
