"""Rebuild a synced API document's chunks from the local archive.

Because every synced record is stored verbatim in ``APISourceRecord``, the
searchable chunks can be regenerated at any time WITHOUT calling the source API
again — after an embedding outage, after a chunking change, or after switching
embedding models. This command is that rebuild.
"""

from django.core.management.base import BaseCommand, CommandError

from knowledge.models import SourceType, UploadedDocument
from knowledge.services.api_content_processor import APIContentProcessingError, APIContentRAGProcessor


class Command(BaseCommand):
    help = "Rebuild RAG chunks for an API-synced document from its stored raw records"

    def add_arguments(self, parser):
        parser.add_argument(
            "document",
            type=str,
            help="Document id (UUID) or exact filename of the API-synced document",
        )
        parser.add_argument(
            "--ai-analysis",
            action="store_true",
            help="Also (re)run the AI description for records that have none",
        )

    def handle(self, *args, **options):
        target = options["document"]
        qs = UploadedDocument.objects.filter(source_type=SourceType.API)
        doc = qs.filter(id=target).first() if _looks_like_uuid(target) else qs.filter(filename=target).first()
        if doc is None:
            raise CommandError(f"No API-synced document matching {target!r}")

        records = list(doc.api_records.all())
        if not records:
            raise CommandError(
                f"'{doc.filename}' has no archived records to rebuild from. "
                "Run sync_api_content first (older syncs predate the archive)."
            )

        self.stdout.write(self.style.WARNING(f"Rebuilding {len(records)} record(s) of '{doc.filename}'..."))

        processor = APIContentRAGProcessor(
            document_name=doc.filename,
            user=doc.uploaded_by,
            api_url=doc.api_url,
            items_key=doc.items_key,
        )
        pages = {r.source_id: r.page for r in records}
        ids = {id(r.raw): r.source_id for r in records}

        try:
            stats = processor.process_items(
                [r.raw for r in records],
                id_fn=lambda item: ids.get(id(item), ""),
                full_refresh=True,
                ai_analysis=options["ai_analysis"],
                pages=pages,
            )
        except APIContentProcessingError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"\nRebuilt from the archive — no source API call:\n"
                f"  Records: {stats['processed']}\n"
                f"  AI analyses: {stats['items_analyzed']}\n"
                f"  Chunks created: {stats['chunks_created']}\n"
                f"  Errors: {stats['errors']}"
            )
        )


def _looks_like_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
