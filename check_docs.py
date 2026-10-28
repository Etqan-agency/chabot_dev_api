#!/usr/bin/env python
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Conf.settings")
django.setup()

from knowledge.models import UploadedDocument  # noqa: E402 -- must follow django.setup()

print("=" * 70)
print("ALL DOCUMENTS IN DATABASE")
print("=" * 70)

docs = UploadedDocument.objects.all().order_by("-created_at")

if docs.exists():
    for doc in docs:
        print("\nFilename:", doc.filename)
        print("ID:", doc.id)
        print("Size:", doc.file_size_mb, "MB")
        print("Chunks:", doc.chunks.count())
        print("Status:", doc.processing_status)
        print("Active:", doc.is_active)
        print("-" * 70)
else:
    print("NO DOCUMENTS FOUND!")

print("\n" + "=" * 70)
print("CHECKING CHUNK CONTENT FOR FIRST DOCUMENT")
print("=" * 70)

if docs.exists():
    first_doc = docs.first()
    chunks = first_doc.chunks.all()

    if chunks.exists():
        for chunk in chunks[:2]:
            print("\nChunk", chunk.position, ":")
            print("Length:", len(chunk.content), "characters")
            try:
                preview = chunk.content[:200].encode("utf-8", errors="ignore").decode("utf-8")
                print("First 200 chars:", preview)
            except Exception as e:
                print("Content preview (error):", str(e))
            print("-" * 70)
    else:
        print("NO CHUNKS FOUND FOR THIS DOCUMENT!")
