"""Network safety helpers — primarily SSRF protection for outbound fetches.

The API-content sync fetches a tenant-supplied URL server-side. Without
validation that is a Server-Side Request Forgery (SSRF) vector: a caller could
point it at internal services (databases, admin panels) or the cloud metadata
endpoint (169.254.169.254) to steal credentials. ``validate_public_url`` blocks
non-public destinations before we ever connect.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL is not a safe, public http(s) destination."""


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — includes cloud metadata
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> str:
    """Return ``url`` if it is a safe public http(s) URL, else raise UnsafeURLError.

    Rejects non-http(s) schemes and any host that resolves to a private,
    loopback, link-local, reserved, or otherwise non-public address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http and https URLs are allowed.")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL is missing a host.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError("Could not resolve the URL's host.") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise UnsafeURLError("URL resolves to a private or reserved network address.")

    return url


# --- Safe fetching ----------------------------------------------------------
# Validating the URL the tenant typed is only half the guard. ``requests``
# follows redirects automatically, and a redirect target is chosen by the remote
# server -- so a host that passes validation can answer
#   302 Location: http://169.254.169.254/latest/meta-data/...
# and the fetcher will obediently follow it. For the API sync that is worse than
# a blind SSRF: the response is ingested into the tenant's knowledge base and
# can then be read back through chat, which turns it into data exfiltration.
#
# So redirects are followed here manually, one hop at a time, with every hop
# re-validated -- and the body is size-capped, because an endless response is
# its own denial of service.

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 25 * 1024 * 1024


class ResponseTooLarge(ValueError):
    """The remote body exceeded the size cap."""


def safe_get(url: str, *, timeout: int = 30, session=None, max_bytes: int | None = None):
    """GET ``url``, validating every hop and capping the body.

    Returns the final ``requests.Response`` with ``_content`` already read.
    Raises :class:`UnsafeURLError` if any hop -- including a redirect target --
    is not a public destination, and :class:`ResponseTooLarge` past the cap.
    """
    import requests

    http = session or requests
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    current = validate_public_url(url)

    for _ in range(MAX_REDIRECTS + 1):
        response = http.get(current, timeout=timeout, allow_redirects=False, stream=True)

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise UnsafeURLError("Redirect without a destination.")
            # Relative redirects resolve against the URL we actually requested.
            current = validate_public_url(requests.compat.urljoin(current, location))
            continue

        # Trust the declared length only to fail early; the real cap is counted.
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            response.close()
            raise ResponseTooLarge(f"Response is larger than {limit} bytes.")

        body = bytearray()
        for piece in response.iter_content(chunk_size=64 * 1024):
            body.extend(piece)
            if len(body) > limit:
                response.close()
                raise ResponseTooLarge(f"Response is larger than {limit} bytes.")
        response._content = bytes(body)
        response._content_consumed = True
        return response

    raise UnsafeURLError(f"Too many redirects (more than {MAX_REDIRECTS}).")
