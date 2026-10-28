# nginx-Fronted Gateway and Explicit Hostnames Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the existing host nginx in front of Traefik as the TLS-terminating gateway, and replace the derived `api.${DOMAIN}` / `app.${DOMAIN}` hostname scheme with two explicit, independent hostnames.

**Architecture:** nginx on the host keeps :80 and :443 and terminates TLS with certbot-issued certificates. Traefik drops to a single plaintext entrypoint published on `127.0.0.1:8081` only, so the stack is unreachable except through nginx. Traefik keeps everything that makes the rolling deploy work — the docker provider, the container-local API, and the per-service load-balancer healthchecks that the drain gate depends on — because all of that is Traefik→container and is unaffected by what sits in front.

**Tech Stack:** Traefik v3.6.25 (file + docker providers), nginx on the host, certbot, Docker Compose v2, Bash 5.

**Spec:** `chatbot_api/docs/superpowers/specs/2026-09-03-infrastructure-pipeline-design.md` — this plan amends §4.1 (topology), §6 (configuration) and §7 (provisioning). Where this plan and the spec disagree, **this plan wins**: the spec assumes Traefik owns 80/443 and obtains its own certificates, which is no longer true.

**Predecessor:** `2026-09-04-deploy-rollback-cd.md` (Plan 3) — merged as PR #3. Every file this plan touches was created or last modified there.

## Global Constraints

- **Hostnames are `api-demo-chatbot.abdulkhalek.dev` and `app-demo-chatbot.abdulkhalek.dev`.** They are flat, independent, and share no derivable prefix. Nothing may reconstruct one from the other.
- **`abdulkhalek.dev` is shared across several servers.** This host owns exactly these two names. No wildcard certificate exists and none should be assumed.
- **nginx runs on the host** (not in a container) and keeps :80 and :443.
- **Traefik publishes to `127.0.0.1:8081` only.** Never `0.0.0.0` — a publicly-bound Traefik port serves the entire stack over plain HTTP, bypassing nginx.
- **TLS is certbot's.** Traefik terminates nothing and requests nothing.
- **ACME configuration stays in `traefik.yml`, dormant.** It is on-demand: with no router naming a `certresolver` and no `domains` declared, it never fires. Leaving it makes a future move back to a Traefik-owned 80/443 a port change plus four labels.
- **`web`'s stop timeout stays 40s**; gunicorn's `--graceful-timeout` is 30.
- **`SECURE_SSL_REDIRECT` must stay `False`.** Traefik health-probes over plain HTTP; a 301 reads as unhealthy against the `healthcheck.status=200` label and drops every backend.
- Bash: `#!/usr/bin/env bash`, `set -euo pipefail`, `shellcheck -x` clean. Workflows `actionlint` clean.
- `shellcheck` and `actionlint` are at `~/.local/bin`, **not** on the default PATH — `export PATH=~/.local/bin:$PATH`.
- The submodules `chatbot_api` and `chatbot_dashboard` show as modified. That is pre-existing. **Never stage them**, and run `git status --short` before every commit — `git commit` with no pathspec commits the whole index.

---

## File Structure

| File | Change |
|---|---|
| `scripts/lib/services.sh` | `smoke_probes` takes two hosts and emits loopback probes alongside the public ones |
| `scripts/deploy.sh` | reads `API_HOST`/`APP_HOST`; refuses if either is missing |
| `scripts/rollback.sh` | same |
| `scripts/lib/rollout.test.sh` | fixture `.env` gains both hosts; new assertions for the loopback probes |
| `docker-compose.production.yml` | port binding, router entrypoints, TLS labels, `${API_HOST}`/`${APP_HOST}` |
| `traefik/traefik.yml` | drop the `web` redirect, add `forwardedHeaders.trustedIPs`, document the dormant blocks |
| `traefik/dynamic/middlewares.yml` | `sourceCriterion` on both rate limiters |
| `.env.example` | `API_HOST`/`APP_HOST`; the Django host settings updated to match |
| `.github/workflows/cd.yml` | `vars.API_HOST` / `vars.APP_HOST` |
| `docs/nginx/*.conf` | **new** — the two vhosts, as reviewable reference |
| `docs/DEPLOY.md` | topology, provisioning, first-deploy checklist |

---

## Task 1: Explicit hostnames

Mechanical and self-contained. Nothing about the gateway changes yet, so the
stack must still work end to end at the end of this task.

**Files:**
- Modify: `scripts/lib/services.sh`, `scripts/deploy.sh`, `scripts/rollback.sh`
- Modify: `scripts/lib/rollout.test.sh` (the `setup_proj` fixture)
- Modify: `docker-compose.production.yml`, `.env.example`, `.github/workflows/cd.yml`

**Interfaces:**
- Consumes: `envval` from `services.sh`.
- Produces: `smoke_probes <api_host> <app_host> <media_path>` — a **three-argument** signature. Task 3 adds a fourth. Both later tasks depend on the argument order.

- [ ] **Step 1: Change `smoke_probes` to take two hosts**

In `scripts/lib/services.sh`, replace the function with:

```bash
# The probes that gate a deploy and verify a rollback, as "<url>|<want>".
# ONE definition, so a rollback can never verify less than the deploy gated on.
#
# Two independent hostnames, not a base domain with derived prefixes. They share
# a parent today, but nothing here may depend on that: `abdulkhalek.dev` is used
# by several unrelated servers, and this host owns exactly these two names.
#
# The media probe is not optional: it is the only check that exercises the
# priority router and the shared volume mount, and /media/ is the one route that
# silently regressed when the all-in-one nginx was replaced.
smoke_probes() {
  local api_host="$1" app_host="$2" media_path="$3"
  printf '%s\n' \
    "https://${api_host}/health/|200" \
    "https://${app_host}/|200" \
    "https://${api_host}/media/${media_path}|200"
}
```

- [ ] **Step 2: Read both hosts in `deploy.sh`**

Replace the `DOMAIN` block (around lines 33-41) with:

```bash
# The smoke checks need the deployed hostnames, which live in the server's .env
# (hand-written at provisioning, never in Git). envval and smoke_probes come
# from services.sh, so a rollback reads the same values through the same code.
#
# Two independent keys, not a base domain: this host's names share a parent by
# coincidence, and `abdulkhalek.dev` belongs to several other servers.
API_HOST="$(envval API_HOST)"
APP_HOST="$(envval APP_HOST)"
[ -n "$API_HOST" ] || { echo "API_HOST is not set in .env" >&2; exit 1; }
[ -n "$APP_HOST" ] || { echo "APP_HOST is not set in .env" >&2; exit 1; }
```

Keep the `MEDIA_PROBE_PATH` and `SMOKE_CURL_EXTRA` lines that follow unchanged.
Then update the smoke loop's terminator:

```bash
done < <(smoke_probes "$API_HOST" "$APP_HOST" "$MEDIA_PROBE_PATH")
```

- [ ] **Step 3: The same two edits in `rollback.sh`**

Replace lines 25-27 with the same `API_HOST`/`APP_HOST` block (keeping the
`# envval and smoke_probes come from services.sh.` comment), and update its
verification loop's terminator identically.

- [ ] **Step 4: Update the compose labels**

Four occurrences in `docker-compose.production.yml`:

```
traefik.http.routers.api.rule=Host(`${API_HOST}`)
traefik.http.services.web.loadbalancer.healthcheck.hostname=${API_HOST}
traefik.http.routers.media.rule=Host(`${API_HOST}`) && PathPrefix(`/media`)
traefik.http.routers.app.rule=Host(`${APP_HOST}`)
```

The `healthcheck.hostname` comment explaining why it exists (Traefik probes the
container IP; without an explicit Host, Django's `ALLOWED_HOSTS` rejects it with
400 and every backend reads as dead) is still true — keep it.

- [ ] **Step 5: Update the test fixture**

`scripts/lib/rollout.test.sh`'s `setup_proj` writes `DOMAIN=example.test`. Both
scripts now refuse without both keys, so every end-to-end assertion fails until
this changes:

```bash
    printf 'API_HOST=api.example.test\nAPP_HOST=app.example.test\n' > "$PROJ/.env"
```

- [ ] **Step 6: Update `.env.example`**

Replace the `DOMAIN` block:

```bash
# --- Deployment identity -----------------------------------------------------
# The two public hostnames. Independent values, NOT derived from a shared base:
# abdulkhalek.dev is used by several unrelated servers and this host owns only
# these two names.
#
# Both need A records pointing at this host before certbot runs. They are NOT a
# precondition for a deploy -- nginx terminates TLS and certbot issues the
# certificates, so nothing in the deploy path talks to Let's Encrypt.
API_HOST=api-demo-chatbot.abdulkhalek.dev
APP_HOST=app-demo-chatbot.abdulkhalek.dev
```

And the Django block, which must match:

```bash
# MUST include both hostnames and localhost: Traefik's load-balancer healthcheck
# sends Host: ${API_HOST}, the dashboard's nginx proxies with Host: ${APP_HOST},
# and the container's own HEALTHCHECK probes over loopback. Miss one and every
# backend reads as unhealthy.
ALLOWED_HOSTS=api-demo-chatbot.abdulkhalek.dev,app-demo-chatbot.abdulkhalek.dev,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://api-demo-chatbot.abdulkhalek.dev,https://app-demo-chatbot.abdulkhalek.dev
# The dashboard is same-origin, so this is only for third-party browser
# clients. Leave empty unless one exists.
CORS_ALLOWED_ORIGINS=
FRONTEND_URL=https://app-demo-chatbot.abdulkhalek.dev
```

- [ ] **Step 7: Update `cd.yml`**

Three places use `vars.DOMAIN`. Replace with `vars.API_HOST` and `vars.APP_HOST`:
the `environment.url` becomes `https://${{ vars.APP_HOST }}`, and the summary's
`env:` block and its two table rows use the two variables directly. These feed a
summary and a link only — `deploy.sh` reads the authoritative values from the
server's `.env`.

- [ ] **Step 8: Verify**

```bash
export PATH=~/.local/bin:$PATH
cd /home/abdulkhalek/Projects/chatbot
bash scripts/lib/rollout.test.sh 2>&1 | tail -1
shellcheck -x scripts/*.sh scripts/lib/*.sh && echo SHELLCHECK_OK
actionlint && echo ACTIONLINT_OK
API_HOST=a.test APP_HOST=b.test POSTGRES_PASSWORD=x \
  docker compose -f docker-compose.production.yml config >/dev/null && echo RENDER_OK
grep -rn 'DOMAIN' scripts/ docker-compose.production.yml .github/workflows/cd.yml | grep -v '^.*#'
```

Expected: `passed: 97  failed: 0`, `SHELLCHECK_OK`, `ACTIONLINT_OK`, `RENDER_OK`,
and the final `grep` returning **nothing** outside comments. A surviving
`${DOMAIN}` in a compose label renders as an empty host rule, which matches
every request — it fails open, not closed, so grep for it rather than trusting
the render to catch it.

- [ ] **Step 9: Commit**

```bash
git add scripts/ docker-compose.production.yml .env.example .github/workflows/cd.yml
git commit -m "refactor: two explicit hostnames instead of a derived base domain

api.\${DOMAIN} / app.\${DOMAIN} baked in an assumption that never held here:
abdulkhalek.dev is shared across several unrelated servers and this host owns
two flat names that share no derivable prefix. API_HOST and APP_HOST are
independent, and nothing reconstructs one from the other.

smoke_probes now takes both hosts; deploy.sh and rollback.sh refuse if either
is missing rather than probing a half-formed URL."
```

---

## Task 2: Traefik behind nginx

The risky task. Between the port change and a working nginx vhost the stack is
unreachable from outside, which is expected — the local rehearsal is what proves
it works, and the rehearsal talks to `127.0.0.1:8081` directly.

**Files:**
- Modify: `traefik/traefik.yml`, `traefik/dynamic/middlewares.yml`, `docker-compose.production.yml`

- [ ] **Step 1: Rework the entrypoints**

In `traefik/traefik.yml`, replace the `entryPoints` block:

```yaml
entryPoints:
  # Plaintext, and published on 127.0.0.1 only (see the ports: block in
  # docker-compose.production.yml). The host's nginx terminates TLS and proxies
  # here; nothing outside the host can reach this.
  #
  # There is deliberately NO redirection to web-secure. nginx already redirects
  # http->https at the edge, and a redirect here would bounce nginx's proxied
  # request straight back to nginx, which re-proxies it: an infinite loop, and
  # the first thing that breaks if this is reintroduced.
  web:
    address: ":80"
    # NOTE: start with only the 127.0.0.1/32 entry below. Every element of
    # trustedIPs must parse as an IP or CIDR -- a placeholder string makes
    # Traefik fail to start, so there is no "fill it in later" state you can
    # boot into. Step 7 measures the real address and adds it.
    forwardedHeaders:
      # Traefik overwrites X-Forwarded-* from an untrusted source. Without this,
      # every request looks like it came from nginx: the access log records the
      # proxy's address instead of the client's, and the rate-limit middlewares
      # key every tenant into ONE shared bucket.
      #
      # The value is the source address Traefik actually sees for a request
      # arriving via a published port, which is NOT 127.0.0.1 -- Docker DNATs
      # it and the container sees the bridge gateway. Step 7 determines it
      # empirically; do not guess it.
      trustedIPs:
        - "127.0.0.1/32"

  # Dormant. Nothing routes here while nginx terminates TLS, and it is not
  # published, so it binds only inside the container. Kept so that moving this
  # stack to a host where Traefik owns 80/443 is a ports change plus four
  # labels rather than a re-derivation.
  web-secure:
    address: ":443"

  # Container-local ONLY. Traefik's API exposes the entire routing
  # configuration, so this must never bind 0.0.0.0 or be published: binding
  # 127.0.0.1 means only a process inside this container's network namespace
  # (i.e. `docker exec`) can read it.
  traefik:
    address: "127.0.0.1:8080"
```

- [ ] **Step 2: Mark the ACME block dormant**

Do not delete it. Add above `certificatesResolvers`:

```yaml
# DORMANT while nginx terminates TLS. ACME is on-demand: no router names a
# certresolver and no `domains` are declared here, so Traefik never contacts
# Let's Encrypt. It is kept, not deleted, because re-enabling it is the whole
# difference between this topology and a Traefik-owned 80/443 one.
#
# If a router is ever given `tls.certresolver=letsencrypt` while Traefik is
# behind nginx, the httpChallenge below will fail forever: it answers on the
# `web` entrypoint, which is not publicly reachable.
```

- [ ] **Step 3: Key the rate limiters on the real client**

In `traefik/dynamic/middlewares.yml`, add a `sourceCriterion` to **both**
limiters. For `rate-limit-api`:

```yaml
    rate-limit-api:
      rateLimit:
        average: 50
        burst: 100
        period: 1s
        # Behind a proxy, the default source criterion is the connecting
        # address -- which is nginx, for every request. That silently turns a
        # per-client limit into one 50 req/s bucket shared by every tenant.
        #
        # excludedIPs, not depth. Both read X-Forwarded-For right to left and
        # both resist forgery given nginx's $proxy_add_x_forwarded_for (which
        # APPENDS the real peer, so the rightmost entry is what nginx actually
        # saw). They differ in how they FAIL: with no X-Forwarded-For at all --
        # a misconfigured vhost, or anything reaching Traefik without going
        # through nginx -- `depth` yields an empty source and every such request
        # lands in ONE shared bucket, which is exactly the collapse this
        # sourceCriterion exists to prevent, arriving silently. excludedIPs
        # falls back to the connecting peer instead. Step 10 proves the
        # degraded path, not just the happy one.
        sourceCriterion:
          ipStrategy:
            excludedIPs:
              - "172.18.0.1/32"
```

Same block on `rate-limit-static`, with its own `average: 100` / `burst: 200`.

The excluded address is the trusted proxy — the same one Step 7 measures for
`trustedIPs`. Leave it as the placeholder `172.18.0.1/32` for now and correct
it in Step 7 alongside the entrypoint, so the two cannot disagree.

**If `excludedIPs` does not behave as described when you test it in Step 10**,
say so with the measurement and fall back to `depth: 1` with the limitation
written into the comment. Do not keep a strategy whose failure mode you have
not observed.

- [ ] **Step 4: Move the routers off TLS**

In `docker-compose.production.yml`, for all three routers (`api`, `media`,
`app`): change `entrypoints=web-secure` to `entrypoints=web`, and **delete** the
`tls=true` and `tls.certresolver=letsencrypt` labels. Leave every other label —
the middlewares, the service port, the healthcheck labels and `media`'s
`priority=100` — untouched.

Add above the `api` router's labels:

```yaml
      # Plain http on the `web` entrypoint: the host's nginx terminates TLS and
      # proxies here over loopback. No tls/certresolver labels -- see the
      # dormant ACME note in traefik/traefik.yml.
```

- [ ] **Step 5: Rebind the published port**

Replace the `traefik` service's `ports:` block:

```yaml
    ports:
      # 127.0.0.1 ONLY. Published on 0.0.0.0 this serves the entire stack over
      # plain http on 8081, bypassing nginx's TLS, its rate limits and its
      # access log -- and ufw does not help, because Docker's iptables rules
      # are inserted ahead of it.
      - "127.0.0.1:8081:80"
```

- [ ] **Step 6: Bring the stack up**

```bash
cd /home/abdulkhalek/Projects/chatbot
export IMAGE_TAG=rehearsal-b   # or whatever tag is present locally
docker compose -f docker-compose.production.yml up -d
sleep 90
docker ps --filter name='^chatbot-' --format '{{.Names}}\t{{.Status}}' | sort
```

Expected: ten containers, the eight with healthchecks all `healthy`.

Then create the media probe object. A plain `compose up` does not write it --
`deploy.sh` does, after `migrate` -- so on a fresh volume the media route would
404 and look like a routing fault:

```bash
docker compose -f docker-compose.production.yml run --rm --no-deps \
  -l traefik.enable=false -T web \
  sh -c 'mkdir -p /app/media/probe && printf ok > /app/media/probe/health.txt' </dev/null
```

- [ ] **Step 7: Determine the real trusted source address**

Do not guess this, and note it can only be measured now -- the port had to be
published (Step 5) and the stack running (Step 6) first. Send a request through
the published port and read what Traefik logged:

```bash
cd /home/abdulkhalek/Projects/chatbot
curl -s -o /dev/null -H "Host: api-demo-chatbot.abdulkhalek.dev" http://127.0.0.1:8081/health/
docker exec chatbot-traefik-1 tail -1 /var/log/traefik/access.log \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ClientAddr:", d.get("ClientAddr"), "| ClientHost:", d.get("ClientHost"))'
docker network inspect chatbot_default --format '{{range .IPAM.Config}}{{.Subnet}} gw={{.Gateway}}{{end}}'
```

Use the observed `ClientHost` to fill in the `trustedIPs` entry — as a `/32` if
it is stable, or as the bridge subnet if it varies. Record which you chose and
why in your report. If `ClientHost` is already the real client address without
`trustedIPs` set, say so and stop: the entry may be unnecessary, and an
unnecessary `trustedIPs` entry widens what Traefik will believe.

`traefik.yml` is **static** configuration: it does not hot-reload. Apply the
value and recreate the container, or every check below still runs against the
placeholder:

```bash
docker compose -f docker-compose.production.yml up -d --no-deps --force-recreate traefik
sleep 10
docker exec chatbot-traefik-1 wget -qO- http://127.0.0.1:8080/api/entrypoints \
  | tr ',' '\n' | grep -i 'trusted' || echo "no trustedIPs reported -- check the file took"
```

- [ ] **Step 8: Prove the routes**

```bash
A=api-demo-chatbot.abdulkhalek.dev
P=app-demo-chatbot.abdulkhalek.dev
for probe in "$A|/health/|200" "$A|/api/v1/|401" "$A|/media/probe/health.txt|200" \
             "$P|/|200" "$P|/api/v1/|401"; do
  h=${probe%%|*}; rest=${probe#*|}; path=${rest%%|*}; want=${rest##*|}
  got=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -H "Host: $h" "http://127.0.0.1:8081$path")
  printf '%-40s %-28s got=%s want=%s\n' "$h" "$path" "$got" "$want"
done
```

Expected: every `got` equals its `want`. Two failures worth naming, because
they look alike and mean different things — **`301` means the entrypoint
redirect survived Step 1** (this is the infinite-loop bug, caught here before
nginx can loop on it), and **`400` means `ALLOWED_HOSTS` in `.env` does not
contain the hostname**, which is a `.env` fix and not a code one.

- [ ] **Step 9: Prove the port is not publicly bound**

```bash
ss -ltnp 2>/dev/null | grep -E ':8081|:443|:80 ' || sudo ss -ltnp | grep -E ':8081|:443|:80 '
```

Expected: `8081` bound to `127.0.0.1` and **not** `0.0.0.0` or `*`. If it shows
`0.0.0.0:8081`, the ports change did not take — recreate the container rather
than reasoning about it.

- [ ] **Step 10: Prove the rate limiter keys on the client, not on nginx**

This is the finding that is invisible to review and only shows up as an outage
under load. The file provider hot-reloads, so no restart is needed.

```bash
cd /home/abdulkhalek/Projects/chatbot
cp traefik/dynamic/middlewares.yml /tmp/mw.bak
# Temporarily make the limit trivially reachable.
sed -i 's/^        average: 50$/        average: 1/; s/^        burst: 100$/        burst: 1/' \
  traefik/dynamic/middlewares.yml
sleep 5   # file provider watch interval

hammer() { # hammer <xff-value>
  for _ in $(seq 1 12); do
    curl -s -o /dev/null -m 5 -w '%{http_code}\n' \
      -H "Host: api-demo-chatbot.abdulkhalek.dev" \
      -H "X-Forwarded-For: $1" http://127.0.0.1:8081/health/
  done | sort | uniq -c
}
echo "--- client 203.0.113.10 ---"; hammer 203.0.113.10
echo "--- client 198.51.100.20 (should NOT inherit the first client's bucket) ---"; hammer 198.51.100.20

cp /tmp/mw.bak traefik/dynamic/middlewares.yml && sleep 5
```

Expected: the first block shows some `429`s (the limit works at all), and the
second block shows a **fresh** allowance rather than being immediately
throttled — proving the two clients have independent buckets. If the second
client is throttled from its first request, the limiter is still keyed on
nginx's address: `trustedIPs` or `sourceCriterion` is wrong. Report the actual
tallies either way.

Confirm the revert took: `grep -n 'average:' traefik/dynamic/middlewares.yml`
must show `50` and `100`.

- [ ] **Step 11: Confirm the deploy machinery still works**

The whole point of keeping Traefik is that the rolling deploy is unaffected.
Prove it rather than assuming:

```bash
docker exec chatbot-traefik-1 wget -qO- http://127.0.0.1:8080/api/http/services/web@docker \
  | tr ',' '\n' | grep -cE '"UP"'
```

Expected: `2`. This is the query `traefik_backends_up` makes, and the gate every
`roll_service` call depends on.

- [ ] **Step 12: Commit**

```bash
git add traefik/ docker-compose.production.yml
git commit -m "feat: run Traefik behind the host nginx on loopback

nginx keeps :80/:443 and terminates TLS with certbot certificates; Traefik
drops to one plaintext entrypoint published on 127.0.0.1:8081, so the stack is
unreachable except through nginx.

Three things this had to get right. The web entrypoint's http->https
redirection is removed: nginx already redirects at the edge, and a redirect
here bounces nginx's own proxied request back to nginx, which re-proxies it.
forwardedHeaders.trustedIPs is set to the address Traefik actually observes for
a DNATed request, so X-Forwarded-For survives. And both rate limiters gained an
explicit sourceCriterion -- without it the default criterion is the connecting
address, which is nginx for every request, collapsing a per-client limit into
one bucket shared by every tenant.

ACME stays in traefik.yml, dormant and commented: no router names a
certresolver, so it never fires, and re-enabling it is the whole difference
between this topology and a Traefik-owned 80/443 one.

Verified against the running stack: five routes correct through 127.0.0.1:8081,
8081 bound to loopback only, two clients rate-limited independently, and
Traefik's API still reporting 2 UP web backends -- the gate roll_service
depends on."
```

---

## Task 3: The loopback smoke probe and the nginx vhosts

**Files:**
- Modify: `scripts/lib/services.sh`, `scripts/deploy.sh`, `scripts/rollback.sh`, `scripts/lib/rollout.test.sh`
- Create: `docs/nginx/api-demo-chatbot.conf`, `docs/nginx/app-demo-chatbot.conf`

**Interfaces:**
- Consumes: `smoke_probes <api_host> <app_host> <media_path>` from Task 1.
- Produces: `smoke_probes <api_host> <app_host> <media_path> [gateway_url]` — a
  fourth, optional argument. Both callers pass it.

- [ ] **Step 1: Add loopback probes to `smoke_probes`**

Append to the function, keeping the existing three:

```bash
smoke_probes() {
  local api_host="$1" app_host="$2" media_path="$3"
  local gateway="${4:-http://127.0.0.1:8081}"
  printf '%s\n' \
    "https://${api_host}/health/|200" \
    "https://${app_host}/|200" \
    "https://${api_host}/media/${media_path}|200"
  # The same two routes again, but straight at Traefik's loopback port with an
  # explicit Host header, bypassing nginx entirely.
  #
  # Without these, an nginx misconfiguration and a broken release are the same
  # observation: the public probes fail, deploy.sh auto-rolls-back, and a
  # perfectly good image is reverted for a fault one layer above it. With them,
  # public-fails + loopback-passes says "the application is fine, fix the
  # gateway" -- which is not a conclusion the operator should have to derive
  # under pressure.
  printf '%s\n' \
    "${gateway}/health/|200|${api_host}" \
    "${gateway}/|200|${app_host}"
}
```

The loopback entries carry a **third** field: the `Host` header to send. The
public entries have only two, so the parsers in both callers must treat the
third as optional.

- [ ] **Step 2: Parse the optional Host field in `deploy.sh`**

Replace the smoke loop's body:

```bash
while IFS= read -r probe; do
  url=${probe%%|*}; rest=${probe#*|}
  want=${rest%%|*}
  # Third field is the Host header, present only on the loopback probes. When
  # the probe has only two fields, `rest` has no second `|` and `${rest#*|}`
  # returns `rest` unchanged -- hence the equality test rather than a -z test.
  host=${rest#*|}; [ "$host" = "$want" ] && host=""
  host_arg=(); [ -n "$host" ] && host_arg=(-H "Host: ${host}")
  # `|| true`, not `|| echo 000`: curl already prints 000 on a connection
  # failure AND exits non-zero, so `|| echo 000` yields the string "000000".
  # shellcheck disable=SC2086  # SMOKE_CURL_EXTRA is a deliberate argument list
  got=$(curl -s -o /dev/null -m 15 -w '%{http_code}' $SMOKE_CURL_EXTRA "${host_arg[@]}" "$url" || true)
  printf '  %-58s -> %s (want %s)\n' "${url}${host:+  [Host: $host]}" "$got" "$want"
  [ "$got" = "$want" ] || smoke_failed=true
done < <(smoke_probes "$API_HOST" "$APP_HOST" "$MEDIA_PROBE_PATH")
```

- [ ] **Step 3: The same parser in `rollback.sh`**

Identical, except the failure action stays `{ echo "ERROR: rollback verification failed" >&2; exit 1; }`.

- [ ] **Step 4: Assert the loopback probes exist and are distinguishable**

Add to the end-to-end section of `scripts/lib/rollout.test.sh`, before the
`service_labels_match` banner:

```bash
# --- smoke_probes emits both public and loopback probes -------------------
out=$(smoke_probes api.example.test app.example.test probe/t.txt)
check "smoke_probes emits five probes" "5" "$(grep -c . <<<"$out")"
check "three probes are public https" "3" \
  "$(grep -c '^https://' <<<"$out" || true)"
check "two probes go straight at the gateway" "2" \
  "$(grep -c '^http://127.0.0.1:8081' <<<"$out" || true)"
check "the loopback probes carry a Host header field" "2" \
  "$(grep -cE '^http://127\.0\.0\.1:8081[^|]*\|200\|[a-z.]+$' <<<"$out" || true)"
check "the gateway URL is overridable" "1" \
  "$(smoke_probes a.test b.test t.txt http://127.0.0.1:9999 | grep -c '9999' || true)"
```

- [ ] **Step 5: Prove a gateway fault is distinguishable from an app fault**

The assertions above prove the probes are emitted. This proves they do what they
are for. With the stack up from Task 2:

```bash
cd /home/abdulkhalek/Projects/chatbot
# The loopback probe must pass while the public one cannot resolve at all.
curl -s -o /dev/null -m 5 -w 'loopback: %{http_code}\n' \
  -H "Host: api-demo-chatbot.abdulkhalek.dev" http://127.0.0.1:8081/health/
curl -s -o /dev/null -m 5 -w 'public:   %{http_code}\n' \
  https://api-demo-chatbot.abdulkhalek.dev/health/ || echo "public:   unreachable"
```

Expected: `loopback: 200` and a failing/unreachable public probe, since nginx is
not configured yet and DNS may not resolve. That asymmetry is exactly the signal
the loopback probe exists to produce — record it verbatim in your report.

- [ ] **Step 6: Write the nginx vhosts**

These are **reference copies**. CD cannot deploy them: they live outside
`DEPLOY_PATH` and are root-owned. They are in the repo so the gateway config is
reviewable rather than existing only on one server.

`docs/nginx/api-demo-chatbot.conf`:

```nginx
# Host nginx vhost for the chatbot API. Reference copy -- the live file is
# /etc/nginx/sites-available/api-demo-chatbot.abdulkhalek.dev and is NOT
# deployed by CD. Keep the two in sync by hand; docs/DEPLOY.md §2 says so.
#
# certbot --nginx manages the ssl_certificate lines and the :80 redirect block.

server {
    listen 80;
    listen [::]:80;
    server_name api-demo-chatbot.abdulkhalek.dev;
    # certbot rewrites this to a 301 to https and adds its own challenge
    # location. Left here so the vhost is valid before the first certbot run.
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name api-demo-chatbot.abdulkhalek.dev;

    # certbot fills these in.
    # ssl_certificate     /etc/letsencrypt/live/api-demo-chatbot.abdulkhalek.dev/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/api-demo-chatbot.abdulkhalek.dev/privkey.pem;

    # Matches the dashboard's internal nginx. Knowledge-base document uploads
    # are the reason: nginx's 1m default rejects them with 413 and the error
    # never reaches the application, so it looks like a client bug.
    client_max_body_size 25M;

    # RAG and LLM calls run long. The dashboard's internal nginx allows 120s
    # for /api/; a shorter timeout here truncates the same request one hop
    # earlier and returns 504.
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;

    # Deliberately NO Strict-Transport-Security here. Traefik's secure-headers
    # middleware already sets it, and adding it in both places sends the header
    # twice.

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        # APPENDS the real peer rather than replacing the header, so a client
        # sending its own X-Forwarded-For cannot displace the true address.
        # Traefik's rate limiters read the rightmost entry (ipStrategy.depth 1).
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP         $remote_addr;
    }
}
```

`docs/nginx/app-demo-chatbot.conf` is the same file with
`app-demo-chatbot.abdulkhalek.dev` throughout. Keep both settings: the dashboard
proxies `/api/` same-origin to `web:8000`, so uploads and slow RAG calls
traverse this vhost too.

- [ ] **Step 7: Verify**

```bash
export PATH=~/.local/bin:$PATH
cd /home/abdulkhalek/Projects/chatbot
bash scripts/lib/rollout.test.sh 2>&1 | tail -1
shellcheck -x scripts/*.sh scripts/lib/*.sh && echo SHELLCHECK_OK
# nginx is on the host; if it is installed, syntax-check the reference copies.
command -v nginx >/dev/null && nginx -t -c /dev/null 2>&1 | head -2 || \
  echo "note: nginx not invocable here; vhosts checked by review only"
```

Expected: `passed: 102  failed: 0` (97 + 5), `SHELLCHECK_OK`. `nginx -t` against
a fragment will not work without a full config — if you cannot syntax-check
them, say so plainly rather than claiming they are validated.

- [ ] **Step 8: Commit**

```bash
git add scripts/ docs/nginx/
git commit -m "feat: loopback smoke probes and the nginx vhost reference

deploy.sh's smoke checks all went through nginx, so an nginx fault and a broken
release produced the same observation -- and deploy.sh would auto-roll-back a
healthy image for a fault one layer above it. Two probes now go straight at
Traefik's loopback port with an explicit Host header: public-fails plus
loopback-passes says 'the application is fine, fix the gateway'.

The two host vhosts are committed as reference copies. CD cannot deploy them --
they live outside DEPLOY_PATH and are root-owned -- but config that exists only
on one server is config nobody can review. Both carry client_max_body_size 25M
and a 120s proxy timeout to match the dashboard's internal nginx, and neither
sets HSTS, which Traefik's secure-headers already does."
```

---

## Task 4: The runbook

**Files:**
- Modify: `docs/DEPLOY.md`

- [ ] **Step 1: Rewrite §1 (Topology)**

The request path gains a hop. State it explicitly:

```
client → nginx (host, :443, certbot TLS) → 127.0.0.1:8081 → Traefik → containers
```

Say that Traefik terminates nothing, requests no certificates, and is reachable
only over loopback; and that the load-balancer healthchecks and the drain gate
are Traefik→container and are unaffected by anything in front.

- [ ] **Step 2: Rewrite §2 (Provisioning)**

Replace the ufw and DNS/ACME steps:

- ufw allows OpenSSH, 80 and 443 **for nginx**. Traefik needs no firewall rule —
  it is published on loopback. Note that Docker inserts its iptables rules ahead
  of ufw's, so a `0.0.0.0` port publication would be reachable regardless of
  what ufw says; loopback binding is the control, not the firewall.
- A and AAAA records for both hostnames must exist **before certbot runs**, not
  before the first deploy. Certificates are decoupled from deploys entirely.
- Install the two vhosts from `docs/nginx/`, then
  `certbot --nginx -d api-demo-chatbot.abdulkhalek.dev -d app-demo-chatbot.abdulkhalek.dev`.
  Renewal is certbot's existing timer; no Traefik involvement.
- Keep the "bring the stack up by hand once" step — `deploy.sh` deploys, it does
  not bootstrap, and it refuses unless `web` is already at 2 replicas with
  Traefik backends registered.

- [ ] **Step 3: Update §3 (Secrets and variables)**

`DOMAIN` becomes `API_HOST` and `APP_HOST`, as repository **variables**. Note
they feed `cd.yml`'s environment URL and job summary only; `deploy.sh` reads the
authoritative values from the server's `.env`.

- [ ] **Step 4: Extend §4 (A normal deploy) with the loopback probes**

Five smoke probes now, not three. Give the operator the decision table:

| Public | Loopback | Means |
|---|---|---|
| fail | pass | The application is healthy. The fault is nginx, certbot or DNS. `deploy.sh` will still have rolled back — re-deploy after fixing the gateway. |
| fail | fail | The release is bad. The auto-rollback was correct. |
| pass | fail | Should be impossible; nginx is proxying somewhere other than `127.0.0.1:8081`. |

- [ ] **Step 5: Document the vhost sync requirement in §2**

`docs/nginx/*.conf` are reference copies; the live files are under
`/etc/nginx/sites-available/` and CD cannot deploy them. Say so in §2, name both
paths, and state plainly that the two are kept in sync by hand. The vhost files
point a reader here, so this section has to actually contain the instruction.

- [ ] **Step 6: Add a gateway section to §8 (Known residuals)**

Two new entries, stated as trade-offs:

- **A second proxy hop.** nginx's `client_max_body_size` and `proxy_read_timeout`
  must stay at or above the dashboard's internal nginx (25M / 120s) or requests
  fail one hop earlier than the settings that were tuned for them. There is no
  automated check that the two agree — a deliberate gap, because the host vhost
  is not deployed by CD.
- **HSTS is set in exactly one place** (Traefik's `secure-headers`). If a future
  nginx change adds it, clients receive the header twice.

Keep the three existing residuals unchanged.

- [ ] **Step 7: Update §11 (First-deploy checklist)**

Replace the ACME item. The new checks: both hostnames resolve; `certbot
certificates` lists both with a future expiry; `curl -sI https://<api-host>/health/`
returns 200 over a valid certificate; `ss -ltnp | grep 8081` shows loopback only;
`docker exec chatbot-traefik-1 wget -qO- http://127.0.0.1:8080/api/http/services/web@docker`
shows two UP backends; `.env` has both hosts in `ALLOWED_HOSTS` and `DEBUG=False`.

- [ ] **Step 8: Verify every changed command**

```bash
cd /home/abdulkhalek/Projects/chatbot
grep -n 'DOMAIN' docs/DEPLOY.md
grep -n '8081' docs/DEPLOY.md | head
grep -rn 'certresolver\|letsencrypt' docs/DEPLOY.md
```

Expected: no `${DOMAIN}` survivals; the loopback port referenced consistently as
`127.0.0.1:8081`; and no remaining claim that Traefik obtains certificates.

- [ ] **Step 9: Commit**

```bash
git add docs/DEPLOY.md
git commit -m "docs: runbook for the nginx-fronted gateway

The request path gains a hop and certificates move to certbot, which decouples
them from deploys: DNS is now a precondition for certbot, not for the first
deploy. Adds the public-vs-loopback decision table, so an operator reading a
failed deploy can tell a broken release from a broken gateway without deriving
it under pressure."
```

---

## Task 5: Cutover on the server — **user-gated**

**Do not start this** until the user confirms DNS records exist for both
hostnames. Report and stop.

- [ ] **Step 1: Confirm DNS**

```bash
dig +short api-demo-chatbot.abdulkhalek.dev
dig +short app-demo-chatbot.abdulkhalek.dev
```

Both must return this host's address.

- [ ] **Step 2: Install the vhosts and issue certificates**

Per `docs/DEPLOY.md` §2. Then confirm:

```bash
certbot certificates | grep -A2 'demo-chatbot'
nginx -t && systemctl reload nginx
```

- [ ] **Step 3: Set the repository variables**

```bash
gh variable set API_HOST --repo Etqan-agency/chatbot --body api-demo-chatbot.abdulkhalek.dev
gh variable set APP_HOST --repo Etqan-agency/chatbot --body app-demo-chatbot.abdulkhalek.dev
gh variable delete DOMAIN --repo Etqan-agency/chatbot
gh variable list --repo Etqan-agency/chatbot
```

- [ ] **Step 4: Update the server's `.env`**

`API_HOST`, `APP_HOST`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and
`FRONTEND_URL` all change. `ALLOWED_HOSTS` must contain both hostnames **and**
`localhost`/`127.0.0.1`.

- [ ] **Step 5: Dry-run, then deploy**

```bash
gh workflow run cd.yml --repo Etqan-agency/chatbot -f dry_run=true
gh run watch --repo Etqan-agency/chatbot
gh workflow run cd.yml --repo Etqan-agency/chatbot
gh run watch --repo Etqan-agency/chatbot
```

Expected on the real run: five smoke probes at their wanted codes, and
`Deploy complete`.

- [ ] **Step 6: Verify from outside the host**

```bash
curl -sI https://api-demo-chatbot.abdulkhalek.dev/health/ | head -1
curl -s  https://api-demo-chatbot.abdulkhalek.dev/health/
curl -sI https://app-demo-chatbot.abdulkhalek.dev/        | head -1
curl -sI https://api-demo-chatbot.abdulkhalek.dev/media/probe/health.txt | head -1
```

Expected: 200 on all three headers over a valid certificate (no `-k`), and
`{"status": "ok", "database": "connected"}` from the health body.

---

## Self-Review

**Coverage of the decisions taken in discussion:**

| Decision | Where |
|---|---|
| Two flat hostnames, no shared base | Task 1 |
| nginx on host keeps 80/443, terminates TLS | Task 2 Step 1, Task 3 Step 6 |
| Traefik on `127.0.0.1:8081`, plaintext | Task 2 Steps 1, 6, 8 |
| certbot, not Traefik ACME | Task 2 Step 2, Task 4 Step 2 |
| ACME kept dormant rather than deleted | Task 2 Step 2 |
| Loopback smoke probe as a hard gate | Task 3 Steps 1-3 |
| nginx vhosts in the repo as reference | Task 3 Step 6 |

**Deliberate deviations from the spec:** §4.1's topology diagram, §6's `DOMAIN`
variable and §7's steps 2 and 7 (ufw for Traefik; DNS as an ACME precondition)
are all superseded here. The spec is not being edited — this plan records the
divergence, and `docs/DEPLOY.md` becomes the operational source of truth.

**Known weak verification:** the nginx vhosts cannot be syntax-checked in this
repository — they are fragments referencing certbot-managed paths on a host this
plan does not touch. Task 3 Step 7 says so rather than claiming otherwise, and
Task 5 Step 2's `nginx -t` on the server is the real check. Task 5 as a whole is
user-gated and unverifiable here.

**Ordering risk:** between Task 2 and Task 5 the stack has no public entrypoint.
That is intentional and safe — nothing is live on these hostnames yet — but the
tasks must not be merged and deployed piecemeal. Task 2's port change without
Task 5's nginx vhost is an outage on any host where this is already serving.
