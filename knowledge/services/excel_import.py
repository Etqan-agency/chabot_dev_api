"""Bulk-import question/answer rows from an .xlsx workbook."""

from __future__ import annotations

from dataclasses import dataclass

import openpyxl

from ..models import QuestionAnswer


@dataclass
class ImportResult:
    created: int
    skipped: int


def import_questions_from_xlsx(uploaded_file, *, created_by=None) -> ImportResult:
    workbook = openpyxl.load_workbook(uploaded_file)
    sheet = workbook.active

    header = {cell.value: idx for idx, cell in enumerate(sheet[1])}
    if "question" not in header or "answer" not in header:
        raise ValueError("Workbook must contain 'question' and 'answer' columns.")

    q_idx = header["question"]
    a_idx = header["answer"]
    created = 0
    skipped = 0

    for row in sheet.iter_rows(min_row=2, values_only=True):
        question = row[q_idx]
        answer = row[a_idx]
        if not question or not answer:
            skipped += 1
            continue
        if QuestionAnswer.objects.filter(question=question).exists():
            skipped += 1
            continue
        QuestionAnswer.objects.create(
            question=str(question),
            answer=str(answer),
            created_by=created_by,
        )
        created += 1

    workbook.close()
    return ImportResult(created=created, skipped=skipped)
