# Infrastructure & Deployment Pipeline — Design

**Date:** 2026-09-03
**Status:** Approved design, pending implementation plan
**Sub-project:** 1 of 4 (see *Program context* below)

---

## 1. Program context

This spec covers the **first** of four sub-projects in the chatbot polish
program. The full program, in the agreed execution order:

| # | Sub-project | Status |
|---|-------------|--------|
| 1 | **Infrastructure & pipeline** (this spec) | designing |
| 2 | Cleanup — dead code, config consolidation, Django style pass | not started |
| 3 | Audit — bug / security / performance review producing a ranked findings queue | not started |
| 4 | Optimization & test depth | not started |

Infrastructure runs first so that every subsequent code change lands behind a
green pipeline and is deployable. The cost of this ordering, accepted
deliberately: the lint gate is built against code that sub-project 2 will
change. Section 5.2 addresses how that gate goes green on day one.

## 2. Goals

1. One reproducible deployment for both components, replacing the current
   manual `git pull && docker compose up -d --build` on the server.
2. Continuous integration on every push and pull request: lint, tests, type
   checks, container builds.
3. Zero-downtime rolling deploys with automatic rollback on failed smoke checks.
4. A coherent unit of deployment — the API and dashboard versions that ship
   together are the versions that were tested together.

### Non-goals

Application-level refactoring, bug fixing, test expansion, and performance work
are sub-projects 2–4 and are explicitly **out of scope** here. The only
application code this spec touches is what deployment itself requires
(Section 4.2).

## 3. Current state

**Two independent Git repositories**, no parent repo:

- `https://github.com/islam302/chatbot_api.git` — Django 5.2 + DRF, single
  `main` branch. 347 tests passing in ~58s against SQLite.
- `git@github.com:islam302/chatbot_dashboard.git` — Vite 8 + React 19 SPA,
  single `main` branch, deployed on Vercel at `clickai-alpha.vercel.app`.

**Deployment today:** `chatbot_api/docker-compose.yml` runs `db`
(pgvector/pgvector:pg16), `redis`, `web` (gunicorn), `worker` (celery), and
`nginx` on a Hostinger VPS. nginx publishes to `127.0.0.1:8017`, behind a
host-level reverse proxy serving
`https://una-ai-tools-apis.una-oic.org/chatbot-api`. Updates are applied by
hand on the server. There is no CI of any kind — neither repo has a
`.github/` directory.

**Reference implementation:** `~/Projects/original_software` — a meta-repo with
three submodules, a Traefik v3 gateway, GHCR images, and
`ci.yml` → `cd.yml` → `rollback.yml` driving a health-gated rolling deploy.
This design ports that pattern. Where it diverges, Section 10 says why.

## 4. Target architecture

### 4.1 Runtime topology

```
Internet
  │
  ├─ :80  ── permanent redirect ──▶ :443
  └─ :443 ──▶ Traefik v3
               │  · Let's Encrypt, HTTP-01 challenge on the web entrypoint
               │  · docker provider via socket-proxy (never the raw socket)
               │  · file provider watching traefik/dynamic/ (hot-reloads)
               │  · API on 127.0.0.1:8080, container-local only
               │
               ├── api.${DOMAIN}/media/* ──▶ media  (nginx:alpine · 1 replica)
               │     └─ higher router priority than the catch-all below
               ├── api.${DOMAIN}        ──▶ web    (gunicorn · 2 replicas)
               └── app.${DOMAIN}        ──▶ dashboard (nginx:alpine · 2 replicas)
                        ├── /api/*   ─┐
                        ├── /media/* ─┴─▶ proxy_pass to web (same-origin)
                        └── /*          ─▶ SPA fallback (try_files → index.html)

  internal network — no Traefik labels, not routable from outside:
      db (pgvector/pgvector:pg16) · redis:7-alpine · worker (celery)
```

**Why `media` is its own service.** The current `docker/nginx/default.conf`
serves `/static/` and `/media/` from volumes and proxies the rest to gunicorn.
Routing Traefik straight at `web` keeps `/static/` working — whitenoise is in
the middleware stack — but **not** `/media/`: `Conf/urls.py:49-50` serves media
only when `DEBUG`, so every tenant upload would 404 in production. A minimal
nginx bound read-only to `media_volume`, on a `PathPrefix('/media')` router with
priority above the `web` catch-all, closes that hole without putting a proxy hop
in front of every API request.

**Why the dashboard proxies rather than calling cross-origin.** `vercel.json`
today rewrites `/api/*` and `/media/*` to the backend, so the SPA already uses
*relative* paths. Reproducing those rewrites in the dashboard's own nginx keeps
the application same-origin and behaviourally identical to production today.
`VITE_API_BASE_URL` therefore stays **empty**, and no API hostname is baked into
the bundle.

`${DOMAIN}` is a placeholder throughout: a GitHub Actions repository variable
and an `.env` value on the server. The real hostname is supplied before the
first deploy and is not hardcoded anywhere.

Ported from `original_software` substantially unchanged: `traefik.yml`
(including the container-local API that the rolling deploy health-gates
against), `dynamic/middlewares.yml`, and the `socket-proxy` sidecar.

**Deliberately not ported** (YAGNI — no current need):

- `pgbouncer` — one gunicorn service at this scale does not need pooling.
- `celerybeat` — chatbot has no periodic task schedule.
- `flower` — no current operational need for a Celery UI.
- A **custom Postgres image**. `original_software` builds one solely to bundle
  a `backup` script that `deploy.sh` invokes as `exec -T postgres backup`.
  Chatbot uses stock `pgvector/pgvector:pg16` and the backup step is a plain
  `pg_dump` to a mounted volume — same guarantee, one fewer image to build,
  push, scan and version.

### 4.2 Application changes required by deployment

These are the only application-code changes in scope. Each exists because the
deployment topology requires it.

**`chatbot_api`:**

1. **Drain-aware health route.** `Conf/urls.py:13` currently returns
   `{"status": "ok"}` unconditionally. `rollout.sh` retires a replica by
   creating a drain file and waiting for Traefik to observe the backend go
   unhealthy before stopping the container. Without a route that returns 503
   while that file exists, `retire_container` waits out its settle window
   against a probe that never fails and the stop is as abrupt as no rolling at
   all. The route becomes: 503 if `DRAIN_FILE` exists, else 200.
2. **Dockerfile hardening.** Add a non-root user, a `HEALTHCHECK`, and a
   multi-stage build. Align the base image to Python 3.13 — the image
   currently pins `python:3.12-slim` while the local venv runs 3.14 — three
   different minor versions across image, CI and development is a standing
   source of "works locally" failures. 3.13 matches `original_software`;
   adopting it means recreating the local venv, which is a one-line change to
   the developer setup and is called out in Phase 2.
3. **CORS origins.** `Conf/settings.py:45-54` unconditionally appends
   `localhost:5173`, `localhost:3000` and `clickai-alpha.vercel.app` to
   `CORS_ALLOWED_ORIGINS` in *every* environment including production. The new
   hostnames must be configurable and the unconditional appends must become
   `DEBUG`-only. This is cleanup work, but it is a cutover blocker, so it
   belongs here rather than in sub-project 2. Note that the same-origin
   dashboard (item 5) means CORS is no longer on the dashboard's critical path
   — which makes tightening it low-risk rather than something that could break
   the UI.

**`chatbot_dashboard`:**

4. **Multi-stage Dockerfile** — `node:22-alpine` build stage → `nginx:alpine`
   serving `/usr/share/nginx/html`.
5. **nginx config reproducing the Vercel rewrites**, so the SPA stays
   same-origin and no API hostname enters the bundle:
   - `location /api/`  → `proxy_pass` to `web`
   - `location /media/` → `proxy_pass` to `web`
   - `location /` → `try_files $uri $uri/ /index.html` (client-side routing)
   - `client_max_body_size 25M` — carried over from
     `docker/nginx/default.conf`. `MAX_UPLOAD_SIZE_MB` is 20; nginx's default
     is 1 MB, so without this every document upload through the SPA fails with
     413 at the proxy before Django ever sees it.
   - Hashed assets under `/assets/` get a long `immutable` cache; `index.html`
     gets `no-store`, or a deploy serves stale asset references.

### 4.3 Meta-repository layout

New repository `github.com/islam302/chatbot`:

```
chatbot/
├── .github/workflows/{ci,cd,rollback}.yml
├── docker-compose.production.yml
├── traefik/
│   ├── traefik.yml
│   └── dynamic/middlewares.yml
├── scripts/
│   ├── deploy.sh
│   ├── rollback.sh
│   └── lib/{rollout.sh, services.sh}
├── chatbot_api/          → submodule
└── chatbot_dashboard/    → submodule
```

Both submodules keep their own history and their own `main`. The meta-repo owns
deployment configuration only and contains no application code.

## 5. Pipeline

### 5.1 Workflow topology

```
component repo push to main
        │
        ▼
  sync-to-meta.yml ──▶ bumps submodule pointer onto release/next
                        in the meta-repo, opens/updates ONE release PR
        │
        │  (a human merges the release PR — this IS the deploy decision)
        ▼
  meta-repo push to main
        │
        ├──▶ ci.yml  ── lint · test · build · push images to GHCR
        │                └─ final job: "CI Summary"
        ▼
  cd.yml ── wait-for-ci ─▶ verify-images ─▶ deploy
        │
        └─ (on failure) ─▶ rollback.yml, run manually
```

Pointer bumps accumulate on `release/next` rather than landing directly on
`main`. This is the mechanism that prevents a new API from deploying against an
old dashboard: without it, each component merge pushes its own bump to `main`
and each one deploys, so the first of any two deploys a mismatched pair.

### 5.2 `ci.yml`

Triggers: pull request and push to `main`. Concurrency group cancels
in-progress runs on the same ref.

| Job | Contents |
|---|---|
| `changes` | `dorny/paths-filter@v3` → `api` / `dashboard` / `infra` (compose, traefik, scripts) |
| `api-lint` | pre-commit: `ruff-check --fix --exit-non-zero-on-fix`, `ruff-format`, `django-upgrade --target-version 5.2`, plus hygiene hooks incl. `detect-private-key` |
| `api-test` | Postgres 16 + pgvector service container; `makemigrations --check`; `migrate`; the 347 tests |
| `dashboard-lint` | `eslint` and `tsc --noEmit` |
| `dashboard-build` | `npm ci && npm run build` (no API URL needed — the SPA is same-origin) |
| `trivy-api`, `trivy-dashboard` | CRITICAL/HIGH, `exit-code: 0` initially — reports without blocking |
| `build-api`, `build-dashboard` | push-to-main only: buildx → GHCR, tags `type=sha,format=long` + `latest`, `cache-from/to: type=gha`, `provenance` and `sbom` enabled |
| `ci-summary` | the named check `cd.yml` waits on |

**Two decisions inside CI worth stating explicitly:**

*Tests run against Postgres, not SQLite.* The suite passes locally on SQLite in
58s, but production is pgvector on Postgres. The `RAG_VECTOR_BACKEND=pgvector`
code path — `knowledge/services/vector_store.py` and the `setup_pgvector`
management command — is never exercised by SQLite. Testing against the
production engine is what makes the gate meaningful.

*The lint gate is blocking from day one.* Measured against the current
codebase with `line-length = 119` (matching `original_software`), `ruff` reports
**66 violations, 46 auto-fixable** — roughly 20 manual edits, several of which
disappear when sub-project 2 deletes the orphaned modules. One prep commit
takes the gate green before `ci.yml` merges. A non-blocking lint gate that
nobody fixes is worse than none.

### 5.3 `cd.yml`

Triggers: push to `main`, plus `workflow_dispatch` with `image_tag`,
`skip_backup` and `dry_run` inputs.

`wait-for-ci` (waits for the `CI Summary` check) → `verify-images`
(`docker manifest inspect` on both image tags, failing loudly if either is
absent) → `deploy`.

The `deploy` job configures SSH from secrets, rsyncs
`docker-compose.production.yml`, `traefik/` and `scripts/` to the server
(`--delete` on the directories, so a removed middleware file stops being
served), then runs the deploy over SSH.

**Three details ported verbatim from `original_software` because they encode
fixed production bugs, and their explanatory comments come with them:**

1. `if: always() && needs.<job>.result == 'success'` on both `verify-images`
   and `deploy`. `wait-for-ci` is push-only and is *skipped* on
   `workflow_dispatch`; Actions propagates a skip transitively through `needs`.
   Without the guard on `deploy`, a dispatched run inherits the skip, deploys
   nothing, and reports SUCCESS.
2. **Two separate SSH invocations.** `ssh` runs its command argument remotely
   as `bash -c "<string>"`, so a registry token embedded in that string is
   visible in the server's process table for the life of the deploy. The
   `docker login` therefore gets its own invocation with the token piped on
   stdin, never in argv.
3. `< /dev/null` on the deploy invocation. `deploy.sh` runs commands that read
   stdin; without this they consume the SSH channel and the script silently
   truncates.

### 5.4 `rollback.yml`

A separate workflow, reading `PREVIOUS` from the server's `.release-state`
file, or accepting an explicit tag.

Deliberately **not** an input on `cd.yml`. In the reference implementation a
`rollback: true` input skipped the deploy job — which made the run green — and
rolled nothing back, so an operator reaching for rollback under pressure got a
green tick and no action.

### 5.5 `scripts/deploy.sh` and `lib/`

`rollout.sh` (395 lines) and its test suite (483 lines) port essentially
unchanged; they are application-agnostic. `services.sh` is chatbot-specific:

```bash
APP_SERVICES=(
  "web 2 40 true"
  "dashboard 2 30 false"
)
# <service> <steady_replicas> <stop_timeout> <drainable>
```

`web` gets a 40s stop timeout because gunicorn's `graceful_timeout` is 30 — a
30s timeout would land SIGKILL at the exact instant gunicorn finished draining.
`dashboard` is `drainable=false`: a static nginx has no drain route to gate on,
so its replicas are replaced without a settle window.

`media` is deliberately **absent** from this table. It is a single replica
serving a read-only volume mount with no in-flight application state, and its
image and config change only when the deployment config does. It is brought up
alongside the data tier with `--no-deps`, not rolled. Adding it to the rolling
table would demand a second replica and a drain route for a static file server
that has neither reason for one.

Deploy sequence:

1. **Preflight** — compose file present, `.env` present, available memory at or
   above 1200 MiB, Traefik API reachable and reporting `web` backends, replica
   counts at steady state, Traefik service-label parity across services.
   All of this runs *before anything changes*, because a mid-rollout abort
   leaves the system in a state only a human can reconcile.
2. `docker compose pull` (stop here if `--dry-run`).
3. **Data tier up with `--no-recreate`.** Recreating Postgres drops every
   connection; it is never recreated by a deploy.
4. `pg_dump` backup to the backups volume (skippable with `--skip-backup`).
5. `collectstatic`, then `migrate` — both as one-off containers labelled
   `traefik.enable=false`, because a `compose run` container inherits its
   service's labels and would otherwise join the routing pool with nothing
   listening.
6. **Roll `worker` before `web`** — consumers before producers, so a new
   producer never sends a task signature an old consumer rejects.
7. Roll `web`, then `dashboard`, per the service table.
8. **Smoke checks:** `https://api.${DOMAIN}/health/` → 200,
   `https://app.${DOMAIN}/` → 200, and a known `https://api.${DOMAIN}/media/…`
   object → 200. The media probe is not optional: it is the only check that
   exercises the priority router and the volume mount, and `/media/` is the one
   route that silently regressed when the all-in-one nginx was replaced. On
   failure, automatically invoke `rollback.sh` with `PREVIOUS`.
9. Record `.release-state` — **only after** smoke checks pass, so `PREVIOUS` is
   always a tag observed serving real traffic. Then prune dangling images.

## 6. Secrets and configuration

| Secret | Purpose |
|---|---|
| `SUBMODULE_TOKEN` | Fine-grained PAT: checkout private submodules, push `release/next`, open the release PR |
| `SSH_PRIVATE_KEY` | Deploy key for the server |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan` output, pinning the host key |
| `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH` | Server coordinates |
| `GHCR_PAT` | Server-side `docker login ghcr.io` |

| Variable | Purpose |
|---|---|
| `DOMAIN` | Base domain; `api.` and `app.` are derived from it |

**Auth decision.** `original_software` uses a GitHub App
(`GH_APP_ID` / `GH_APP_PRIVATE_KEY`) for cross-repo access. This design starts
with a fine-grained PAT: materially simpler to create and rotate for a
single-owner set of three repositories. A GitHub App is the correct answer if
ownership ever broadens — the migration is a change to the token-acquisition
step of each workflow, so it is contained but touches every workflow file.
Documented here as the deliberate upgrade path.

The server's `.env` is written by hand during provisioning and never enters
Git.

## 7. Phase 0 — provisioning runbook

Provider-neutral: any Ubuntu host reachable over SSH.

**Sizing: 4 vCPU / 8 GB with a swap file.** `DEPLOY.md` sizes the *current*
single-replica stack at 2 vCPU / 4 GB. This topology roughly doubles the
application tier — 2× `web` at 3 gunicorn workers each, 2× `dashboard`, plus
worker, Postgres, Redis and Traefik — and `deploy.sh`'s preflight refuses to
proceed below its free-memory floor. A 4 GB host would boot and then fail the
preflight partway through a rollout, which is the worst place to discover it.
This is a real cost increase over the current box, driven by the zero-downtime
requirement.

Steps:

1. Install Docker Engine and the Compose v2 plugin.
2. `ufw`: allow OpenSSH, 80, 443; enable.
3. Create the `deploy` user; add to the `docker` group.
4. Generate the deploy keypair. Public half → server `authorized_keys`; private
   half → `SSH_PRIVATE_KEY`. `ssh-keyscan <host>` → `SSH_KNOWN_HOSTS`.
5. Create a swap file sized to at least half of RAM.
6. Create `DEPLOY_PATH` and write `.env` by hand from `.env.example`.
7. **DNS A records for `api.${DOMAIN}` and `app.${DOMAIN}` pointing at the
   host, before the first deploy.** Let's Encrypt HTTP-01 resolves the
   hostname; ACME fails without them and Traefik serves its self-signed
   default certificate.
8. Create the external Docker network Traefik attaches to.

## 8. Cutover — data migration and DNS flip

*(Phase 6 in §12.)*

The only irreversible, client-facing step. It runs last, after the new stack is
verified.

1. `pg_dump` the old VPS database; restore to the new host; **verify row counts
   per table** against the source.
2. `rsync` the `media_volume` contents — tenant original uploads.
3. Run `setup_pgvector` on the new host to rebuild the HNSW index from the
   restored `DocumentChunk.embedding` JSON.
4. Deploy the new stack and run smoke checks against the new hostnames while
   the old stack still serves production traffic.
5. Flip DNS. **Keep the old stack running through the TTL window** as the
   rollback path.
6. Retire the Vercel dashboard deployment once the new one is confirmed.

### 8.1 Cutover checklist — silent-failure items

Each of these fails *quietly* if missed. They matter more than the mechanics:

- **Paddle webhook URL** is registered in Paddle's dashboard against the old
  host. Payments stop being fulfilled until it is repointed at
  `api.${DOMAIN}/api/v1/billing/paddle/webhook/`. Verify against a sandbox
  event before the flip.
- **Meta WhatsApp webhook URL** is registered the same way and has its own
  verify-token handshake on re-registration.
- **Tenants calling the old URL with `x-api-key`** break at the flip. There is
  no useful redirect for a POST body — they need advance notice.
- **`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `CORS_ALLOWED_ORIGINS`** must
  contain the new hostnames *before* the flip.
- **`FRONTEND_URL`** is used to build email-verification links
  (`Conf/settings.py:395`). Stale value ⇒ every verification email points at
  the old dashboard.

## 9. Testing strategy

| Layer | How it is verified |
|---|---|
| `rollout.sh` | Its ported 483-line test suite, run in CI |
| Container builds | `ci.yml` builds both images on every PR |
| API test suite | 347 tests against Postgres + pgvector in CI |
| Dashboard | `eslint`, `tsc --noEmit`, production build |
| Full stack | `docker compose` run locally under an override before the VPS ever sees it |
| Deployed stack | `deploy.sh` smoke checks gate every rollout; failure auto-rolls-back |
| Cutover | Row-count verification, webhook replay, and a manual pass over §8.1 |

## 10. Divergences from the reference implementation

| Reference | Here | Why |
|---|---|---|
| 3 submodules | 2 | No separate marketing frontend |
| 4 images | 2 built + stock nginx for `media` | No custom Postgres image; no separate frontend |
| pgbouncer | none | One gunicorn service; no pooling need at this scale |
| celerybeat, flower | none | No periodic schedule; no current UI need |
| `exec -T postgres backup` | `pg_dump` | Follows from dropping the custom Postgres image |
| GitHub App | fine-grained PAT | Single owner; documented upgrade path (§6) |
| `main` + `develop` | `main` only | Matches both repos' existing single-branch state |

## 11. Risks and open items

| Item | Handling |
|---|---|
| **Production DB engine unconfirmed.** `DEPLOY.md` describes Postgres, but the live host has not been inspected; it may have drifted to SQLite. | Confirm before the cutover. If SQLite, §8 step 1 becomes a Django `dumpdata`/`loaddata` migration and §8 step 3 is a full embedding backfill rather than an index rebuild. |
| **Domain not yet chosen.** | Parameterized as `${DOMAIN}` throughout. Blocks only the first deploy, not any implementation work. |
| **Drain route is new code on the hot path.** | Covered by tests in the same commit; `/health/` is exercised by `deploy.sh` smoke checks on every rollout. |
| **Media served by a new component.** `/media/` moves from the old all-in-one nginx to a dedicated service on a priority router. | Smoke-check a known media URL during the cutover rehearsal (§8), not just `/health/` and `/`. |
| **Lint gate lands before the cleanup sub-project.** | One prep commit clears the measured 66 violations (§5.2). |
| **First rolling deploy on a topology no one has run before.** | `--dry-run` first; local compose-override rehearsal before the VPS; old stack stays live through cutover. |

## 12. Implementation phasing

| Phase | Deliverable |
|---|---|
| 0 | Provisioning runbook executed; host reachable; DNS live |
| 1 | Meta-repo created, submodules wired, `sync-to-meta.yml` in both component repos |
| 2 | Dockerfiles (API hardening, dashboard new + nginx proxy config), drain route, CORS fix, lint prep commit, local venv on 3.13 |
| 3 | `docker-compose.production.yml` incl. the `media` service + Traefik config; full stack verified locally |
| 4 | `ci.yml` green on both repos; images publishing to GHCR |
| 5 | `scripts/` ported; `cd.yml` + `rollback.yml`; first `--dry-run`, then first real deploy to the new host |
| 6 | Data migration, cutover per §8, old stack retired |

Sub-projects 2–4 begin after Phase 6.
