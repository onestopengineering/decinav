"""Repository-wide ADR loading and integrity analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re

from decinav.frontmatter import ADRMetadata, ADRParseError, ParsedADR, parse_adr


ADR_FILENAME_PATTERN = re.compile(r"^(\d{4})-.+\.md$", re.IGNORECASE)


class ADRRepositoryError(RuntimeError):
    """Raised when an ADR repository cannot be inspected."""


@dataclass(frozen=True, slots=True)
class ADRRecord:
    path: Path
    document: ParsedADR

    @property
    def metadata(self) -> ADRMetadata:
        return self.document.metadata


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: Path | None = None

    def format(self, *, relative_to: Path | None = None) -> str:
        location = ""
        if self.path is not None:
            display_path = self.path
            if relative_to is not None:
                try:
                    display_path = self.path.relative_to(relative_to)
                except ValueError:
                    pass
            location = f"{display_path}: "
        return f"[{self.code}] {location}{self.message}"


@dataclass(frozen=True, slots=True)
class RepositoryAnalysis:
    adr_dir: Path
    records: tuple[ADRRecord, ...]
    unique_records: tuple[ADRRecord, ...]
    duplicate_ids: frozenset[str]
    unresolved_ids: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]


def analyze_repository(adr_dir: Path) -> RepositoryAnalysis:
    """Load every ADR and report all repository-level integrity failures."""

    records, load_issues = load_repository_records(adr_dir)
    return analyze_records(adr_dir, records, initial_issues=load_issues)


def load_repository_records(
    adr_dir: Path,
) -> tuple[tuple[ADRRecord, ...], tuple[ValidationIssue, ...]]:
    """Load parseable records while retaining malformed-file issues separately."""

    if not adr_dir.is_dir():
        raise ADRRepositoryError(f"{adr_dir} does not exist; run 'decinav init' first")

    records: list[ADRRecord] = []
    issues: list[ValidationIssue] = []
    for path in list_adr_paths(adr_dir):
        try:
            document = parse_adr(path)
        except ADRParseError as exc:
            issues.append(ValidationIssue("malformed-frontmatter", exc.reason, path))
            continue
        records.append(ADRRecord(path=path, document=document))
    return tuple(records), tuple(issues)


def analyze_records(
    adr_dir: Path,
    records: tuple[ADRRecord, ...] | list[ADRRecord],
    *,
    initial_issues: tuple[ValidationIssue, ...] | list[ValidationIssue] = (),
) -> RepositoryAnalysis:
    """Analyze already-parsed records, including candidates not yet on disk."""

    sorted_records = sorted(records, key=_record_sort_key)
    issues = list(initial_issues)
    for record in sorted_records:
        mismatch = _filename_mismatch(record)
        if mismatch is not None:
            issues.append(mismatch)

    records_by_id: dict[str, list[ADRRecord]] = defaultdict(list)
    for record in sorted_records:
        records_by_id[record.metadata.id].append(record)

    duplicate_ids = frozenset(
        adr_id for adr_id, matching in records_by_id.items() if len(matching) > 1
    )
    for adr_id in sorted(duplicate_ids, key=_adr_number):
        paths = ", ".join(str(record.path.name) for record in records_by_id[adr_id])
        issues.append(
            ValidationIssue(
                "duplicate-id",
                f"{adr_id} is declared by multiple files: {paths}",
            )
        )

    unique_records = tuple(
        record for record in sorted_records if record.metadata.id not in duplicate_ids
    )
    unique_by_id = {record.metadata.id: record for record in unique_records}

    unresolved_ids: set[str] = set()
    for record in unique_records:
        relationships = (
            ("depends_on", record.metadata.depends_on),
            ("supersedes", record.metadata.supersedes),
        )
        for field, related_relationships in relationships:
            for relationship in related_relationships:
                related_id = relationship.id
                if related_id not in unique_by_id:
                    unresolved_ids.add(related_id)
                if related_id not in records_by_id:
                    issues.append(
                        ValidationIssue(
                            "dangling-reference",
                            f"{record.metadata.id}.{field} references missing {related_id}",
                            record.path,
                        )
                    )

    adjacency: dict[str, set[str]] = {adr_id: set() for adr_id in unique_by_id}
    for record in unique_records:
        current_id = record.metadata.id
        for dependency in record.metadata.depends_on:
            dependency_id = dependency.id
            if dependency_id in unique_by_id:
                adjacency[dependency_id].add(current_id)
        for superseded in record.metadata.supersedes:
            superseded_id = superseded.id
            if superseded_id in unique_by_id:
                adjacency[superseded_id].add(current_id)

    for component in _find_cyclic_components(adjacency):
        issues.append(
            ValidationIssue(
                "circular-relationship",
                "causal relationship cycle among: " + ", ".join(component),
            )
        )

    return RepositoryAnalysis(
        adr_dir=adr_dir,
        records=tuple(sorted_records),
        unique_records=unique_records,
        duplicate_ids=duplicate_ids,
        unresolved_ids=tuple(sorted(unresolved_ids, key=_adr_number)),
        issues=tuple(issues),
    )


def list_adr_paths(adr_dir: Path) -> list[Path]:
    try:
        paths = [
            path
            for path in adr_dir.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".md"
            and path.name.casefold() != "readme.md"
        ]
    except OSError as exc:
        raise ADRRepositoryError(f"could not read {adr_dir}: {exc}") from exc
    return sorted(paths, key=lambda path: path.name.casefold())


def _filename_mismatch(record: ADRRecord) -> ValidationIssue | None:
    match = ADR_FILENAME_PATTERN.fullmatch(record.path.name)
    expected_number = record.metadata.id.removeprefix("ADR-")
    if match is not None and match.group(1) == expected_number:
        return None
    return ValidationIssue(
        "filename-id-mismatch",
        (
            f"filename must start with '{expected_number}-' to match "
            f"frontmatter ID {record.metadata.id}"
        ),
        record.path,
    )


def _record_sort_key(record: ADRRecord) -> tuple[int, str]:
    return (_adr_number(record.metadata.id), record.path.name.casefold())


def _adr_number(adr_id: str) -> int:
    return int(adr_id.removeprefix("ADR-"))


def _find_cyclic_components(adjacency: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """Return cyclic strongly connected components without recursion limits."""

    visited: set[str] = set()
    finish_order: list[str] = []
    ordered_neighbors = {
        node: tuple(sorted(neighbors, key=_adr_number))
        for node, neighbors in adjacency.items()
    }
    for start in sorted(adjacency, key=_adr_number):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, neighbor_index = stack[-1]
            neighbors = ordered_neighbors[node]
            if neighbor_index >= len(neighbors):
                stack.pop()
                finish_order.append(node)
                continue
            neighbor = neighbors[neighbor_index]
            stack[-1] = (node, neighbor_index + 1)
            if neighbor not in visited:
                visited.add(neighbor)
                stack.append((neighbor, 0))

    reverse_adjacency: dict[str, set[str]] = {
        node: set() for node in adjacency
    }
    for source, targets in adjacency.items():
        for target in targets:
            reverse_adjacency[target].add(source)

    assigned: set[str] = set()
    cyclic: list[tuple[str, ...]] = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: list[str] = []
        component_stack = [start]
        assigned.add(start)
        while component_stack:
            node = component_stack.pop()
            component.append(node)
            for neighbor in sorted(
                reverse_adjacency[node], key=_adr_number, reverse=True
            ):
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    component_stack.append(neighbor)
        is_self_cycle = len(component) == 1 and component[0] in adjacency[component[0]]
        if len(component) > 1 or is_self_cycle:
            cyclic.append(tuple(sorted(component, key=_adr_number)))

    return tuple(sorted(cyclic, key=lambda component: _adr_number(component[0])))
