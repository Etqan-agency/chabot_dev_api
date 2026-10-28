"""Re-run the chunking + embedding pipeline for stored documents.

Usage:
  python manage.py reindex_documents             # only PENDING / FAILED
  python manage.py reindex_documents --all       # all active documents
  python manage.py reindex_documents <doc_id>    # a specific document
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from knowledge.models import DocumentStatus, UploadedDocument
from knowledge.services.chunking import ingest_document


class Command(BaseCommand):
    help = "Chunk and embed uploaded documents into DocumentChunk rows."

    def add_arguments(self, parser):
        parser.add_argument("doc_ids", nargs="*", type=str)
        parser.add_argument(
            "--all",
            action="store_true",
            help="Reindex every active document, even completed ones.",
        )

    def handle(self, *args, **options):
        ids = options["doc_ids"]
        force_all = options["all"]

        if ids:
            qs = UploadedDocument.objects.filter(id__in=ids)
            missing = set(ids) - {str(d.id) for d in qs}
            if missing:
                raise CommandError(f"Unknown document ids: {', '.join(missing)}")
        elif force_all:
            qs = UploadedDocument.objects.filter(is_active=True)
        else:
            qs = UploadedDocument.objects.filter(
                is_active=True,
                processing_status__in=[DocumentStatus.PENDING, DocumentStatus.FAILED],
            )

        total = qs.count()
        if not total:
            self.stdout.write("No documents to reindex.")
            return

        ok = 0
        for doc in qs:
            self.stdout.write(f"→ {doc.filename}")
            try:
                result = ingest_document(doc)
                self.stdout.write(self.style.SUCCESS(f"  {result.chunks_created} chunks"))
                ok += 1
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"  failed: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"Done — {ok}/{total} succeeded."))
