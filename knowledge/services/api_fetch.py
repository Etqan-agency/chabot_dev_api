"""Fetch an external API *completely* — every page, not just the first one.

A paginated source hands out its content a page at a time. Ingesting only the
URL the tenant typed silently drops every record after the first page, which is
the single biggest way an API import loses data. ``fetch_all_pages`` follows the
source's own "next" pointer until it runs out.

Every hop is re-validated with :func:`validate_public_url`: the next-page URL is
supplied by the remote server, so trusting it blindly would reopen the SSRF hole
that validating the first URL closed.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from .net import UnsafeURLError, safe_get, validate_public_url

logger = logging.getLogger(__name__)

# Hard ceiling on pages per sync, so a source with a cyclic or endless paginator
# cannot spin forever. Overridable per call and via settings.
DEFAULT_MAX_PAGES = 200
DEFAULT_TIMEOUT = 30

# Keys a JSON body may use to point at the next page, in order of preference.
_NEXT_KEYS = (
    "next",
    "next_page_url",
    "nextPageUrl",
    "next_url",
    "nextUrl",
    "next_page",
    "nextPage",
)


def _max_pages(explicit: int | None) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    return int(getattr(settings, "API_SYNC_MAX_PAGES", DEFAULT_MAX_PAGES) or DEFAULT_MAX_PAGES)


def _next_from_body(payload: Any) -> str | None:
    """Pull the next-page URL out of a JSON body, if it advertises one."""
    if not isinstance(payload, dict):
        return None

    candidates: list[Any] = [payload.get(k) for k in _NEXT_KEYS]

    # JSON:API / HAL style: {"links": {"next": "..."}} — the link may itself be
    # an object such as {"href": "..."}.
    links = payload.get("links")
    if isinstance(links, dict):
        candidates.extend(links.get(k) for k in _NEXT_KEYS)
    meta = payload.get("meta")
    if isinstance(meta, dict):
        candidates.extend(meta.get(k) for k in _NEXT_KEYS)

    for value in candidates:
        if isinstance(value, dict):
            value = value.get("href") or value.get("url")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _next_from_headers(response) -> str | None:
    """RFC 5988 ``Link: <url>; rel="next"`` header."""
    links = getattr(response, "links", None)
    if isinstance(links, dict):
        nxt = links.get("next")
        if isinstance(nxt, dict):
            url = nxt.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def fetch_all_pages(
    url: str,
    *,
    max_pages: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> tuple[list[Any], bool]:
    """Fetch ``url`` and every following page.

    Returns ``(payloads, truncated)`` — one decoded JSON body per page, and
    whether the page ceiling cut the walk short (so the caller can say so
    instead of pretending the import was complete).

    The first request's errors propagate (the caller reports a failed sync); a
    later page that fails stops the walk but keeps the pages already fetched —
    a partial import that is honestly reported beats losing everything.
    """
    limit = _max_pages(max_pages)
    payloads: list[Any] = []
    seen: set[str] = set()
    current: str | None = url
    truncated = False

    while current:
        if current in seen:  # paginator pointing at itself / a cycle
            logger.warning("API sync: next-page URL repeated, stopping at %d page(s)", len(payloads))
            break
        seen.add(current)

        try:
            # safe_get, not http.get: redirects are followed one hop at a time
            # with every destination re-validated, and the body is size-capped.
            # requests follows redirects on its own, and the target is chosen by
            # the remote server -- so a host that passed validation could answer
            # `302 Location: http://169.254.169.254/...` and have the metadata
            # service's reply ingested into this tenant's knowledge base, where
            # chat would read it back out.
            response = safe_get(current, timeout=timeout, session=session)
            response.raise_for_status()
            payloads.append(response.json())
        except Exception:
            if not payloads:
                raise  # the very first page failed -> the sync genuinely failed
            # Keep what we already hold rather than discarding a good import.
            logger.warning("API sync: page %d failed, keeping earlier pages", len(payloads) + 1, exc_info=True)
            truncated = True
            break

        if len(payloads) >= limit:
            # Only "truncated" if the source actually had more to give.
            if _next_from_body(payloads[-1]) or _next_from_headers(response):
                truncated = True
                logger.warning("API sync: stopped at the %d-page ceiling; source has more", limit)
            break

        nxt = _next_from_body(payloads[-1]) or _next_from_headers(response)
        if not nxt:
            break

        try:
            current = validate_public_url(requests.compat.urljoin(current, nxt))
        except UnsafeURLError:
            logger.warning("API sync: refusing unsafe next-page URL, stopping")
            break
        except Exception:
            logger.warning("API sync: unusable next-page URL, stopping", exc_info=True)
            break

    return payloads, truncated
