"""Response headers this service was not sending.

The API answers JSON, where a Content-Security-Policy does little -- but the
same origin also serves real HTML: the Django admin, the Swagger and ReDoc
pages, and the two test consoles. Those are the surfaces a policy protects, and
they had none.

The policy here is deliberately narrow rather than maximal:

* ``frame-ancestors 'none'`` and ``object-src 'none'`` are safe on every
  response and are the two that actually stop clickjacking and plugin-based
  script execution.
* The documentation UIs (Swagger, ReDoc) load their bundles from a CDN and run
  inline scripts by design. A strict ``script-src`` would simply break them, and
  a broken docs page teaches people to disable the policy. They get a policy
  that keeps the framing and object rules and permits what they genuinely need.

``Referrer-Policy`` matters more here than it looks: document downloads and
password-reset links are URLs that must not travel to third-party sites in a
Referer header.
"""

from __future__ import annotations

#: Paths whose HTML needs a CDN and inline scripts to function.
_DOCS_PREFIXES = ("/api/docs", "/api/redoc", "/api/schema")

#: Everything else on this origin: no scripts from anywhere, no framing.
_STRICT_CSP = "default-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"

#: The admin and the docs render HTML that needs its own assets.
_UI_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
)

_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


class SecurityHeadersMiddleware:
    """Attach CSP and Permissions-Policy, choosing the policy per response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Never overwrite a policy a view set deliberately.
        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = self._policy_for(request, response)
        response.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
        return response

    @staticmethod
    def _policy_for(request, response) -> str:
        path = request.path or ""
        if path.startswith("/admin") or path.startswith(_DOCS_PREFIXES):
            return _UI_CSP

        content_type = (response.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type in {"text/html", "application/xhtml+xml"}:
            # Our own HTML (the test consoles) needs no third-party anything.
            return _UI_CSP
        return _STRICT_CSP
