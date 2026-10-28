# Scaling the knowledge base

How to grow the chatbot's data (uploads, API/live-DB sync) while keeping answers
fast and accurate. All four levers below are independent and configured by env var.

## TL;DR — what to set

| Goal | Env var | Default | Notes |
|------|---------|---------|-------|
| Cap upload size | `MAX_UPLOAD_SIZE_MB` | `20` | `0` = no limit |
| Final chunks sent to LLM | `RAG_TOP_K` | `6` | answer breadth |
| Candidate pool before re-rank | `RAG_FETCH_K` | `40` | `>= top_k` |
| Diversify results (MMR) | `RAG_USE_MMR` / `RAG_MMR_LAMBDA` | `true` / `0.6` | avoids duplicate chunks |
| Min similarity | `RAG_SIMILARITY_THRESHOLD` | `0.5` | precision/recall trade-off |
| Memory per query (numpy) | `RAG_SCAN_BATCH` | `2000` | rows scanned per batch |
| Vector backend | `RAG_VECTOR_BACKEND` | `numpy` | `numpy` or `pgvector` |
| Ingestion execution | `INGESTION_MODE` | `sync` | `sync` or `thread` |

## 1. Incremental sync (live / updating data)

`POST /api/v1/sync-api-content/` (and `manage.py sync_api_content`) now sync
**incrementally**: only new or changed source records are re-embedded, unchanged
records are skipped (no embedding cost), and records that disappeared from the
source are removed.

- Each chunk stores a `source_id` (a natural id key — `id`/`pk`/`uuid`/`slug`/`_id`
  — or a content hash) and a `content_hash`.
- On re-sync, only items whose hash changed are re-chunked/embedded.
- Pass `full_refresh=true` to force a full rebuild.

Response reports `items_new`, `items_updated`, `items_unchanged`, `items_removed`,
`chunks_created`. For a live DB, call this on a schedule (cron / Celery beat).

## 2. Retrieval quality (stay accurate as data grows)

More data only helps if the *right* chunks are retrieved. The pipeline now:

- Pulls `RAG_FETCH_K` candidates, then re-ranks to `RAG_TOP_K` with **MMR**
  (relevance + diversity) so near-duplicate chunks don't crowd out the answer.
- Supports filtering by user (multi-tenancy) and by `document_ids`.
- Honors a configurable similarity `threshold`.

Tuning tips: raise `RAG_TOP_K` for broad questions; lower the threshold if the
bot too often says it has no info; raise it if answers pull in irrelevant text.

## 3. Background ingestion (no upload timeouts)

Ingestion (parse → chunk → embed) is synchronous by default. For large files set:

```
INGESTION_MODE=thread
```

Uploads then return immediately with `processing_status=processing`; poll
`GET /api/v1/documents/{id}/` until `completed`. Thread mode is a single-server,
zero-infra option (best with Postgres). For multi-worker scale, plug a real task
queue (Celery/RQ) into `knowledge/services/ingestion.py::dispatch_ingestion` —
no call sites change.

## 4. pgvector backend (millions of chunks, fast)

The default `numpy` backend scans candidate rows in batches (memory bounded by
`RAG_SCAN_BATCH`). It's fine to ~20–30k chunks per query scope. Beyond that, use
an indexed vector search:

1. Point `DATABASE_URL` at PostgreSQL and `pip install pgvector`.
2. `python manage.py migrate`
3. `python manage.py setup_pgvector`  (creates the column, backfills, builds an HNSW index)
4. Set `RAG_VECTOR_BACKEND=pgvector`
5. Re-run `setup_pgvector` after large ingests to backfill new rows.

> **Dimension note:** pgvector's HNSW/IVFFlat index caps at 2000 dims. The default
> embedding model `text-embedding-3-large` is **3072** dims — switch to
> `EMBEDDING_MODEL=text-embedding-3-small` (1536) to enable indexing, or use
> `halfvec`. Without an index, pgvector still works but isn't faster than numpy.

If the pgvector column/extension is missing, retrieval automatically falls back
to numpy and logs a warning — it never hard-fails.

## Capacity summary

| Backend | Comfortable size | Per-query cost |
|---------|------------------|----------------|
| numpy (default) | ~20–30k chunks / scope | scans candidates in RAM (batched) |
| pgvector + HNSW (1536-dim) | millions | ANN index, sub-100ms |

The real long-term ceiling becomes embedding **cost** and retrieval **accuracy**,
not query performance — which is why levers 1 and 2 matter as much as 4.
