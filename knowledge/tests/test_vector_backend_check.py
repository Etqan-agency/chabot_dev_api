"""A configured-but-unusable pgvector backend must announce itself.

`RAG_VECTOR_BACKEND=pgvector` on a database with no `vector` extension and no
`embedding_vec` column is not an error at any point: `search_candidates`
catches `VectorBackendUnavailable` and quietly serves every query from the
numpy path instead. Retrieval keeps working, slowly, and the only trace is one
log line per query -- which is indistinguishable from noise on a busy host and
absent entirely on an idle one.

That is the state the new production host is in right now, and nothing would
have surfaced it. These tests pin a startup check that does.
"""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from knowledge.checks import check_vector_backend, check_vector_index
from knowledge.services.vector_store import PgVectorStore

DATABASES = ["default"]


class UnavailableReasonTests(TestCase):
    """The probe itself, against the real connection."""

    def test_reports_a_reason_when_pgvector_is_not_set_up(self):
        # True in both environments this suite runs in: SQLite locally, and
        # PostgreSQL-without-the-column in CI. Neither has pgvector configured.
        reason = PgVectorStore().unavailable_reason()

        self.assertIsNotNone(reason)

    def test_the_reason_names_what_is_actually_wrong(self):
        reason = PgVectorStore().unavailable_reason()

        if connection.vendor == "postgresql":
            # Right engine, missing column -- `setup_pgvector` is the remedy.
            self.assertIn(PgVectorStore.COLUMN, reason)
            self.assertIn("setup_pgvector", reason)
        else:
            # Wrong engine entirely; no amount of setup_pgvector helps.
            self.assertIn("PostgreSQL", reason)
            self.assertIn(connection.vendor, reason)


class CheckTests(SimpleTestCase):
    """The check's own logic, with the probe stubbed at a seam we own."""

    @override_settings(RAG_VECTOR_BACKEND="numpy")
    def test_the_numpy_backend_is_not_reported(self):
        messages = check_vector_backend(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])

    @override_settings(RAG_VECTOR_BACKEND="numpy")
    def test_the_numpy_backend_does_not_even_probe_the_database(self):
        # Configuring numpy is a deliberate choice, not a degraded state, so
        # the check must cost nothing at all in that case.
        with mock.patch.object(PgVectorStore, "unavailable_reason") as probe:
            check_vector_backend(app_configs=None, databases=DATABASES)

        probe.assert_not_called()

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_an_unusable_backend_produces_one_warning(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value="the column is missing"):
            messages = check_vector_backend(app_configs=None, databases=DATABASES)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, "knowledge.W001")

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_the_warning_carries_the_reason_and_the_consequence(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value="the column is missing"):
            message = check_vector_backend(app_configs=None, databases=DATABASES)[0]

        text = f"{message.msg} {message.hint}"
        self.assertIn("the column is missing", text)
        # Whoever reads this needs to know it is not failing, which is exactly
        # why they have not noticed it.
        self.assertIn("numpy", text)
        self.assertIn("setup_pgvector", text)

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_a_usable_backend_is_silent(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value=None):
            messages = check_vector_backend(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_it_is_skipped_when_no_database_is_available(self):
        # Django runs checks in contexts with no database (`manage.py check`
        # without --database). Probing there would raise rather than warn.
        with mock.patch.object(PgVectorStore, "unavailable_reason") as probe:
            messages = check_vector_backend(app_configs=None, databases=[])

        self.assertEqual(messages, [])
        probe.assert_not_called()

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_a_failing_probe_does_not_break_the_command(self):
        # System checks run as part of `migrate`. If the probe raised -- an
        # unreachable database, a permissions problem -- it would abort the
        # command it was only meant to annotate. A database that cannot be
        # queried is already loud everywhere else; this check stays quiet.
        with mock.patch.object(PgVectorStore, "unavailable_reason", side_effect=RuntimeError("no db")):
            messages = check_vector_backend(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])


class UnindexedCheckTests(SimpleTestCase):
    """W002: pgvector working exactly as configured, with no index behind it.

    Strictly quieter than W001. There the backend cannot run and retrieval
    falls back to numpy; here it runs, returns the right rows, logs nothing
    unusual, and sequentially scans the whole chunk table to do it. The cost
    arrives gradually as the corpus grows, which is the worst way for it to
    arrive.
    """

    @override_settings(RAG_VECTOR_BACKEND="numpy")
    def test_the_numpy_backend_does_not_probe(self):
        with mock.patch.object(PgVectorStore, "unindexed_reason") as probe:
            messages = check_vector_index(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])
        probe.assert_not_called()

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_an_unusable_backend_defers_to_W001(self):
        # One cause must not produce two warnings. A missing column is not an
        # unindexed table, and reporting it as one sends the reader to the
        # wrong remedy.
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value="no column"):
            with mock.patch.object(PgVectorStore, "unindexed_reason") as probe:
                messages = check_vector_index(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])
        probe.assert_not_called()

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_a_usable_but_unindexed_backend_warns(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value=None):
            with mock.patch.object(PgVectorStore, "unindexed_reason", return_value="no HNSW index"):
                messages = check_vector_index(app_configs=None, databases=DATABASES)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, "knowledge.W002")

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_the_warning_names_the_remedy_and_the_version_trap(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value=None):
            with mock.patch.object(PgVectorStore, "unindexed_reason", return_value="no HNSW index"):
                message = check_vector_index(app_configs=None, databases=DATABASES)[0]

        text = f"{message.msg} {message.hint}"
        self.assertIn("no HNSW index", text)
        self.assertIn("setup_pgvector", text)
        # The one way to end up here after running setup_pgvector.
        self.assertIn("halfvec", text)

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_an_indexed_backend_is_silent(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason", return_value=None):
            with mock.patch.object(PgVectorStore, "unindexed_reason", return_value=None):
                messages = check_vector_index(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_a_database_error_does_not_break_the_command_it_annotates(self):
        # These checks run during `migrate`. An unreachable database must not
        # abort it -- and is already loud everywhere else.
        with mock.patch.object(PgVectorStore, "unavailable_reason", side_effect=RuntimeError("boom")):
            messages = check_vector_index(app_configs=None, databases=DATABASES)

        self.assertEqual(messages, [])

    @override_settings(RAG_VECTOR_BACKEND="pgvector")
    def test_no_databases_means_no_probe(self):
        with mock.patch.object(PgVectorStore, "unavailable_reason") as probe:
            messages = check_vector_index(app_configs=None, databases=[])

        self.assertEqual(messages, [])
        probe.assert_not_called()
