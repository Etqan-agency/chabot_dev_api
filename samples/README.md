# Sample documents and a quick test walkthrough

Two demo documents about a fictional company (**Acme Robotics**) plus a
small set of sample questions you can use to exercise the full chat
pipeline.

| File                       | Topics covered                                           |
|----------------------------|----------------------------------------------------------|
| `company_handbook.docx`    | Pricing, lead times, support contacts, returns, plans    |
| `installation_guide.docx`  | Box contents, site requirements, first boot, troubleshooting |

The matching `.md` files are the source content (and `_build_docx.py`
re-renders them to `.docx` if you edit the markdown).

## End-to-end test

Set these once in your shell so the snippets below are copy-paste-able:

```bash
export BASE=http://localhost:8000
export USER=admin
export PASS=your-superuser-password
```

### 1. Get a JWT

```bash
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}" \
  | python -c "import sys,json; print(json.load(sys.stdin)['access'])")
echo $TOKEN
```

### 2. Upload both documents (auto-ingested)

```bash
curl -X POST $BASE/api/v1/documents/ \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@samples/company_handbook.docx"

curl -X POST $BASE/api/v1/documents/ \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@samples/installation_guide.docx"
```

Each response includes `processing_status: "completed"` and a
`chunk_count` once ingestion finishes (usually under a couple of
seconds for these short docs).

### 3. (Optional) Seed the Q&A bank

This proves the **Q&A bank → RAG** priority order: the bank answer
should win even though the doc would also retrieve.

```bash
curl -X POST $BASE/api/v1/questions/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "How long is the Atlas-1 warranty?",
    "answer": "The Atlas-1 ships with a 24-month limited warranty.",
    "answer_type": "single"
  }'
```

The post-save signal embeds it automatically.

### 4. Ask the bot

```bash
ask () {
  curl -s -X POST $BASE/api/v1/chat/ \
    -H "Content-Type: application/json" \
    -d "{\"question\":\"$1\"}" | python -m json.tool
}

ask "How much does the Atlas-2 Pro cost?"
ask "What's the lead time for Atlas-1?"
ask "What do I do if the robot won't charge on its dock?"
ask "Who owns the company headquarters and where?"
ask "Do you ship to the moon?"        # should hit the fallback path
```

Each reply includes a `source` field — `qa_bank`, `rag`, or `fallback`
— plus the matching `sources` (Q&A ids or document chunks).

### 5. Send feedback on the answer

```bash
curl -X POST $BASE/api/v1/chat/feedback/ \
  -H "Content-Type: application/json" \
  -d '{
    "question": "How much does the Atlas-2 Pro cost?",
    "answer": "The Atlas-2 Pro retails for €18,900.",
    "source": "rag",
    "rating": "up"
  }'
```

### 6. Inspect via analytics

```bash
curl -s $BASE/api/v1/analytics/ \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

You should see `total_chunks` reflect the chunks created from both
docs and `feedback_summary` showing the rating you just submitted.
