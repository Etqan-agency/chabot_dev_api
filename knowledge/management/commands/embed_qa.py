"""Backfill embeddings for the Q&A bank.

Usage:
  python manage.py embed_qa            # embed any rows missing a vector
  python manage.py embed_qa --all      # re-embed everything
  python manage.py embed_qa --batch 64 # custom batch size
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from knowledge.models import QuestionAnswer
from knowledge.services.embeddings import embed_texts


class Command(BaseCommand):
    help = "Compute and store embeddings for QuestionAnswer rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-embed every row, not just rows missing an embedding.",
        )
        parser.add_argument("--batch", type=int, default=64, help="Number of rows per embedding call.")

    def handle(self, *args, **options):
        force = options["all"]
        batch = options["batch"]

        model = QuestionAnswer
        qs = model.objects.all() if force else model.objects.filter(embedding__isnull=True)
        count = qs.count()
        if not count:
            self.stdout.write("Nothing to embed.")
            return

        self.stdout.write(f"Embedding {count} row(s)…")
        total = 0
        ids: list = []
        texts: list[str] = []

        for obj in qs.iterator(chunk_size=batch):
            ids.append(obj.id)
            texts.append(_make_input(obj))
            if len(ids) >= batch:
                self._flush(model, ids, texts)
                total += len(ids)
                ids, texts = [], []

        if ids:
            self._flush(model, ids, texts)
            total += len(ids)

        self.stdout.write(self.style.SUCCESS(f"Embedded {total} row(s)."))

    @staticmethod
    def _flush(model, ids, texts):
        vectors, model_name = embed_texts(texts)
        for obj_id, vector in zip(ids, vectors, strict=True):
            model.objects.filter(pk=obj_id).update(embedding=vector, embedding_model=model_name)


def _make_input(obj) -> str:
    """Combine question + answer for richer semantic matching."""
    parts = [obj.question]
    overview = getattr(obj, "overview_description", "") or ""
    if overview:
        parts.append(overview)
    if obj.answer:
        parts.append(obj.answer)
    return "\n".join(parts)[:8000]
