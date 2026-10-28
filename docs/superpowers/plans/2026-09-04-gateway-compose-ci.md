# Gateway, Compose and CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the deployment meta-repo's own contents — a Traefik v3 gateway,
a production compose file describing the whole stack, continuous integration
publishing images to GHCR, and the `sync-to-meta` workflows that make a meta-repo
commit the unit of deployment.

**Architecture:** All deployment configuration lives in the meta-repo
(`Etqan-agency/chatbot`); the two component repos gain only a `sync-to-meta`
workflow each. Nothing here deploys to a server — `deploy.sh`, `rollback.sh`,
`cd.yml` and the cutover are Plan 3. This plan ends when the full stack runs
locally under compose and CI is green with images published to GHCR.

**Tech Stack:** Traefik v3 · Docker Compose · GitHub Actions · GHCR ·
pgvector/pgvector:pg16 · redis:7-alpine · nginx:alpine

**Spec:** `chatbot_api/docs/superpowers/specs/2026-09-03-infrastructure-pipeline-design.md`
(sections §4.1, §4.3, §5.1, §5.2)

**Predecessor:** `chatbot_api/docs/superpowers/plans/2026-09-03-component-readiness.md`
(complete and merged — both components now build hardened production images)

## Global Constraints

- **`${DOMAIN}` stays parameterized.** No real hostname is hardcoded anywhere. It
  is an `.env` value on the server and a GitHub Actions **variable** in CI.
  Hostnames are always `api.${DOMAIN}` and `app.${DOMAIN}`.
- **GHCR image names must be lowercase:** `ghcr.io/etqan-agency/chatbot/api` and
  `ghcr.io/etqan-agency/chatbot/dashboard`. `IMAGE_PREFIX = etqan-agency/chatbot`.
- **Docker network name is pinned to `chatbot_default`.** `traefik.yml`'s
  `providers.docker.network` references it by name; if the compose project name
  changes, the file must change with it.
- **A `media` service is mandatory.** The dashboard's nginx resolves `media:80` at
  parse time and will not start without it. Django serves `/media/` only under
  `DEBUG`.
- **`ALLOWED_HOSTS` must contain `api.${DOMAIN}`, `app.${DOMAIN}` and `localhost`.**
  The dashboard proxies with `Host: app.${DOMAIN}`; the container `HEALTHCHECK`
  probes over loopback.
- Secrets are never written to disk in this repo. Workflows reference secret
  names only; the repository owner populates them.
- The API image already exposes a drain-aware `/health/` returning 503 while
  `DRAIN_FILE` (`/tmp/drain`) exists. Plan 3's rolling deploy depends on it; this
  plan must wire Traefik's health check to it correctly.

---

## File Structure

**Meta-repo — `/home/abdulkhalek/Projects/chatbot` (`Etqan-agency/chatbot`):**

| Path | Responsibility |
|------|----------------|
| `.gitignore` | Create — ignore local scratch (`home.png`, `.playwright-mcp/`, `.env`) |
| `traefik/traefik.yml` | Create — static config: entrypoints, ACME, providers, container-local API |
| `traefik/dynamic/middlewares.yml` | Create — hot-reloading middleware definitions |
| `docker-compose.production.yml` | Create — the whole stack, one file |
| `.env.example` | Create — every variable the compose file reads |
| `.github/workflows/ci.yml` | Create — lint, test, build, publish |
| `README.md` | Modify — replace "not yet implemented" with what now exists |

**Component repos:**

| Path | Responsibility |
|------|----------------|
| `chatbot_api/.github/workflows/sync-to-meta.yml` | Create — bump the API submodule pointer |
| `chatbot_dashboard/.github/workflows/sync-to-meta.yml` | Create — bump the dashboard pointer |

**Why one compose file rather than a base plus overrides:** the stack has exactly
one deployment target. A base/override split would add indirection to describe a
single environment. The one local-only concern (ACME staging) is handled by an
environment variable, not a second file.

---

### Task 1: Meta-repo working copy and Traefik configuration

Turns the existing `Projects/chatbot` directory into the meta-repo's working copy,
so the local layout matches the deployed one, then adds the gateway configuration.

**Files:**
- Create: `chatbot/.gitignore`, `chatbot/traefik/traefik.yml`, `chatbot/traefik/dynamic/middlewares.yml`

**Interfaces:**
- Consumes: nothing from earlier plans.
- Produces: a Traefik container that starts, serves its container-local API on
  `127.0.0.1:8080`, and exposes `/api/http/services/` — the endpoint Plan 3's
  `rollout.sh` health-gates against. Task 2's compose file mounts these files.

- [ ] **Step 1: Verify both component checkouts are clean and pushed**

```bash
cd /home/abdulkhalek/Projects/chatbot/chatbot_api && git status --short && git log --oneline -1
cd ../chatbot_dashboard && git status --short && git log --oneline -1
```

Expected: no output from either `status`, and both logs at the merge commit
(`414d656` / `873a987`). Do not continue if either has uncommitted work — the next
step writes into their parent directory and you want a clean point to return to.

- [ ] **Step 2: Attach the meta-repo to the existing directory**

```bash
cd /home/abdulkhalek/Projects/chatbot
git init -q -b main
git remote add origin git@github.com:Etqan-agency/chatbot.git
git fetch -q origin
git checkout main
```

`git checkout main` writes only `README.md` and `.gitmodules`. The two submodule
paths are **gitlinks**: git does not populate or modify them, and the existing
directories are left exactly as they are.

- [ ] **Step 3: Verify nothing was destroyed**

```bash
ls chatbot_api/.venv chatbot_api/.env chatbot_dashboard/node_modules >/dev/null \
  && echo "component working state intact"
git submodule status
```

Expected: `component working state intact`, and `git submodule status` listing both
submodules. A leading `-` means "not initialised" — harmless here, because the
directories are already correct clones. **If any of those three paths is missing,
stop and report** — that would mean the checkout clobbered a working directory,
which it should never do.

- [ ] **Step 4: Create the meta-repo `.gitignore`**

Create `/home/abdulkhalek/Projects/chatbot/.gitignore`:

```gitignore
# Local scratch that must never reach the repo
.env
*.png
.playwright-mcp/

# Editor / OS
.vscode/
.idea/
.DS_Store
```

- [ ] **Step 5: Create the Traefik static configuration**

Create `traefik/traefik.yml`:

```yaml
log:
  level: INFO

accessLog:
  filePath: "/var/log/traefik/access.log"
  format: json
  bufferingSize: 100
  filters:
    statusCodes:
      - "200-599"
  fields:
    defaultMode: keep
    headers:
      defaultMode: drop
      names:
        User-Agent: keep
        Content-Type: keep
        X-Forwarded-For: keep

entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint:
          to: web-secure
          scheme: https
          permanent: true

  web-secure:
    address: ":443"

  # Container-local ONLY. Traefik's API exposes the entire routing
  # configuration, so this must never bind 0.0.0.0 or be published: binding
  # 127.0.0.1 means only a process inside this container's network namespace
  # (i.e. `docker exec`) can read it.
  traefik:
    address: "127.0.0.1:8080"

# Read-only view of the live routing configuration. Plan 3's rolling deploy
# queries /api/http/services/<name>@docker to learn whether Traefik considers a
# new replica UP, rather than inferring it from container health and a timer.
api:
  dashboard: false
  insecure: true

certificatesResolvers:
  letsencrypt:
    acme:
      email: etqan.agency.company@gmail.com
      storage: /etc/traefik/acme/acme.json
      httpChallenge:
        entryPoint: web

providers:
  # Dynamic config lives in its own directory and hot-reloads. Pointing this at
  # a single file (or at traefik.yml itself) means every routing change needs a
  # full Traefik restart.
  file:
    directory: /etc/traefik/dynamic
    watch: true

  # Routers and services also come from container labels, so replicas are
  # discovered as they start and retired as they stop. Nothing is exposed
  # unless it opts in with `traefik.enable=true`.
  docker:
    endpoint: "tcp://socket-proxy:2375"
    exposedByDefault: false
    network: chatbot_default
    # Defence in depth behind the `-l traefik.enable=false` flag Plan 3 puts on
    # one-off containers. A `docker compose run` container inherits its
    # service's labels; without this it registers as an extra backend with
    # nothing listening. Ordinary compose containers carry oneoff=False, so
    # this matches them and excludes only one-offs.
    constraints: "Label(`com.docker.compose.oneoff`,`False`)"
```

- [ ] **Step 6: Create the dynamic middleware configuration**

Create `traefik/dynamic/middlewares.yml`:

```yaml
http:
  middlewares:
    # Django builds absolute URLs and validates CSRF against the forwarded
    # scheme. Behind TLS termination it would otherwise see plain http.
    csrf-headers:
      headers:
        customRequestHeaders:
          X-Forwarded-Proto: "https"
        hostsProxyHeaders:
          - "X-CSRFToken"

    secure-headers:
      headers:
        stsSeconds: 31536000
        stsIncludeSubdomains: true
        stsPreload: true
        frameDeny: true
        contentTypeNosniff: true
        browserXssFilter: true
        referrerPolicy: "strict-origin-when-cross-origin"

    # The API is called by tenants' own integrations as well as the dashboard,
    # so this is a coarse abuse guard only. Per-tenant limits are enforced in
    # the application (knowledge/services/quota.py), which is the layer that
    # knows who the caller is.
    rate-limit-api:
      rateLimit:
        average: 50
        burst: 100
        period: 1s

    # Static asset serving; the limit exists to blunt scrapers, not to shape
    # normal use.
    rate-limit-static:
      rateLimit:
        average: 100
        burst: 200
        period: 1s
```

- [ ] **Step 7: Verify Traefik starts and its API answers**

Traefik cannot start without its docker provider endpoint, so this check runs it
with the file provider only:

```bash
cd /home/abdulkhalek/Projects/chatbot
docker run --rm -d --name traefik-check \
  -v "$PWD/traefik/traefik.yml:/etc/traefik/traefik.yml:ro" \
  -v "$PWD/traefik/dynamic:/etc/traefik/dynamic:ro" \
  traefik:v3.6 \
  --providers.docker=false

sleep 5
docker logs traefik-check 2>&1 | grep -iE "error|fatal" | head -5
docker exec traefik-check wget -qO- http://127.0.0.1:8080/api/http/middlewares \
  | head -c 300
docker rm -f traefik-check
```

Expected: the middlewares endpoint returns JSON naming `csrf-headers`,
`secure-headers`, `rate-limit-api` and `rate-limit-static`. That is the
substantive proof — the static config parsed AND the file provider loaded the
dynamic directory.

**Two classes of ERR line are expected here and are not defects.** Verified
2026-09-04:

- `providerName=docker` — "lookup socket-proxy ... no such host", retrying.
  `--providers.docker=false` does **not** remove a provider declared in the
  static file, so Traefik keeps dialling an endpoint that only exists under
  compose. Harmless standalone; resolved in Task 2 where socket-proxy runs.
- `resolver=letsencrypt` — "unable to get ACME account: open
  /etc/traefik/acme/acme.json: no such file", and the follow-on "HTTP challenge
  is not enabled". No ACME volume is mounted in this ad-hoc run and there is no
  resolvable hostname to validate.

Assert only that **no error outside those two classes** appears. Certificate
issuance is genuinely unproven until a real deploy against real DNS — that
belongs to Plan 3, not here, and must not be mistaken for verified by this step.

- [ ] **Step 8: Verify the API is NOT reachable from outside the container**

```bash
docker run --rm -d --name traefik-check2 -p 8080:8080 \
  -v "$PWD/traefik/traefik.yml:/etc/traefik/traefik.yml:ro" \
  -v "$PWD/traefik/dynamic:/etc/traefik/dynamic:ro" \
  traefik:v3.6 --providers.docker=false
sleep 4
curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/http/middlewares
docker rm -f traefik-check2
```

Expected: `000` (connection refused/failed) — **not** a 200. The API binds
`127.0.0.1` inside the container, so publishing the port must not expose it. A
200 here means the entrypoint address is wrong and the whole routing table is
readable from the network.

- [ ] **Step 9: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add .gitignore traefik/
git commit -m "feat: Traefik v3 gateway configuration

Static config pins the docker provider to chatbot_default and exposes a
container-local API on 127.0.0.1:8080 -- Plan 3's rolling deploy queries
/api/http/services/<name>@docker to decide when a new replica is really
in rotation, rather than inferring it from a timer.

Dynamic middlewares live in their own watched directory so routing
changes hot-reload; pointing the file provider at a single file is what
forces a full restart for every change."
```

---

### Task 2: Production compose — gateway, data tier, API

Builds the stack bottom-up and verifies the API is reachable through Traefik
before any other service is added. Splitting this from Task 3 means a routing
failure is diagnosed against two services, not six.

**Files:**
- Create: `chatbot/docker-compose.production.yml`
- Create: `chatbot/.env.example`

**Interfaces:**
- Consumes: `traefik/` from Task 1.
- Produces: `docker-compose.production.yml` with networks `chatbot_default` and
  `chatbot_socket_proxy`, volumes, and services `traefik`, `socket-proxy`, `db`,
  `redis`, `web`, `worker`. Task 3 appends `media` and `dashboard` to the same
  file. Plan 3's `deploy.sh` reads `IMAGE_TAG` from the environment.

- [ ] **Step 1: Create the compose file**

Create `docker-compose.production.yml`:

```yaml
networks:
  # Named explicitly to pin it: traefik.yml's providers.docker.network
  # references chatbot_default by name, and every service that does not name a
  # network lands here.
  default:
    name: chatbot_default
  # traefik <-> socket-proxy only. No application container joins this.
  socket_proxy:
    name: chatbot_socket_proxy

volumes:
  pgdata: {}
  pgbackups: {}
  redisdata: {}
  static_volume: {}
  media_volume: {}
  traefik_acme: {}
  traefik_logs: {}

services:
  traefik:
    image: traefik:v3.6
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./traefik/traefik.yml:/etc/traefik/traefik.yml:ro
      - ./traefik/dynamic:/etc/traefik/dynamic:ro
      - traefik_acme:/etc/traefik/acme
      - traefik_logs:/var/log/traefik
    networks:
      - default
      - socket_proxy
    depends_on:
      - socket-proxy

  # Traefik never touches the raw Docker socket. This proxy exposes only the
  # read-only endpoints the docker provider needs; a Traefik compromise
  # therefore cannot create or exec containers.
  socket-proxy:
    image: tecnativa/docker-socket-proxy:0.3.0
    restart: unless-stopped
    environment:
      CONTAINERS: 1
      NETWORKS: 1
      SERVICES: 1
      TASKS: 1
      POST: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - socket_proxy

  db:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-chatbot}
      POSTGRES_USER: ${POSTGRES_USER:-chatbot}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - pgbackups:/backups
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-chatbot} -d ${POSTGRES_DB:-chatbot}"]
      interval: 5s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  web:
    image: ghcr.io/etqan-agency/chatbot/api:${IMAGE_TAG:-latest}
    restart: unless-stopped
    env_file: .env
    # RUN_MIGRATIONS is deliberately NOT set. docker/entrypoint.sh gates
    # migrate + collectstatic behind it, and this service runs 2 replicas —
    # both would race to apply the same migration on every start. Migrations
    # run as an explicit one-off container instead (see Step 5 below, and
    # Plan 3's deploy.sh, which does the same thing before rolling replicas).
    volumes:
      - static_volume:/app/staticfiles
      - media_volume:/app/media
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    deploy:
      replicas: 2
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.api.rule=Host(`api.${DOMAIN}`)"
      - "traefik.http.routers.api.entrypoints=web-secure"
      - "traefik.http.routers.api.tls=true"
      - "traefik.http.routers.api.tls.certresolver=letsencrypt"
      - "traefik.http.routers.api.middlewares=csrf-headers@file,secure-headers@file,rate-limit-api@file"
      - "traefik.http.services.web.loadbalancer.server.port=8000"
      # The drain-aware probe. Returns 503 while /tmp/drain exists, which is how
      # Plan 3 removes a replica from rotation BEFORE stopping it.
      - "traefik.http.services.web.loadbalancer.healthcheck.path=/health/"
      - "traefik.http.services.web.loadbalancer.healthcheck.interval=5s"
      - "traefik.http.services.web.loadbalancer.healthcheck.timeout=3s"
      # Traefik probes the backend at its CONTAINER IP. Without an explicit
      # hostname it sends that IP as the Host header, Django's ALLOWED_HOSTS
      # rejects it with 400, Traefik concludes every backend is dead, and the
      # whole host 503s. Only `web` needs this -- nginx does not validate Host.
      - "traefik.http.services.web.loadbalancer.healthcheck.hostname=api.${DOMAIN}"
      # Without an explicit status, Traefik counts any 2xx OR 3xx as healthy. A
      # redirect (e.g. Django's SSL redirect) must never read as health, or the
      # drain gate Plan 3 depends on silently stops gating anything.
      - "traefik.http.services.web.loadbalancer.healthcheck.status=200"

  worker:
    image: ghcr.io/etqan-agency/chatbot/api:${IMAGE_TAG:-latest}
    restart: unless-stopped
    command: ["celery", "-A", "Conf", "worker", "-l", "info", "--concurrency", "2"]
    env_file: .env
    volumes:
      - media_volume:/app/media
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    # The image's own HEALTHCHECK curls :8000/health/, which only makes sense
    # for gunicorn. This service runs celery, so nothing listens there and the
    # inherited probe would report unhealthy forever -- noise that masks real
    # failures, and a deadlock for anything that later waits on
    # `condition: service_healthy`. Override it with a probe of the thing that
    # is actually running.
    healthcheck:
      test: ["CMD-SHELL", "celery -A Conf inspect ping -d celery@$$HOSTNAME || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
```

Note what is **absent**: no service sets `RUN_MIGRATIONS`. The entrypoint's
migrate/collectstatic path is bypassed entirely, because `web` runs two replicas
and both would race on the same migration at every start. Schema changes are a
single-writer operation, so they run as a one-off container — which is also
exactly how Plan 3's `deploy.sh` sequences them, before rolling any replica.

- [ ] **Step 2: Create `.env.example`**

Create `.env.example`:

```bash
# --- Deployment identity -----------------------------------------------------
# Base domain. Hostnames are derived: api.${DOMAIN} and app.${DOMAIN}.
# BOTH need A records pointing at this host before the first deploy --
# Let's Encrypt resolves them over HTTP-01.
DOMAIN=example.com

# Image tag to run. CD sets this to the commit SHA; `latest` is a convenience
# for manual bring-up only -- never rely on it for a real deploy, because it
# makes "what is running?" unanswerable.
IMAGE_TAG=latest

# --- Database ----------------------------------------------------------------
POSTGRES_DB=chatbot
POSTGRES_USER=chatbot
POSTGRES_PASSWORD=
# Must match the three values above.
DATABASE_URL=postgres://chatbot:CHANGEME@db:5432/chatbot

# --- Django ------------------------------------------------------------------
SECRET_KEY=
DEBUG=False
# MUST include both hostnames and localhost: the dashboard proxies with
# Host: app.${DOMAIN}, and the container HEALTHCHECK probes over loopback.
ALLOWED_HOSTS=api.example.com,app.example.com,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://api.example.com,https://app.example.com
# The dashboard is same-origin, so this is only for third-party browser
# clients. Leave empty unless one exists.
CORS_ALLOWED_ORIGINS=
FRONTEND_URL=https://app.example.com

# --- Production behaviour ----------------------------------------------------
INGESTION_MODE=celery
RAG_VECTOR_BACKEND=pgvector
USE_REDIS_CACHE=True
REDIS_URL=redis://redis:6379/0
UNANSWERED_CAPTURE_MODE=celery

# --- LLM ---------------------------------------------------------------------
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
EMBEDDING_MODEL=text-embedding-3-large
OPENAI_API_KEY=

# --- Billing -----------------------------------------------------------------
PADDLE_ENV=production
PADDLE_API_KEY=
PADDLE_WEBHOOK_SECRET=

# --- Email -------------------------------------------------------------------
EMAIL_HOST=
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
DEFAULT_FROM_EMAIL=click.ai <no-reply@example.com>

# --- Local verification only -------------------------------------------------
# Traefik reads static config from env vars too. Point ACME at Let's Encrypt
# STAGING when testing locally, or repeated failures against the production CA
# will burn the real rate limit. Leave UNSET in production.
# TRAEFIK_CERTIFICATESRESOLVERS_LETSENCRYPT_ACME_CASERVER=https://acme-staging-v02.api.letsencrypt.org/directory
```

- [ ] **Step 3: Prepare a local `.env` for verification**

```bash
cd /home/abdulkhalek/Projects/chatbot
cp .env.example .env
```

Then edit `.env`: set `DOMAIN=chatbot.local`, `POSTGRES_PASSWORD=localdev`,
`SECRET_KEY=local-only-not-secret`, `DATABASE_URL=postgres://chatbot:localdev@db:5432/chatbot`,
`ALLOWED_HOSTS=api.chatbot.local,app.chatbot.local,localhost,127.0.0.1`,
`INGESTION_MODE=sync`, `RAG_VECTOR_BACKEND=numpy`, and uncomment the ACME staging
line. `.env` is gitignored — confirm with `git check-ignore -v .env`.

`RAG_VECTOR_BACKEND=numpy` for local bring-up: `pgvector` needs the extension
installed by a management command that Plan 3's deploy runs, and this task is
verifying routing, not retrieval.

- [ ] **Step 4: Build and tag the API image locally**

The compose file names a GHCR image that does not exist yet. Build it locally
under the same name so compose finds it without a registry:

```bash
cd /home/abdulkhalek/Projects/chatbot/chatbot_api
docker build -t ghcr.io/etqan-agency/chatbot/api:latest .
```

- [ ] **Step 5: Start the data tier, then migrate as a one-off**

```bash
cd /home/abdulkhalek/Projects/chatbot
docker compose -f docker-compose.production.yml up -d db redis
sleep 15

# `--no-deps` so this does not start web's dependents, and
# `-l traefik.enable=false` because a `compose run` container INHERITS its
# service's labels — without it this one-off joins the routing pool with
# nothing listening on :8000 and Traefik 502s a share of traffic.
docker compose -f docker-compose.production.yml run --rm --no-deps \
  -l traefik.enable=false -T web python manage.py migrate --noinput
docker compose -f docker-compose.production.yml run --rm --no-deps \
  -l traefik.enable=false -T web python manage.py collectstatic --noinput
```

Expected: migrations apply cleanly and collectstatic reports the number of files
copied.

- [ ] **Step 6: Bring up the rest of the stack**

```bash
docker compose -f docker-compose.production.yml up -d
sleep 45
docker compose -f docker-compose.production.yml ps
```

Expected: `traefik`, `socket-proxy`, `db`, `redis`, `worker` running; `web` running
with 2 replicas and reporting healthy. If `web` is restarting, read
`docker compose -f docker-compose.production.yml logs web | tail -40` before
changing anything.

- [ ] **Step 7: Verify the API answers through Traefik**

Traefik serves its self-signed default certificate because ACME cannot validate
`chatbot.local`, so `-k` is expected here:

```bash
curl -sk -m 10 -H "Host: api.chatbot.local" https://localhost/health/ -w '\n%{http_code}\n'
```

Expected: the health JSON and `200`. **A 404 means the router rule did not match;
a 503 means Traefik has no healthy backend** — check the health-check hostname
label first, because that is the failure this label exists to prevent.

- [ ] **Step 8: Verify the HTTP→HTTPS redirect**

```bash
curl -s -o /dev/null -m 10 -H "Host: api.chatbot.local" http://localhost/health/ -w '%{http_code}\n'
```

Expected: `301`.

- [ ] **Step 9: Verify Traefik sees both replicas**

```bash
docker exec $(docker compose -f docker-compose.production.yml ps -q traefik) \
  wget -qO- http://127.0.0.1:8080/api/http/services/web@docker
```

Expected: JSON with a `serverStatus` object containing **two** entries, both `"UP"`.
This is exactly the endpoint Plan 3's `rollout.sh` polls, so it must work now.

- [ ] **Step 10: Tear down**

```bash
docker compose -f docker-compose.production.yml down -v
```

`-v` removes the volumes so Task 3 starts from a clean database.

- [ ] **Step 11: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add docker-compose.production.yml .env.example
git commit -m "feat: production compose -- gateway, data tier, API

Traefik reaches Docker only through a read-only socket proxy, so a
gateway compromise cannot create or exec containers.

The web service carries an explicit health-check hostname label: Traefik
probes backends at their container IP, and without it Django's
ALLOWED_HOSTS answers the probe with 400, Traefik marks every backend
dead, and the whole host 503s. The explicit status=200 stops a redirect
from reading as health, which would silently disable the drain gate."
```

---

### Task 3: Media and dashboard services

Adds the two remaining routed services. The `media` service is not optional: the
dashboard's nginx resolves `media:80` at parse time and will not start without it.

**Files:**
- Modify: `chatbot/docker-compose.production.yml` (append two services)

**Interfaces:**
- Consumes: the compose file from Task 2.
- Produces: services `media` (nginx on `media_volume`, `PathPrefix('/media')` router
  at priority 100) and `dashboard` (2 replicas on `app.${DOMAIN}`). Plan 3's
  `services.sh` rolls `web` and `dashboard`; `media` is deliberately excluded.

- [ ] **Step 1: Create the media service's nginx configuration**

Create a new top-level directory `media/` in the meta-repo, containing
`media/nginx.conf`:

```nginx
server {
    listen 80;
    server_name _;

    # Container health only. This must NOT live under /media/: that location
    # ends in `try_files $uri =404`, so a bare directory request returns 404
    # and a health probe pointed at it could never pass — the container would
    # serve files correctly while permanently reporting unhealthy.
    location = /healthz {
        access_log off;
        add_header Content-Type text/plain;
        return 200 "ok\n";
    }

    # Read-only view of the same volume the API writes uploads to.
    location /media/ {
        alias /srv/media/;
        access_log off;
        expires 7d;
        add_header Cache-Control "public";
        try_files $uri =404;
    }

    # Anything else reaching this service is a routing mistake; answer cheaply
    # rather than falling through to nginx's default index.
    location / {
        return 404;
    }
}
```

- [ ] **Step 2: Append the two services to the compose file**

Add to `docker-compose.production.yml` under `services:`:

```yaml
  # Serves tenant uploads. Django mounts MEDIA_URL only under DEBUG, so in
  # production /media/ MUST come from here. The router carries a higher
  # priority than the api catch-all so that PathPrefix wins on the same host.
  media:
    image: nginx:alpine
    restart: unless-stopped
    volumes:
      - ./media/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - media_volume:/srv/media:ro
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1/healthz"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 5s
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.media.rule=Host(`api.${DOMAIN}`) && PathPrefix(`/media`)"
      - "traefik.http.routers.media.entrypoints=web-secure"
      - "traefik.http.routers.media.tls=true"
      - "traefik.http.routers.media.tls.certresolver=letsencrypt"
      - "traefik.http.routers.media.middlewares=secure-headers@file,rate-limit-static@file"
      # Higher than the api router's default (which is the rule's length).
      # Without this, matching is by rule length and the outcome is incidental
      # rather than intended.
      - "traefik.http.routers.media.priority=100"
      - "traefik.http.services.media.loadbalancer.server.port=80"

  dashboard:
    image: ghcr.io/etqan-agency/chatbot/dashboard:${IMAGE_TAG:-latest}
    restart: unless-stopped
    depends_on:
      - web
      - media
    deploy:
      replicas: 2
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.app.rule=Host(`app.${DOMAIN}`)"
      - "traefik.http.routers.app.entrypoints=web-secure"
      - "traefik.http.routers.app.tls=true"
      - "traefik.http.routers.app.tls.certresolver=letsencrypt"
      - "traefik.http.routers.app.middlewares=secure-headers@file,rate-limit-static@file"
      - "traefik.http.services.dashboard.loadbalancer.server.port=80"
      - "traefik.http.services.dashboard.loadbalancer.healthcheck.path=/"
      - "traefik.http.services.dashboard.loadbalancer.healthcheck.interval=10s"
      - "traefik.http.services.dashboard.loadbalancer.healthcheck.timeout=3s"
```

`depends_on: [web, media]` is required, not cosmetic: the dashboard's nginx
resolves both upstreams at parse time and exits if either name does not resolve.

- [ ] **Step 3: Build the dashboard image locally**

```bash
cd /home/abdulkhalek/Projects/chatbot/chatbot_dashboard
docker build -t ghcr.io/etqan-agency/chatbot/dashboard:latest .
```

- [ ] **Step 4: Bring the full stack up**

```bash
cd /home/abdulkhalek/Projects/chatbot
docker compose -f docker-compose.production.yml up -d
sleep 60
docker compose -f docker-compose.production.yml ps
```

Expected: all seven services up; `web` and `dashboard` at 2 replicas each.

- [ ] **Step 5: Seed a media file so the route can be proven**

An empty volume cannot distinguish "routing is broken" from "file absent":

```bash
docker compose -f docker-compose.production.yml exec -T web \
  sh -c 'mkdir -p /app/media/probe && echo hello-media > /app/media/probe/t.txt'
```

- [ ] **Step 6: Verify all four routes**

```bash
curl -sk -o /dev/null -m 10 -w 'api-health: %{http_code}\n' -H "Host: api.chatbot.local" https://localhost/health/
curl -sk       -m 10 -w '\nmedia-body-above, code: %{http_code}\n' -H "Host: api.chatbot.local" https://localhost/media/probe/t.txt
curl -sk -o /dev/null -m 10 -w 'app-root:   %{http_code}\n' -H "Host: app.chatbot.local" https://localhost/
curl -sk -o /dev/null -m 10 -w 'app-route:  %{http_code}\n' -H "Host: app.chatbot.local" https://localhost/login
```

Expected: `api-health: 200`; the media request printing `hello-media` with code
`200`; `app-root: 200`; `app-route: 200`.

**The media check is the one that matters most.** A `404` means the priority label
is not winning and the request went to `web`, which is exactly the production bug
this service exists to prevent.

- [ ] **Step 7: Verify the dashboard's same-origin API proxy end to end**

```bash
curl -sk -o /dev/null -m 15 -w 'app-proxied-api: %{http_code}\n' \
  -H "Host: app.chatbot.local" https://localhost/api/v1/
```

Expected: `401` — the API answered ("Authentication credentials were not
provided"), proving the request traversed dashboard nginx → `web`. A `200` would
mean it fell through to the SPA fallback; a `502` would mean nginx could not reach
the upstream.

- [ ] **Step 8: Tear down and commit**

```bash
docker compose -f docker-compose.production.yml down -v
git add docker-compose.production.yml media/
git commit -m "feat: media and dashboard services

Django mounts MEDIA_URL only under DEBUG, so production media must come
from a dedicated nginx bound read-only to the media volume. Its router
takes an explicit priority: without one, matching between the api
catch-all and this PathPrefix is decided by rule length, which makes the
outcome incidental rather than intended.

dashboard depends_on [web, media] because its nginx resolves both
upstreams at parse time and exits if either name is unresolvable."
```

---

### Task 4: Continuous integration

**Files:**
- Create: `chatbot/.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the Dockerfiles in both submodules; the compose file for nothing (CI
  builds images directly).
- Produces: a workflow whose final job is named **`CI Summary`** — Plan 3's
  `cd.yml` waits on that exact check name. Images published to
  `ghcr.io/etqan-agency/chatbot/api` and `.../dashboard`, tagged with the full
  commit SHA and `latest`.

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  pull_request:
    branches: ['main']
  push:
    branches: ['main']

concurrency:
  group: ci-${{ github.head_ref || github.run_id }}
  cancel-in-progress: true

env:
  REGISTRY: ghcr.io
  # Must be lowercase for the Docker registry.
  IMAGE_PREFIX: etqan-agency/chatbot

jobs:
  changes:
    name: Detect Changes
    runs-on: ubuntu-latest
    outputs:
      api: ${{ steps.filter.outputs.api }}
      dashboard: ${{ steps.filter.outputs.dashboard }}
      infra: ${{ steps.filter.outputs.infra }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            api:
              - 'chatbot_api'
            dashboard:
              - 'chatbot_dashboard'
            infra:
              - 'docker-compose.production.yml'
              - 'traefik/**'
              - 'media/**'
              - '.github/workflows/**'

  api-lint:
    name: API Linting
    runs-on: ubuntu-latest
    needs: changes
    if: needs.changes.outputs.api == 'true' || github.event_name == 'push'
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          token: ${{ secrets.SUBMODULE_TOKEN }}
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: pip install pre-commit
      - name: Run pre-commit
        working-directory: chatbot_api
        run: pre-commit run --all-files

  api-test:
    name: API Tests
    runs-on: ubuntu-latest
    needs: changes
    if: needs.changes.outputs.api == 'true' || github.event_name == 'push'
    services:
      # Tests run against the production engine. SQLite never exercises the
      # pgvector retrieval path, which is precisely the code a regression
      # would hide in.
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_DB: chatbot_test
          POSTGRES_USER: chatbot
          POSTGRES_PASSWORD: chatbot
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U chatbot"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      DATABASE_URL: postgres://chatbot:chatbot@localhost:5432/chatbot_test
      SECRET_KEY: ci-only-not-secret
      DEBUG: 'False'
      ALLOWED_HOSTS: localhost,127.0.0.1
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          token: ${{ secrets.SUBMODULE_TOKEN }}
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - name: Install dependencies
        working-directory: chatbot_api
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
      - name: Check for missing migrations
        working-directory: chatbot_api
        run: python manage.py makemigrations --check --dry-run
      - name: Run migrations
        working-directory: chatbot_api
        run: python manage.py migrate --noinput
      - name: Run tests
        working-directory: chatbot_api
        run: python manage.py test

  dashboard-checks:
    name: Dashboard Lint, Types and Build
    runs-on: ubuntu-latest
    needs: changes
    if: needs.changes.outputs.dashboard == 'true' || github.event_name == 'push'
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          token: ${{ secrets.SUBMODULE_TOKEN }}
      - uses: actions/setup-node@v4
        with:
          node-version: '22'
          cache: 'npm'
          cache-dependency-path: chatbot_dashboard/package-lock.json
      - name: Install dependencies
        working-directory: chatbot_dashboard
        run: npm ci
      - name: Lint
        working-directory: chatbot_dashboard
        run: npm run lint
      - name: Type check
        working-directory: chatbot_dashboard
        run: npx tsc --noEmit
      - name: Build
        working-directory: chatbot_dashboard
        run: npm run build

  build-api:
    name: Build and Push API Image
    runs-on: ubuntu-latest
    needs: [api-lint, api-test]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          token: ${{ secrets.SUBMODULE_TOKEN }}
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_PREFIX }}/api
          tags: |
            type=sha,prefix=,format=long
            type=raw,value=latest
      - uses: docker/build-push-action@v6
        with:
          context: ./chatbot_api
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha,scope=api
          cache-to: type=gha,scope=api,mode=max
          provenance: true
          sbom: true

  build-dashboard:
    name: Build and Push Dashboard Image
    runs-on: ubuntu-latest
    needs: [dashboard-checks]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          token: ${{ secrets.SUBMODULE_TOKEN }}
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_PREFIX }}/dashboard
          tags: |
            type=sha,prefix=,format=long
            type=raw,value=latest
      - uses: docker/build-push-action@v6
        with:
          context: ./chatbot_dashboard
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha,scope=dashboard
          cache-to: type=gha,scope=dashboard,mode=max
          provenance: true
          sbom: true

  ci-summary:
    name: CI Summary
    runs-on: ubuntu-latest
    needs: [build-api, build-dashboard]
    if: always() && github.event_name == 'push' && github.ref == 'refs/heads/main'
    steps:
      - name: Summarise
        run: |
          echo "## CI Pipeline Summary" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "| Component | Status |" >> $GITHUB_STEP_SUMMARY
          echo "|-----------|--------|" >> $GITHUB_STEP_SUMMARY
          echo "| API | ${{ needs.build-api.result }} |" >> $GITHUB_STEP_SUMMARY
          echo "| Dashboard | ${{ needs.build-dashboard.result }} |" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "**Commit:** \`${{ github.sha }}\`" >> $GITHUB_STEP_SUMMARY
      - name: Fail if any build failed
        if: needs.build-api.result != 'success' || needs.build-dashboard.result != 'success'
        run: exit 1
```

The final `Fail if any build failed` step is not decoration. `ci-summary` runs
under `always()`, so without it the job reports success even when a build failed —
and Plan 3's `cd.yml` waits on this check name to decide whether to deploy.

- [ ] **Step 2: Validate the workflow's syntax locally**

```bash
cd /home/abdulkhalek/Projects/chatbot
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml: valid YAML')"
```

Expected: `ci.yml: valid YAML`.

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/ci.yml
git commit -m "feat: CI -- lint, test against Postgres, publish images to GHCR

Tests run against pgvector/pg16 rather than SQLite: the pgvector
retrieval path is exactly the code SQLite never exercises, so testing on
SQLite makes the gate meaningless for the backend that ships.

ci-summary runs under always() so it reports regardless of upstream
results; the explicit failure step is what stops it reporting success
after a failed build, since Plan 3's cd.yml gates deployment on this
check name."
git push origin main
```

- [ ] **Step 4: Watch the first real run**

```bash
gh run watch --repo Etqan-agency/chatbot $(gh run list --repo Etqan-agency/chatbot --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: every job green and `CI Summary` present.

**If `api-lint`, `api-test`, `dashboard-checks` or either build fails at the
checkout step with a submodule error**, the `SUBMODULE_TOKEN` secret is missing or
lacks `contents: read` on both component repos. That is a repository-settings fix,
not a workflow bug — report it rather than working around it by making the
submodules public.

- [ ] **Step 5: Verify the images are actually published**

```bash
gh api "orgs/Etqan-agency/packages?package_type=container" --jq '.[].name'
```

Expected: `chatbot/api` and `chatbot/dashboard` present.

---

### Task 5: `sync-to-meta` workflows

Makes a meta-repo commit the unit of deployment. Each component bumps its own
submodule pointer onto a shared `release/next` branch and opens **one** release PR;
merging it is the deploy decision. Without this batching, each component merge
pushes its own bump straight to `main`, and the first of any two deploys a
mismatched pair.

**Files:**
- Create: `chatbot_api/.github/workflows/sync-to-meta.yml`
- Create: `chatbot_dashboard/.github/workflows/sync-to-meta.yml`

**Interfaces:**
- Consumes: the meta-repo's `main` branch and `.gitmodules` from Task 1.
- Produces: a `release/next` branch in the meta-repo and one open PR into `main`
  accumulating pointer bumps. Plan 3's `cd.yml` triggers on push to `main`, so
  merging that PR is what triggers a deploy.

- [ ] **Step 1: Create the API workflow**

Create `chatbot_api/.github/workflows/sync-to-meta.yml`:

```yaml
name: Sync pointer to meta-repo

on:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: sync-meta-${{ github.ref }}
  cancel-in-progress: false

jobs:
  bump-pointer:
    name: Bump submodule pointer in meta-repo
    runs-on: ubuntu-latest
    env:
      META_OWNER: Etqan-agency
      META_REPO: chatbot
      SUBMODULE_PATH: chatbot_api
    steps:
      - name: Checkout meta-repo
        uses: actions/checkout@v4
        with:
          repository: ${{ env.META_OWNER }}/${{ env.META_REPO }}
          token: ${{ secrets.META_TOKEN }}
          ref: main
          fetch-depth: 0
          persist-credentials: true

      - name: Bump pointer on release/next and open the release PR
        env:
          SHA: ${{ github.sha }}
          ACTOR: ${{ github.actor }}
          GH_TOKEN: ${{ secrets.META_TOKEN }}
        run: |
          set -euo pipefail
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          BRANCH=release/next

          # Accumulate onto release/next rather than main. A commit on main is
          # deployable, so pushing each component's bump straight there
          # produces commits that are internally inconsistent -- a new API
          # against an old dashboard -- and deploys each one.
          if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
            git checkout -B "$BRANCH" "origin/$BRANCH"
          else
            git checkout -B "$BRANCH" origin/main
          fi

          git update-index --cacheinfo 160000,"$SHA","$SUBMODULE_PATH"
          git checkout -- . 2>/dev/null || true

          if git diff --cached --quiet; then
            echo "Pointer already at $SHA -- nothing to do."
            exit 0
          fi

          git commit -m "chore: bump $SUBMODULE_PATH to ${SHA:0:7}

          Pushed by @$ACTOR to $SUBMODULE_PATH main."
          git push origin "$BRANCH"

          # One PR, updated in place. Creating a second would defeat the
          # batching this whole workflow exists for.
          if ! gh pr view "$BRANCH" --repo "$META_OWNER/$META_REPO" >/dev/null 2>&1; then
            gh pr create \
              --repo "$META_OWNER/$META_REPO" \
              --base main --head "$BRANCH" \
              --title "Release: submodule pointer updates" \
              --body "Accumulated submodule pointer bumps. Merging this PR is the deploy decision -- it puts a coherent set of pointers on main, which is what CD deploys."
          fi
```

- [ ] **Step 2: Create the dashboard workflow**

Create `chatbot_dashboard/.github/workflows/sync-to-meta.yml` with **identical
content**, changing only:

```yaml
      SUBMODULE_PATH: chatbot_dashboard
```

Both files are otherwise byte-identical. Repeating the file rather than sharing it
is deliberate: a reusable workflow would couple the two component repos to a third
location for no benefit at this size.

- [ ] **Step 3: Validate both workflows' syntax**

```bash
for f in /home/abdulkhalek/Projects/chatbot/chatbot_api/.github/workflows/sync-to-meta.yml \
         /home/abdulkhalek/Projects/chatbot/chatbot_dashboard/.github/workflows/sync-to-meta.yml; do
  python3 -c "import yaml,sys; yaml.safe_load(open('$f')); print('$f: valid')"
done
```

Expected: both report valid.

- [ ] **Step 4: Commit and push both**

```bash
cd /home/abdulkhalek/Projects/chatbot/chatbot_api
git add .github/workflows/sync-to-meta.yml
git commit -m "ci: bump this component's submodule pointer in the meta-repo

Bumps accumulate on release/next and land as one PR rather than going
straight to main. A commit on the meta-repo's main is deployable, so
per-component bumps on main would each deploy an internally inconsistent
set of pointers."
git push origin main

cd ../chatbot_dashboard
git add .github/workflows/sync-to-meta.yml
git commit -m "ci: bump this component's submodule pointer in the meta-repo

Bumps accumulate on release/next and land as one PR rather than going
straight to main. A commit on the meta-repo's main is deployable, so
per-component bumps on main would each deploy an internally inconsistent
set of pointers."
git push origin main
```

- [ ] **Step 5: Verify the workflow actually fired and produced a PR**

Pushing in Step 4 triggers both workflows.

```bash
gh run list --repo Etqan-agency/chatbot_api --workflow sync-to-meta.yml --limit 1
gh run list --repo Etqan-agency/chatbot_dashboard --workflow sync-to-meta.yml --limit 1
gh pr list --repo Etqan-agency/chatbot --state open
```

Expected: both runs successful, and **exactly one** open PR titled
"Release: submodule pointer updates" in the meta-repo. Two PRs would mean the
"update in place" guard failed and the batching is not working.

**If the runs fail at checkout,** the `META_TOKEN` secret is missing from the
component repos. It needs `contents: write` and `pull-requests: write` on
`Etqan-agency/chatbot`. That is a settings fix — report it.

- [ ] **Step 6: Update the meta-repo README**

Replace the "not yet implemented" markers for `traefik/`, `docker-compose.production.yml`
and `.github/workflows/` with a short description of what each now contains, and
record that `scripts/`, `cd.yml` and `rollback.yml` remain Plan 3's work. Commit
and push.

---

## Completion Criteria

```bash
# Full stack runs locally
cd /home/abdulkhalek/Projects/chatbot
docker compose -f docker-compose.production.yml up -d && sleep 60
curl -sk -H "Host: api.chatbot.local" https://localhost/health/          # 200
curl -sk -H "Host: api.chatbot.local" https://localhost/media/probe/t.txt # 200 + body
curl -sk -H "Host: app.chatbot.local" https://localhost/                  # 200
curl -sk -H "Host: app.chatbot.local" https://localhost/api/v1/           # 401 (proxied)
docker compose -f docker-compose.production.yml down -v

# CI green, images published
gh run list --repo Etqan-agency/chatbot --limit 1
gh api "orgs/Etqan-agency/packages?package_type=container" --jq '.[].name'

# Exactly one release PR open
gh pr list --repo Etqan-agency/chatbot --state open
```

Plan 3 (`deploy.sh`, `rollback.sh`, `cd.yml`, `rollback.yml`, provisioning
verification, cutover) starts from here.

## Secrets Required Before Task 4

Populated by the repository owner; this plan never writes them.

| Secret | Where | Scope |
|---|---|---|
| `SUBMODULE_TOKEN` | `Etqan-agency/chatbot` | `contents: read` on both component repos |
| `META_TOKEN` | both component repos | `contents: write`, `pull-requests: write` on `Etqan-agency/chatbot` |

`GITHUB_TOKEN` is provided automatically and is what pushes to GHCR.

## Deliberately Not In This Plan

- `scripts/deploy.sh`, `scripts/rollback.sh`, `scripts/lib/rollout.sh` — Plan 3
- `cd.yml`, `rollback.yml` — Plan 3
- VPS provisioning verification and the DNS cutover — Plan 3
- Moving `migrate` out of `docker/entrypoint.sh` into an explicit deploy step —
  Plan 3, where `deploy.sh` runs it as a one-off container
- Trivy image scanning — deferred; adds a gate before the pipeline itself is proven
- Deleting the orphaned modules, the audit, optimization — sub-projects 2-4
