# Deploying to a Hostinger VPS (Docker Compose)

Runs the whole stack on one VPS: **Django (gunicorn)** + **Postgres/pgvector** +
**Redis** + **Celery worker** + **nginx**. Postgres lives on the same VPS.

```
            ┌──────── nginx :80/:443 ────────┐
 Internet ─▶│  /static /media  →  files       │
            │  everything else →  web:8000     │
            └───────────────┬─────────────────┘
                            │
                    ┌───────▼────────┐   enqueue   ┌──────────────┐
                    │  web (gunicorn)│────────────▶│ redis (broker)│
                    └───────┬────────┘             └──────┬───────┘
                            │                              │
                    ┌───────▼────────┐             ┌───────▼────────┐
                    │ db (pgvector)  │◀────────────│ worker (celery)│
                    └────────────────┘             └────────────────┘
```

---

## 1. One-time VPS setup (Ubuntu)

SSH into the VPS, then install Docker + Compose plugin:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER     # log out/in so docker runs without sudo
docker compose version            # verify the v2 plugin is present
```

Open the firewall for web traffic:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

---

## 2. Get the code + configure

```bash
git clone <your-repo-url> chatbot && cd chatbot
cp .env.example .env
nano .env        # fill in EVERYTHING (see checklist below)
```

`.env` checklist (the must-change values):
- `SECRET_KEY` — `python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `DEBUG=False`
- `ALLOWED_HOSTS` — your domain + VPS IP
- `CSRF_TRUSTED_ORIGINS` — `https://your-domain.com`
- `POSTGRES_PASSWORD` and the matching password inside `DATABASE_URL`
- `OPENAI_API_KEY`
- Keep `INGESTION_MODE=celery` and `RAG_VECTOR_BACKEND=pgvector`

---

## 3. Build & start

```bash
docker compose up -d --build
docker compose ps          # all services should be "Up"/"healthy"
docker compose logs -f web # watch migrations + collectstatic run once
```

On first boot the `web` container automatically:
1. runs `migrate`,
2. runs `collectstatic`,
3. runs `setup_pgvector` (creates the extension + HNSW index).

Create the first admin user:

```bash
docker compose exec web python manage.py createsuperuser
```

The API is now live on `http://<VPS-IP>/api/v1/` and admin on `/admin/`.

---

## 4. Enable HTTPS (recommended)

Point your domain's A record at the VPS IP, then issue a certificate. Simplest
path with this layout — use the host's certbot and bind-mount the certs:

```bash
sudo apt install certbot
sudo certbot certonly --standalone -d your-domain.com   # stop nginx first or use webroot
```

Then in `docker-compose.yml` uncomment the `443` port and add:
```yaml
  nginx:
    volumes:
      - /etc/letsencrypt:/etc/letsencrypt:ro
```
Uncomment the HTTPS `server { ... }` block in `docker/nginx/default.conf`,
set your domain, and in `.env` set `SECURE_SSL_REDIRECT=True` and
`SECURE_HSTS_SECONDS=31536000`. Recreate: `docker compose up -d`.

---

## 5. Day-to-day operations

```bash
# Update to the latest code
git pull && docker compose up -d --build

# Logs
docker compose logs -f web
docker compose logs -f worker

# Django management commands
docker compose exec web python manage.py <command>

# Re-backfill pgvector after a very large ingest
docker compose exec web python manage.py setup_pgvector

# Run the test suite inside the image
docker compose exec web python manage.py test knowledge.tests
```

### Backups (Postgres)

```bash
# Backup
docker compose exec db pg_dump -U chatbot chatbot > backup_$(date +%F).sql
# Restore
cat backup.sql | docker compose exec -T db psql -U chatbot chatbot
```

Media files live in the `media_volume` Docker volume — include it in your
backup routine if tenants upload original files.

---

## 6. How background ingestion works in prod

With `INGESTION_MODE=celery`:
1. `POST /documents/` (or `upload-word/`) returns immediately; the document is
   `pending`.
2. The Celery `worker` picks up the task → parses, chunks, embeds → status
   becomes `completed` (or `failed` with an error message).
3. Poll `GET /documents/<id>/` to watch `processing_status` and `chunk_count`.

The same setting governs `POST /sync-api-content/`:
1. The call returns `202` with a `run_id` and a `status_url`; the run is `queued`.
2. The worker fetches every page, describes each record with the LLM, and embeds.
3. Poll `GET /sync-api-content/runs/<run_id>/` until `done` is `true`.

This one is not optional at any real size: an API import makes **one LLM call
per record**, so a few hundred records run for minutes. Left in-request it
exceeds both `gunicorn --timeout 120` and nginx's `proxy_read_timeout 120s`, and
the tenant gets a **504** while gunicorn kills the worker mid-import. Keep
`INGESTION_MODE=celery` and a `worker` container running in production.

Scale throughput by raising the worker concurrency (`--concurrency`) in
`docker-compose.yml`, or run more `worker` replicas.

---

## 7. Sizing notes for a single Hostinger VPS

- **2 vCPU / 4 GB RAM** comfortably runs this stack for small/medium tenants.
- Postgres + pgvector keeps vector search fast as data grows (HNSW index).
- Per-question LLM cost is constant regardless of corpus size (only top-K chunks
  are sent), so tenants can grow their knowledge base without rising per-answer cost.
- If RAM is tight, lower gunicorn `--workers` and Celery `--concurrency`.
