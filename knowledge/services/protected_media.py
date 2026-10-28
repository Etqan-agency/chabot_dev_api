"""Hand a tenant their own uploaded file, and nobody else's.

``MEDIA_URL`` used to be the only way to read an uploaded document, and nginx
served that location directly off disk with no authentication. The document's
path -- ``documents/<uuid>/<filename>`` -- is unguessable, but it is also
returned in API responses, so the file was protected by nothing stronger than
the secrecy of a URL that travels through browsers, referrers, proxies, logs
and support tickets. Once seen, it was public forever, and with ``access_log
off`` there was no record of who fetched it.

Downloads now go through a view that checks ownership. The bytes themselves are
still served by nginx -- streaming megabytes through gunicorn would tie up a
worker per download -- using ``X-Accel-Redirect`` into a location marked
``internal``, which nginx refuses to serve on a direct request.

Where no such front end exists (runserver, tests, a deployment without nginx)
the file is streamed by Django instead. The authorisation check is identical in
both cases; only who moves the bytes differs.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import posixpath
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse

logger = logging.getLogger(__name__)

#: nginx location that maps to MEDIA_ROOT and is marked `internal`.
INTERNAL_PREFIX = "/protected-media/"


def _use_x_accel() -> bool:
    return bool(getattr(settings, "USE_X_ACCEL_REDIRECT", False))


def _safe_name(name: str) -> str:
    """The storage-relative path, rejected if it tries to escape MEDIA_ROOT.

    The name comes from the database rather than a request, so this is a
    backstop; it costs nothing and it means a corrupted or hand-edited row
    cannot turn into an arbitrary-file read.
    """
    normalised = posixpath.normpath(str(name).replace("\\", "/")).lstrip("/")
    if normalised.startswith("../") or normalised == ".." or os.path.isabs(normalised):
        raise Http404("No such file.")
    return normalised


def serve(file_field, *, download_name: str = "") -> HttpResponse:
    """Return a response delivering ``file_field``. Caller has already authorised."""
    if not file_field:
        raise Http404("This document has no stored file.")

    name = _safe_name(file_field.name)
    filename = download_name or posixpath.basename(name) or "download"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # Never inline: an uploaded .html or .svg rendered on our origin would run
    # its script against this domain.
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"

    if _use_x_accel():
        response = HttpResponse(status=200)
        response["X-Accel-Redirect"] = INTERNAL_PREFIX + quote(name)
        response["Content-Type"] = content_type
        response["Content-Disposition"] = disposition
        # Let nginx set the length; ours would be a guess.
        del response["Content-Length"]
        return response

    try:
        handle = file_field.open("rb")
    except FileNotFoundError as exc:
        raise Http404("The stored file is missing.") from exc

    response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = disposition
    return response
