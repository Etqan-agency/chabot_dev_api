"""Shared validation for uploaded knowledge files."""

from __future__ import annotations

from django.conf import settings


def validate_upload_size(uploaded_file) -> None:
    """Reject files larger than ``MAX_UPLOAD_SIZE_MB``.

    Raises ``ValueError`` with a human-readable message so callers can map it
    to a 400 response. Ingestion is synchronous, so this guards request time,
    embedding cost, and memory.
    """
    max_mb = float(getattr(settings, "MAX_UPLOAD_SIZE_MB", 20) or 0)
    if max_mb <= 0:
        return  # 0 / unset disables the limit

    size = getattr(uploaded_file, "size", 0) or 0
    max_bytes = int(max_mb * 1024 * 1024)
    if size > max_bytes:
        actual_mb = size / (1024 * 1024)
        raise ValueError(f"File is too large ({actual_mb:.1f} MB). Maximum allowed size is {max_mb:g} MB.")


def validate_upload_type(uploaded_file) -> None:
    """Reject a file the ingestion pipeline could never read.

    Without this, any 20 MB blob was accepted, written to disk, counted against
    the tenant's storage quota, and only then failed to parse -- so the upload
    endpoint doubled as a small file host for content the product has no use
    for. Checking here turns that into an immediate, explanatory 400.

    The extension is the check, deliberately, not the browser's Content-Type:
    that header is chosen by the client and means nothing. The extension is not
    a trust boundary either -- it decides which parser runs, and every parser in
    the allowlist is one we already run on tenant data.
    """
    from pathlib import Path

    from .chunking import SUPPORTED_SUFFIXES

    name = getattr(uploaded_file, "name", "") or ""
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported file type '{suffix or '(none)'}'. Allowed: {allowed}.")
