# Points & Billing — backend / ops guide

How tenants are charged, how plans and renewals work, and how to size a plan.
For the frontend contract see `FRONTEND_API.md` §7 and §11.

---

## 1. The model in one screen

- **Points are the currency.** 1 point = **$0.001 of real OpenAI cost**. Every
  OpenAI call is charged to the tenant whose work it is, at its real cost:

  | Operation | Kind (`UsageRecord.kind`) | Typical cost |
  |-----------|---------------------------|--------------|
  | Chat answer — dashboard, API **and WhatsApp** (LLM + the question's embedding) | `chat` | ~12 points on gpt-4o, < 1 on gpt-4o-mini |
  | Indexing a document (upload, upload-word, reindex) | `document` | ~1–2 points per 10 pages |
  | Knowledge edits, whole-source rewrite, resolving an unanswered question | `knowledge` | a fraction of a point |
  | Website crawl | `crawl` | ~10 points per 50 pages |
  | API import (AI analysis + embeddings) | `api_sync` | depends on the record count |
  | Guided-tree translation (gpt-4o-mini) | `translation` | ~1 point per language for a small tree |

  Prices come from `LLM_PRICING` / `EMBEDDING_PRICING` in `Conf/settings.py`
  (USD per million tokens). **A model missing there costs 0** — add every model
  you switch to.
- **Plans grant points per period.** Assigning or renewing a plan **resets** the
  wallet to `Plan.points` — no rollover, no point purchases.
- **Billing is manual.** The admin creates the plan, creates the user, assigns the
  plan and renews it by hand. No payment goes through this system.
- **Stops at zero.** With less than 1 point left, or once the plan's period has
  ended, every OpenAI operation returns **`402`** until the admin renews. Reading
  (lists, the tree, analytics) keeps working. WhatsApp customers get a neutral
  "assistant unavailable" reply.
- **Free tier** (a user with no subscription): `FREE_TIER_POINTS` (300) per
  calendar month, lazily reset on the first request of the month; 1 document,
  0.5 MB, no API sync / crawl / API key.
- **Staff are exempt.** Admin/staff usage is recorded (tokens, cost) but never
  charged or blocked.

---

## 2. Sizing a plan

Rule of thumb: **`points = price_usd × 250`** — 25% of the price goes to OpenAI.

| Price | Points | ≈ gpt-4o answers | ≈ gpt-4o-mini answers |
|-------|--------|------------------|-----------------------|
| $10   | 2,500  | ~200             | ~3,500                |
| $20   | 5,000  | ~410             | ~7,000                |
| $50   | 12,500 | ~1,000           | ~17,800               |
| $100  | 25,000 | ~2,000           | ~35,000               |

A gpt-4o RAG answer is ~3,500 input + ~300 output tokens:
`3500 × $2.5/M + 300 × $10/M ≈ $0.012 = 12 points`. Measure the real average
from production before fixing prices:

```sql
SELECT kind, count(*), avg(tokens_in), avg(tokens_out), avg(points_milli) / 1000.0 AS avg_points
FROM knowledge_usagerecord WHERE created_at > now() - interval '30 days' GROUP BY kind;
```

Out of the $20 the margin is roughly $20 − $5 (OpenAI) − server costs.

---

## 3. Operating it (admin API)

```http
POST /api/v1/plans/                          { "name": "Pro", "price_usd": "20.00", "points": 5000, ... }
POST /api/v1/users/                          { "username": "...", "email": "...", "password": "...", ... }
POST /api/v1/subscriptions/                  { "user": "<id>", "plan": "<id>", "duration_days": 30 }
POST /api/v1/subscriptions/<sub id>/renew/   { "duration_days": 30 }      # optional body
POST /api/v1/subscriptions/add-points/       { "user": "<id>", "amount": 500 }
GET  /api/v1/subscriptions/                  # points_balance, can_renew, renewal_reason per row
GET  /api/v1/points-history/?user=<id>       # where a tenant's points went
```

- **Assign** puts a user on a plan for `duration_days` and resets the wallet to
  the plan's points (a plan with `points: 0` leaves the wallet alone).
- **Renew** restarts the period on the same plan (default length = the previous
  period) and resets the points. It is allowed only when the subscription
  **expired/was canceled** or its **points ran out** — otherwise `409
  not_renewable`. The dashboard shows the Renew button only then.
- **Add points** is a manual correction; whatever is left is lost at the next
  renewal.
- Renewing (or assigning) resumes tree translations that paused for lack of
  points.

---

## 4. How charging works (code map)

- `knowledge/services/billing.py` — `charge_to(user, kind)` opens a scope (a
  context variable); the OpenAI backends (`llm.py`, `embeddings.py`) call
  `meter()` after every call, which charges the innermost scope. Each scope
  writes **one** `UsageRecord` (tokens, `cost_usd`, `points_milli`).
- **Chat** holds `CHAT_POINTS_RESERVE` points atomically before answering
  (`quota.reserve_chat_points`) and settles to the real cost after — or releases
  everything when the answer fails. Concurrent requests can't overspend.
- **Everything else** is charged as it goes and checks `scope_has_points()`
  between batches, so a long job stops costing once the points are gone: a
  document fails with "Out of points", the tree leaves the remaining nodes
  untranslated (`paused: true`) until the next renewal.
- Only the thread that opened a scope writes to the database; worker threads
  (batched translation, API analysis) accumulate in memory and are charged by
  the owning thread.
- Wallets (`subscriptions.CreditWallet.milli_points`) hold **milli-points**, so
  tiny embedding calls are charged exactly. Balances never go below zero.
- A call outside any scope (a management command such as `embed_qa` or
  `reindex_documents`) is not charged.

---

## 5. Config reference

| Setting | Default | Meaning |
|---------|---------|---------|
| `FREE_TIER_POINTS` | `300` | Monthly points of a user with no subscription. |
| `CHAT_POINTS_RESERVE` | `20` | Points held before each chat answer (settled to the real cost). |
| `LLM_PRICING` / `EMBEDDING_PRICING` | see settings | USD per million tokens, per model. |
| `FREE_TIER_MAX_DOCUMENTS` | `1` | Free-tier document count. |
| `FREE_TIER_MAX_TOTAL_MB` | `0.5` | Free-tier total storage. |
| `FREE_TIER_ALLOW_API_SYNC` | `false` | Free-tier access to sync-api-content / crawl. |

Per plan: `points`, `price_usd` (display), `max_documents`, `max_total_mb`,
`max_requests_per_min`, `llm_model`, `allow_api_sync`, and the optional extra
caps `monthly_questions` / `monthly_token_cap` (0 = off).

The old `CREDITS_PER_QUESTION`, `FREE_TIER_CREDITS` and
`PLAN_TOKENS_PER_QUESTION` settings no longer exist; remove them from `.env`.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Everything OpenAI returns **402** "out of points" | `points_balance < 1`. Renew the subscription (or add points). |
| **402** "Your subscription has expired" | The period ended or it was canceled. Renew it. |
| Renew returns **409** | Still running with points left — nothing to renew yet. |
| Tree language shows `paused: true` | The tenant ran out of points mid-translation; it resumes after renewal. |
| WhatsApp replies "This assistant is unavailable right now" | The tenant is out of points, lapsed, suspended or rate-limited. |
| Points never go down for a model | The model is missing from `LLM_PRICING` / `EMBEDDING_PRICING` (costs 0). |

---

## 7. Paddle (not used)

`POST /api/v1/billing/paddle/webhook/` is kept for a future self-serve checkout:
a signed `transaction.completed` resets the payer's wallet to the plan's points
(matched by `Plan.paddle_price_id`, deduplicated by `event_id`), and
`subscription.*` events set or cancel the subscription without granting points.
It needs `PADDLE_WEBHOOK_SECRET` (and `PADDLE_ENV` / `PADDLE_API_KEY`) to be
configured; without the secret every call is refused with `403`.
