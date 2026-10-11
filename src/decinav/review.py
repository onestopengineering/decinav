"""Human review gate: accept, reject, and attest ADRs deterministically."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tempfile

from decinav.frontmatter import ADR_ID_PATTERN, ADRParseError, ADRStatus, parse_adr_text
from decinav.graphing import GraphUpdateResult, regenerate_graph
from decinav.provenance import (
    ProvenanceError,
    load_provenance,
    mark_accepted,
    mark_attested,
    remove_records,
)
from decinav.repository import ADRRecord, RepositoryAnalysis, analyze_repository
from decinav.workflows import RepositoryPreflightError


_STATUS_LINE_PATTERN = re.compile(
    r'^(?P<prefix>status:[ \t]*)(?P<quote>["\']?)proposed(?P=quote)[ \t]*(?P<eol>\r?)$',
    re.MULTILINE,
)


class ReviewError(RuntimeError):
    """Raised when a review action cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class ReviewResult:
    adr_ids: tuple[str, ...]
    paths: tuple[Path, ...]
    graph: GraphUpdateResult | None


def accept_adrs(project_root: Path, adr_ids: tuple[str, ...]) -> ReviewResult:
    """Promote proposed ADRs to accepted, record the reviewer commit, regenerate graph."""

    adr_dir = project_root / "docs" / "adr"
    analysis = _clean_analysis(adr_dir)
    _require_readable_provenance(project_root)
    ids = normalize_ids(adr_ids)
    records = _find_records(analysis, ids)
    for record in records:
        if record.metadata.status is not ADRStatus.PROPOSED:
            raise ReviewError(
                f"{record.metadata.id} has status {record.metadata.status.value}; "
                "only proposed ADRs can be accepted"
            )

    updated_texts: list[tuple[Path, str]] = []
    for record in records:
        try:
            with record.path.open(encoding="utf-8", newline="") as file:
                text = file.read()
        except (OSError, UnicodeError) as exc:
            raise ReviewError(f"could not read {record.path}: {exc}") from exc
        updated = _set_status_accepted(text, source=str(record.path))
        try:
            parsed = parse_adr_text(updated, source=str(record.path))
        except ADRParseError as exc:
            raise ReviewError(f"acceptance would corrupt {record.path}: {exc.reason}") from exc
        if parsed.metadata.status is not ADRStatus.ACCEPTED:
            raise ReviewError(f"status rewrite failed for {record.path}")
        updated_texts.append((record.path, updated))

    for path, updated in updated_texts:
        _atomic_write_text(path, updated)
    provenance_failure: str | None = None
    try:
        mark_accepted(project_root, ids)
    except ProvenanceError as exc:
        provenance_failure = str(exc)
    graph = regenerate_graph(adr_dir)
    if provenance_failure is not None:
        raise ReviewError(
            "ADRs were accepted and the graph updated, but provenance was not recorded: "
            f"{provenance_failure}. Fix .decinav/provenance.json and run "
            f"'decinav attest {' '.join(ids)}'."
        )
    return ReviewResult(ids, tuple(path for path, _ in updated_texts), graph)


def reject_adr(project_root: Path, adr_id: str) -> ReviewResult:
    """Delete one proposed ADR that nothing references, then regenerate the graph."""

    adr_dir = project_root / "docs" / "adr"
    analysis = _clean_analysis(adr_dir)
    _require_readable_provenance(project_root)
    (normalized,) = normalize_ids((adr_id,))
    (record,) = _find_records(analysis, (normalized,))
    if record.metadata.status is not ADRStatus.PROPOSED:
        raise ReviewError(
            f"{normalized} has status {record.metadata.status.value}; only proposed ADRs "
            "can be rejected. Use supersedes for accepted decisions."
        )
    dependents = sorted(
        other.metadata.id
        for other in analysis.unique_records
        if other.metadata.id != normalized
        and normalized in (*other.metadata.dependency_ids, *other.metadata.superseded_ids)
    )
    if dependents:
        raise ReviewError(
            f"{normalized} is referenced by {', '.join(dependents)}; "
            "remove those relationships first"
        )
    try:
        record.path.unlink()
    except OSError as exc:
        raise ReviewError(f"could not delete {record.path}: {exc}") from exc
    provenance_failure: str | None = None
    try:
        remove_records(project_root, (normalized,))
    except ProvenanceError as exc:
        provenance_failure = str(exc)
    graph = regenerate_graph(adr_dir)
    if provenance_failure is not None:
        raise ReviewError(
            f"{normalized} was deleted and the graph updated, but its provenance entry "
            f"remains: {provenance_failure}"
        )
    return ReviewResult((normalized,), (record.path,), graph)


def attest_adrs(project_root: Path, adr_ids: tuple[str, ...]) -> ReviewResult:
    """Record that a human reviewed the ADRs as current at the present commit."""

    adr_dir = project_root / "docs" / "adr"
    analysis = _clean_analysis(adr_dir)
    ids = normalize_ids(adr_ids)
    records = _find_records(analysis, ids)
    try:
        mark_attested(project_root, ids)
    except ProvenanceError as exc:
        raise ReviewError(str(exc)) from exc
    return ReviewResult(ids, tuple(record.path for record in records), None)


def _require_readable_provenance(project_root: Path) -> None:
    try:
        load_provenance(project_root)
    except ProvenanceError as exc:
        raise ReviewError(str(exc)) from exc


def normalize_ids(adr_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not adr_ids:
        raise ReviewError("at least one ADR ID is required")
    normalized: list[str] = []
    for raw in adr_ids:
        candidate = raw.strip().upper()
        match = ADR_ID_PATTERN.fullmatch(candidate)
        if match is None or int(match.group(1)) == 0:
            raise ReviewError(f"invalid ADR ID {raw!r}; expected ADR-0001 through ADR-9999")
        if candidate in normalized:
            raise ReviewError(f"duplicate ADR ID {candidate}")
        normalized.append(candidate)
    return tuple(normalized)


def _clean_analysis(adr_dir: Path) -> RepositoryAnalysis:
    analysis = analyze_repository(adr_dir)
    if analysis.issues:
        raise RepositoryPreflightError(analysis)
    return analysis


def _find_records(analysis: RepositoryAnalysis, ids: tuple[str, ...]) -> tuple[ADRRecord, ...]:
    by_id = {record.metadata.id: record for record in analysis.unique_records}
    missing = [adr_id for adr_id in ids if adr_id not in by_id]
    if missing:
        raise ReviewError("unknown ADR ID(s): " + ", ".join(missing))
    return tuple(by_id[adr_id] for adr_id in ids)


def _set_status_accepted(text: str, *, source: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ReviewError(f"{source}: document must start with a YAML '---' delimiter")
    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            closing_index = index
            break
    if closing_index is None:
        raise ReviewError(f"{source}: frontmatter is missing its closing '---' delimiter")
    frontmatter = "".join(lines[1:closing_index])
    matches = list(_STATUS_LINE_PATTERN.finditer(frontmatter))
    if len(matches) != 1:
        raise ReviewError(f"{source}: expected exactly one 'status: proposed' line")
    match = matches[0]
    replacement = f'{match.group("prefix")}"accepted"{match.group("eol")}'
    updated_frontmatter = (
        frontmatter[: match.start()] + replacement + frontmatter[match.end() :]
    )
    return lines[0] + updated_frontmatter + "".join(lines[closing_index:])


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise ReviewError(f"could not update {path}: {exc}") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
