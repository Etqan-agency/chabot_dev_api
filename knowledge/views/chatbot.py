from rest_framework import generics, permissions
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..models import ChatbotConfig
from ..serializers import ChatbotConfigSerializer


class ChatbotConfigView(generics.RetrieveUpdateAPIView):
    """Get or update the authenticated user's chatbot configuration.

    Identity/behaviour fields only; the grounding rules are enforced server-side
    in the prompt assembler, so a bot can never be configured to answer outside
    its own data.
    """

    serializer_class = ChatbotConfigSerializer
    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        config, _ = ChatbotConfig.objects.get_or_create(user=self.request.user)
        return config
