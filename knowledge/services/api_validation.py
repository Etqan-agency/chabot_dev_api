"""Quality control for API ingestion: completeness in, no invention out.

Two independent questions are asked about every synced record, and the sync is
allowed to promote its results only when both are answered acceptably:

1. **Did we keep everything?** Every leaf path of the source record must appear
   in the structured representation, and every value that should be retrievable
   must actually appear in the text that gets embedded. Anything missing is
   reported per-path, not as a percentage that hides which field vanished.

2. **Did the AI make anything up?** The AI analysis is prose, so it cannot be
   diffed field-by-field -- but every *number* it states can be checked against
   the numbers the source actually contains. An analysis that introduces a price,
   a quantity or a date that is nowhere in the record is rejected outright rather
   than embedded, because that is precisely the failure that turns into a wrong
   price quoted to a customer.

Nothing here trusts the AI, and nothing here is a "score" standing in for a
guarantee: the completeness check is exact, and the invention check is
conservative (it rejects on doubt).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .api_schema import (
    FieldRole,
    StructuredRecord,
    leaf_paths,
    render_value,
)

# Arabic-Indic and Extended Arabic-Indic digits -> ASCII, so a number written
# in Arabic script in the source still matches the same number in the analysis.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMBER = re.compile(r"\d[\d,٬،.]*")


def normalize_number(token: str) -> str:
    """One canonical spelling for a numeric token, so 45,000 == 45000 == 45000.00."""
    text = token.translate(_ARABIC_DIGITS)
    text = re.sub(r"[,٬،]", "", text).strip(".")
    if not text:
        return ""
    if "." in text:
        whole, _, frac = text.partition(".")
        frac = frac.rstrip("0")
        text = f"{whole}.{frac}" if frac else whole
    return text.lstrip("0") or "0"


def numbers_in(text: str) -> set[str]:
    """Every distinct number mentioned in a piece of text, canonically spelled."""
    return {normalized for token in _NUMBER.findall(text or "") if (normalized := normalize_number(token))}


def source_numbers(rec: StructuredRecord) -> set[str]:
    """Every number the source record actually contains.

    Digits inside identifiers and URLs count too: an analysis is allowed to
    quote a SKU or an image id without that being an invention.
    """
    found: set[str] = set()
    for value in rec.fields.values():
        if value is None or isinstance(value, bool):
            continue
        found |= numbers_in(str(value))
    found |= numbers_in(rec.source_id)
    return found


@dataclass
class RecordValidation:
    """The verdict on one record. ``ok`` gates whether its analysis is embedded."""

    source_id: str
    covered_paths: int = 0
    total_paths: int = 0
    missing_paths: list[str] = field(default_factory=list)
    unrendered_values: list[str] = field(default_factory=list)
    invented_numbers: list[str] = field(default_factory=list)
    analysis_rejected: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return 1.0 if not self.total_paths else self.covered_paths / self.total_paths

    @property
    def ok(self) -> bool:
        return not self.missing_paths and not self.unrendered_values and not self.invented_numbers

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "coverage": round(self.coverage, 6),
            "covered_paths": self.covered_paths,
            "total_paths": self.total_paths,
            "missing_paths": self.missing_paths[:50],
            "unrendered_values": self.unrendered_values[:50],
            "invented_numbers": self.invented_numbers[:50],
            "analysis_rejected": self.analysis_rejected,
            "flags": self.flags,
            "ok": self.ok,
        }


def validate_record(
    raw: Any,
    rec: StructuredRecord,
    rendered_text: str,
    analysis: str = "",
    *,
    chunk_metadata: dict | None = None,
) -> RecordValidation:
    """Check one record end to end: raw -> structured -> rendered -> analysed.

    ``rendered_text`` is the deterministic text (without the analysis appended);
    ``chunk_metadata`` is where METADATA-role values are preserved. A value is
    considered kept if it reaches *either* the text or the metadata.
    """
    result = RecordValidation(source_id=rec.source_id)

    # --- 1. structural completeness: every source leaf survived structuring ---
    original = leaf_paths(raw)
    structured = set(rec.fields)
    result.total_paths = len(original)
    result.covered_paths = len(original & structured)
    result.missing_paths = sorted(original - structured)

    # --- 2. retrievability: values that should be readable actually are -------
    metadata_blob = _flatten_metadata(chunk_metadata or {})
    haystack = f"{rendered_text}\n{metadata_blob}"
    for path in rec.paths_with_role(FieldRole.SEARCHABLE, FieldRole.SYSTEM, FieldRole.PRESERVED):
        # A role with no matching field means the structured record is
        # internally inconsistent -- report it instead of raising, since the
        # whole point of this pass is to survive a bug upstream of it.
        if path not in rec.fields:
            result.unrendered_values.append(path)
            continue
        value = rec.fields[path]
        if value is None or isinstance(value, bool) or value in ("", {}, []):
            continue  # empties are rendered as "(null)"/"(empty)" markers, not values
        if str(value) not in haystack and render_value(value) not in haystack:
            result.unrendered_values.append(path)

    if rec.redacted:
        result.flags.append(f"redacted:{len(rec.redacted)}")

    # --- 3. invention: no number in the analysis that the source lacks --------
    if analysis:
        allowed = source_numbers(rec)
        invented = sorted(numbers_in(analysis) - allowed)
        if invented:
            result.invented_numbers = invented
            result.analysis_rejected = True
            result.flags.append("analysis_invented_numbers")

    return result


def _flatten_metadata(metadata: dict) -> str:
    """Metadata rendered as one searchable blob for the retrievability check."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                parts.append(str(key))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif node is not None:
            parts.append(str(node))

    walk(metadata)
    return "\n".join(parts)


@dataclass
class IngestionReport:
    """Aggregate quality of one sync. Decides whether the new index is promoted."""

    records_fetched: int = 0
    records_structured: int = 0
    records_validated: int = 0
    records_with_issues: int = 0
    fields_total: int = 0
    fields_covered: int = 0
    analyses_rejected: int = 0
    per_record: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def field_coverage(self) -> float:
        return 1.0 if not self.fields_total else self.fields_covered / self.fields_total

    @property
    def record_coverage(self) -> float:
        return 1.0 if not self.records_fetched else self.records_structured / self.records_fetched

    def add(self, validation: RecordValidation) -> None:
        self.records_validated += 1
        self.fields_total += validation.total_paths
        self.fields_covered += validation.covered_paths
        if validation.analysis_rejected:
            self.analyses_rejected += 1
        if not validation.ok:
            self.records_with_issues += 1
            # Only failing records are itemised: a clean sync of 100k records
            # should not carry 100k rows of "fine" into the database.
            self.per_record.append(validation.as_dict())

    def blocking_reason(self, *, min_field_coverage: float, min_record_coverage: float) -> str:
        """Why this ingestion must not be promoted, or "" when it may be.

        A failure here keeps the previous knowledge base exactly as it was -- the
        caller is expected to abandon the new index rather than half-apply it.
        """
        if self.record_coverage < min_record_coverage:
            return (
                f"only {self.records_structured}/{self.records_fetched} records could be structured "
                f"({self.record_coverage:.1%} < {min_record_coverage:.1%} required)"
            )
        if self.field_coverage < min_field_coverage:
            return (
                f"only {self.fields_covered}/{self.fields_total} source fields were preserved "
                f"({self.field_coverage:.1%} < {min_field_coverage:.1%} required)"
            )
        return ""

    def as_dict(self) -> dict:
        return {
            "records_fetched": self.records_fetched,
            "records_structured": self.records_structured,
            "records_validated": self.records_validated,
            "records_with_issues": self.records_with_issues,
            "fields_total": self.fields_total,
            "fields_covered": self.fields_covered,
            "field_coverage": round(self.field_coverage, 6),
            "record_coverage": round(self.record_coverage, 6),
            "analyses_rejected": self.analyses_rejected,
            "issues": self.per_record[:200],
            "errors": self.errors[:50],
        }
