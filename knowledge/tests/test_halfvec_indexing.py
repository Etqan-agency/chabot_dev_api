"""Half-precision indexing for embeddings above pgvector's 2000-dim index limit.

pgvector will not build an HNSW index on a `vector` column wider than 2000
dimensions, and the configured EMBEDDING_MODEL (text-embedding-3-large) is
3072. The fix is pgvector's documented half-precision route: keep storing
float32 in `vector(N)`, and index the *expression* `embedding_vec::halfvec(N)`,
which indexes up to 4000.

That only works if the index expression and the query's left-hand side are the
same expression. If they drift, Postgres silently plans a sequential scan --
correct answers, no index, and nothing anywhere says so. These tests pin the
two to a single source of truth, which is the only reason that guarantee holds.

Everything here is pure string construction on purpose: it runs on SQLite in
CI, where the real thing cannot.
"""

from __future__ import annotations

from unittest import skipUnless

from django.db import connection
from django.test import SimpleTestCase, TestCase

from knowledge.management.commands.setup_pgvector import (
    HALFVEC_MIN_VERSION,
    index_sql,
    summary,
)
from knowledge.models import DocumentChunk
from knowledge.services.vector_store import (
    HNSW_VECTOR_DIM_LIMIT,
    distance_expr,
    halfvec_required,
)


class HalfvecThresholdTests(SimpleTestCase):
    def test_the_limit_itself_needs_no_halfvec(self):
        # <=, not <: pgvector indexes exactly 2000 dimensions on `vector`.
        self.assertFalse(halfvec_required(HNSW_VECTOR_DIM_LIMIT))
        self.assertTrue(halfvec_required(HNSW_VECTOR_DIM_LIMIT + 1))

    def test_the_configured_production_dimension_needs_halfvec(self):
        # text-embedding-3-large. This is the case the feature exists for.
        self.assertTrue(halfvec_required(3072))

    def test_a_small_dimension_does_not(self):
        # text-embedding-3-small.
        self.assertFalse(halfvec_required(1536))


class DistanceExpressionTests(SimpleTestCase):
    def test_small_dimension_compares_the_column_directly(self):
        lhs, rhs = distance_expr("c.embedding_vec", 1536)
        self.assertEqual(lhs, "c.embedding_vec")
        self.assertEqual(rhs, "%s::vector")

    def test_large_dimension_casts_both_sides_to_halfvec(self):
        lhs, rhs = distance_expr("c.embedding_vec", 3072)
        self.assertEqual(lhs, "c.embedding_vec::halfvec(3072)")
        self.assertEqual(rhs, "%s::halfvec(3072)")

    def test_both_sides_carry_the_same_dimension(self):
        # A halfvec of one width cannot be compared with a halfvec of another,
        # so a mismatch here is a hard query error rather than a slow query.
        lhs, rhs = distance_expr("c.embedding_vec", 3072)
        self.assertIn("(3072)", lhs)
        self.assertIn("(3072)", rhs)


class IndexMatchesQueryTests(SimpleTestCase):
    """The whole point: the DDL and the query must agree, or the index is dead.

    Not a tautology test. These two strings are built in different modules --
    one at setup time, one per query -- and nothing but this pairing keeps them
    consistent. Getting it wrong costs nothing visible and everything in
    latency.
    """

    def _indexed_expression(self, dim):
        """The expression Postgres will actually index, parsed out of the DDL.

        Balanced-paren scan rather than a split: the expression itself contains
        parentheses (`halfvec(3072)`), so counting is the only way to find
        where it ends.
        """
        sql = index_sql("knowledge_documentchunk", "embedding_vec", dim)
        rest = sql.split("USING hnsw (", 1)[1]
        assert rest[0] == "("
        depth = 0
        for i, ch in enumerate(rest):
            depth += (ch == "(") - (ch == ")")
            if depth == 0:
                return rest[1:i]
        raise AssertionError(f"unbalanced parentheses in index DDL: {sql}")

    def test_halfvec_index_expression_matches_the_query_lhs(self):
        dim = 3072
        lhs, _ = distance_expr("embedding_vec", dim)
        self.assertEqual(self._indexed_expression(dim), lhs)
        self.assertIn("halfvec_cosine_ops", index_sql("t", "embedding_vec", dim))

    def test_plain_index_expression_matches_the_query_lhs(self):
        dim = 1536
        lhs, _ = distance_expr("embedding_vec", dim)
        self.assertEqual(self._indexed_expression(dim), lhs)
        self.assertIn("vector_cosine_ops", index_sql("t", "embedding_vec", dim))

    def test_index_is_created_if_not_exists(self):
        # setup_pgvector is documented as re-runnable after large ingests.
        self.assertIn("IF NOT EXISTS", index_sql("t", "embedding_vec", 3072))


class SummaryTests(SimpleTestCase):
    """The closing message must describe what was actually built.

    The command used to print a green "pgvector is ready" whatever happened,
    including when it had just skipped the index entirely.
    """

    def test_plain_index_reports_success(self):
        text, ok = summary(1536, index_kind="vector")
        self.assertTrue(ok)
        self.assertIn("HNSW", text)

    def test_halfvec_index_reports_success_and_names_the_precision_cost(self):
        text, ok = summary(3072, index_kind="halfvec")
        self.assertTrue(ok)
        self.assertIn("halfvec", text)
        # The trade-off is real and must not be silent: the index is searched
        # at 16-bit precision even though storage stays float32.
        self.assertIn("16-bit", text)

    def test_no_index_does_not_claim_readiness(self):
        text, ok = summary(3072, index_kind=None)
        self.assertFalse(ok)
        self.assertNotIn("pgvector is ready", text)
        self.assertIn("NOT INDEXED", text)
        self.assertIn("sequential scan", text)

    def test_no_index_explains_the_version_requirement(self):
        # The only way to reach index_kind=None at a large dimension now is a
        # pgvector too old for halfvec, so the message has to say so.
        text, _ = summary(3072, index_kind=None)
        self.assertIn("0.7", text)


@skipUnless(connection.vendor == "postgresql", "needs the real pgvector")
class PlannerUsesTheIndexTests(TestCase):
    """Does Postgres actually use it? The string tests cannot answer that.

    Everything above checks that two expressions match each other. This checks
    the thing that matters: that the planner recognises the match and reaches
    for the index. Those are not the same claim -- an expression index is used
    only when the query's expression is equivalent to the indexed one after
    parsing, and nothing in the SQL text guarantees that.

    Runs only where pgvector is real, which in CI is the api-test job's
    pgvector/pgvector:pg16 service. On SQLite it skips.
    """

    DIM = 3072  # text-embedding-3-large, the configured production width.

    def _plan(self, lhs, rhs, vec):
        table = DocumentChunk._meta.db_table
        with connection.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding_vec vector({self.DIM});")
            cur.execute(index_sql(table, "embedding_vec", self.DIM))
            # The table is empty, so a sequential scan is genuinely cheapest and
            # the planner would pick it whatever the index says. Taking that
            # option away is what makes the result mean "can use the index".
            cur.execute("SET LOCAL enable_seqscan = off;")
            cur.execute(
                f"EXPLAIN SELECT id FROM {table} ORDER BY {lhs} <=> {rhs} LIMIT 5",
                [vec],
            )
            return "\n".join(row[0] for row in cur.fetchall())

    def setUp(self):
        self.vec = "[" + ",".join(["0.01"] * self.DIM) + "]"

    def test_pgvector_here_is_new_enough_for_halfvec(self):
        # If this fails, the rest of the class is testing nothing, and the
        # production remedy in setup_pgvector's message would be wrong too.
        with connection.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
            version = tuple(int(p) for p in cur.fetchone()[0].split(".")[:2])

        self.assertGreaterEqual(version, HALFVEC_MIN_VERSION)

    def test_the_generated_query_uses_the_generated_index(self):
        lhs, rhs = distance_expr("embedding_vec", self.DIM)

        plan = self._plan(lhs, rhs, self.vec)

        self.assertIn("embedding_vec_hnsw", plan)

    def test_the_old_hardcoded_expression_does_not(self):
        # The negative control, and the reason the test above has teeth. This
        # is exactly what the query path said before this change: correct
        # results, no index, no error, no log line.
        plan = self._plan("embedding_vec", "%s::vector", self.vec)

        self.assertNotIn("embedding_vec_hnsw", plan)
