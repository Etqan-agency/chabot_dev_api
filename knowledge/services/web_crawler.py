"""Crawl a client's website and ingest every page as RAG knowledge.

Given a start URL, this discovers as many pages of the SAME site as it can
(sitemap first, then breadth-first over internal links), extracts the main
readable text of each page (stripping nav/menus/footers/boilerplate with
``trafilatura`` — best-in-class main-content extraction), and feeds the pages
into the same incremental pipeline the API-sync uses. Re-crawling only
re-embeds pages whose content changed (``source_id`` = the page URL).

Safety:
* Every URL is SSRF-checked (``validate_public_url``) before we connect, so a
  tenant can't point the crawler at internal/metadata addresses.
* We stay on the start URL's host, honour ``robots.txt``, only parse
  ``text/html``, cap the page count, and time out each request.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from django.conf import settings

from ..models import DocumentStatus, SourceType, UploadedDocument
from .api_content_processor import APIContentProcessingError, APIContentRAGProcessor
from .net import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)

USER_AGENT = "ChatBotApi-Crawler/1.0 (+knowledge ingestion)"

# Non-page resources we never want to fetch as knowledge.
_SKIP_SUFFIXES = (
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".tar",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".css",
    ".js",
    ".json",
    ".xml",
    ".rss",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".dmg",
    ".exe",
)


def _default_limits() -> tuple[int, int, int]:
    return (
        int(getattr(settings, "WEBSITE_CRAWL_MAX_PAGES", 100)),
        int(getattr(settings, "WEBSITE_CRAWL_MAX_PAGES_CAP", 300)),
        int(getattr(settings, "WEBSITE_CRAWL_TIMEOUT", 15)),
    )


def _normalize(url: str) -> str:
    """Drop the fragment and trailing slash so URL variants dedupe as one page.

    ``https://ex.com`` and ``https://ex.com/`` (and ``/about`` vs ``/about/``)
    are the same page, so collapse a trailing slash when there's no query string.
    """
    url, _frag = urldefrag(url)
    if url.endswith("/") and "?" not in url:
        url = url.rstrip("/")
    return url


def _same_site(url: str, host: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return False
    # Treat "www." as the same host.
    return netloc == host or netloc == f"www.{host}" or f"www.{netloc}" == host


def _looks_like_page(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not path.endswith(_SKIP_SUFFIXES)


def _load_robots(scheme: str, host: str, timeout: int) -> RobotFileParser | None:
    """Fetch and parse robots.txt; None if it can't be loaded (then allow all)."""
    robots_url = f"{scheme}://{host}/robots.txt"
    try:
        validate_public_url(robots_url)
        resp = requests.get(robots_url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp
    except (requests.RequestException, UnsafeURLError, ValueError):
        return None


def _discover_sitemap_urls(scheme: str, host: str, timeout: int, limit: int) -> list[str]:
    """Best-effort: pull page URLs from the site's sitemap(s)."""
    try:
        import trafilatura.sitemaps as sm  # type: ignore
    except Exception:  # noqa: BLE001 - sitemap discovery is optional
        return []
    homepage = f"{scheme}://{host}"
    try:
        urls = sm.sitemap_search(homepage, target_lang=None)
    except Exception:  # noqa: BLE001
        return []
    return [u for u in (urls or []) if _looks_like_page(u)][: limit * 2]


def _extract_links(html: str, base_url: str, host: str) -> list[str]:
    """Return normalized, same-site, page-like links found in the HTML."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = _normalize(urljoin(base_url, href))
        if _same_site(absolute, host) and _looks_like_page(absolute):
            out.append(absolute)
    return out


def _extract_title(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:255]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _extract_main_text(html: str, url: str) -> str:
    try:
        import trafilatura  # type: ignore
    except Exception:  # noqa: BLE001
        return ""
    try:
        return (
            trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                favor_recall=True,
                no_fallback=False,
            )
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def crawl_site(start_url: str, *, max_pages: int, timeout: int) -> list[dict]:
    """Crawl the site at ``start_url`` and return ``[{url, title, content}]``.

    Discovers pages via the sitemap first, then breadth-first over internal
    links, staying on the start URL's host. Each returned page has non-empty
    extracted text.
    """
    validate_public_url(start_url)
    start = _normalize(start_url)
    parsed = urlparse(start)
    host = parsed.netloc.lower()
    scheme = parsed.scheme

    robots = _load_robots(scheme, host, timeout)
    seen: set[str] = set()
    queue: deque[str] = deque()

    # Seed: sitemap URLs (broad coverage) then the start page itself.
    for u in _discover_sitemap_urls(scheme, host, timeout, max_pages):
        queue.append(_normalize(u))
    queue.append(start)

    pages: list[dict] = []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        if not _same_site(url, host) or not _looks_like_page(url):
            continue
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            continue
        try:
            validate_public_url(url)
        except UnsafeURLError:
            continue

        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            continue
        # A redirect could bounce us off-site or to an internal host — re-check.
        final_url = _normalize(str(resp.url))
        if not _same_site(final_url, host):
            continue
        try:
            validate_public_url(final_url)
        except UnsafeURLError:
            continue
        if resp.status_code >= 400:
            continue
        if "html" not in resp.headers.get("content-type", "").lower():
            continue

        html = resp.text
        seen.add(final_url)

        # Enqueue newly discovered internal links.
        for link in _extract_links(html, final_url, host):
            if link not in seen:
                queue.append(link)

        text = _extract_main_text(html, final_url)
        if text and text.strip():
            pages.append(
                {
                    "url": final_url,
                    "title": _extract_title(html),
                    "content": text.strip(),
                }
            )

    logger.info("Crawl of %s: %d pages ingested (visited %d)", start, len(pages), len(seen))
    return pages


def _page_text(item: dict) -> str:
    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    body = (item.get("content") or "").strip()
    header = f"{title}\n{url}\n\n" if title else f"{url}\n\n"
    return header + body


def _page_id(item: dict) -> str:
    return (item.get("url") or "").strip()


def ingest_site(
    *,
    user,
    start_url: str,
    document_name: str,
    max_pages: int,
    timeout: int,
    full_refresh: bool = False,
) -> dict:
    """Crawl ``start_url`` and incrementally sync its pages into RAG chunks.

    Returns the sync stats plus ``pages_crawled``. Marks the owning document
    COMPLETED/FAILED so callers polling ``GET /documents/{id}/`` see progress.
    The embedding calls are charged to ``user`` (points).
    """
    from ..models import UsageKind
    from . import billing

    with billing.charge_to(user, UsageKind.CRAWL):
        return _ingest_site(
            user=user,
            start_url=start_url,
            document_name=document_name,
            max_pages=max_pages,
            timeout=timeout,
            full_refresh=full_refresh,
        )


def _ingest_site(*, user, start_url, document_name, max_pages, timeout, full_refresh) -> dict:
    processor = APIContentRAGProcessor(
        document_name=document_name,
        user=user,
        api_url=start_url,
        items_key="",
        source_type=SourceType.WEBSITE,
    )
    doc = processor.api_document
    doc.processing_status = DocumentStatus.PROCESSING
    doc.error_message = ""
    doc.save(update_fields=["processing_status", "error_message"])

    try:
        pages = crawl_site(start_url, max_pages=max_pages, timeout=timeout)
        if not pages:
            doc.processing_status = DocumentStatus.FAILED
            doc.error_message = "No readable pages were found at that URL."
            doc.save(update_fields=["processing_status", "error_message"])
            return {"status": "empty", "pages_crawled": 0}

        stats = processor.process_items(
            pages,
            extract_text_fn=_page_text,
            id_fn=_page_id,
            full_refresh=full_refresh,
        )
        doc.processing_status = DocumentStatus.COMPLETED
        doc.save(update_fields=["processing_status"])
        return {"status": "success", "pages_crawled": len(pages), **stats}
    except APIContentProcessingError as exc:
        doc.processing_status = DocumentStatus.FAILED
        doc.error_message = str(exc)[:500]
        doc.save(update_fields=["processing_status", "error_message"])
        raise
    except Exception as exc:  # noqa: BLE001
        doc.processing_status = DocumentStatus.FAILED
        doc.error_message = str(exc)[:500]
        doc.save(update_fields=["processing_status", "error_message"])
        raise


def _run_in_thread(user_id, start_url, document_name, max_pages, timeout, full_refresh) -> None:
    from django.contrib.auth import get_user_model
    from django.db import connection

    try:
        user = get_user_model().objects.get(pk=user_id)
        ingest_site(
            user=user,
            start_url=start_url,
            document_name=document_name,
            max_pages=max_pages,
            timeout=timeout,
            full_refresh=full_refresh,
        )
    except Exception:
        logger.exception("Background website crawl failed for %s", start_url)
    finally:
        connection.close()


def dispatch_site_crawl(
    *,
    user,
    start_url: str,
    document_name: str,
    max_pages: int,
    timeout: int,
    full_refresh: bool = False,
) -> UploadedDocument:
    """Create the owning document, start the crawl in the background, return the doc.

    Crawling a whole site is slow, so it always runs off the request thread; the
    caller returns 202 and the client polls ``GET /documents/{id}/`` for status.
    """
    processor = APIContentRAGProcessor(
        document_name=document_name,
        user=user,
        api_url=start_url,
        items_key="",
        source_type=SourceType.WEBSITE,
    )
    doc = processor.api_document
    doc.processing_status = DocumentStatus.PROCESSING
    doc.error_message = ""
    doc.save(update_fields=["processing_status", "error_message"])

    threading.Thread(
        target=_run_in_thread,
        args=(user.pk, start_url, document_name, max_pages, timeout, full_refresh),
        daemon=True,
    ).start()
    logger.info("Queued website crawl for %s (doc %s)", start_url, doc.pk)
    return doc
