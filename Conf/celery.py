"""Celery application for background jobs (document ingestion, etc.).

Broker + result backend come from ``CELERY_*`` settings (Redis by default).
Worker: ``celery -A Conf worker -l info``.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Conf.settings")

app = Celery("chatbot")
# All Celery settings live under the CELERY_ namespace in Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):  # pragma: no cover - smoke test helper
    print(f"Request: {self.request!r}")
