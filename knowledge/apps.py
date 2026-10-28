from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "knowledge"
    verbose_name = "Knowledge Base"

    def ready(self):
        # Wire up post-save signals (auto-embedding of Q&A rows).
        # Registering the startup checks is the import's only purpose.
        from . import (
            checks,  # noqa: F401
            signals,  # noqa: F401
        )
