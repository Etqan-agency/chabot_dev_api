"""Lenient request parsers."""

from rest_framework.parsers import JSONParser


class PlainTextJSONParser(JSONParser):
    """Parse ``text/plain`` request bodies as JSON.

    A convenience so clients that forget to set ``Content-Type: application/json``
    (e.g. ``fetch`` with a string body, or Postman's "raw + Text") still work
    instead of failing with ``415 Unsupported Media Type``. The body must still
    be valid JSON; malformed JSON returns a normal parse error.
    """

    media_type = "text/plain"
