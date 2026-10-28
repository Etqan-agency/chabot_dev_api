"""Conf package.

Expose the Celery app so ``@shared_task`` and the worker discover it. Guarded so
the project still imports if Celery isn't installed (e.g. a minimal dev box).
"""

try:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except ModuleNotFoundError:  # Celery not installed — background jobs disabled.
    celery_app = None
    __all__ = ()
