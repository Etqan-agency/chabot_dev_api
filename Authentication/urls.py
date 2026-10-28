from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from .views import (
    APIKeyViewSet,
    CustomTokenObtainPairView,
    EmailVerifyView,
    RegisterView,
    UserViewSet,
)

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("api-keys", APIKeyViewSet, basename="api-key")

urlpatterns = [
    path("auth/login/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/verify/", TokenVerifyView.as_view(), name="token_verify"),
    # Public self-signup (anyone) — creates an inactive account + emails a link.
    path("auth/register/", RegisterView.as_view(), name="register-public"),
    # Public: activate a new account from its email link (uid + token).
    path("auth/verify-email/", EmailVerifyView.as_view(), name="verify-email-public"),
]

urlpatterns += router.urls
