import logging

from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import APIKey

logger = logging.getLogger(__name__)


class APIKeyAuthentication(TokenAuthentication):
    """Authenticate using a per-user API key.

    Two equivalent ways to send the key:
      * ``X-API-Key: <key>``            (custom header)
      * ``Authorization: ApiKey <key>`` (standard token scheme)
    """

    keyword = "ApiKey"
    # Django/WSGI form of the "X-API-Key" request header.
    header = "HTTP_X_API_KEY"

    def authenticate(self, request):
        # Prefer the custom X-API-Key header; otherwise fall back to the
        # "Authorization: ApiKey <key>" scheme handled by the parent class.
        raw_key = request.META.get(self.header)
        if raw_key:
            return self.authenticate_credentials(raw_key.strip())
        return super().authenticate(request)

    def authenticate_credentials(self, key):
        """Validate API key and return (user, auth)."""
        try:
            api_key = APIKey.objects.select_related("user").get(key=key)
        except APIKey.DoesNotExist:
            # Do not leak whether a key exists; the message is deliberately generic.
            raise AuthenticationFailed("Invalid API key.") from None

        if not api_key.is_active:
            raise AuthenticationFailed("API key is inactive.")

        if not api_key.user.is_active:
            raise AuthenticationFailed("User account is inactive.")

        api_key.last_used_at = timezone.now()
        api_key.save(update_fields=["last_used_at"])

        return (api_key.user, api_key)
