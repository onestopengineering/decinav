"""End-to-end baseline and ingest workflows shared by CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex

from decinav.ai_adapter import AIResponse, execute_ai
from decinav.generation import (
    CandidateValidationError,
    Envelope,
    EnvelopeError,
    parse_envelope_document,
    validate_candidates,
    write_candidate_batch,
)
from decinav.provenance import (
    ProvenanceError,
    load_provenance,
    record_generations,
    relative_source_paths,
)
from decinav.graphing import GraphError, GraphUpdateResult, regenerate_graph
from decinav.prompts import build_baseline_prompt, build_ingest_prompt
from decinav.repository import (
    ADRRepositoryError,
    RepositoryAnalysis,
    analyze_repository,
    list_adr_paths,
)
from decinav.source_bundle import (
    SourceBundle,
    SourceBundleError,
    build_baseline_bundle,
    build_brief_bundle,
    build_ingest_bundle,
)


MAX_ENVELOPE_FILE_BYTES = 10 * 1024 * 1024
_SOURCES_SECTION_PATTERN = re.compile(
    r"^#{1,6}[ \t]+sources\b[^\n]*\n(?P<body>.*?)(?=^#{1,6}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


class WorkflowError(RuntimeError):
    """Raised for workflow policy failures that are not parser errors."""


class RepositoryPreflightError(RuntimeError):
    """Raised when generation starts from an invalid ADR repository."""

    def __init__(self, analysis: RepositoryAnalysis) -> None:
        self.analysis = analysis
        super().__init__(
            f"existing ADR repository has {len(analysis.issues)} validation error(s)"
        )


class PostWriteGraphError(RuntimeError):
    """Raised when valid ADR files were retained but the derived graph failed."""

    def __init__(self, written_paths: tuple[Path, ...], reason: str) -> None:
        self.written_paths = written_paths
        self.reason = reason
        super().__init__(reason)


class PostWriteProvenanceError(RuntimeError):
    """Raised when ADR files and the graph were written but provenance was not."""

    def __init__(self, written_paths: tuple[Path, ...], reason: str) -> None:
        self.written_paths = written_paths
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    bundle: SourceBundle
    ai_response: AIResponse
    written_paths: tuple[Path, ...]
    graph: GraphUpdateResult


@dataclass(frozen=True, slots=True)
class ProposeResult:
    envelope_path: Path
    written_paths: tuple[Path, ...]
    graph: GraphUpdateResult
    generator: str
    sources: tuple[str, ...]


def run_baseline(project_root: Path, *, brief_path: Path | None = None) -> GenerationResult:
    """Generate the one allowed Day 0 baseline ADR from code structure or a brief."""

    adr_dir = project_root / "docs" / "adr"
    _require_graph_target(adr_dir)
    existing_paths = list_adr_paths(adr_dir)
    if existing_paths:
        filenames = ", ".join(path.name for path in existing_paths)
        raise WorkflowError(
            "baseline requires an empty ADR repository; found: " + filenames
        )
    _require_clean_repository(adr_dir)
    _require_readable_provenance(project_root)

    if brief_path is None:
        bundle = build_baseline_bundle(project_root)
        prompt = build_baseline_prompt(bundle)
    else:
        candidate = brief_path if brief_path.is_absolute() else project_root / brief_path
        bundle = build_brief_bundle(candidate)
        prompt = build_baseline_prompt(bundle, intended=True)
    response = execute_ai(prompt, cwd=project_root)
    envelope = parse_envelope_document(response.stdout)
    batch = validate_candidates(
        envelope.records,
        adr_dir=adr_dir,
        expected_start=1,
        operation="baseline",
    )
    written_paths = write_candidate_batch(batch)
    provenance_failure = _record_provenance(
        project_root,
        batch,
        generated_by="baseline" if brief_path is None else "baseline-brief",
        generator=_configured_generator(),
        bundle_sources=_bundle_sources(project_root, bundle),
    )
    graph = _regenerate_after_write(adr_dir, written_paths)
    _raise_if_provenance_failed(written_paths, provenance_failure)
    return GenerationResult(bundle, response, written_paths, graph)


def run_ingest(project_root: Path, source_folder: Path) -> GenerationResult:
    """Extract atomic ADRs from supported informal decision documents."""

    adr_dir = project_root / "docs" / "adr"
    _require_graph_target(adr_dir)
    existing_analysis = _require_clean_repository(adr_dir)
    _require_readable_provenance(project_root)
    _reject_target_as_source(adr_dir, source_folder)
    next_id = _next_id(existing_analysis)

    bundle = build_ingest_bundle(source_folder)
    prompt = build_ingest_prompt(
        bundle,
        next_id=next_id,
        existing_records=existing_analysis.unique_records,
    )
    response = execute_ai(prompt, cwd=project_root)
    envelope = parse_envelope_document(response.stdout)
    batch = validate_candidates(
        envelope.records,
        adr_dir=adr_dir,
        expected_start=next_id,
        operation="ingest",
    )
    written_paths = write_candidate_batch(batch)
    provenance_failure = _record_provenance(
        project_root,
        batch,
        generated_by="ingest",
        generator=_configured_generator(),
        bundle_sources=_bundle_sources(project_root, bundle),
    )
    graph = _regenerate_after_write(adr_dir, written_paths)
    _raise_if_provenance_failed(written_paths, provenance_failure)
    return GenerationResult(bundle, response, written_paths, graph)


def run_propose(project_root: Path, envelope_path: Path) -> ProposeResult:
    """Publish agent-authored ADRs from an envelope file through the strict gate."""

    adr_dir = project_root / "docs" / "adr"
    _require_graph_target(adr_dir)
    existing_analysis = _require_clean_repository(adr_dir)
    _require_readable_provenance(project_root)
    next_id = _next_id(existing_analysis)

    envelope = _read_envelope_file(project_root, envelope_path)
    batch = validate_candidates(
        envelope.records,
        adr_dir=adr_dir,
        expected_start=next_id,
        operation="propose",
    )
    written_paths = write_candidate_batch(batch)
    generator = "agent"
    sources: tuple[str, ...] = ()
    if envelope.provenance is not None:
        generator = envelope.provenance.generator or generator
        sources = envelope.provenance.sources
    provenance_failure = _record_provenance(
        project_root,
        batch,
        generated_by="propose",
        generator=generator,
        bundle_sources=sources,
    )
    graph = _regenerate_after_write(adr_dir, written_paths)
    _raise_if_provenance_failed(written_paths, provenance_failure)
    resolved_envelope = (
        envelope_path if envelope_path.is_absolute() else project_root / envelope_path
    )
    return ProposeResult(resolved_envelope, written_paths, graph, generator, sources)


def _read_envelope_file(project_root: Path, envelope_path: Path) -> Envelope:
    candidate = envelope_path if envelope_path.is_absolute() else project_root / envelope_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"envelope file does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise WorkflowError(f"envelope path is not a file: {resolved}")
    try:
        if resolved.stat().st_size > MAX_ENVELOPE_FILE_BYTES:
            raise WorkflowError(
                f"envelope exceeds the {MAX_ENVELOPE_FILE_BYTES}-byte safety limit"
            )
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkflowError(f"could not read envelope as UTF-8: {resolved}: {exc}") from exc
    if not text.strip():
        raise EnvelopeError(f"envelope file is empty: {resolved}")
    return parse_envelope_document(text)


def _require_readable_provenance(project_root: Path) -> None:
    """Fail before any model call or write when the sidecar is unreadable."""

    try:
        load_provenance(project_root)
    except ProvenanceError as exc:
        raise WorkflowError(str(exc)) from exc


def _record_provenance(
    project_root: Path,
    batch,
    *,
    generated_by: str,
    generator: str,
    bundle_sources: tuple[str, ...],
) -> str | None:
    """Record provenance per ADR; return an error message instead of raising."""

    sources_by_id = {
        candidate.document.metadata.id: _sources_for_candidate(
            candidate.document.body, bundle_sources
        )
        for candidate in batch.candidates
    }
    try:
        record_generations(
            project_root,
            sources_by_id,
            generated_by=generated_by,
            generator=generator,
        )
    except ProvenanceError as exc:
        return str(exc)
    return None


def _raise_if_provenance_failed(
    written_paths: tuple[Path, ...], failure: str | None
) -> None:
    if failure is not None:
        ids = ", ".join(f"ADR-{path.name[:4]}" for path in written_paths)
        raise PostWriteProvenanceError(
            written_paths,
            f"provenance was not recorded: {failure}. The ADR files and graph are in "
            f"place; run 'decinav attest {ids}' after fixing .decinav/provenance.json.",
        )


def _sources_for_candidate(body: str, bundle_sources: tuple[str, ...]) -> tuple[str, ...]:
    """Narrow the bundle to the files the record's Sources section names."""

    match = _SOURCES_SECTION_PATTERN.search(body)
    if match is None or not bundle_sources:
        return bundle_sources
    section = match.group("body")
    named = tuple(
        source
        for source in bundle_sources
        if source in section or Path(source).name in section
    )
    return named or bundle_sources


def _bundle_sources(project_root: Path, bundle: SourceBundle) -> tuple[str, ...]:
    return relative_source_paths(
        project_root,
        tuple(bundle.root / source.relative_path for source in bundle.files),
    )


def _configured_generator() -> str:
    template = os.environ.get("DECINAV_AI_CMD", "").strip()
    if not template:
        return "unknown"
    if template == "mock":
        return "mock"
    try:
        arguments = shlex.split(template)
    except ValueError:
        return "unknown"
    return Path(arguments[0]).name if arguments else "unknown"


def _require_clean_repository(adr_dir: Path) -> RepositoryAnalysis:
    analysis = analyze_repository(adr_dir)
    if analysis.issues:
        raise RepositoryPreflightError(analysis)
    return analysis


def _require_graph_target(adr_dir: Path) -> None:
    if not adr_dir.is_dir():
        raise ADRRepositoryError(f"{adr_dir} does not exist; run 'decinav init' first")
    readme = adr_dir / "README.md"
    if not readme.is_file():
        raise GraphError(f"{readme} does not exist; run 'decinav init' first")


def _reject_target_as_source(adr_dir: Path, source_folder: Path) -> None:
    try:
        resolved_source = source_folder.resolve(strict=True)
        resolved_adr_dir = adr_dir.resolve(strict=True)
    except OSError as exc:
        raise SourceBundleError(f"could not resolve ingest source folder: {exc}") from exc
    if resolved_source == resolved_adr_dir or resolved_adr_dir in resolved_source.parents:
        raise SourceBundleError("docs/adr cannot be used as an ingest source folder")


def _next_id(analysis: RepositoryAnalysis) -> int:
    if not analysis.unique_records:
        return 1
    highest = max(
        int(record.metadata.id.removeprefix("ADR-"))
        for record in analysis.unique_records
    )
    if highest >= 9999:
        raise WorkflowError("ADR ID space is exhausted at ADR-9999")
    return highest + 1


def _regenerate_after_write(
    adr_dir: Path, written_paths: tuple[Path, ...]
) -> GraphUpdateResult:
    try:
        return regenerate_graph(adr_dir)
    except (ADRRepositoryError, GraphError) as exc:
        raise PostWriteGraphError(written_paths, str(exc)) from exc
