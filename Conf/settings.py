"""Django settings for the ChatBot REST API."""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from Conf.db import database_config

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if not value:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


_INSECURE_SECRET_KEY = "django-insecure-change-me"  # dev default; refused in production below
SECRET_KEY = os.getenv("SECRET_KEY", _INSECURE_SECRET_KEY)
DEBUG = _env_bool("DEBUG", default=False)

ALLOWED_HOSTS = _env_list(
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "una-ai-tools-apis.una-oic.org"],
)

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


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",
    "django_filters",
    # Local
    "Authentication",
    "knowledge",
    "subscriptions",
    "WhatsApp",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "Conf.security_headers.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "Conf.urls"
WSGI_APPLICATION = "Conf.wsgi.application"
ASGI_APPLICATION = "Conf.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# `manage.py test` forces DEBUG to False AFTER settings are imported, so the
# suite would look like production to the guard in database_config and be
# refused on any machine without Postgres. The invocation is the only thing
# that still distinguishes them at import time.
_RUNNING_TESTS = sys.argv[1:2] == ["test"] or Path(sys.argv[0]).name.startswith("pytest")

# SECRET_KEY is also SIMPLE_JWT's SIGNING_KEY (see below), so a known value is
# not a weak secret -- it is the ability to mint a valid access token for any
# account, including a superuser, without touching the login endpoint. The
# fallback exists so a checkout runs; shipping with it must be impossible, and
# silence is what makes that easy, so refuse to boot instead.
if SECRET_KEY == _INSECURE_SECRET_KEY and not DEBUG and not _RUNNING_TESTS:
    raise ImproperlyConfigured(
        "SECRET_KEY is not set and DEBUG is off, so this process would run on "
        "the public development key.\n"
        "\n"
        "That key signs every JWT this service issues. Anyone holding it can "
        "forge an access token for any user, superusers included -- no "
        "password, no login attempt, nothing in the auth logs.\n"
        "\n"
        "Set SECRET_KEY to a long random value, unique to this deployment. To "
        "generate one:\n"
        '  python -c "from django.core.management.utils import '
        'get_random_secret_key as k; print(k())"'
    )

DATABASES = {
    "default": database_config(
        os.getenv("DATABASE_URL"),
        debug=DEBUG,
        base_dir=BASE_DIR,
        allow_sqlite=_env_bool("ALLOW_SQLITE", default=False),
        running_tests=_RUNNING_TESTS,
    )
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", BASE_DIR / "media"))

# Hand file delivery to nginx via X-Accel-Redirect once Django has authorised it
# (see knowledge/services/protected_media.py). Off by default so runserver and
# the test suite stream the bytes themselves; the Docker image turns it on.
USE_X_ACCEL_REDIRECT = _env_bool("USE_X_ACCEL_REDIRECT", default=False)

AUTH_USER_MODEL = "Authentication.User"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Scoped throttling: only acts on views that set `throttle_scope` (e.g. login),
    # so it never affects authenticated API traffic or the WhatsApp webhook.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",  # brute-force / credential-stuffing protection
        "register": "5/min",  # limit public self-signup abuse per IP
        # Endpoints that cost real money per call (LLM + embeddings) or that
        # send mail. Credit quotas already cap a tenant's SPEND; these cap the
        # RATE, which is what protects the providers' limits, the worker pool
        # and a user's inbox. Generous enough not to be felt in normal use.
        "chat": "60/min",
        "ingest": "10/min",  # API sync + website crawl: minutes of work each
        "upload": "30/min",
        "email_code": "5/min",  # password / email-change codes
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        # Accept text/plain bodies as JSON too (tolerant of clients that omit
        # the Content-Type: application/json header).
        "Conf.parsers.PlainTextJSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=3),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "ChatBot API",
    "DESCRIPTION": "REST API for the ChatBot platform with WhatsApp integration.",
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/v1/",
    "TAGS": [
        {"name": "Authentication", "description": "Login, refresh, register"},
        {"name": "Questions", "description": "Fixed and dynamic Q&A"},
        {"name": "Tree", "description": "Hierarchical guided questions"},
        {"name": "Documents", "description": "RAG document storage"},
        {"name": "Chat", "description": "Chat endpoints"},
        {"name": "WhatsApp", "description": "WhatsApp integration"},
        {"name": "Analytics", "description": "Aggregated metrics"},
    ],
}


SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# Document downloads and password-reset links are URLs that must not travel
# to a third-party site in a Referer header.
SECURE_REFERRER_POLICY = "same-origin"


CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "chatbot-api-cache",
    }
}


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
        "simple": {"format": "{levelname} {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "knowledge": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "WhatsApp": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# Application-specific knobs
RAG_SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.5"))

# Max size (MB) for a single uploaded knowledge document. Ingestion runs
# synchronously (parse -> chunk -> embed), so this caps request time and cost.
MAX_UPLOAD_SIZE_MB = float(os.getenv("MAX_UPLOAD_SIZE_MB", "20"))

# --- Rolling deploys ---------------------------------------------------------
# While this file exists inside a container, /health/ returns 503 so Traefik
# takes the replica out of rotation BEFORE the rolling deploy stops it. Created
# and removed by scripts/lib/rollout.sh; irrelevant outside a rollout.
DRAIN_FILE = os.getenv("DRAIN_FILE", "/tmp/drain")  # noqa: S108

# --- Retrieval (RAG) tuning -------------------------------------------------
# Final number of chunks passed to the LLM.
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
# Candidate pool pulled before re-ranking (fetch_k >= top_k).
RAG_FETCH_K = int(os.getenv("RAG_FETCH_K", "40"))
# Diversify results with Maximal Marginal Relevance (avoids near-duplicate chunks).
RAG_USE_MMR = _env_bool("RAG_USE_MMR", default=True)
# MMR trade-off: 1.0 = pure relevance, 0.0 = pure diversity.
RAG_MMR_LAMBDA = float(os.getenv("RAG_MMR_LAMBDA", "0.6"))
# Rows scanned per batch by the numpy backend (bounds peak memory per query).
RAG_SCAN_BATCH = int(os.getenv("RAG_SCAN_BATCH", "2000"))
# Vector search backend: "numpy" (works everywhere) or "pgvector" (Postgres + index).
RAG_VECTOR_BACKEND = os.getenv("RAG_VECTOR_BACKEND", "numpy").lower()

# Max conversation turns kept in a chat `history` (1 turn = user + assistant).
CHAT_MAX_HISTORY_TURNS = int(os.getenv("CHAT_MAX_HISTORY_TURNS", "10"))

# --- Guided question tree ----------------------------------------------------
# The canonical language: the ONE language authored by hand; every other active
# language is a generated positional mirror of it. Never deletable.
GUIDED_TREE_CANONICAL_LANGUAGE = os.getenv("GUIDED_TREE_CANONICAL_LANGUAGE", "ar")
# Seconds to cache each language's rendered tree JSON (0 disables caching).
GUIDED_TREE_CACHE_TTL = int(os.getenv("GUIDED_TREE_CACHE_TTL", "300"))
# Run translation on write in a background thread (like INGESTION_MODE "thread").
# "thread" = don't block the write; "sync" = inline (deterministic for tests).
GUIDED_TREE_TRANSLATE_MODE = os.getenv("GUIDED_TREE_TRANSLATE_MODE", "thread").lower()
# The model that translates the tree -- a small fast one is plenty -- and the one
# tried when it fails (empty = LLM_MODEL). Both go through LLM_PROVIDER.
GUIDED_TREE_TRANSLATE_MODEL = os.getenv("GUIDED_TREE_TRANSLATE_MODEL", "gpt-4o-mini")
GUIDED_TREE_TRANSLATE_FALLBACK_MODEL = os.getenv("GUIDED_TREE_TRANSLATE_FALLBACK_MODEL", "")
# Texts go to the LLM in batches (one call each, bounded by characters and
# count), and up to this many batches -- across all languages -- run at once.
GUIDED_TREE_TRANSLATE_BATCH_CHARS = int(os.getenv("GUIDED_TREE_TRANSLATE_BATCH_CHARS", "3000"))
GUIDED_TREE_TRANSLATE_BATCH_ITEMS = int(os.getenv("GUIDED_TREE_TRANSLATE_BATCH_ITEMS", "40"))
GUIDED_TREE_TRANSLATE_CONCURRENCY = int(os.getenv("GUIDED_TREE_TRANSLATE_CONCURRENCY", "6"))

# --- Multi-tenancy: per-tenant default quotas --------------------------------
# A "tenant" is a User. These are the DEFAULTS; any tenant can be given its own
# limits via a TenantQuota row (null fields on that row fall back to these).
TENANT_MAX_DOCUMENTS = int(os.getenv("TENANT_MAX_DOCUMENTS", "100"))
TENANT_MAX_TOTAL_MB = float(os.getenv("TENANT_MAX_TOTAL_MB", "200"))
# Sustained chat rate limit per tenant (requests counted in a 60s window).
TENANT_MAX_REQUESTS_PER_MIN = int(os.getenv("TENANT_MAX_REQUESTS_PER_MIN", "60"))
# Monthly LLM token budget per tenant (input+output). 0 = unlimited.
TENANT_MONTHLY_TOKEN_CAP = int(os.getenv("TENANT_MONTHLY_TOKEN_CAP", "0"))

# --- LLM / embedding pricing (USD per 1,000,000 tokens) ----------------------
# The real provider cost of every call; points are charged from it (see
# knowledge/services/billing.py). An unknown model costs 0 -- add it here.
LLM_PRICING = {
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
}
EMBEDDING_PRICING = {
    "text-embedding-3-large": 0.13,
    "text-embedding-3-small": 0.02,
}

# --- Points -----------------------------------------------------------------
# Every OpenAI call (chat, document embeddings, crawl, API import, knowledge
# edits, tree translation) is charged to the tenant at its real cost:
# 1 point = $0.001. A plan grants Plan.points per period (no rollover); a
# tenant with no subscription gets FREE_TIER_POINTS each calendar month.
FREE_TIER_POINTS = int(os.getenv("FREE_TIER_POINTS", "300"))
# Points held before a chat answer and settled to the real cost after it (a
# gpt-4o RAG answer is ~12). A tenant with less still gets an answer: the
# reservation shrinks to what is left.
CHAT_POINTS_RESERVE = int(os.getenv("CHAT_POINTS_RESERVE", "20"))

# --- Free tier limits -------------------------------------------------------
# Applied to non-staff tenants with NO active paid plan (public free signups).
# Staff/admin and paid-plan tenants are unaffected. Keeps free data tiny: one
# small Word file (~3 pages).
FREE_TIER_MAX_DOCUMENTS = int(os.getenv("FREE_TIER_MAX_DOCUMENTS", "1"))
FREE_TIER_MAX_TOTAL_MB = float(os.getenv("FREE_TIER_MAX_TOTAL_MB", "0.5"))
# Free tier cannot import knowledge from an external API (sync-api-content).
FREE_TIER_ALLOW_API_SYNC = os.getenv("FREE_TIER_ALLOW_API_SYNC", "false").lower() in {"1", "true", "yes", "on"}

# --- WhatsApp ---------------------------------------------------------------
# "thread" answers an incoming message after the webhook has already returned
# 200 (Meta re-delivers anything slower than a few seconds); "sync" answers
# inline (tests).
WHATSAPP_PROCESS_MODE = os.getenv("WHATSAPP_PROCESS_MODE", "thread").lower()
# Conversation context for each answer: up to this many earlier messages of the
# session, from the last WHATSAPP_HISTORY_MINUTES minutes (0 messages = none).
WHATSAPP_HISTORY_MESSAGES = int(os.getenv("WHATSAPP_HISTORY_MESSAGES", "10"))
WHATSAPP_HISTORY_MINUTES = int(os.getenv("WHATSAPP_HISTORY_MINUTES", "60"))
# Meta Graph API version for every WhatsApp call.
WHATSAPP_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
# Embedded Signup (tenants connect their own number from the dashboard): the
# Meta App id and the "Facebook Login for Business" configuration id. Both are
# public (the dashboard needs them); the button is hidden until both are set.
# The code exchange also needs WHATSAPP_APP_SECRET.
WHATSAPP_APP_ID = os.getenv("WHATSAPP_APP_ID", "")
WHATSAPP_ES_CONFIG_ID = os.getenv("WHATSAPP_ES_CONFIG_ID", "")

# --- External API sync ------------------------------------------------------
# A paginated source is followed to the end so no page is silently dropped;
# this ceiling stops a cyclic or endless paginator from running forever.
API_SYNC_MAX_PAGES = int(os.getenv("API_SYNC_MAX_PAGES", "200"))
# Describe each synced record with the chat LLM before indexing it. The
# description is stored next to the raw record, never in place of it, so
# turning this off (or an LLM outage) costs retrieval quality, never data.
API_SYNC_AI_ANALYSIS = _env_bool("API_SYNC_AI_ANALYSIS", default=True)
# Records analysed concurrently (the calls are I/O-bound).
API_SYNC_ANALYSIS_WORKERS = int(os.getenv("API_SYNC_ANALYSIS_WORKERS", "4"))

# --- API ingestion quality control ------------------------------------------
# Fraction of source leaf fields that must survive structuring, and fraction of
# fetched records that must be structurable, for a sync to be promoted. The
# structuring pass is deterministic and lossless, so the honest bar is 1.0:
# anything less means a real bug, and the right response is to keep the previous
# knowledge base rather than to quietly index an incomplete one. Lower it only
# to get an unblocked import out of a source you already know is malformed.
API_SYNC_MIN_FIELD_COVERAGE = float(os.getenv("API_SYNC_MIN_FIELD_COVERAGE", "1.0"))
API_SYNC_MIN_RECORD_COVERAGE = float(os.getenv("API_SYNC_MIN_RECORD_COVERAGE", "1.0"))
# Inline every URL in the embedded text. Off by default: on a media-heavy
# catalogue image links are ~30% of the text and carry no retrievable meaning,
# so they are preserved in the record's metadata instead of diluting the vector.
API_RAG_INCLUDE_URLS = _env_bool("API_RAG_INCLUDE_URLS", default=False)

# --- Paddle (Billing) -------------------------------------------------------
# Server API key (outbound calls) and the WEBHOOK signing secret (to verify
# incoming notifications). Keep both in .env only. Start in sandbox.
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "")
PADDLE_ENV = os.getenv("PADDLE_ENV", "sandbox").lower()  # "sandbox" | "production"
PADDLE_API_BASE = "https://api.paddle.com" if PADDLE_ENV == "production" else "https://sandbox-api.paddle.com"

# --- Ingestion execution ----------------------------------------------------
# How document ingestion runs:
#   "sync"   - in-request (default; simplest)
#   "thread" - daemon thread, returns immediately (single server, no infra)
#   "celery" - enqueue to a Celery worker (production; needs Redis + worker)
INGESTION_MODE = os.getenv("INGESTION_MODE", "sync").lower()

# --- Website crawl (import a client's whole site as knowledge) ----------------
# Default pages to crawl per request, a hard server-side cap the request can't
# exceed, and the per-page HTTP timeout (seconds).
WEBSITE_CRAWL_MAX_PAGES = int(os.getenv("WEBSITE_CRAWL_MAX_PAGES", "100"))
WEBSITE_CRAWL_MAX_PAGES_CAP = int(os.getenv("WEBSITE_CRAWL_MAX_PAGES_CAP", "300"))
WEBSITE_CRAWL_TIMEOUT = int(os.getenv("WEBSITE_CRAWL_TIMEOUT", "15"))

# How low-confidence chat questions are captured as knowledge gaps (same modes
# as INGESTION_MODE). The capture runs an LLM filter, so "thread"/"celery" keep
# it off the chat request path in production; "sync" keeps tests deterministic.
UNANSWERED_CAPTURE_MODE = os.getenv("UNANSWERED_CAPTURE_MODE", "sync").lower()

# --- Email -------------------------------------------------------------------
# Real SMTP is used as soon as EMAIL_HOST is set (host/user/password via env);
# until then emails are printed to the server console so the flow is testable
# with zero infrastructure.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
    # Gmail prints app passwords in groups of four; the spaces are presentation
    # only and are not part of the secret. Pasted verbatim they fail auth with a
    # 535 that says nothing about why, so they are stripped once here rather than
    # in every .env anyone ever writes.
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").replace(" ", "")
    EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
    # Every send is synchronous with fail_silently=False, so without a timeout an
    # unreachable relay holds the worker for as long as the OS allows and
    # registration just hangs. Ten seconds is generous for a slow provider.
    EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "ChatBot <no-reply@chatbot.local>")
# Where the email-verification link points. The frontend page reads uid+token
# from the query string and POSTs them to /auth/verify-email/ to activate.
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
# How long an email-verification code stays valid, and how many wrong tries
# before it's locked.
EMAIL_VERIFICATION_TTL_MINUTES = int(os.getenv("EMAIL_VERIFICATION_TTL_MINUTES", "15"))
EMAIL_VERIFICATION_MAX_ATTEMPTS = int(os.getenv("EMAIL_VERIFICATION_MAX_ATTEMPTS", "5"))

# --- Celery / Redis ---------------------------------------------------------
# In Docker these point at the `redis` service; locally default to localhost.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 60 * 30  # 30 min hard cap per ingestion task
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# --- Production hardening (behind the nginx reverse proxy) -------------------
# Trust the proxy's X-Forwarded-Proto so Django knows the request was HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
# Domains allowed to POST (admin, browsable API). Set to your https domain(s).
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS", default=[])

# Extra HTTPS-only protections, enabled when DEBUG is off.
if not DEBUG:
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", default=True)
    CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", default=True)
    SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", default=False)
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    SECURE_HSTS_PRELOAD = _env_bool("SECURE_HSTS_PRELOAD", default=True)

# --- Redis cache (used when REDIS_URL points at a reachable Redis) -----------
# Overrides the in-memory cache so rate-limit counters etc. are shared across
# gunicorn workers. Falls back silently to locmem if you don't set USE_REDIS_CACHE.
if _env_bool("USE_REDIS_CACHE", default=False):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
