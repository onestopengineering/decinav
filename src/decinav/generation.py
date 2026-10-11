"""Strict AI envelope parsing, prospective validation, and batch persistence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Literal

from decinav.frontmatter import (
    ADRDomain,
    ADRParseError,
    ADRStatus,
    ParsedADR,
    parse_adr_text,
)
from decinav.repository import (
    ADRRecord,
    ADRRepositoryError,
    RepositoryAnalysis,
    ValidationIssue,
    analyze_records,
    load_repository_records,
)


MAX_GENERATED_RECORDS = 100
GENERATED_FILENAME_PATTERN = re.compile(
    r"^\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$"
)
ENVELOPE_PROVENANCE_FIELDS = frozenset({"generator", "sources", "note"})
_SOURCES_HEADING_PATTERN = re.compile(r"^#{1,6}[ \t]+sources\b", re.IGNORECASE | re.MULTILINE)
GenerationOperation = Literal["baseline", "ingest", "propose"]


class EnvelopeError(RuntimeError):
    """Raised when AI stdout is not exactly the required JSON envelope."""


class CandidateValidationError(RuntimeError):
    """Raised when the prospective ADR repository is not strictly valid."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__(f"generated ADR validation failed with {len(issues)} error(s)")


class BatchWriteError(RuntimeError):
    """Raised when validated candidates cannot be persisted as one batch."""


@dataclass(frozen=True, slots=True)
class EnvelopeRecord:
    filename: str
    content: str


@dataclass(frozen=True, slots=True)
class EnvelopeProvenance:
    generator: str | None
    sources: tuple[str, ...]
    note: str | None


@dataclass(frozen=True, slots=True)
class Envelope:
    records: tuple[EnvelopeRecord, ...]
    provenance: EnvelopeProvenance | None


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    envelope: EnvelopeRecord
    path: Path
    document: ParsedADR

    @property
    def repository_record(self) -> ADRRecord:
        return ADRRecord(path=self.path, document=self.document)


@dataclass(frozen=True, slots=True)
class ValidatedBatch:
    candidates: tuple[CandidateRecord, ...]
    prospective_analysis: RepositoryAnalysis


def parse_json_object(
    stdout: str,
    *,
    allow_markdown_fence: bool = False,
) -> dict[str, Any]:
    """Parse one JSON object with duplicate-key and optional fence handling."""

    candidate = stdout.strip()
    if allow_markdown_fence and candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise EnvelopeError("AI stdout contains an incomplete Markdown fence")
        opening = lines[0].strip().casefold()
        if opening not in {"```", "```json"}:
            raise EnvelopeError("AI JSON Markdown fence must be ``` or ```json")
        candidate = "\n".join(lines[1:-1]).strip()

    try:
        raw = json.loads(candidate, object_pairs_hook=_strict_json_object)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(
            f"AI stdout must be exactly one JSON object: {exc.msg} at "
            f"line {exc.lineno}, column {exc.colno}"
        ) from exc
    except _DuplicateJSONKeyError as exc:
        raise EnvelopeError(f"AI JSON contains duplicate key {exc.key!r}") from exc

    if not isinstance(raw, dict):
        raise EnvelopeError("AI JSON root must be an object")
    return raw


def parse_envelope(stdout: str) -> tuple[EnvelopeRecord, ...]:
    """Demand one exact generation envelope; conversational wrappers are invalid."""

    return parse_envelope_document(stdout).records


def parse_envelope_document(stdout: str) -> Envelope:
    """Parse the records envelope plus its optional provenance block."""

    raw = parse_json_object(stdout)

    if set(raw) - {"records", "provenance"} or "records" not in raw:
        raise EnvelopeError(
            "AI JSON root must contain exactly one key: 'records' "
            "(an optional 'provenance' object is also accepted)"
        )
    provenance = _parse_envelope_provenance(raw.get("provenance"))
    records_value = raw["records"]
    if not isinstance(records_value, list) or not records_value:
        raise EnvelopeError("AI JSON field 'records' must be a non-empty array")
    if len(records_value) > MAX_GENERATED_RECORDS:
        raise EnvelopeError(
            f"AI JSON contains more than {MAX_GENERATED_RECORDS} generated records"
        )

    records: list[EnvelopeRecord] = []
    seen_filenames: set[str] = set()
    for index, value in enumerate(records_value, start=1):
        if not isinstance(value, dict) or set(value) != {"filename", "content"}:
            raise EnvelopeError(
                f"record {index} must contain exactly 'filename' and 'content'"
            )
        filename = value["filename"]
        content = value["content"]
        if not isinstance(filename, str) or not GENERATED_FILENAME_PATTERN.fullmatch(
            filename
        ):
            raise EnvelopeError(
                f"record {index} filename must match 0001-lowercase-slug.md"
            )
        if filename in seen_filenames:
            raise EnvelopeError(f"AI JSON contains duplicate filename {filename!r}")
        if not isinstance(content, str) or not content:
            raise EnvelopeError(f"record {index} content must be a non-empty string")
        if "\x00" in content:
            raise EnvelopeError(f"record {index} content contains a null byte")
        seen_filenames.add(filename)
        records.append(EnvelopeRecord(filename, content))
    return Envelope(tuple(records), provenance)


def _parse_envelope_provenance(value: object) -> EnvelopeProvenance | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EnvelopeError("envelope 'provenance' must be an object")
    unknown = set(value) - ENVELOPE_PROVENANCE_FIELDS
    if unknown:
        raise EnvelopeError(
            "envelope 'provenance' contains unknown fields: " + ", ".join(sorted(unknown))
        )
    generator = value.get("generator")
    if generator is not None and (not isinstance(generator, str) or not generator.strip()):
        raise EnvelopeError("envelope provenance 'generator' must be a non-empty string")
    sources_value = value.get("sources", [])
    if not isinstance(sources_value, list) or not all(
        isinstance(item, str) and item.strip() for item in sources_value
    ):
        raise EnvelopeError("envelope provenance 'sources' must be a list of non-empty strings")
    note = value.get("note")
    if note is not None and not isinstance(note, str):
        raise EnvelopeError("envelope provenance 'note' must be a string")
    return EnvelopeProvenance(
        generator=generator.strip() if isinstance(generator, str) else None,
        sources=tuple(item.strip() for item in sources_value),
        note=note,
    )


def validate_candidates(
    records: tuple[EnvelopeRecord, ...],
    *,
    adr_dir: Path,
    expected_start: int,
    operation: GenerationOperation,
) -> ValidatedBatch:
    """Validate candidates and the complete prospective graph without writing."""

    try:
        existing_records, load_issues = load_repository_records(adr_dir)
    except ADRRepositoryError as exc:
        raise CandidateValidationError(
            (ValidationIssue("repository", str(exc)),)
        ) from exc

    initial_issues = list(load_issues)
    candidates: list[CandidateRecord] = []
    for record in records:
        path = adr_dir / record.filename
        if path.exists():
            initial_issues.append(
                ValidationIssue("file-collision", "generated filename already exists", path)
            )
        try:
            document = parse_adr_text(record.content, source=str(path))
        except ADRParseError as exc:
            initial_issues.append(
                ValidationIssue("malformed-frontmatter", exc.reason, path)
            )
            continue
        if document.metadata.schema_version != 2:
            initial_issues.append(
                ValidationIssue(
                    "generated-schema-version",
                    "AI-generated ADRs must declare schema_version: 2",
                    path,
                )
            )
        if operation != "baseline" and document.metadata.status is not ADRStatus.PROPOSED:
            initial_issues.append(
                ValidationIssue(
                    "generated-status",
                    "generated ADRs must use status: proposed; a human accepts them "
                    "with 'decinav accept'",
                    path,
                )
            )
        if _SOURCES_HEADING_PATTERN.search(document.body) is None:
            initial_issues.append(
                ValidationIssue(
                    "missing-sources-section",
                    "generated ADRs must contain a '## Sources' section naming their evidence",
                    path,
                )
            )
        candidates.append(CandidateRecord(record, path, document))

    if len(candidates) == len(records):
        actual_numbers = sorted(
            int(candidate.document.metadata.id.removeprefix("ADR-"))
            for candidate in candidates
        )
        expected_numbers = list(range(expected_start, expected_start + len(candidates)))
        if actual_numbers != expected_numbers or expected_numbers[-1] > 9999:
            expected_ids = _format_expected_range(expected_numbers)
            actual_ids = ", ".join(f"ADR-{number:04d}" for number in actual_numbers)
            initial_issues.append(
                ValidationIssue(
                    "candidate-id-sequence",
                    f"expected contiguous IDs {expected_ids}; received {actual_ids}",
                )
            )

    if operation == "baseline":
        _validate_baseline_contract(records, candidates, existing_records, initial_issues)

    combined_records = [*existing_records]
    combined_records.extend(candidate.repository_record for candidate in candidates)
    analysis = analyze_records(
        adr_dir,
        combined_records,
        initial_issues=initial_issues,
    )
    if analysis.issues:
        raise CandidateValidationError(analysis.issues)

    sorted_candidates = tuple(
        sorted(
            candidates,
            key=lambda candidate: int(
                candidate.document.metadata.id.removeprefix("ADR-")
            ),
        )
    )
    return ValidatedBatch(sorted_candidates, analysis)


def write_candidate_batch(batch: ValidatedBatch) -> tuple[Path, ...]:
    """Stage complete files, publish without overwrite, and roll back on failure."""

    if not batch.candidates:
        raise BatchWriteError("validated candidate batch is empty")
    adr_dir = batch.prospective_analysis.adr_dir
    linked_targets: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix=".decinav-stage-", dir=adr_dir) as staging:
            staging_dir = Path(staging)
            staged_paths: list[tuple[Path, Path]] = []
            for candidate in batch.candidates:
                staged_path = staging_dir / candidate.envelope.filename
                with staged_path.open("x", encoding="utf-8", newline="\n") as staged_file:
                    staged_file.write(candidate.envelope.content)
                    staged_file.flush()
                    os.fsync(staged_file.fileno())
                staged_path.chmod(0o644)
                staged_paths.append((staged_path, candidate.path))

            for staged_path, target_path in staged_paths:
                os.link(staged_path, target_path)
                linked_targets.append(target_path)
    except OSError as exc:
        rollback_failures: list[str] = []
        for target in reversed(linked_targets):
            try:
                target.unlink()
            except OSError as rollback_exc:
                rollback_failures.append(f"{target}: {rollback_exc}")
        detail = f"could not write generated ADR batch: {exc}"
        if rollback_failures:
            detail += "; rollback failures: " + "; ".join(rollback_failures)
        raise BatchWriteError(detail) from exc
    return tuple(candidate.path for candidate in batch.candidates)


class _DuplicateJSONKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result


def _validate_baseline_contract(
    envelope_records: tuple[EnvelopeRecord, ...],
    candidates: list[CandidateRecord],
    existing_records: tuple[ADRRecord, ...],
    issues: list[ValidationIssue],
) -> None:
    if existing_records:
        issues.append(
            ValidationIssue("baseline-not-empty", "baseline requires an empty ADR repository")
        )
    if len(envelope_records) != 1:
        issues.append(
            ValidationIssue("baseline-record-count", "baseline must return exactly one record")
        )
        return
    if envelope_records[0].filename != "0001-baseline.md":
        issues.append(
            ValidationIssue(
                "baseline-filename", "baseline filename must be 0001-baseline.md"
            )
        )
    if len(candidates) != 1:
        return
    metadata = candidates[0].document.metadata
    if metadata.id != "ADR-0001":
        issues.append(ValidationIssue("baseline-id", "baseline ID must be ADR-0001"))
    if metadata.status is not ADRStatus.BASELINE:
        issues.append(
            ValidationIssue("baseline-status", "baseline status must be baseline")
        )
    if metadata.domain is not ADRDomain.SYSTEM:
        issues.append(ValidationIssue("baseline-domain", "baseline domain must be system"))
    if metadata.depends_on or metadata.supersedes:
        issues.append(
            ValidationIssue(
                "baseline-relationships",
                "baseline depends_on and supersedes relationships must be empty",
            )
        )


def _format_expected_range(numbers: list[int]) -> str:
    if not numbers:
        return "(none)"
    first = f"ADR-{numbers[0]:04d}"
    if len(numbers) == 1:
        return first
    return f"{first} through ADR-{numbers[-1]:04d}"
