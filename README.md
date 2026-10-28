# ChatBot API

Django REST Framework API powering a knowledge-grounded chatbot, with a
modern RAG pipeline and a WhatsApp Cloud API integration.

## Stack

- Django 5.x + Django REST Framework
- JWT auth (`djangorestframework-simplejwt` with token blacklist)
- `django-filter`, `drf-spectacular`, OpenAPI 3 docs
- **RAG pipeline**: Q&A bank → document retrieval → fallback
  - Embeddings: OpenAI `text-embedding-3-large` (multilingual)
  - LLM: configurable (`gpt-4o`, `claude-sonnet-4-6`, …)
  - Storage: persisted `DocumentChunk` rows with JSON embeddings,
    cosine search via numpy (swap for pgvector when you outgrow this)
- WhatsApp Cloud API integration (webhook + send)

## How the chatbot answers a question

```
question
   │
   ├─► (1) Q&A bank — embedding match against QuestionAnswer
   │       above QA_BANK_THRESHOLD (default 0.82) ──► return curated answer
   │
   ├─► (2) RAG over DocumentChunk — top-k chunks above RAG_SIMILARITY_THRESHOLD
   │       (default 0.45) ──► LLM with strict "answer only from context" prompt
   │
   └─► (3) Fallback ──► polite "I don't have enough information" reply
```

Every reply carries its `source` (`qa_bank` / `rag` / `fallback`) and the
matching sources, so the client can render confidence/citations and post
👍/👎 feedback to `/api/v1/chat/feedback/`.

## How to "train" the bot on your data

1. **Add Q&A pairs** via `POST /api/v1/questions/` — embeddings are
   generated automatically by a post-save signal. These are the
   highest-priority answers (the chat pipeline checks them first).
2. **Upload reference docs** via `POST /api/v1/documents/` (multipart).
   Each upload is parsed → chunked → embedded → stored in `DocumentChunk`
   synchronously. The next chat request retrieves from the new chunks.
3. **Backfill** for rows that were missing embeddings:
   ```bash
   python manage.py embed_qa            # missing only
   python manage.py embed_qa --all      # re-embed everything
   python manage.py reindex_documents   # PENDING / FAILED docs
   ```

There is no fine-tuning step. Putting authoritative Q&A in the bank +
clean reference documents in the doc store is the modern best practice
and gives you full control over what the bot says.

## Project layout

```
ChatBotApi/
├── Conf/                     Django project (settings, urls, wsgi/asgi)
├── knowledge/                Q&A, tree, languages, documents, chat, analytics
│   ├── models.py             QuestionAnswer, SimpleQuestionTree,
│   │                         AvailableLanguage, UploadedDocument,
│   │                         DocumentChunk, ChatFeedback
│   ├── filters.py
│   ├── permissions.py
│   ├── signals.py            post-save → auto-embed Q&A
│   ├── serializers/          split per resource
│   ├── views/                split per resource
│   ├── services/
│   │   ├── embeddings.py     provider-agnostic embedding client
│   │   ├── llm.py            provider-agnostic chat client (openai|anthropic)
│   │   ├── retrieval.py      Q&A + chunk cosine search (numpy)
│   │   ├── chunking.py       parse + chunk + embed + persist
│   │   ├── rag.py            answer_question() — Q&A → RAG → fallback
│   │   └── excel_import.py
│   ├── management/commands/  embed_qa, reindex_documents
│   └── urls.py               DRF router + APIViews
├── WhatsApp/                 Meta WhatsApp Cloud API integration
├── api-docs.html             Standalone HTML reference
├── samples/                  Demo .docx + walkthrough
├── manage.py
├── requirements.txt
└── .env.example
```

## Getting started

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows
pip install -r requirements.txt

cp .env.example .env              # set SECRET_KEY, OPENAI_API_KEY, …

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## API surface

All endpoints are mounted under `/api/v1/`.

| Endpoint                            | Description                                |
|-------------------------------------|--------------------------------------------|
| `POST /auth/login/`                 | Obtain a JWT token pair                    |
| `POST /auth/refresh/`               | Refresh an access token                    |
| `POST /auth/verify/`                | Verify an access token                     |
| `POST /users/register/`             | Register a new user                        |
| `GET  /users/me/`                   | Current authenticated user                 |
| `*    /questions/`                  | Q&A bank CRUD + `most-asked`, `bulk-update`, `increment-count` |
| `*    /question-tree/`              | Hierarchical tree CRUD + `tree`, `children`|
| `*    /languages/`                  | Available languages                        |
| `*    /documents/`                  | Doc upload (auto-ingest) + `reindex`       |
| `POST /imports/excel/`              | Bulk-import Q&A from .xlsx                 |
| `POST /chat/`                       | Q&A bank → RAG → fallback chat             |
| `POST /chat/feedback/`              | 👍/👎 feedback on an answer                |
| `POST /search/`                     | LIKE search across the Q&A bank            |
| `GET  /analytics/`                  | Aggregated stats incl. chunks + feedback   |
| `*    /whatsapp/users\|sessions\|messages\|analytics/` | Read-only WhatsApp data |
| `POST /whatsapp/send/`              | Send a free-form WhatsApp text             |
| `GET/POST /whatsapp/webhook/`       | Meta webhook verify / receiver             |

OpenAPI schema at `/api/schema/`, Swagger UI at `/api/docs/`, ReDoc at
`/api/redoc/`. A standalone, browsable reference also lives in
`api-docs.html` at the project root.

## Configuration knobs (env)

| Variable                   | Default                       | Notes |
|----------------------------|-------------------------------|-------|
| `EMBEDDING_PROVIDER`       | `openai`                      |       |
| `EMBEDDING_MODEL`          | `text-embedding-3-large`      | use `-small` for cheaper, lower quality |
| `LLM_PROVIDER`             | `openai`                      | `openai` or `anthropic` |
| `LLM_MODEL`                | `gpt-4o`                      | e.g. `claude-sonnet-4-6` |
| `QA_BANK_THRESHOLD`        | `0.82`                        | cosine threshold for the curated bank |
| `RAG_SIMILARITY_THRESHOLD` | `0.45`                        | cosine threshold for chunk retrieval |
| `DATABASE_URL`             | (sqlite)                      | `postgres://…` switches to Postgres |

## Roadmap (next leverage)

- **pgvector** — drop-in for the JSON embedding storage when datasets grow
- **Hybrid retrieval** — combine BM25 (Postgres FTS) with dense + RRF fusion
- **Reranker** — Cohere Rerank or a local cross-encoder for the top-k
- **Semantic cache** — Redis-backed cache keyed on question embedding
- **Streaming** — SSE for incremental chat responses
