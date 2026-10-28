"""Enable the pgvector backend on PostgreSQL.

Creates the ``vector`` extension, adds an ``embedding_vec`` column to the chunk
table, backfills it from the existing JSON ``embedding`` values, and builds an
HNSW cosine index. After running this, set ``RAG_VECTOR_BACKEND=pgvector``.

Above 2000 dimensions the index is built on the ``halfvec`` cast of the column
rather than on the column itself, because pgvector will not index a ``vector``
wider than that. See ``knowledge.services.vector_store.distance_expr``.

No-op-safe: refuses to run on non-PostgreSQL databases (e.g. SQLite) with a
clear message, so it never corrupts the default setup.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from knowledge.models import DocumentChunk
from knowledge.services.vector_store import (
    HNSW_VECTOR_DIM_LIMIT,
    distance_expr,
    halfvec_required,
)

# `halfvec` arrived in pgvector 0.7.0. Below that a wide embedding cannot be
# indexed at all.
HALFVEC_MIN_VERSION = (0, 7)

# Re-exported under its original name: callers and docs refer to the limit by
# name rather than by the number 2000.
INDEXABLE_DIM_LIMIT = HNSW_VECTOR_DIM_LIMIT


def index_sql(table: str, column: str, dim: int) -> str:
    """DDL for the HNSW cosine index appropriate to `dim`.

    The indexed expression comes from ``distance_expr`` -- the same function
    the query path uses to build its left-hand side. Postgres uses an
    expression index only when the query's expression matches the indexed one,
    and a mismatch is silent: the planner falls back to a sequential scan and
    the query still returns the right rows, just slowly and indefinitely.
    Deriving both from one function makes that drift impossible rather than
    merely unlikely.
    """
    expr, _ = distance_expr(column, dim)
    opclass = "halfvec_cosine_ops" if halfvec_required(dim) else "vector_cosine_ops"
    return f"CREATE INDEX IF NOT EXISTS {table}_{column}_hnsw ON {table} USING hnsw (({expr}) {opclass});"


def summary(dim, index_kind):
    """Return (message, ok) describing what the run actually built.

    `index_kind` is "vector", "halfvec", or None for no index at all.

    The command used to print a green "pgvector is ready" in every case,
    including the one where it had just skipped the index because the dimension
    exceeded the limit -- contradicting, one line later, the warning it had
    itself emitted. DEPLOY.md sends an operator here as a step to tick off
    after the data restore, so that line decides whether the deployment record
    says "pgvector set up" for a setup that scans every chunk on every search.
    """
    if index_kind == "halfvec":
        return (
            f"\npgvector is ready, with an HNSW cosine index over halfvec({dim}).\n"
            f"Dimension {dim} exceeds pgvector's {HNSW_VECTOR_DIM_LIMIT}-dim limit for\n"
            "indexing a `vector` column, so the index is built on the column's halfvec\n"
            "cast: stored values keep full float32 precision, while index search runs at\n"
            "16-bit precision. The recall cost is small, but it is not zero.\n"
            "Now set RAG_VECTOR_BACKEND=pgvector to use it.\n"
            "Re-run this command after large ingests to backfill new rows.",
            True,
        )
    if index_kind == "vector":
        return (
            f"\npgvector is ready, with an HNSW cosine index over vector({dim}).\n"
            "Now set RAG_VECTOR_BACKEND=pgvector to use it.\n"
            "Re-run this command after large ingests to backfill new rows.",
            True,
        )
    return (
        f"\npgvector is ENABLED BUT NOT INDEXED. Dimension {dim} exceeds pgvector's\n"
        f"{HNSW_VECTOR_DIM_LIMIT}-dim limit for indexing a `vector` column, and the halfvec\n"
        f"fallback needs pgvector {HALFVEC_MIN_VERSION[0]}.{HALFVEC_MIN_VERSION[1]} "
        "or newer, which this server does not have.\n"
        "Every search is therefore a sequential scan over the whole chunk table:\n"
        "correct results, fine at a small corpus, and not what this command exists\n"
        "to produce.\n"
        f"Upgrade pgvector, or switch EMBEDDING_MODEL to a <={HNSW_VECTOR_DIM_LIMIT}-dim "
        "model\n(e.g. text-embedding-3-small, 1536 dims) and re-embed.\n"
        "Re-run this command after large ingests to backfill new rows.",
        False,
    )


class Command(BaseCommand):
    help = "Set up the pgvector column + HNSW index and backfill from JSON embeddings (PostgreSQL only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dim",
            type=int,
            default=None,
            help="Embedding dimension. Inferred from existing data if omitted.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                f"pgvector requires PostgreSQL; current database is '{connection.vendor}'. "
                "Set DATABASE_URL to a Postgres instance first."
            )

        table = DocumentChunk._meta.db_table
        column = "embedding_vec"
        dim = options["dim"] or self._infer_dim()
        if not dim:
            raise CommandError(
                "Could not infer embedding dimension (no embeddings found). "
                "Ingest at least one document or pass --dim."
            )

        with connection.cursor() as cur:
            self.stdout.write("Creating 'vector' extension...")
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

            self.stdout.write(f"Adding column {column} vector({dim})...")
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} vector({dim});")

            self.stdout.write("Backfilling vectors from JSON embeddings...")
            cur.execute(
                f"UPDATE {table} SET {column} = embedding::text::vector "
                f"WHERE {column} IS NULL AND embedding IS NOT NULL;"
            )

            index_kind = self._index_kind(cur, dim)
            if index_kind is not None:
                self.stdout.write(f"Building HNSW cosine index ({index_kind})...")
                cur.execute(index_sql(table, column, dim))

        text, ok = summary(dim, index_kind)
        self.stdout.write(self.style.SUCCESS(text) if ok else self.style.WARNING(text))

    def _index_kind(self, cur, dim):
        """Which index this server can build at `dim`, or None."""
        if not halfvec_required(dim):
            return "vector"
        return "halfvec" if self._pgvector_version(cur) >= HALFVEC_MIN_VERSION else None

    @staticmethod
    def _pgvector_version(cur):
        """(major, minor) of the installed pgvector, or (0, 0) if unreadable.

        Unreadable means no halfvec. Refusing to guess is the point: claiming
        an index was built when it was not is the defect being fixed here, and
        an optimistic default would reintroduce it by another route.
        """
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
        row = cur.fetchone()
        if not row or not row[0]:
            return (0, 0)
        parts = str(row[0]).split(".")
        try:
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except ValueError:
            return (0, 0)

    def _infer_dim(self):
        sample = DocumentChunk.objects.exclude(embedding=None).values_list("embedding", flat=True).first()
        return len(sample) if sample else None
