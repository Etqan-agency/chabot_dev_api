# ChatBot API — Frontend Integration Guide

Everything a frontend developer needs to build against this API. All examples
use `fetch`; adapt freely to axios/your client.

---

## 1. Base URL & conventions

```
Base URL:  https://chatbot-api.zaychain.com/api/v1
```

- All requests/responses are **JSON** unless uploading files (then `multipart/form-data`).
- Every endpoint below requires **authentication** (see §2) except where noted.
- Trailing slashes are required (`/chat/`, not `/chat`).
- Timestamps are ISO-8601 UTC.
- List endpoints are **paginated** (20/page): `?page=2`. Response shape:
  `{ "count", "next", "previous", "results": [...] }`.

> **CORS:** your site's origin must be whitelisted server-side
> (`CORS_ALLOWED_ORIGINS`). Give the backend team your frontend URL(s).

---

## 2. Authentication

Two interchangeable ways to authenticate. Pick one per request.

### Sign up — `POST /auth/register/`  (public, no auth)
Anyone can create an account:
```js
await fetch(`${BASE}/auth/register/`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    username, email, first_name, last_name, password, password_confirm,
  }),
});
// 201 -> { detail, username, email, email_verification: "activation_sent" | "send_failed" }
```
`email` is **required and unique**. The account is created **inactive** and an
activation link is emailed — the user must verify (see "New accounts must verify"
below) before they can log in. Rate-limited to 5/min per IP. `400` on validation
errors (duplicate email/username, password mismatch/too weak).

### Option A — JWT (recommended for user-facing apps)

**Login:**
```js
const res = await fetch(`${BASE}/auth/login/`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username, password }),
});
const data = await res.json();
// data.access   -> short-lived access token (send on every request)
// data.refresh  -> use to get a new access token
// data.user     -> { id, username, email, role, api_key, ... }
```

**Use the token on every request:**
```js
headers: { "Authorization": `Bearer ${accessToken}` }
```

**Refresh when the access token expires (401):**
```js
const res = await fetch(`${BASE}/auth/refresh/`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ refresh: refreshToken }),
});
const { access } = await res.json();
```

Access token lifetime: 3 days. Refresh token: 30 days.

### Option B — API key (good for embedding a single tenant's bot)

Send the tenant's key as a header — no login needed:
```js
headers: { "x-api-key": "427a48c9...." }
```

> The whole API is **multi-tenant**: the token/key identifies the tenant, and
> all data (documents, chat answers) is automatically scoped to them. You never
> pass a user/tenant id in the body.

### New accounts must verify their email before they can log in

A newly created account is **inactive** until its email is verified — login
returns `401` and the API key won't authenticate. The user gets an email with a
link to `FRONTEND_URL/verify-email?uid=...&token=...`. Your frontend page reads
`uid` and `token` from the query string and POSTs them to activate (public, no
auth):

```js
await fetch(`${BASE}/auth/verify-email/`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ uid, token }),
});
// 200 -> account is active + email_verified; the user can now log in.
// 400 -> invalid/expired link (ask an admin to re-send).
```

A direct `GET /auth/verify-email/?uid=...&token=...` also works (for clicking the
link without a dedicated page). Links are **single-use** and expire (~3 days); an
admin can re-send via `POST /users/{id}/resend-activation/`.

---

## 3. Chat (the main endpoint)

### `POST /chat/`

**Request:**
```json
{
  "question": "ما هي المنتجات المتوفرة لديكم؟",
  "history": [
    { "role": "user",      "content": "السلام عليكم" },
    { "role": "assistant", "content": "وعليكم السلام! كيف أقدر أساعدك؟" }
  ]
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `question` | yes | Any language; the bot replies in the same language **and dialect**. |
| `history` | no | Prior turns for context. Keep the **last 10 turns max** (20 messages) — older ones rejected with 400. |
| `language` | no | Optional hint (`"ar"`, `"en"`, …). Omit it — the server auto-detects. |

**Response — 200:**
```json
{
  "answer": "أهلاً! عندنا انترنت داونلود مانجر (IDM) لمدة سنة بسعر 12,000 دينار...",
  "source": "rag",
  "source_id": "",
  "confident": true,
  "response_time_ms": 2643,
  "sources": [
    {
      "filename": "products",
      "document_id": "42b71e56-...",
      "chunk_id": "d6e3e001-...",
      "position": 11,
      "score": 0.81
    }
  ],
  "prompt_tokens": 2503,
  "completion_tokens": 144,
  "cost_usd": 0.007698,
  "points_used": 7.699,
  "points_balance": 4870.551
}
```

| Field | Meaning |
|-------|---------|
| `answer` | The text to render (may contain **Markdown** — render links/lists). |
| `confident` | `true` when a strong match was found; `false` = weaker/fallback answer. Optionally show a subtle "not sure" hint when false. |
| `sources` | Which knowledge chunks were used. Optional to display (e.g. "Sources"). |
| `prompt_tokens` / `completion_tokens` / `cost_usd` | Usage of this call (for your own metering/UI; safe to ignore). |
| `points_used` | Points this answer took from the wallet (its real OpenAI cost × 1000; `0` for staff). See §7. |
| `points_balance` | The wallet after this answer (`null` for staff). Update a points badge from it. |

**Frontend flow for a chat UI:**
1. Keep an array of `{role, content}` messages in state.
2. On send: push the user message, POST `{ question, history: lastMessages }`.
3. Append `answer` as an assistant message. Render with a Markdown renderer.
4. Trim history to the last ~20 messages before sending.

```js
async function ask(question, history) {
  const res = await fetch(`${BASE}/chat/`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
    body: JSON.stringify({ question, history: history.slice(-20) }),
  });
  if (!res.ok) throw await res.json();   // see §10 for error shapes
  return res.json();
}
```

> **Streaming:** not supported yet — the full answer returns in one response
> (typically 1–4 s). Show a typing indicator while awaiting.

### `POST /chat/feedback/` — thumbs up/down on an answer
```json
{ "question": "...", "answer": "...", "source": "rag", "rating": "up", "comment": "" }
```
`rating`: `"up"` | `"down"`. Returns `201` with the saved row.

---

## 4. Documents (knowledge management UI)

Uploads are processed in the **background**. After upload, poll the document
until `processing_status` is `completed`.

### List — `GET /documents/`
Paginated. Each item:
```json
{
  "id": "677f38e1-...",
  "filename": "about.docx",
  "file_size_mb": 0.01,
  "chunk_count": 2,
  "processing_status": "completed",   // pending | processing | completed | failed
  "error_message": "",
  "is_active": true,
  "source_type": "file",              // file | api | website
  "uploaded_by_username": "original_software",
  "created_at": "2026-06-17T01:03:41Z"
}
```
Filters: `?processing_status=completed`, `?search=<filename>`, `?ordering=-created_at`.

### Upload a Word file — `POST /documents/upload-word/`  (multipart)
```js
const fd = new FormData();
fd.append("file", fileInput.files[0]);   // .docx only, max 20 MB
const res = await fetch(`${BASE}/documents/upload-word/`, {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },   // do NOT set Content-Type for FormData
  body: fd,
});
```
Returns `201` with the document (`processing_status: "pending"`). Then poll §below.

### Upload any file — `POST /documents/`  (multipart)
Same as above with field `file`; optional `is_active`.

### Poll status — `GET /documents/{id}/`
Poll every ~2 s until `processing_status === "completed"` (or `"failed"` →
show `error_message`). Then `chunk_count` is the number of searchable pieces.

### Delete — `DELETE /documents/{id}/`  → `204`
### Re-index — `POST /documents/{id}/reindex/`  → `202` (re-embeds the document)

> To review or correct the *content* the bot learned from a document — rather
> than replacing the whole file — open it as a page: `GET`/`PUT`
> `/knowledge/sources/{id}/` serves and saves the whole document as one text
> (§5e), and §5d edits it one piece at a time. Note that re-indexing rebuilds a
> document's chunks from the original file, so it replaces any corrections made
> either way.

> **Points.** Upload, upload-word and reindex embed the text with OpenAI, charged
> to the tenant's points (~1–2 points per 10 pages). With less than 1 point, or a
> lapsed plan, they return **`402`** and nothing is stored (§7).

> Limits per tenant: **100 documents / 200 MB / 20 MB per file** by default.
> Over the limit returns **413** (see §10). **Free-tier** tenants are much
> tighter — **1 document, ~0.5 MB** (one small file); see §7.

---

## 5. Sync content from an external API — `POST /sync-api-content/`
Pull JSON from a URL into the tenant's knowledge.
```json
{ "api_url": "https://api.example.com/products", "items_key": "results",
  "ai_analysis": true, "max_pages": 200, "full_refresh": false }
```
In production the import runs on a worker, so the call returns **`202`**
immediately and you poll for the result:
```json
{
  "status": "queued",
  "run_id": "94a99870-5d52-46e3-abe4-d9846e524d04",
  "status_url": "/api/v1/sync-api-content/runs/94a99870-.../",
  "detail": "The import is running in the background. Poll status_url for progress."
}
```
A full import runs one LLM call per record, so a few hundred records take
minutes — far longer than any gateway will hold a connection open. Do **not**
block the UI on this request; show progress from `status_url` (§5a).

On a deployment configured to import in-request (`INGESTION_MODE=sync`, small
self-hosted setups) the same call instead returns `200` with the finished sync
report below. Branch on the status code, not on the body.

Returns `200` with the sync report:
```json
{
  "status": "success",
  "run_id": "94a99870-5d52-46e3-abe4-d9846e524d04",
  "pages_fetched": 3,       // pages read from the source
  "truncated": false,       // true = max_pages stopped it early, there is more
  "processed": 41,          // records read from the response
  "items_stored": 41,       // records archived verbatim
  "items_analyzed": 41,     // records the AI described
  "analysis_errors": 0,     // records the AI could not describe (retried next run)
  "items_new": 40,          // embedded for the first time
  "items_updated": 1,       // changed at the source, re-embedded
  "items_unchanged": 0,     // skipped — no embedding cost
  "items_removed": 0,       // gone from the source, dropped from knowledge
  "items_protected": 0,     // hand-edited records the sync kept (§5d)
  "chunks_created": 57,
  "errors": 0
}
```
Re-running only re-embeds changed items (incremental), so syncing a live
database on a schedule is cheap.

The **entire source** is imported:

* **Every page.** If the response paginates (`next`, `links.next`, or a `Link`
  header), the sync follows it to the end. `pages_fetched` in the reply says how
  many were read; `truncated: true` means `max_pages` cut it short.
* **Every field.** Each list element becomes a record, and the response's other
  top-level fields (`count`, `next`, anything else) are kept as one extra
  `response_metadata` record per page. `items_key` is optional — when that key is
  absent the longest top-level list is used, so a bare JSON array,
  `{ "data": [...] }`, or a single JSON object all work as-is.
* **Verbatim.** Every record is archived exactly as fetched *before* it is
  chunked or embedded, so an embedding outage can cost searchability but never
  the data — it is re-indexed from the archive, with no second call to the source.

**AI analysis** (`ai_analysis`, default `true`): each new or changed record is
described in plain language by the chat LLM, and that description is indexed
**next to** the raw JSON, never instead of it — so answers retrieve on prose and
still quote exact fields. It is best-effort: with no LLM configured, or during a
provider outage, the sync still stores and indexes everything and retries the
description on the next run. Analysis tokens are metered like any other LLM call.
Stats: `items_stored`, `items_analyzed`, `analysis_errors`.

> **Not on the free tier.** This endpoint returns **`402`** unless the tenant's
> plan enables it (`allow_api_sync`). Free-tier users import knowledge only by
> uploading a small file (§4). On `402` show the `detail` (not on the plan, or
> out of points — every import is charged to the tenant's points, §7).

### 5a. Poll an import — `GET /sync-api-content/runs/<run_id>/`
The other half of the `202`. Poll every few seconds until `done` is `true`.
```json
{
  "run_id": "94a99870-...",
  "status": "embedding",   // queued -> fetching -> structuring -> embedding -> completed
  "done": false,
  "endpoint": "https://api.example.com/products",
  "pages_fetched": 3,
  "records_fetched": 200,
  "records_processed": 41,
  "chunks_created": 57,
  "field_coverage": 1.0,
  "duration_ms": 153540
}
```
Terminal states — `done: true` — and what to show:

| `status` | meaning | extra fields |
|---|---|---|
| `completed` | the import landed | `result` — the full sync report above |
| `blocked` | quality control refused it; **existing knowledge is unchanged** | `detail`, `reason`, `validation` |
| `failed` | the import did not finish | `detail` — why |

Runs are tenant-scoped: another tenant's `run_id` returns `404`, never its data.

---

## 5b. Crawl a website into knowledge — `POST /crawl-website/`
Give the tenant's own **website URL** and the bot learns the whole site: it
discovers as many pages of the same site as it can (sitemap + internal links),
extracts each page's main readable text (nav/menus/footers stripped), and stores
them as knowledge. Great for onboarding — the client pastes their site instead of
uploading files.
```json
// request
{
  "url": "https://client.com",   // required; bare host is upgraded to https://
  "document_name": "Website: client.com",  // optional (defaults to the host)
  "max_pages": 100,              // optional; clamped to the server cap (300)
  "full_refresh": false          // optional; false = incremental re-crawl
}
```
Crawling a whole site is slow, so it runs in the **background** and returns
**`202`** immediately:
```json
{ "status": "processing", "document_id": "<uuid>", "document_name": "...", "max_pages": 100,
  "detail": "Crawl started. Poll GET /documents/{id}/ for progress ..." }
```
Poll `GET /documents/{document_id}/` and watch `processing_status`:
`processing` → `completed` (or `failed`, with `error_message`). The crawled pages
become one knowledge document (source type `website`); re-crawling only re-embeds
pages whose content changed. Same-site only, honours `robots.txt`, and rejects
private/internal URLs (**`400`**).

> **Not on the free tier.** Same gate as §5 — returns **`402`** unless the plan
> enables it (`allow_api_sync`), or when the tenant is out of points (the page
> embeddings are charged to its points, §7). Show the `detail` on `402`.

---

## 5c. Guided question tree — `/guided-tree/` + `/tree-languages/`
A tappable tree of questions for a guided chatbot: pick a language → show root
questions → tap one → show its answer and/or child questions to drill into.
**Multilingual with one canonical language** (default Arabic): you author only the
canonical tree; every other language is generated automatically as a
**positional mirror**, translated by the LLM (OpenAI) in the background. Each
tenant picks its own languages. All scoped to the tenant.

**Read the tree (public, any authenticated tenant, cached ~5 min):**
```
GET /guided-tree/?language=en        # omit language → canonical
→ { "language": "en", "canonical_language": "ar",
    "tree": [ { "id", "title", "answer", "order", "language",
                "children": [ …same shape recursively… ] } ] }
```
Tap a node = descend into its `children`; show `answer` if present.

**Languages (per tenant):**
```
GET  /tree-languages/            (?active=1 to filter)
→ [ { id, code, name, is_active,
      scope: "account" | "global",       # yours, or one an admin added for every tenant
      translated_nodes, total_nodes,     # progress: nodes in this language / in the canonical tree
      translating,                       # true while a background translation is running
      paused } ]                         # true when untranslated nodes wait for points (§7)
POST /tree-languages/            { "code": "en", "name": "English" }   → 201, translates the WHOLE tree into it
DELETE /tree-languages/{code}/                                         → 204, removes it + its translated tree
```
- Any tenant adds/removes **its own** languages (`scope: "account"`). Adding one
  translates the whole tree in the background; poll `GET /tree-languages/` and
  show `translated_nodes / total_nodes` until `translating` is `false`.
- After that, **every new or edited question is translated into all of the
  tenant's languages automatically** — only what changed is re-translated.
- `global` languages apply to every tenant and are admin-only
  (`POST { …, "global": true }` / `DELETE` → `403` for non-admins). Adding a code
  that is already global → `400`. The canonical language can't be added or removed (`400`).

**Authoring (writes always hit the canonical language; mirrors follow async):**
```
POST   /guided-tree/             { "title", "answer"?, "parent"? (id), "order"?, "is_active"? }
PATCH  /guided-tree/{id}/        { "title"?, "answer"?, "order"?, "is_active"? }   (edit via ANY language node → resolves to canonical)
DELETE /guided-tree/{id}/                                              (removes every language mirror too)
GET    /guided-tree/flat/?language=ar                                  (flat dump for an admin editor)
POST   /guided-tree/retranslate/ { "language"? }                       (re-translate one/all languages from scratch) → 202
```

Notes:
- `order` must be **unique among siblings** — omit it to auto-append; a clash returns **`400`**.
- Edits/additions **don't block** on translation; re-fetch the mirror tree a moment later.
- Deleting or reordering refreshes the affected languages automatically.
- Translation happens **in place**: while a language is being (re)translated it
  keeps serving its previous tree, never an empty one. A node whose translation
  failed shows the canonical text and is retried on the next change or `retranslate`.
- **Feature-gated.** Reading the tree is always allowed, but **authoring** it
  (`POST`/`PATCH`/`DELETE /guided-tree/`, `retranslate`, `POST /tree-languages/`) returns **`402`** when the
  `guided_tree` feature is off for the tenant (see §11). Check `/my-features/` to
  show/hide the authoring UI.
- **Points.** Translation is charged to the tenant's points (gpt-4o-mini, ~1 point
  per language for a small tree). `POST /tree-languages/` and `retranslate` return
  **`402`** with less than 1 point. If the points run out mid-translation the
  remaining nodes keep the canonical text and the language shows `paused: true`;
  it finishes by itself after the admin renews the plan.

---

## 5d. Knowledge content — `/knowledge/` (full CRUD)
Documents are what you *uploaded*; **knowledge content** is what the bot actually
answers from. This is full CRUD over those pieces, across every source (file
upload, API sync, website crawl) — so a wrong, stale or embarrassing answer gets
fixed in place, without re-uploading a file or re-crawling a site.

> **Building the knowledge screen? Start at §5e, not here.** §5e serves each
> source as **one continuous text** that a person can read, edit in a textarea
> and delete — one page per source, no chunks. This section is the per-piece
> API underneath it: use it for search, for auditing, and for the one case §5e
> refuses (API-synced sources).

| Method | Path | What it does |
|---|---|---|
| `GET` | `/knowledge/` | List & search everything the bot knows |
| `POST` | `/knowledge/` | Teach it one new piece |
| `GET` | `/knowledge/{id}/` | Read one piece |
| `PATCH` / `PUT` | `/knowledge/{id}/` | Fix the wording |
| `DELETE` | `/knowledge/{id}/` | Make it forget one piece |
| `POST` | `/knowledge/bulk-delete/` | Forget many at once |
| `GET` | `/knowledge/stats/` | Totals per source + how much was corrected |
| `GET` | `/knowledge/sources/` | The sources, grouped file / api / website |
| `GET` | `/knowledge/sources/{document_id}/` | One source as **one block of text** |
| `PUT` | `/knowledge/sources/{document_id}/` | Save that whole text back |
| `DELETE` | `/knowledge/sources/{document_id}/` | Delete the source and everything it taught |

Everything is scoped to the caller: you only ever see or touch knowledge that
belongs to your own documents. Another tenant's piece is a plain **`404`**.

### List — `GET /knowledge/`
Paginated (20/page). Each row carries what a review screen needs:
```json
{
  "id": "cd0d9c53-...",
  "document": "531a98e9-...",
  "document_name": "handbook.docx",
  "source_type": "file",              // file | api | website
  "api_url": "",                      // set when it came from an API sync
  "is_active": true,                  // its document is active
  "position": 3,                      // order within the document
  "content": "Refunds are processed within 14 days.",
  "char_count": 37,
  "metadata": { "manually_edited": true },
  "source_id": "",                    // the source record's id (API sync)
  "embedding_model": "text-embedding-3-large",
  "is_embedded": true,                // false = not searchable yet
  "was_edited": true,
  "edited_at": "2026-09-14T00:11:07Z",
  "created_at": "2026-09-14T00:11:07Z",
  "updated_at": "2026-09-14T00:11:07Z"
}
```

**Filters** — combine freely:

| Query | Effect |
|---|---|
| `?document=<uuid>` | only this document's knowledge |
| `?source_type=file\|api\|website` | only one kind of source |
| `?document_name=handbook` | substring of the document name |
| `?source_id=42` | the source record a piece came from (API sync) |
| `?is_active=true` | only knowledge from active documents |
| `?edited=true` | only the pieces a human corrected (`false` = only untouched) |
| `?search=refund` | substring of the content |
| `?ordering=-updated_at` | also `created_at`, `position`, `edited_at` |

### Fix one piece — `PATCH /knowledge/{id}/`
```js
await api(`/knowledge/${id}/`, {
  method: "PATCH",
  body: { content: "Refunds are processed within 14 days." },
});
// 200 -> the updated row (same shape as the list row above)
```
`PUT` behaves the same. `content` must be non-empty; `metadata` is optional and
merges into the existing metadata.

### Teach one new piece — `POST /knowledge/`
```json
{ "document": "531a98e9-...", "content": "Gift cards never expire." }
```
`201` with the new row. `document` is required (which document it belongs to)
and must be one of yours; it is appended at the end (`position`) and is
searchable immediately.

> **Points.** Teaching, fixing the wording (`PATCH` with new `content`), saving a
> whole source (§5e) and resolving an unanswered question with an answer (§8)
> re-embed the text — a fraction of a point each, charged to the tenant. With
> less than 1 point (or a lapsed plan) they return **`402`** (§7).

### Forget — `DELETE /knowledge/{id}/` → `204`
```json
// POST /knowledge/bulk-delete/   (max 500 ids)
{ "ids": ["cd0d9c53-...", "a5998233-..."] }
// 200 -> { "deleted": 2 }
```
Ids that aren't yours are silently skipped — the count tells you what actually went.

### Overview — `GET /knowledge/stats/`
```json
{ "total": 412, "edited": 7, "by_source": { "file": 120, "api": 280, "website": 12 },
  "documents": 5 }
```

### Two things worth knowing

**1. Every write re-embeds.** Retrieval matches on a piece's *embedding*, not on
its text — so saving new wording without re-embedding would leave the bot showing
your correction while still being retrieved for the wording you just removed.
Create and edit both embed synchronously and refresh the vector index. If the
embedding provider is down, the write is rejected with **`400`** and the previous
text is left exactly as it was — never half-applied. (Re-saving identical text is
a no-op and costs nothing.)

**2. How corrections interact with re-syncs.**

| You re-run | What happens to your correction |
|---|---|
| API sync, source record unchanged | **Kept.** The reply counts it in `items_protected` |
| API sync, source record changed | Replaced — the source is newer and wins |
| API sync with `full_refresh: true` | Replaced — it rebuilds everything from the source |
| `POST /documents/{id}/reindex/` | Replaced — chunks are rebuilt from the original file |
| Re-crawl of a website | Replaced for pages whose content changed |
| `PUT /knowledge/sources/{id}/` (§5e) | Replaced — the whole source becomes the text you saved |

So a correction sticks for as long as the source behind it says the same thing.
Use `?edited=true` to review every correction currently in force.

---

## 5e. A source as one editable page — `/knowledge/sources/`
§5d addresses knowledge one chunk at a time, which is what *auditing* needs.
These endpoints address it the way a **person reads and writes**: a whole
source, in order, as **one block of text** — shown in a page, edited in a
textarea, saved, or deleted. Your UI never has to think in chunks.

| | |
|---|---|
| `GET /knowledge/sources/` | the list of sources — one row per page you can open |
| `GET /knowledge/sources/{document_id}/` | that page: the whole text in `content` |
| `PUT /knowledge/sources/{document_id}/` | save the edited text (re-embedded) |
| `DELETE /knowledge/sources/{document_id}/` | delete the source entirely |

Same rows and same tenant scoping as §5d, different unit. `{document_id}` that
is malformed, unknown, or another tenant's is a plain **`404`** in all cases.

### The sources — `GET /knowledge/sources/`
```json
{
  "sources": {
    "file":    [ { "id": "…", "filename": "handbook.pdf", "source_type": "file",
                   "api_url": "", "is_active": true, "processing_status": "completed",
                   "section_count": 14, "edited_count": 2, "char_count": 16320,
                   "created_at": "…", "updated_at": "…" } ],
    "api":     [ … ],
    "website": [ … ]
  },
  "totals": {
    "file":    { "documents": 1, "sections": 14, "chars": 16320 },
    "api":     { "documents": 0, "sections": 0,  "chars": 0 },
    "website": { "documents": 0, "sections": 0,  "chars": 0 }
  }
}
```
All three keys are **always present**, empty or not, so a tab per source type
never reads `undefined`. A source with nothing indexed yet is still listed, with
`section_count: 0` and its `processing_status` — otherwise a failed upload would
vanish with no way to see or fix or delete it.

`char_count` is how much *text* the source contributed, which is not its file
size: a 4 MB PDF of scans contributes nothing.

### Read the page — `GET /knowledge/sources/{document_id}/`
```json
{
  "document": { "id": "…", "filename": "handbook.pdf", "source_type": "file",
                "api_url": "", "is_active": true, "processing_status": "completed",
                "created_at": "…", "updated_at": "…" },
  "content": "Refunds take 30 days from delivery. Shipping is free above …",
  "char_count": 16320,
  "section_count": 14,
  "edited_count": 2,
  "sections": [ … ]
}
```

**`content` is the whole source as one readable text — render that.** The server
has already done the stitching: knowledge is *stored* in overlapping ~1200-character
pieces (each needs its own embedding, and the overlap keeps a sentence that
straddles a boundary wholly inside *some* embedding), and joining those naively
would show the reader the same sentence twice at every boundary. `content` has
those repeats removed, exactly once each, in reading order. `char_count` is its
length — what a person actually reads.

```js
const page = await api(`/knowledge/sources/${documentId}/`);
textarea.value = page.content;          // that is the whole integration
```

`sections` is the same text as the pieces it was built from, each with its chunk
`id`, `position`, `char_count`, `was_edited` and a `lead` (how many of its opening
characters merely repeat the previous one). **Ignore it for a reading/editing
page** — it is there for an audit view that wants to see, or fix, one piece at a
time via `/knowledge/{id}/` (§5d).

### Save the page — `PUT /knowledge/sources/{document_id}/`
```js
await api(`/knowledge/sources/${documentId}/`, {
  method: "PUT",
  body: { content: textarea.value },
});
// 200 -> the same payload as the GET above, rebuilt from what you just saved
```
The text is re-chunked and re-embedded, and the old pieces are replaced — so the
bot *answers* from the new wording, not just displays it. Send the **whole**
text, not a diff; what you send is what the source becomes.

| | |
|---|---|
| `200` | saved — the response is the fresh page, re-read it into your editor |
| `400` | empty content, over **1,000,000 characters**, or an API-synced source (below) |
| `404` | not yours / no such source |
| `503` | the embedding provider is down — **nothing changed**, the old text is still live; offer a retry |

Read → save unchanged → read again returns exactly what you started with, so an
editor that saves on every keystroke never inflates the document.

> **API-synced sources are edited piece by piece, not as a page.** `PUT` on a
> source with `source_type: "api"` returns **`400`**. A sync tracks each record by
> its id and deliberately keeps the records a human corrected (`items_protected`,
> §5); rewriting the whole document throws those ids away, and the next sync would
> re-import every record *next to* your text instead of replacing it — the same
> facts, twice. For those sources, edit the section with `PATCH /knowledge/{id}/`
> (§5d) and the sync keeps protecting it. Hide or disable the page's editor when
> `document.source_type === "api"`.

A source whose upload **failed** to parse can be rescued this way: paste its text,
`PUT` it, and `processing_status` flips to `completed`.

### Delete the page — `DELETE /knowledge/sources/{document_id}/` → `204`
Removes the source and everything it taught the bot (its sections cascade).
Identical to `DELETE /documents/{id}/` — it is exposed here so the page has one
URL for read, save and delete.

---

## 6. Bot configuration (optional) — `GET` / `PATCH /chatbot-config/`
The bot works with **zero config** (it infers identity from the uploaded data).
Use this only to customise its presentation.
```json
// PATCH body — send only fields you want to change
{
  "assistant_name": "مساعد يونا",
  "company_name": "يونا",
  "tone": "friendly",            // friendly | formal | concise
  "default_language": "auto",
  "no_answer_message": "",       // optional static "no info" reply
  "top_k": null,                 // retrieval overrides (null = server default)
  "similarity_threshold": null
}
```
Grounding/safety rules are server-enforced and **not** configurable.

---

## 7. Usage analytics — `GET /analytics/usage/`
Returns the tenant's own rollups + live quota:
```json
{
  "tenant": { "id": "...", "username": "original_software" },
  "totals":     { "requests": 1280, "total_tokens": 3904000, "cost_usd": 12.64, "avg_response_ms": 1180, "confident_rate": 0.86 },
  "this_month": { ... },
  "today":      { ... },
  "quota": { "documents_used": 2, "max_documents": 100, "storage_mb_used": 0.06, "max_total_mb": 200, "tokens_this_month": 410000, "monthly_token_cap": 0, "max_requests_per_min": 60, "is_suspended": false }
}
```
Admins can pass `?scope=all` for a per-tenant breakdown.

### Points & plan — `GET /my-subscription/`
Everything that calls OpenAI is paid with **points**, at its **real cost**:
**1 point = $0.001**. Typical costs:

| Operation | Points |
|---|---|
| Chat answer (dashboard, API or WhatsApp) on gpt-4o | ~12 (gpt-4o-mini: < 1) |
| Indexing a document (upload / reindex) | ~1–2 per 10 pages |
| Knowledge edit / resolving an unanswered question | a fraction of a point |
| Website crawl | ~10 per 50 pages |
| API import | depends on the number of records |
| Guided-tree translation | ~1 per language for a small tree |

Each plan grants a fixed number of points per period (`plan.points`); a tenant
with **no plan** gets **300 points per calendar month**. Points **never roll
over** and can't be bought: an admin assigns the plan and renews it by hand
(§11). With **less than 1 point** left, or once the plan's **period has ended**,
every one of those operations returns **`402`** — show "out of points / plan
expired — contact the administrator". Reading (lists, tree, analytics) still works.

```json
{
  "subscription": { ..., "points_balance": 4966.028, "points_allowance": 5000,
                    "can_renew": false, "renewal_reason": null } | null,
  "on_free_tier": false,
  "points": { "balance": 4966.028, "allowance": 5000, "used": 33.972,
              "exempt": false, "exhausted": false, "usd_per_point": 0.001 },
  "renewal_reason": null,
  "usage": { "documents_used": 1, "documents_limit": 100, "storage_mb_limit": 200, ... }
}
```

- `points.balance` / `points.allowance` → "4,966 / 5,000 points" with a progress bar.
  `allowance` is this period's total: the plan's (or free tier's) points plus any
  admin top-up, so `used = allowance - balance`.
- `points.exhausted: true` → the account is stopped until the admin renews.
- `renewal_reason` → `"expired"` | `"points_exhausted"` | `null`: show a banner
  ("your plan has expired" / "you're out of points — contact the administrator").
- `points.exempt: true` means this user (admin/staff) never spends points —
  their usage is internal testing; show "unlimited" instead of a counter.
- The old `credits` block (`questions_left`, `credits_per_question`) is gone.

Enforcement is **strict and race-safe**: a chat answer **holds** up to 20 points
atomically *before* it is generated (parallel requests can never exceed the
balance), then is **settled to its real cost** — or released in full if the
answer fails (a `503` never costs anything).

### Where the points went — `GET /points-history/`
One row per metered operation, newest first (paginated; `?kind=` filters by
`chat`, `document`, `knowledge`, `crawl`, `api_sync`, `translation`):
```json
{ "count": 6, "next": null, "previous": null, "results": [
  { "id": "…", "kind": "chat", "model": "gpt-4o", "tokens_in": 3414, "tokens_out": 280,
    "cost_usd": 0.011302, "points": 11.302, "created_at": "2026-10-08T15:55:22Z" } ] }
```
Admins can pass `?user=<id>`; a tenant asking for someone else gets `403`.

**Free-tier limits** (a signup with no paid plan): **1 document, ~0.5 MB total**
(one small Word file), and **no external-API import** (§5 returns `402`).
Uploading a 2nd file or a larger one returns `413`. Plans lift these via
their `max_documents` / `max_total_mb` / `allow_api_sync`.

### Billing is manual
There is **no checkout**: the admin creates the plan, creates the user, assigns
the plan and renews it (§11). An "Upgrade" or "Renew" action in a tenant UI
should tell the user to contact the administrator. (The Paddle webhook still
exists for a future self-serve checkout but is not used.)

---

## 8. Unanswered questions (knowledge gaps)

When the bot can't confidently answer (`confident: false`), the question is
captured for that tenant as a **knowledge gap** — after an AI filter drops
noise (greetings, chit-chat) and de-duplicates repeats. Build a review UI so
the tenant can see what customers ask that the bot can't answer yet, and fill
those gaps.

> No create endpoint — rows are produced automatically by the chat pipeline.
> All endpoints are scoped to the authenticated tenant.

### List — `GET /unanswered/`
Paginated, most-frequent first. Each item:
```json
{
  "id": "9c1e...",
  "question": "Do you offer refunds on annual plans?",
  "question_key": "do you offer refunds on annual plans",
  "language": "en",
  "reason": "asks about refund policy",
  "status": "new",              // new | reviewed | answered | dismissed
  "occurrences": 4,
  "last_asked_at": "2026-07-12T09:10:00Z",
  "created_at": "2026-07-10T14:00:00Z"
}
```
| Field | Meaning |
|-------|---------|
| `occurrences` | How many times this gap was asked (dedup counter). Sort by this to prioritise. |
| `reason` | Why the AI filter kept it as a real question. |
| `status` | Review state you control (see below). |

Filters: `?status=new`, `?language=ar`, `?search=<text>`,
`?ordering=-occurrences` (also `last_asked_at`, `created_at`).

### Retrieve — `GET /unanswered/{id}/`

### Re-triage — `PATCH /unanswered/{id}/`
Only `status` is writable:
```json
{ "status": "reviewed" }
```

### Resolve into knowledge — `POST /unanswered/{id}/resolve/`
Provide an answer and it is **embedded into the tenant's knowledge**, so the bot
retrieves it the next time the question is asked; the gap is marked `answered`.
```json
{ "answer": "Yes, we refund annual plans within 14 days of purchase." }
```
Send an **empty body** to just mark it `answered` without adding knowledge.
Returns the updated row. (May return `503` if embedding is temporarily
unavailable — see §10.)

### Dismiss — `POST /unanswered/{id}/dismiss/`
Marks the gap `dismissed` (not worth answering).

### Delete — `DELETE /unanswered/{id}/`  → `204`

---

## 9. Account settings (the logged-in user)

Self-service account management for the authenticated user. Build a "Settings"
page from these.

### Get my profile — `GET /users/me/`
Returns the current user: `{ id, username, email, email_verified, first_name, last_name, role, api_key, is_active, date_joined }`.

> `email_verified` is `false` until the user confirms their email. New accounts
> verify at signup via the activation link (§2, which also flips `is_active` to
> `true`); a logged-in user changing their email verifies the new address via the
> flow below. Show a "verify your email" banner while it's `false`.

### Edit name / username — `PATCH /users/me/`
```json
{ "first_name": "Alice", "last_name": "Smith", "username": "alice" }
```
Send any subset. Username must stay unique (400 if taken). **Email is not
editable here** — it changes only through the verified flow below. The API key
is never editable by the user. Returns the updated user, so refresh whatever
copy of `me` you cache (name/username may have changed).

### Change password (verified, two steps)
A password change requires the current password **and** a code emailed to the
user — a second factor proving they control the account's email.

**Step 1 — request a code:** `POST /users/request-password-code/`  (empty body)
Emails a **6-digit code** to the user's email (valid ~15 min). `400` if the
account has no email.

**Step 2 — change:** `POST /users/change-password/`
```json
{ "old_password": "...", "new_password": "...", "new_password_confirm": "...", "code": "123456" }
```
Requires the current password **and** the code. `200` on success; `400` if the
old password is wrong, the code is missing/wrong/expired, or the new password
fails validation (or after 5 wrong code attempts — request a new one).

```js
// Settings UI: password change flow
await api("/users/request-password-code/", { method: "POST" });
// ...user reads the code from their inbox...
await api("/users/change-password/", { method: "POST",
  body: { old_password, new_password, new_password_confirm, code } });
```

### Change email (verified, two steps)
Email is never set directly — the user must prove they control the new address.

**Step 1 — request a code:** `POST /users/change-email/`
```json
{ "new_email": "new@example.com" }
```
Sends a **6-digit code** to the new address (valid ~15 min). The account email
is **not** changed yet. `400` if the address is already in use or is the current
one. Calling this again (a "resend") sends a fresh code and **invalidates the
previous one** — always verify with the latest code.

**Step 2 — confirm:** `POST /users/verify-email/`
```json
{ "code": "123456" }
```
On success (`200`) the email is updated and the **updated user is returned**
(refresh your cached `me`). `400` for a wrong/expired code, or after **5 wrong
attempts** (the code locks — have the user request a new one via step 1).

```js
// Settings UI: email change flow
await api("/users/change-email/", { method: "POST", body: { new_email } });
// ...user reads the code from their inbox and types it...
const updated = await api("/users/verify-email/", { method: "POST", body: { code } });
// updated.email is now the new address — update your local user state.
```

### View my API key — `GET /users/api-key/`
Read-only. Users **cannot** rotate their own key (only an admin can). Don't show
a "regenerate" button in the user settings UI.

> **Paid feature.** Free-tier users get **`402`** here, and `api_key` is `null`
> in `GET /users/me/` and in the login response. Hide the API-key section for
> them and show a "not on your plan — contact the administrator" note instead
> (check the plan via `/my-subscription/` and `GET /my-features/`).

---

## 10. Error handling

Errors return JSON `{ "detail": "..." }` (or field errors for 400 validation).

| Status | Meaning | Frontend action |
|--------|---------|-----------------|
| `400` | Bad request / validation | Show the field message(s). |
| `401` | Missing/expired token | Refresh token (§2) or send to login. |
| `402` | Out of points, plan expired, or a feature not on the plan | Show the `detail` and "contact the administrator" (§7). |
| `403` | Tenant suspended / not allowed | Show "account suspended / no access". |
| `404` | Not found — or not yours | Treat as not found; never reveal another tenant's data. |
| `409` | Conflict (e.g. renewing a subscription that is still running with points left) | Show the `detail`. |
| `413` | Document/storage quota exceeded | Tell the user to delete docs or upgrade. |
| `429` | Rate limit or monthly token budget hit | Back off / show "try again shortly". |
| `503` | RAG backend unavailable (LLM/provider) | Show "service busy, retry". |

### Rate limits (the `429`s you can actually hit)

Only these endpoints are throttled; everything else is limited by the tenant's
per-minute request cap (`quota.max_requests_per_min`, §7).

| Endpoint | Limit |
|---|---|
| `POST /auth/login/` | 10/min |
| `POST /auth/register/` | 5/min |
| `POST /chat/` | 60/min |
| `POST /sync-api-content/`, `POST /crawl-website/` | 10/min (shared) |
| `POST /documents/`, `POST /documents/upload-word/`, `POST /documents/{id}/reindex/` | 30/min (shared) |
| `POST /users/change-email/`, `POST /users/request-password-code/` | 5/min (shared) |

Back off and retry; these are per-IP/tenant and reset within the minute.

Example handling:
```js
const res = await fetch(...);
if (!res.ok) {
  const err = await res.json().catch(() => ({}));
  if (res.status === 401) { /* refresh or re-login */ }
  else if (res.status === 429) { /* show rate-limit toast */ }
  else { showError(err.detail || "Something went wrong"); }
  return;
}
```

---

## 11. Admin endpoints (only if you build an admin panel)

Require an **admin** token. Highlights:
- `POST /users/` — create a tenant (returns the new user + `api_key`). **Email
  verification is required:** with an email, the account is created **inactive**
  (`is_active: false`, cannot log in) and an activation link is emailed; the
  response includes `email_verification` (`activation_sent` | `send_failed` |
  `not_sent`). The user activates via the public link (§2). A no-email account
  stays active.
- `POST /users/{id}/resend-activation/` — re-send the activation link to an
  inactive user (first email failed, or the link expired).
- `GET /users/` — list tenants (each includes its `api_key`).
- `POST /users/{id}/regenerate-api-key/` — rotate a user's key (admin only; users can't rotate their own).
- `POST /users/{id}/set-password/` — admin resets a user's password.
- `PATCH /users/{id}/` — update a tenant. Deactivating (`is_active: false`) your
  own admin account is refused (**`400`**, "Admin/staff accounts cannot be
  deactivated") so an admin can't lock themselves out. (Recovery if it ever
  happens on the server: `python manage.py reactivate_admin`.)
- `DELETE /users/{id}/` — delete a tenant and everything they own (documents,
  knowledge, API key, subscription — cascading, with no undo). Deleting **your
  own account** is refused with **`400`**.

### Admins cannot manage other admins

**Admin is one flat role** — every admin reaches every endpoint in this section.
So anything that would let one admin take over another's account is refused with
**`403`** "Admin accounts can only be managed by their owner":

| Endpoint | On a tenant | On another admin | On yourself |
|---|---|---|---|
| `PATCH /users/{id}/` | ✅ | **`403`** | ✅ |
| `DELETE /users/{id}/` | ✅ | **`403`** | `400` |
| `POST /users/{id}/set-password/` | ✅ | **`403`** | ✅ |
| `GET /users/{id}/api-key/` | ✅ | **`403`** | ✅ |
| `POST /users/{id}/regenerate-api-key/` | ✅ | **`403`** | ✅ |

Two quieter places follow the same rule, by hiding rather than refusing:

* `GET /users/` embeds each row's `api_key` — it is **`null`** for other admins
  (a key authenticates as its owner, so printing it hands over the account).
* `GET /api-keys/` and its `revoke`/`activate`/`regenerate` actions **do not list
  other admins' keys at all**; those ids return **`404`**, like any id that isn't
  yours.

For the UI: disable these actions on rows whose `role` is `admin` (and delete on
your own row), with the reason as the tooltip, rather than letting the call fail.
An admin account that genuinely must be changed by someone else is changed from
the Django admin.
- `GET /api-keys/`, `POST /api-keys/{id}/revoke|activate|regenerate/` — key control.

**Per-user feature control (admin) — turn features on/off for a specific tenant:**
Features are gated capabilities (`api_sync`, `website_crawl`, `api_key`,
`guided_tree`, `whatsapp`). Each resolves as: **staff → always on**, else an
admin **override**, else the **plan/tier default**. So you can grant a free user a
paid feature — or revoke one — without changing their plan.
- `GET /feature-catalog/` — list of controllable features `[{ key, label, description }]`
  (any authenticated user; use it to render the dashboard toggles).
- `GET /my-features/` — the caller's own resolved features
  `{ features: { api_sync: { enabled, source, label }, … } }` (`source` = `staff` | `override` | `default`).
  Use it in the frontend to show/hide UI.
- `GET /user-features/{user_id}/` — a user's resolved features **plus** their raw `overrides` (admin).
- `PATCH /user-features/{user_id}/` — set overrides (admin):
  ```json
  { "overrides": { "api_sync": true, "website_crawl": true, "api_key": false, "guided_tree": null } }
  ```
  `true`/`false` force it; **`null` clears** the override (inherit the default). Unknown keys are ignored.
  When a feature is off, its endpoint returns **`402`**.

**Plans & billing (admin) — billing is manual:** create the plan → create the
user (`POST /users/`) → assign the plan → renew it when it ends or runs out.
- `GET /plans/` — anyone authenticated can list active plans. Admin `POST` /
  `PATCH` / `DELETE /plans/` manage them: set `name`, `price_usd`, **`points`**,
  `max_documents`, `max_total_mb`, `llm_model`, `is_active`, `allow_api_sync`.
  **Rule of thumb: `points = price_usd × 250`** (25% of the price goes to
  OpenAI: a $20 plan → 5,000 points ≈ 410 gpt-4o answers). `monthly_questions` /
  `monthly_token_cap` are optional extra caps (`0` = off). The old `questions` /
  `included_credits` fields are gone.
- `POST /subscriptions/` — assign/move a user to a plan (`{ user, plan, duration_days }`).
  **The wallet is reset to the plan's `points`** (no rollover; a downgrade
  shrinks it, an upgrade grows it). Storage/document limits and features follow
  the new plan immediately. A plan with `points: 0` leaves the balance untouched.
- `GET /subscriptions/` — list all subscriptions. Each row has `points_balance`,
  `points_allowance`, **`can_renew`** and `renewal_reason` (`"expired"` |
  `"points_exhausted"` | `null`).
- `POST /subscriptions/{id}/renew/` — renew on the same plan (`{ duration_days? }`,
  default = the previous period's length): a new period from now and the points
  reset to the plan's. **Only when `can_renew` is true** — the period ended or
  the points ran out; otherwise **`409`** `{"code": "not_renewable"}`. Show the
  Renew button only on those rows.
- `POST /subscriptions/add-points/` — add points to a wallet (`{ user, amount }`)
  as a manual correction; returns `{ user, points_balance }`. Lost at the next
  renewal (no rollover).
- `GET /points-history/?user=<id>` — any tenant's points history (§7).
- `POST /billing/paddle/webhook/` — not used while billing is manual.

**WhatsApp — a tenant connects its own number (Embedded Signup):**
- `GET /whatsapp/connection/` — the caller's number and what the button needs:
  ```json
  { "connected": true, "allowed": true,
    "account": { "id": "…", "phone_number_id": "800…", "display_phone_number": "+20 100 000 0000",
                 "display_name": "Tea Shop", "waba_id": "900…", "connection": "embedded_signup",
                 "is_active": true, "last_error": "" },
    "signup": { "enabled": true, "app_id": "…", "config_id": "…", "graph_version": "v23.0" } }
  ```
  Show "Connect WhatsApp" only when `allowed && signup.enabled`. The button loads
  the Facebook JS SDK, calls `FB.login({ config_id, response_type: "code",
  override_default_response_type: true, extras: { setup: {}, featureType: "",
  sessionInfoVersion: "3" } })` and listens for the `WA_EMBEDDED_SIGNUP`
  `FINISH` postMessage (`waba_id`, `phone_number_id`).
- `POST /whatsapp/connection/embedded-signup/` `{ code, waba_id, phone_number_id }`
  — the server exchanges the code for the tenant's own token (stored encrypted),
  checks the number is in that business account, subscribes the app and
  registers the number. Returns the same shape as `GET`, plus `warning` (`""`,
  or a Meta problem that didn't stop the connection, also kept in
  `account.last_error`). Errors carry a `code`: `402 feature_disabled`,
  `409 number_taken` (another account has it), `400 number_not_in_waba`,
  `502 meta_error` / `meta_unreachable`, `503 not_configured`.
- `POST /whatsapp/connection/disconnect/` — stop the bot on the number
  (conversations are kept); `400 not_connected` when there is none.

**WhatsApp:**
- `GET/POST/PATCH/DELETE /whatsapp/accounts/` (admin) — link a tenant to its
  WhatsApp Business number (`tenant`, `phone_number_id`, `access_token`,
  `display_name`; read-only `connection`, `waba_id`, `display_phone_number`,
  `last_error`). Customer messages to that number are answered from that
  tenant's knowledge (with the last turns of the conversation as context),
  charged to its points, and replied to **from that number**. A message Meta
  delivers twice is answered once.
- `POST /whatsapp/send/` `{ to_number, message }` — sends from the **caller's own**
  connected number (`400` when it has none). Admins may add `account` (an
  accounts id) to send from a tenant's number; anyone else gets `403`. Meta only
  delivers free-form text within 24 h of the customer's last message.
- `GET /whatsapp/users/`, `/sessions/`, `/messages/` — read-only, scoped to the
  caller's number. Each message appears once; a reply that couldn't be sent has
  `status: "failed"` and an `error_message`.

---

## 12. Minimal client wrapper (copy-paste starter)

```js
const BASE = "https://chatbot-api.zaychain.com/api/v1";
let token = localStorage.getItem("access");

async function api(path, { method = "GET", body, isForm } = {}) {
  const headers = { "Authorization": `Bearer ${token}` };
  if (!isForm) headers["Content-Type"] = "application/json";
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || "API error"), { status: res.status, data });
  return data;
}

// Usage:
await api("/auth/login/", { method: "POST", body: { username, password } });
await api("/chat/", { method: "POST", body: { question: "مرحبا", history: [] } });
await api("/documents/");

// Review and fix what the bot learned — a whole source, as one page:
const { sources } = await api("/knowledge/sources/");
const doc = sources.file[0];
const page = await api(`/knowledge/sources/${doc.id}/`);
await api(`/knowledge/sources/${doc.id}/`, {
  method: "PUT",
  body: { content: page.content.replace("30 days", "14 days") },
});
// ...or one piece at a time (and the only way for API-synced sources):
const { results } = await api("/knowledge/?search=refund");
await api(`/knowledge/${results[0].id}/`, {
  method: "PATCH",
  body: { content: "Refunds are processed within 14 days." },
});
```
