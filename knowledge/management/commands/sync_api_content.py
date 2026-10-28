"""Django management command to fetch API content and process for RAG."""

import requests
from django.core.management.base import BaseCommand, CommandError

from knowledge.services.api_content_processor import (
    APIContentProcessingError,
    APIContentRAGProcessor,
    IngestionBlocked,
    extract_all,
    source_statistics,
)
from knowledge.services.api_fetch import fetch_all_pages
from knowledge.services.api_ingest_run import IngestionRunRecorder


class Command(BaseCommand):
    help = "Fetch content from API and process for RAG"

    def add_arguments(self, parser):
        parser.add_argument(
            "api_url",
            type=str,
            help="API endpoint URL to fetch content from",
        )
        parser.add_argument(
            "--document-name",
            type=str,
            default="API Content",
            help="Name for the virtual document (default: 'API Content')",
        )
        parser.add_argument(
            "--items-key",
            type=str,
            default="results",
            help=(
                "JSON key containing the items list (default: 'results'). "
                "Auto-detected when absent; the response's other top-level "
                "fields are imported too."
            ),
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=0,
            help="Cap on pages followed for a paginated source (0 = the configured default)",
        )
        parser.add_argument(
            "--no-ai-analysis",
            action="store_true",
            help="Skip the AI description pass (records are still stored and indexed)",
        )

    def handle(self, *args, **options):
        api_url = options["api_url"]
        document_name = options["document_name"]
        items_key = options["items_key"]

        try:
            # Fetch the WHOLE source: every page, and every field of each page.
            self.stdout.write(self.style.WARNING(f"Fetching from {api_url}..."))
            payloads, truncated = fetch_all_pages(api_url, max_pages=options["max_pages"] or None)
            items, pages = extract_all(payloads, items_key)

            if not items:
                raise CommandError("The API response contained no content to import")

            self.stdout.write(self.style.SUCCESS(f"Fetched {len(items)} records across {len(payloads)} page(s)"))
            if truncated:
                self.stdout.write(self.style.WARNING("Stopped early: the source has more pages (--max-pages)"))

            # The census of the payload as it arrived, before any transform.
            # Everything reported below is measured against these numbers.
            census = source_statistics(
                items,
                endpoint=api_url,
                pages_fetched=len(payloads),
                truncated=truncated,
            )
            self.stdout.write(
                f"  {census['total_records']} record(s), {census['total_fields']} field(s), "
                f"{census['unique_ids']} unique id(s), {census['response_bytes']} byte(s)"
            )

            # Process for RAG
            self.stdout.write(self.style.WARNING("Processing content for RAG..."))
            recorder = IngestionRunRecorder(endpoint=api_url)
            recorder.fetched(
                pages=len(payloads),
                records=len(items),
                truncated=truncated,
                source_stats=census,
            )
            processor = APIContentRAGProcessor(document_name=document_name, api_url=api_url, items_key=items_key)
            recorder.stage("embedding", document=processor.api_document)
            try:
                stats = processor.process_items(
                    items,
                    ai_analysis=not options["no_ai_analysis"],
                    pages=pages,
                )
            except IngestionBlocked as exc:
                recorder.blocked(exc.reason, exc.report)
                raise CommandError(
                    f"Ingestion blocked by quality control: {exc.reason}\n"
                    "The previous knowledge base was left unchanged."
                ) from exc
            recorder.succeeded(stats)

            coverage = stats["field_coverage"]
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nRAG processing completed:\n"
                    f"  Items processed: {stats['processed']}\n"
                    f"  Records archived: {stats['items_stored']}\n"
                    f"  AI analyses: {stats['items_analyzed']}"
                    f" (rejected for inventing numbers: {stats['analyses_rejected']})\n"
                    f"  Chunks created: {stats['chunks_created']}\n"
                    f"  Source coverage: {stats['fields_covered']}/{stats['fields_total']} fields"
                    f" ({coverage:.1%})\n"
                    f"  Records needing review: {stats['records_with_issues']}\n"
                    f"  Errors: {stats['errors']}"
                )
            )

            self.stdout.write(self.style.SUCCESS("\nContent is now available in RAG queries!"))

        except (APIContentProcessingError, CommandError) as e:
            raise CommandError(f"Processing failed: {e}") from e
        except requests.RequestException as e:
            raise CommandError(f"API request failed: {e}") from e
        except Exception as e:
            raise CommandError(f"Unexpected error: {e}") from e
