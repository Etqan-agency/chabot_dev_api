# Component Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both repositories deployable — a drain-aware health probe, an
enforced lint toolchain, environment-correct CORS, and hardened production
container images for the API and the dashboard.

**Architecture:** All work happens inside the two existing repositories
(`chatbot_api`, `chatbot_dashboard`). Nothing here creates the meta-repo, the
Traefik gateway, or any GitHub Actions workflow — those are Plan 2. This plan
ends when both production images build and run locally and both test suites are
green.

**Tech Stack:** Django 5.2 · DRF · Python 3.13 · ruff · pre-commit · Vite 8 ·
React 19 · TypeScript · ESLint 9 flat config · Docker multi-stage builds ·
nginx:alpine

**Spec:** `docs/superpowers/specs/2026-09-03-infrastructure-pipeline-design.md`
(sections §4.2, §5.2)

## Global Constraints

- Python **3.13** everywhere: production image, CI, and the local venv.
- ruff `line-length = 119`, `target-version = "py313"`.
- ruff rule selection: `["F", "E", "W", "I", "UP", "DJ", "B", "C4"]`,
  `ignore = ["DJ001"]`.
- ruff excludes: `*/migrations/*.py`, `staticfiles/*`, `_backup_auth_refactor/*`,
  `.venv/*`.
- The API test suite is Django `unittest` (`python manage.py test`), **not**
  pytest. Tests subclass `django.test.TestCase` or
  `rest_framework.test.APITestCase`.
- All 347 existing API tests must still pass at the end of every task.
- `VITE_API_BASE_URL` stays **empty**. The dashboard is same-origin; no API
  hostname is baked into the bundle.
- Do not modify anything under `_backup_auth_refactor/`.
- Do not delete the orphaned modules listed in the spec's sub-project 2. They
  are out of scope here even though they carry lint violations — the ruff
  excludes and `--fix` handle them.

---

## File Structure

**`chatbot_api`:**

| Path | Responsibility |
|------|----------------|
| `pyproject.toml` | Create — ruff configuration (the repo has none today) |
| `.pre-commit-config.yaml` | Create — hook definitions CI will run |
| `Conf/views.py` | Create — project-level probe views (health/drain) |
| `Conf/tests.py` | Create — tests for the probe and CORS helper |
| `Conf/urls.py` | Modify — import the health view instead of defining it inline |
| `Conf/settings.py` | Modify — add `DRAIN_FILE`; make dev CORS origins `DEBUG`-only |
| `Dockerfile` | Modify — multi-stage, non-root, `HEALTHCHECK`, Python 3.13 |
| `.env.example` | Modify — document `DRAIN_FILE` and the `ALLOWED_HOSTS` localhost requirement |

**`chatbot_dashboard`:**

| Path | Responsibility |
|------|----------------|
| `eslint.config.js` | Create — ESLint 9 flat config (none exists; `npm run lint` is currently broken) |
| `package.json` | Modify — add the ESLint devDependencies the `lint` script needs |
| `Dockerfile` | Create — node build stage → nginx runtime |
| `docker/nginx.conf` | Create — SPA fallback, `/api` + `/media` proxy, upload size, cache headers |
| `.dockerignore` | Create — keep `node_modules` and `dist` out of build context |

**Why `Conf/views.py` rather than leaving the view in `urls.py`:** the health
view grows from 2 lines to ~35 with drain handling, DB checking and log
silencing. A URLconf that also defines view logic is the kind of file that
quietly accumulates; splitting it now costs one import.

---

### Task 0: Move the local environment to Python 3.13

Every later task runs the test suite through `.venv/bin/python`, and the
production image, CI and pre-commit all pin 3.13. The local venv is currently
3.14.7. If the suite is going to break on 3.13 it must break here, before six
tasks of work are layered on top of an untested interpreter — not in CI.

**Files:** none tracked. This task replaces the untracked `.venv/` directory.

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `chatbot_api/.venv` running Python 3.13.x with `requirements.txt`
  installed, plus `ruff` and `pre-commit`. Every subsequent task's
  `.venv/bin/python manage.py test` runs on it.

- [ ] **Step 1: Confirm a 3.13 interpreter is available**

Run: `python3.13 --version`
Expected: `Python 3.13.x`

If it is missing, install it (`pyenv install 3.13`) before continuing. Do not
substitute another version — 3.13 is a Global Constraint of this plan and of
the production image.

- [ ] **Step 2: Replace the virtual environment**

```bash
cd chatbot_api
rm -rf .venv
python3.13 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install ruff pre-commit
```

Verified on 2026-09-03 against Python 3.13.7: every pinned requirement resolves
and installs cleanly — including `psycopg2-binary` (2.9.12), the one dependency
whose wheels historically lag new Python releases — and all modules import.

- [ ] **Step 3: Confirm the interpreter**

Run: `cd chatbot_api && .venv/bin/python --version`
Expected: `Python 3.13.x`

- [ ] **Step 4: Run the full suite on the new interpreter**

Run: `cd chatbot_api && .venv/bin/python manage.py test`
Expected: `Ran 347 tests`, `OK`

This is the gate for the whole plan. Verified on 2026-09-03: the full suite
passes on 3.13.7 in ~58s, the same as on 3.14. So a failure here means
something changed since this plan was written — resolve it, or revisit the 3.13
decision with the spec's author, before any other task starts.

- [ ] **Step 5: Nothing to commit**

`.venv/` is untracked (`.gitignore`). There is no commit for this task; its
deliverable is a working local interpreter. Proceed to Task 1.

---

### Task 1: ruff and pre-commit toolchain

Establishes the lint baseline **first** so every later task in this plan is
written against an enforced style. The spec (§5.2) requires this gate to be
blocking and green from day one.

**Files:**
- Create: `chatbot_api/pyproject.toml`
- Create: `chatbot_api/.pre-commit-config.yaml`
- Modify: many files across `chatbot_api/` (auto-fixes plus ~32 manual)

**Interfaces:**
- Consumes: the Python 3.13 venv with `ruff` and `pre-commit` installed, from
  Task 0.
- Produces: `ruff check .` and `ruff format --check .` both exit 0 from the
  `chatbot_api/` directory. Plan 2's `api-lint` CI job runs exactly these.

- [ ] **Step 1: Create the ruff configuration**

Create `chatbot_api/pyproject.toml`:

```toml
[tool.ruff]
line-length = 119
target-version = "py313"
extend-exclude = [
    "*/migrations/*.py",
    "staticfiles/*",
    "_backup_auth_refactor/*",
    ".venv/*",
]

[tool.ruff.lint]
select = ["F", "E", "W", "I", "UP", "DJ", "B", "C4"]
# DJ001 (null=True on CharField/TextField) is a real finding on 5 fields, but
# every fix needs a schema migration and a data audit of existing NULLs. That
# is sub-project 2 work, not a prerequisite for deploying. Tracked there.
ignore = ["DJ001"]

[tool.ruff.lint.isort]
known-first-party = ["Authentication", "Conf", "knowledge", "subscriptions", "WhatsApp"]
```

- [ ] **Step 2: Confirm the violation count matches the plan**

Run: `cd chatbot_api && ruff check . --statistics`

Expected: 95 errors, 63 auto-fixable. If the count differs materially, the
codebase has moved since this plan was written — stop and re-baseline before
continuing rather than fixing a different set of problems than the one costed.

- [ ] **Step 3: Apply the automatic fixes**

```bash
cd chatbot_api
ruff check . --fix
ruff format .
```

- [ ] **Step 4: Verify the test suite still passes after auto-fixes**

Run: `cd chatbot_api && .venv/bin/python manage.py test`
Expected: `Ran 347 tests`, `OK`

Auto-fixes touch import ordering and unused imports across the codebase. If any
test now fails, an import that looked unused was actually a side-effecting
import (signal registration is the usual culprit) — restore that specific
import with a `# noqa: F401` and an explanatory comment rather than reverting
the whole run.

- [ ] **Step 5: Fix the remaining violations by rule**

Run `ruff check .` and work through what remains. Grouped by rule, with the fix
for each:

**`B904` (8) — `raise ... from` inside `except`.** Add `from exc` so the
original traceback is not lost:

```python
except SomeError as exc:
    raise OtherError("message") from exc
```

Where the original is deliberately discarded, use `from None` and say why in a
comment.

**`B905` (4) — `zip()` without `strict`.** Add `strict=` explicitly. Use
`strict=True` when the sequences must be the same length (the common case, and
it turns a silent truncation into a loud error); `strict=False` only where
truncation is intended, with a comment.

**`C408` (4) — `dict()` / `list()` calls.** Replace `dict(a=1)` with `{"a": 1}`
and `list()` with `[]`.

**`C416` (1) — redundant comprehension.** Replace `[x for x in items]` with
`list(items)`.

**`E501` (4) — lines over 119 chars.** At
`knowledge/services/api_content_processor.py:71`,
`knowledge/services/guided_tree.py:287`, and
`knowledge/tests/test_guided_tree.py:169-170`. Wrap; do not raise the limit.

**`E702` (4) — semicolon statements.** All four are in
`knowledge/services/guided_tree.py:291-297`. Split onto separate lines.

**`E402` (2) — imports not at top.** `check_docs.py:10` and
`subscriptions/tests.py:310`. If the late import is deliberate (avoiding a
circular import, or requiring Django setup first), add
`# noqa: E402` with a comment saying which; otherwise move it up.

**`E741` (1) — ambiguous name `l`.** `knowledge/tests/test_guided_tree.py:187`.
Rename to something meaningful, e.g. `lang`.

**`F841` (2) — assigned but never used.** `WhatsApp/views.py:91` (`exc`) —
delete the binding or use it in the log message. `knowledge/services/rag.py:125`
(`dialect`) — **this one is a latent bug, not a style issue**: `detect_dialect()`
is called and its result thrown away, so dialect detection does nothing. Do
**not** silently delete it here. Add `# noqa: F841` with a comment pointing at
the audit sub-project, so the behaviour question is decided deliberately rather
than by a lint fix.

**`DJ012` (3) — model body ordering.** `Authentication/models.py:61,109,150`.
Move each `__str__` above the custom/`save` methods it currently follows. No
migration needed — this is source ordering only.

- [ ] **Step 6: Verify lint is clean**

```bash
cd chatbot_api
ruff check .        # expect: "All checks passed!"
ruff format --check .
```

- [ ] **Step 7: Verify the test suite still passes**

Run: `cd chatbot_api && .venv/bin/python manage.py test`
Expected: `Ran 347 tests`, `OK`

- [ ] **Step 8: Create the pre-commit configuration**

Create `chatbot_api/.pre-commit-config.yaml`:

```yaml
exclude: '^_backup_auth_refactor/|/migrations/|^staticfiles/'
default_stages: [pre-commit]
minimum_pre_commit_version: "3.2.0"

default_language_version:
  python: python3.13

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-json
      - id: check-toml
      - id: check-yaml
      - id: check-merge-conflict
      - id: debug-statements
      - id: check-case-conflict
      - id: detect-private-key

  - repo: https://github.com/adamchainz/django-upgrade
    rev: '1.29.1'
    hooks:
      - id: django-upgrade
        args: ['--target-version', '5.2']

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.5
    hooks:
      - id: ruff-check
        args: [--fix, --exit-non-zero-on-fix]
      - id: ruff-format
```

- [ ] **Step 9: Run pre-commit across the repository**

```bash
cd chatbot_api
pip install pre-commit
pre-commit run --all-files
```

Expected: all hooks pass. `django-upgrade`, `trailing-whitespace` and
`end-of-file-fixer` may rewrite files on the first run — re-run until clean,
then re-run the test suite.

- [ ] **Step 10: Verify the test suite one more time**

Run: `cd chatbot_api && .venv/bin/python manage.py test`
Expected: `Ran 347 tests`, `OK`

- [ ] **Step 11: Commit**

```bash
cd chatbot_api
git add pyproject.toml .pre-commit-config.yaml
git add -u
git commit -m "build: add ruff and pre-commit, fix resulting violations

Establishes the lint gate CI will enforce. Configuration matches
original_software (line-length 119) so the two projects stay consistent.

DJ001 is ignored rather than fixed: the five null=True string fields each
need a schema migration and an audit of existing NULLs, which belongs to
the cleanup sub-project.

rag.py's unused 'dialect' is suppressed rather than deleted -- it is a
latent bug (detect_dialect's result is discarded), and deciding what the
code should do is an audit question, not a lint fix."
```

---

### Task 2: Drain-aware health endpoint

`rollout.sh` retires a replica by creating a drain file inside the container and
waiting for Traefik to observe the backend leave rotation before stopping it.
Without a route that returns 503 while that file exists, `retire_container`
waits out its settle window against a probe that never fails, and the stop is as
abrupt as no rolling at all — i.e. the whole zero-downtime design silently
degrades to a plain restart.

**Files:**
- Create: `chatbot_api/Conf/views.py`
- Create: `chatbot_api/Conf/tests.py`
- Modify: `chatbot_api/Conf/urls.py:13-14` (remove the inline view), `:33-47` (import it)
- Modify: `chatbot_api/Conf/settings.py` (add `DRAIN_FILE`)
- Modify: `chatbot_api/.env.example`

**Interfaces:**
- Consumes: the lint configuration from Task 1.
- Produces:
  - `Conf.views.health(request) -> JsonResponse` — 200 `{"status": "ok",
    "database": "connected"}` when healthy; 503 `{"status": "draining", ...}`
    while the drain file exists; 503 `{"status": "unhealthy", ...}` when the DB
    is unreachable. URL name stays `health` at path `/health/`.
  - `settings.DRAIN_FILE: str` — default `/tmp/drain`, overridable by the
    `DRAIN_FILE` environment variable.
  - Plan 3's `scripts/lib/rollout.sh` creates and removes that file; Task 4's
    `HEALTHCHECK` and Plan 3's deploy smoke checks both call `/health/`.

- [ ] **Step 1: Write the failing tests**

Create `chatbot_api/Conf/tests.py`:

```python
"""Health probe: drain behaviour, database reporting, and logging discipline."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest import mock

from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse


class HealthProbeTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.drain_file = Path(self._tmp.name) / "drain"

    def test_returns_ok_when_not_draining(self):
        with override_settings(DRAIN_FILE=str(self.drain_file)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_returns_503_draining_when_drain_file_exists(self):
        self.drain_file.touch()

        with override_settings(DRAIN_FILE=str(self.drain_file)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "draining")

    def test_draining_response_still_reports_database_state(self):
        self.drain_file.touch()

        with override_settings(DRAIN_FILE=str(self.drain_file)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.json()["database"], "connected")

    def test_reports_unhealthy_when_database_is_unreachable(self):
        with (
            override_settings(DRAIN_FILE=str(self.drain_file)),
            mock.patch(
                "Conf.views.connection.ensure_connection",
                side_effect=OperationalError("database is down"),
            ),
        ):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unhealthy")
        self.assertEqual(response.json()["database"], "disconnected")

    def test_draining_503_is_not_logged_to_django_request(self):
        """Probe 503s must stay off the `django.request` logger.

        Traefik probes this endpoint every few seconds per replica, so a
        rolling deploy's drain window -- a deliberate 503, not a fault --
        would otherwise emit an ERROR per probe for the whole rollout and
        bury real failures.
        """
        self.drain_file.touch()

        with override_settings(DRAIN_FILE=str(self.drain_file)):
            with self.assertNoLogs("django.request", level=logging.DEBUG):
                response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)

    def test_unhealthy_503_is_not_logged_to_django_request(self):
        with (
            override_settings(DRAIN_FILE=str(self.drain_file)),
            mock.patch(
                "Conf.views.connection.ensure_connection",
                side_effect=OperationalError("database is down"),
            ),
        ):
            with self.assertNoLogs("django.request", level=logging.DEBUG):
                response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd chatbot_api && .venv/bin/python manage.py test Conf -v 2`

Expected: FAIL. The first errors will be
`ModuleNotFoundError: No module named 'Conf.views'` from the `mock.patch`
targets, and assertion failures on `response.json()["status"]` because the
current view returns only `{"status": "ok"}` with no `database` key and no
drain handling.

- [ ] **Step 3: Add the `DRAIN_FILE` setting**

In `chatbot_api/Conf/settings.py`, in the application-knobs section near
`MAX_UPLOAD_SIZE_MB`, add:

```python
# --- Rolling deploys ---------------------------------------------------------
# While this file exists inside a container, /health/ returns 503 so Traefik
# takes the replica out of rotation BEFORE the rolling deploy stops it. Created
# and removed by scripts/lib/rollout.sh; irrelevant outside a rollout.
DRAIN_FILE = os.getenv("DRAIN_FILE", "/tmp/drain")  # noqa: S108
```

- [ ] **Step 4: Create the health view**

Create `chatbot_api/Conf/views.py`:

```python
"""Project-level views owned by no app: the liveness / drain probe."""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def _silence_request_error_log(response: JsonResponse) -> JsonResponse:
    """Keep a probe's 503 off the ``django.request`` logger.

    Django's ``log_response()`` logs every 5xx it returns to ``django.request``
    at ERROR. Traefik probes this endpoint every few seconds per replica, so a
    rolling deploy's drain window -- a deliberate 503, not a fault -- would
    emit one ERROR per probe for the length of the rollout and bury real
    failures in the noise.

    ``_has_been_logged`` is the same flag Django sets after logging a response,
    so pre-setting it makes ``log_response()`` skip this one. The health state
    is logged here instead, on this module's own logger.
    """
    response._has_been_logged = True
    return response


def health(_request):
    """Liveness and drain probe for Traefik, Docker and the deploy smoke checks.

    Returns 503 while the drain file exists so the replica leaves rotation
    before a rolling deploy stops it. See scripts/lib/rollout.sh.
    """
    try:
        connection.ensure_connection()
        db_status = "connected"
    except Exception:
        db_status = "disconnected"

    # Draining takes precedence: the container is being removed from rotation
    # deliberately and is not broken. The database state is still reported so
    # an operator can tell the two situations apart.
    if Path(settings.DRAIN_FILE).exists():
        logger.info("Health probe: draining (database=%s)", db_status)
        return _silence_request_error_log(JsonResponse({"status": "draining", "database": db_status}, status=503))

    if db_status == "connected":
        return JsonResponse({"status": "ok", "database": db_status})

    logger.error("Health probe: unhealthy (database=%s)", db_status)
    return _silence_request_error_log(JsonResponse({"status": "unhealthy", "database": db_status}, status=503))
```

The healthy payload keeps `"status": "ok"` — the same value the old view
returned — so any existing external monitor keeps working.

- [ ] **Step 5: Wire the view into the URLconf**

In `chatbot_api/Conf/urls.py`, delete the inline definition at lines 13-14:

```python
def health(_request):
    return JsonResponse({"status": "ok"})
```

Add to the imports:

```python
from .views import health
```

Remove `JsonResponse` from the `django.http` import if nothing else in the file
uses it. The `path("health/", health, name="health")` entry is unchanged.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd chatbot_api && .venv/bin/python manage.py test Conf -v 2`
Expected: PASS, 6 tests.

- [ ] **Step 7: Run the full suite for regressions**

Run: `cd chatbot_api && .venv/bin/python manage.py test`
Expected: `Ran 353 tests`, `OK` (347 existing + 6 new)

- [ ] **Step 8: Document the setting**

Add to `chatbot_api/.env.example`:

```bash
# --- Rolling deploys ---------------------------------------------------------
# Path checked by /health/. While this file exists the probe returns 503 so the
# replica leaves rotation before a rolling deploy stops it. Leave the default
# unless the container filesystem layout changes.
DRAIN_FILE=/tmp/drain
```

- [ ] **Step 9: Verify lint is still clean**

Run: `cd chatbot_api && ruff check . && ruff format --check .`
Expected: All checks passed.

- [ ] **Step 10: Commit**

```bash
cd chatbot_api
git add Conf/views.py Conf/tests.py Conf/urls.py Conf/settings.py .env.example
git commit -m "feat: drain-aware health probe

/health/ returns 503 while DRAIN_FILE exists so Traefik removes the
replica from rotation before a rolling deploy stops it. Without this the
rollout's settle window waits on a probe that never fails and the stop is
as abrupt as no rolling at all.

Probe 503s are kept off django.request: at one probe per replica every few
seconds, a drain window would otherwise emit an ERROR per probe for the
whole rollout.

The view moves out of urls.py into Conf/views.py -- it is now ~35 lines
with drain handling, DB checking and log silencing."
```

---

### Task 3: Environment-aware CORS configuration

`Conf/settings.py:45-54` appends `localhost:5173`, `localhost:3000` and a Vercel
origin to `CORS_ALLOWED_ORIGINS` in **every** environment, production included.
With `CORS_ALLOW_CREDENTIALS = True` that means any page served from localhost
can read authenticated API responses. It is also a cutover blocker: the new
hostnames must be configurable.

**Files:**
- Modify: `chatbot_api/Conf/settings.py:38-55`
- Modify: `chatbot_api/Conf/tests.py` (append a test class)
- Modify: `chatbot_api/.env.example`

**Interfaces:**
- Consumes: nothing from Tasks 1-2 beyond the lint gate.
- Produces: `Conf.settings._with_dev_origins(origins: list[str], *, debug: bool)
  -> list[str]` — returns `origins` unchanged when `debug` is False; returns it
  with the local dev-server origins appended (no duplicates, order preserved)
  when True.

- [ ] **Step 1: Write the failing tests**

Append to `chatbot_api/Conf/tests.py`:

```python
class DevCorsOriginTests(TestCase):
    """Local dev origins must never be trusted outside DEBUG.

    CORS_ALLOW_CREDENTIALS is True, so a trusted origin can read authenticated
    responses. Before this change the localhost origins were appended
    unconditionally, including in production.
    """

    def test_dev_origins_are_not_added_when_debug_is_off(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins(["https://app.example.com"], debug=False)

        self.assertEqual(result, ["https://app.example.com"])

    def test_dev_origins_are_added_when_debug_is_on(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins([], debug=True)

        self.assertIn("http://localhost:5173", result)
        self.assertIn("http://localhost:3000", result)

    def test_configured_origins_are_preserved_and_come_first(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins(["https://app.example.com"], debug=True)

        self.assertEqual(result[0], "https://app.example.com")

    def test_does_not_duplicate_an_already_configured_dev_origin(self):
        from Conf.settings import _with_dev_origins

        result = _with_dev_origins(["http://localhost:5173"], debug=True)

        self.assertEqual(result.count("http://localhost:5173"), 1)

    def test_does_not_mutate_the_list_it_is_given(self):
        from Conf.settings import _with_dev_origins

        configured = ["https://app.example.com"]
        _with_dev_origins(configured, debug=True)

        self.assertEqual(configured, ["https://app.example.com"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd chatbot_api && .venv/bin/python manage.py test Conf.DevCorsOriginTests -v 2`
Expected: FAIL with `ImportError: cannot import name '_with_dev_origins' from 'Conf.settings'`

- [ ] **Step 3: Replace the CORS block**

In `chatbot_api/Conf/settings.py`, replace lines 38-55 entirely:

```python
CORS_ALLOWED_ORIGINS = _env_list("CORS_ALLOWED_ORIGINS", default=[])

# Local frontend dev servers (Vite :5173, CRA :3000), so a developer can point
# a local UI at any deployment without editing CORS_ALLOWED_ORIGINS.
_LOCAL_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _with_dev_origins(origins: list[str], *, debug: bool) -> list[str]:
    """Append the local dev-server origins, but only when DEBUG is on.

    These were previously appended unconditionally, in production too. Combined
    with CORS_ALLOW_CREDENTIALS=True that let any page served from localhost
    read authenticated API responses. Production origins now come exclusively
    from the CORS_ALLOWED_ORIGINS environment variable.

    Returns a new list; the argument is not mutated.
    """
    merged = list(origins)
    if not debug:
        return merged
    for origin in _LOCAL_DEV_ORIGINS:
        if origin not in merged:
            merged.append(origin)
    return merged


CORS_ALLOWED_ORIGINS = _with_dev_origins(CORS_ALLOWED_ORIGINS, debug=DEBUG)
CORS_ALLOW_CREDENTIALS = True
```

Note the hardcoded `https://clickai-alpha.vercel.app` default is gone. Any
deployment that still needs it must list it in the `CORS_ALLOWED_ORIGINS`
environment variable.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd chatbot_api && .venv/bin/python manage.py test Conf.DevCorsOriginTests -v 2`
Expected: PASS, 5 tests.

- [ ] **Step 5: Run the full suite for regressions**

Run: `cd chatbot_api && .venv/bin/python manage.py test`
Expected: `Ran 358 tests`, `OK`

- [ ] **Step 6: Update the environment documentation**

In `chatbot_api/.env.example`, replace the `CORS_ALLOWED_ORIGINS` entry:

```bash
# Comma-separated browser origins allowed to call this API with credentials.
# Production MUST list every real frontend origin here -- nothing is implied.
# Local dev servers (localhost:5173/:3000) are added automatically when
# DEBUG=True and are never trusted when DEBUG=False.
CORS_ALLOWED_ORIGINS=
```

Also confirm this line is present, adding it if not, because Task 4's
container `HEALTHCHECK` requests `http://localhost:8000/health/` and Django
rejects a `Host` header absent from `ALLOWED_HOSTS` with 400:

```bash
# MUST include "localhost": the container HEALTHCHECK probes /health/ over
# loopback, and a Host header not listed here is rejected with 400.
ALLOWED_HOSTS=localhost,127.0.0.1
```

- [ ] **Step 7: Verify lint is still clean**

Run: `cd chatbot_api && ruff check . && ruff format --check .`
Expected: All checks passed.

- [ ] **Step 8: Commit**

```bash
cd chatbot_api
git add Conf/settings.py Conf/tests.py .env.example
git commit -m "fix: only trust local dev CORS origins when DEBUG

localhost:5173/:3000 and a hardcoded Vercel origin were appended to
CORS_ALLOWED_ORIGINS in every environment. With CORS_ALLOW_CREDENTIALS=True
that let any page served from localhost read authenticated responses in
production.

Production origins now come exclusively from the environment variable.
Also documents that ALLOWED_HOSTS must contain localhost, since the
container HEALTHCHECK probes /health/ over loopback."
```

---

### Task 4: Harden the API production image

**Files:**
- Modify: `chatbot_api/Dockerfile` (full rewrite)
- Modify: `chatbot_api/.dockerignore`

**Interfaces:**
- Consumes: `/health/` from Task 2 (the `HEALTHCHECK` target); `ALLOWED_HOSTS`
  containing `localhost` from Task 3.
- Produces: an image that runs as UID 10001, reports Docker health from
  `/health/`, and keeps `docker/entrypoint.sh` as its entrypoint so
  `RUN_MIGRATIONS=1` still gates migrations and collectstatic. Plan 2's
  `build-api` job builds this file; Plan 3's compose runs it.

- [ ] **Step 1: Rewrite the Dockerfile**

Replace `chatbot_api/Dockerfile` entirely:

```dockerfile
# syntax=docker/dockerfile:1

# ---- builder: compile wheels so the runtime image carries no toolchain ------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ---- runtime ----------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=Conf.settings \
    DRAIN_FILE=/tmp/drain

# curl backs the HEALTHCHECK; libpq5 is psycopg's runtime library. Neither
# build-essential nor libpq-dev is present here -- that is the point of the
# builder stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels

COPY --chown=app:app . .
RUN chmod +x docker/entrypoint.sh

# Owned by app so collectstatic, media writes, and the SQLite fallback all work
# as a non-root user. Note this chowns /app ITSELF, not just its contents:
# `WORKDIR /app` creates the directory as root:root, and `COPY --chown` only
# sets ownership on the files it copies. Without this, uid 10001 cannot create
# /app/db.sqlite3 (the DATABASE_URL-unset fallback), migrate fails with
# "unable to open database file", and entrypoint.sh aborts under `set -e`
# before gunicorn ever binds.
RUN mkdir -p /app/staticfiles /app/media && chown -R app:app /app

USER app

EXPOSE 8000

# start-period covers migrate + collectstatic on first boot, which run in the
# entrypoint before gunicorn binds.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/ || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "Conf.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
```

`--graceful-timeout 30` is stated explicitly rather than relied on as a default,
because Plan 3's service table gives `web` a 40-second stop timeout precisely to
sit above it. The two numbers have to be read together.

- [ ] **Step 2: Add the docs and spec directories to `.dockerignore`**

In `chatbot_api/.dockerignore`, under the "Docs / backups" section, the
existing `*.md` line already excludes markdown. Add the directory itself so the
build context stays small:

```
docs/
```

- [ ] **Step 3: Build the image**

Run: `cd chatbot_api && docker build -t chatbot-api:local .`
Expected: build succeeds.

- [ ] **Step 4: Verify the image runs as a non-root user**

Run: `docker run --rm chatbot-api:local id -u`
Expected: `10001`

- [ ] **Step 5: Verify the health probe answers inside the container**

```bash
docker run -d --name chatbot-api-check \
  -e DEBUG=True \
  -e SECRET_KEY=test-only \
  -e ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e RUN_MIGRATIONS=1 \
  -p 8000:8000 chatbot-api:local

# Give the entrypoint time to migrate and collectstatic.
sleep 45
curl -si http://localhost:8000/health/ | head -1
docker inspect --format '{{.State.Health.Status}}' chatbot-api-check
```

Expected: `HTTP/1.1 200 OK` and health status `healthy`.

This runs on SQLite inside the container, which is fine — the probe only needs
a reachable database, and Plan 2 runs the real suite against Postgres.

- [ ] **Step 6: Verify the drain file flips the probe to 503**

```bash
docker exec chatbot-api-check touch /tmp/drain
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health/
docker exec chatbot-api-check rm /tmp/drain
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health/
```

Expected: `503` then `200`. This is the end-to-end proof that Plan 3's rolling
deploy will actually be able to drain a replica.

- [ ] **Step 7: Clean up**

```bash
docker rm -f chatbot-api-check
```

- [ ] **Step 8: Commit**

```bash
cd chatbot_api
git add Dockerfile .dockerignore
git commit -m "build: multi-stage, non-root API image with a health check

Splits the build so the runtime image carries no compiler or libpq-dev,
runs as UID 10001 instead of root, and adds a HEALTHCHECK against /health/
so Docker and Traefik agree on when a replica is serving.

Pins Python 3.13, replacing 3.12 -- image, CI and the local venv were on
three different minor versions.

gunicorn's --graceful-timeout is now explicit at 30s because the rolling
deploy's 40s stop timeout is chosen to sit above it."
```

---

### Task 5: ESLint toolchain for the dashboard

`package.json` defines `"lint": "eslint ."`, but ESLint is not a dependency and
no config file exists — the script fails with `eslint: command not found`. Plan
2's `dashboard-lint` job runs this script, so it has to work first.

**Files:**
- Create: `chatbot_dashboard/eslint.config.js`
- Modify: `chatbot_dashboard/package.json`, `chatbot_dashboard/package-lock.json`
- Delete: `chatbot_dashboard/pnpm-lock.yaml` (untracked stray — see Step 4)

**Interfaces:**
- Consumes: nothing.
- Produces: `npm run lint` and `npx tsc --noEmit` both exit 0, and exactly one
  lockfile in the repository. Plan 2's `dashboard-lint` job runs those two
  commands and `npm ci` against `package-lock.json`.

- [ ] **Step 1: Install the ESLint dependencies**

```bash
cd chatbot_dashboard
npm install --save-dev \
  eslint@^9 \
  @eslint/js@^9 \
  typescript-eslint@^8 \
  eslint-plugin-react-hooks@^5 \
  eslint-plugin-react-refresh@^0.4 \
  globals@^15
```

- [ ] **Step 2: Create the flat config**

Create `chatbot_dashboard/eslint.config.js`:

```js
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
    },
  },
)
```

- [ ] **Step 3: Run the linter**

Run: `cd chatbot_dashboard && npm run lint`

**Expected: exit code 0, zero errors, zero warnings.** This was verified against
this exact config on 2026-09-03 — the dashboard is already clean under it, so
there is no first-run cleanup to do.

If findings *do* appear, the codebase has changed since this plan was written.
Fix genuine defects. For a rule that is noisy without indicating a real defect,
disable it in `eslint.config.js` with a comment saying why — do not leave the
run red, and do not blanket-disable `@typescript-eslint/no-explicit-any`
without inspecting each site.

- [ ] **Step 4: Resolve the duplicate lockfiles**

The repository contains a committed `package-lock.json` **and** an untracked
`pnpm-lock.yaml`. Two lockfiles for two package managers can resolve to
different dependency trees, and CI will use `npm ci` against
`package-lock.json`. Pick npm — it is what is committed and what
`package-lock.json` already pins — and delete the stray file:

```bash
cd chatbot_dashboard
rm -f pnpm-lock.yaml
```

Add it to `.gitignore` only if the team genuinely uses pnpm locally; otherwise
its absence is the signal.

Separately, `npm install` rewrites two lines in `package-lock.json`, syncing
the project name from `chatbot-dashboard` to `click-ai-dashboard` to match
`package.json`. That is a pre-existing mismatch, not a change introduced here;
commit it with this task so `npm ci` stops drifting.

- [ ] **Step 5: Verify types still check**

Run: `cd chatbot_dashboard && npx tsc --noEmit`
Expected: no output, exit code 0.

- [ ] **Step 6: Verify the production build still succeeds**

Run: `cd chatbot_dashboard && npm run build`
Expected: build succeeds, `dist/` is produced.

- [ ] **Step 7: Commit**

```bash
cd chatbot_dashboard
git add eslint.config.js package.json package-lock.json
git commit -m "build: add ESLint 9 flat config

package.json defined a lint script but ESLint was neither installed nor
configured, so 'npm run lint' failed with command-not-found. CI runs this
script, so it has to work."
```

---

### Task 6: Dashboard production image and nginx configuration

The SPA is served by nginx, which also reproduces the `/api` and `/media`
rewrites `vercel.json` performs today. Keeping the app same-origin means no API
hostname is baked into the bundle and CORS is off the dashboard's critical path.

**Files:**
- Create: `chatbot_dashboard/Dockerfile`
- Create: `chatbot_dashboard/docker/nginx.conf`
- Create: `chatbot_dashboard/.dockerignore`

**Interfaces:**
- Consumes: a working `npm run build` from Task 5.
- Produces: an image serving the SPA on port 80, proxying `/api/` and `/media/`
  to an upstream named `web` on port 8000. Plan 2's `build-dashboard` job builds
  it; Plan 3's compose provides the `web` service that resolves that name.

- [ ] **Step 1: Create the nginx configuration**

Create `chatbot_dashboard/docker/nginx.conf`:

```nginx
# The API service as named in docker-compose.production.yml. nginx resolves
# this at startup, so the dashboard container must start after web is
# resolvable -- compose's depends_on handles that.
upstream api {
    server web:8000;
}

server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    # Must be >= MAX_UPLOAD_SIZE_MB (20 MB) plus overhead. nginx defaults to
    # 1 MB, which would 413 every document upload at the proxy before Django
    # ever saw the request. Carried over from the API's old nginx config.
    client_max_body_size 25M;

    # Same-origin API access, reproducing the rewrites vercel.json performs
    # today. This is what keeps VITE_API_BASE_URL empty and CORS off the
    # dashboard's critical path.
    location /api/ {
        proxy_pass http://api;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        # Chat answers wait on an LLM round trip; the default 60s is too tight.
        proxy_read_timeout 120s;
    }

    location /media/ {
        proxy_pass http://api;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        access_log off;
    }

    # Vite emits content-hashed filenames under /assets/, so they are safe to
    # cache indefinitely.
    location /assets/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
        try_files $uri =404;
    }

    # index.html must never be cached: it carries the hashed asset references,
    # so a cached copy points at assets a deploy has already replaced.
    location = /index.html {
        add_header Cache-Control "no-store";
    }

    # Client-side routing: any unknown path renders the SPA.
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

- [ ] **Step 2: Create the Dockerfile**

Create `chatbot_dashboard/Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1

# ---- builder ----------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# VITE_API_BASE_URL is deliberately left empty: nginx proxies /api and /media
# same-origin, so no API hostname is inlined into the bundle and a domain
# change needs no rebuild.
ENV VITE_API_BASE_URL=""

# `npm run build` is `tsc -b && vite build`, so this type-checks as well.
RUN npm run build


# ---- runtime ----------------------------------------------------------------
FROM nginx:alpine AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1
```

- [ ] **Step 3: Create the dockerignore**

Create `chatbot_dashboard/.dockerignore`:

```
node_modules
dist
.git
.gitignore
.env
.env.*
!.env.example
.vscode
.idea
.DS_Store
*.log
README.md
FRONTEND_API.md
api-docs.html
```

- [ ] **Step 4: Build the image**

Run: `cd chatbot_dashboard && docker build -t chatbot-dashboard:local .`
Expected: build succeeds.

- [ ] **Step 5: Verify nginx accepts the configuration**

Create the stub network FIRST, then run the syntax check inside it:

```bash
docker network create chatbot-check
docker run -d --name web --network chatbot-check nginx:alpine
docker run --rm --network chatbot-check chatbot-dashboard:local nginx -t
```

Expected: `syntax is ok` / `test is successful`.

The stub must exist before this check. nginx resolves `upstream` hostnames at
*parse* time, so running `nginx -t` on a standalone container fails with
`host not found in upstream "web"` no matter how correct the config is. (An
earlier draft of this step asserted both that it would pass and that it would
fail with that message — it cannot do both.) Running it inside the network
where `web` resolves tests what the step is actually for: that the config
parses.

Step 6 reuses this same network and stub, so nothing is created twice.

- [ ] **Step 6: Verify the SPA is served, with the upstream stubbed**

Create a throwaway network with a stand-in for `web` so nginx can start:

```bash
docker network create chatbot-check
docker run -d --name web --network chatbot-check nginx:alpine
docker run -d --name dash --network chatbot-check -p 8080:80 chatbot-dashboard:local

sleep 3
curl -s -o /dev/null -w 'root: %{http_code}\n'        http://localhost:8080/
curl -s -o /dev/null -w 'spa-route: %{http_code}\n'   http://localhost:8080/login
curl -sI http://localhost:8080/index.html | grep -i cache-control
```

Expected: `root: 200`, `spa-route: 200` (proving the SPA fallback works for
client-side routes), and `Cache-Control: no-store` on `index.html`.

- [ ] **Step 7: Verify the API proxy forwards rather than falling through to the SPA**

```bash
curl -s -o /dev/null -w 'api: %{http_code}\n' http://localhost:8080/api/v1/
```

Expected: **any non-200 code** — in practice `502`, because the stub `nginx:alpine`
standing in for `web` listens on port 80 while the upstream targets `web:8000`, so
the connection is refused. (An earlier draft of this step predicted `404`; that
assumed the stub served on 8000, which it does not.) The
point is that it is **not** `200`: a 200 would mean `/api/` fell through to the
SPA fallback instead of being proxied, which is the failure mode this config
exists to prevent.

- [ ] **Step 8: Clean up**

```bash
docker rm -f web dash
docker network rm chatbot-check
```

- [ ] **Step 9: Commit**

```bash
cd chatbot_dashboard
git add Dockerfile docker/nginx.conf .dockerignore
git commit -m "build: production image serving the SPA behind nginx

Multi-stage build: node compiles the bundle, nginx:alpine serves it.

nginx reproduces the /api and /media rewrites vercel.json performs today,
so the app stays same-origin: VITE_API_BASE_URL stays empty, no API
hostname is inlined into the bundle, and CORS is off the critical path.

client_max_body_size is 25M, carried over from the API's nginx config --
the 1M default would 413 every document upload at the proxy. index.html is
no-store so a deploy never serves stale hashed-asset references."
```

---

## Completion Criteria

Plan 1 is done when all of the following hold:

```bash
# chatbot_api
cd chatbot_api
ruff check . && ruff format --check .        # All checks passed
pre-commit run --all-files                    # all hooks pass
.venv/bin/python manage.py test               # Ran 358 tests, OK
docker build -t chatbot-api:local .           # succeeds

# chatbot_dashboard
cd ../chatbot_dashboard
npm run lint                                  # exit 0
npx tsc --noEmit                              # exit 0
npm run build                                 # succeeds
docker build -t chatbot-dashboard:local .     # succeeds
```

Plan 2 (meta-repo, submodules, `sync-to-meta`, Traefik, production compose,
`ci.yml`) starts from here.

## Deliberately Not In This Plan

- Deleting the orphaned modules (`knowledge/views/questions.py`,
  `views/tree.py`, `views/imports.py`, `views/languages.py`,
  `serializers/questions.py`, `serializers/tree.py`,
  `services/excel_import.py`, `management/commands/embed_qa.py`) — sub-project 2.
- Fixing `DJ001` on the five `null=True` string fields — needs migrations,
  sub-project 2.
- Deciding what `rag.py`'s discarded `dialect` value should do — sub-project 3
  (audit).
- Consolidating the `os.getenv` reads in `services/llm.py`,
  `services/embeddings.py` and `WhatsApp/services/` into settings —
  sub-project 2.
