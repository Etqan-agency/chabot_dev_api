# Multi-Tenant Chatbot — System Design

This document describes how the ChatBot API isolates, serves, limits, and meters
many independent customers ("tenants") from one shared deployment.

> **Tenant model:** a *tenant is a `User`*. Every user owns an isolated
> chatbot: its own knowledge base, persona, API key, quota, and usage history.
> One user's data and limits never touch another's.

---

## 1. High-level architecture

```
                         ┌─────────────────────────────────────────────┐
   Client (web / app /   │                 Django REST API             │
   Postman / WhatsApp)   │                                             │
        │                │  ┌────────────┐   ┌──────────────────────┐  │
        │  x-api-key /   │  │   Auth     │   │   Tenant scoping      │  │
        │  Bearer JWT    │─▶│ APIKey/JWT │──▶│ filter(uploaded_by=u) │  │
        │                │  └────────────┘   └──────────┬───────────┘  │
        │                │        │ resolves request.user             │
        │                │  ┌─────▼──────┐   ┌──────────▼───────────┐  │
        │                │  │  Quota     │   │   RAG pipeline        │  │
        │                │  │  gate      │   │  retrieve → ground →  │  │
        │                │  │ (429/413)  │   │  LLM → meter usage    │  │
        │                │  └────────────┘   └──────────┬───────────┘  │
        │                └─────────────────────────────┼──────────────┘
        │                                               │
        ▼                                ┌──────────────▼──────────────┐
   Response (+ tokens,                   │  DB: users, api_keys,        │
   cost, confident)                      │  documents, chunks (vectors),│
                                         │  configs, quotas, usage      │
                                         └──────────────────────────────┘
                                                        │
                                  ┌─────────────────────┴──────────────┐
                                  │  Vector backend                     │
                                  │  numpy (any DB)  |  pgvector (PG)    │
                                  └─────────────────────────────────────┘
                                                        │
                                  ┌─────────────────────┴──────────────┐
                                  │  OpenAI: embeddings + chat LLM      │
                                  └─────────────────────────────────────┘
```

---

## 2. Tenancy & isolation

Isolation is enforced at **every layer**, not just one:

| Layer | Mechanism | Code |
|------|-----------|------|
| Authentication | API key → exactly one `User`; JWT → one `User` | `Authentication/authentication.py` |
| Documents API | `get_queryset()` filters `uploaded_by=request.user` | `knowledge/views/documents.py` |
| Retrieval | Vector search filters `document__uploaded_by=user` | `knowledge/services/vector_store.py::_base_queryset` |
| RAG guard | No authenticated user ⇒ retrieval is **skipped entirely** (never searches the shared table) | `knowledge/services/rag.py` |
| Config | `ChatbotConfig` is `OneToOne(user)`; `get_or_create(user=...)` | `knowledge/views/chatbot.py` |
| Usage / quota | All rows are FK'd to the tenant; analytics scoped to caller | `knowledge/services/quota.py` |

**Why a defense-in-depth approach:** a single forgotten filter would leak one
tenant's legal/medical/business data to another. The RAG-layer guard means that
even a misconfigured caller (no user) can never trigger an unscoped vector scan.

Isolation is locked in by tests — see `knowledge/tests/test_isolation.py`
(vector search, list scoping, cross-tenant GET/DELETE/reindex all denied).

---

## 3. Request flow (chat)

1. **Authenticate** — `x-api-key` (or `Authorization: ApiKey …` / `Bearer …`)
   resolves `request.user` (the tenant).
2. **Quota gate** — `quota.check_chat_allowed(user)`: suspended ⇒ 403,
   lapsed plan ⇒ 402, over rate limit ⇒ 429, over an optional monthly
   question/token cap ⇒ 429, less than 1 point ⇒ 402. Then up to
   `CHAT_POINTS_RESERVE` points are held atomically (see `PAYMENTS.md`).
3. **Resolve config** — persona (name/company/tone/language) + retrieval
   overrides from `ChatbotConfig`. Grounding rules are **fixed in code**, not
   tenant-editable.
4. **Retrieve** — embed the question, vector-search **this tenant's** chunks,
   MMR re-rank → top-K. Below threshold ⇒ a no-threshold fallback so broad/meta
   questions ("who are you?") still answer from the tenant's own data.
5. **Ground & answer** — system prompt forces an insider voice ("we/our") and
   forbids hedging; the LLM answers using only retrieved knowledge.
6. **Meter** — every OpenAI call (the question's embedding, the answer) is
   metered into the chat's points scope; the hold is settled to the real cost
   and one `UsageRecord` is written (tokens in/out, cost, points, latency,
   confidence, chunk count).
7. **Respond** — answer + sources + `confident` + tokens + `cost_usd` +
   `points_used` / `points_balance`.

---

## 4. Data model

```
User (= Tenant)  ──1:1── APIKey            (knowledge_apikey)
   │
   ├──1:1── ChatbotConfig                  persona + retrieval overrides
   ├──1:1── TenantQuota                    limits (null field ⇒ global default)
   ├──1:N── UploadedDocument ──1:N── DocumentChunk(embedding JSON)
   ├──1:N── UsageRecord                    one row per metered chat answer
   └──1:N── ChatFeedback                   thumbs up/down on answers
```

Key fields:
- `DocumentChunk.embedding` — JSON list (portable; swappable for a pgvector
  column). Carries `source_id` + `content_hash` for incremental re-sync.
- `TenantQuota` — `max_documents`, `max_total_mb`, `max_requests_per_min`,
  `monthly_token_cap` (0 = unlimited), `is_suspended`. Any null falls back to
  the `TENANT_*` settings default.
- `UsageRecord` — `tokens_in/out`, `cost_usd`, `response_time_ms`, `confident`,
  `chunk_count`. Question text is intentionally **not** stored (privacy).

---

## 5. Quotas & rate limiting

All enforcement lives in `knowledge/services/quota.py` (single source of truth):

| Limit | Default (settings) | Enforced at | On breach |
|------|--------------------|-------------|-----------|
| Documents per tenant | `TENANT_MAX_DOCUMENTS=100` | upload | `413` |
| Total storage (MB) | `TENANT_MAX_TOTAL_MB=200` | upload | `413` |
| Chat requests / min | `TENANT_MAX_REQUESTS_PER_MIN=60` | chat | `429` |
| Monthly token budget | `TENANT_MONTHLY_TOKEN_CAP=0` (off) | chat | `429` |
| Suspension | `is_suspended` | upload + chat | `403` |

Per-tenant overrides: create/edit a `TenantQuota` row (Django admin or shell).
Rate limit is a sliding 60-second window counted from `UsageRecord`.

---

## 6. Usage metering & analytics

Every chat answer records token usage and an **estimated** cost from
`LLM_PRICING` (USD per 1M tokens) in settings. Unknown models cost 0 (analytics
never blocks).

`GET /api/v1/analytics/usage/`
- **self:** totals + this-month + today rollups (requests, tokens, cost,
  avg latency, confident-rate) plus live quota consumption.
- **`?scope=all` (admin):** the same rollup per tenant.

---

## 7. Scaling path

The retrieval backend is swappable (`RAG_VECTOR_BACKEND`):

| Stage | Backend | Good for | Query cost |
|------|---------|----------|-----------|
| Now | `numpy` (brute force, batched) | up to ~5k chunks/tenant on SQLite | O(N) per query |
| Next | `pgvector` (Postgres + HNSW) | hundreds of thousands+ chunks | O(log N) |

Switching to `pgvector` requires Postgres + the `setup_pgvector` command; the
code already falls back to numpy if the vector column is missing. Per-question
**LLM cost is constant** regardless of corpus size (only top-K chunks are sent),
so a tenant's knowledge base can grow large without growing per-answer cost.
See `SCALING.md` for the deep dive.

---

## 8. What is *not* tenant-configurable (by design)

To guarantee safe, grounded answers for every tenant, these are fixed in code:
- The strict-grounding rules and insider voice (`build_system_prompt`).
- The isolation filters (a tenant cannot widen its own retrieval scope).
- The metering — every answer is recorded.

Tenants control only identity/presentation: assistant name, company name, tone,
default language, and optional retrieval knobs (`top_k`, `similarity_threshold`).

---

## 9. Test coverage

`python manage.py test knowledge.tests` (81 tests):

| File | Covers |
|------|--------|
| `test_isolation.py` | vector + REST cross-tenant isolation |
| `test_quota.py` | document/size/rate/token/suspension limits + cost |
| `test_usage.py` | metering + analytics endpoint (self + admin) |
| `test_chat.py` | chat auth, quota gating, metering, validation, language |
| `test_rag.py` | every answer branch + the anonymous isolation guard |
| `test_documents.py` | upload quota (413), word upload, reindex scoping |
| `test_chatbot_config.py` | config CRUD + system-prompt assembly |
| `test_authentication.py` | API key + JWT auth paths |
| `test_vector_store.py` | numpy backend edge cases |
| `test_retrieval.py` | search orchestration + MMR re-ranking |
