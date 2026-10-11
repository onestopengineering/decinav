"""Mermaid graph rendering and managed README updates."""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote

from decinav.frontmatter import ADRDomain, ADR_ID_PATTERN
from decinav.repository import (
    ADRRecord,
    RepositoryAnalysis,
    analyze_repository,
)


GRAPH_START_MARKER = "<!-- decinav:graph:start -->"
GRAPH_END_MARKER = "<!-- decinav:graph:end -->"
LEGACY_GRAPH_MARKERS = (("<!-- arch-cli:graph:start -->", "<!-- arch-cli:graph:end -->"),)
DOMAIN_STYLES = {
    ADRDomain.FRONTEND: "fill:#ffedd5,stroke:#c2410c,color:#111827",
    ADRDomain.BACKEND: "fill:#dbeafe,stroke:#1d4ed8,color:#111827",
    ADRDomain.INFRASTRUCTURE: "fill:#e5e7eb,stroke:#4b5563,color:#111827",
    ADRDomain.DATA: "fill:#dcfce7,stroke:#15803d,color:#111827",
    ADRDomain.SYSTEM: "fill:#f3e8ff,stroke:#7e22ce,color:#111827",
}
IMPACT_CLASS_DEFINITIONS = {
    "affected": "fill:#f9dbdb,stroke:#c0392b",
    "prerequisite": "fill:#d4e6f1,stroke:#2980b9",
    "proposed": "fill:#d5f5e3,stroke:#27ae60,stroke-dasharray: 5 5",
}


class GraphError(RuntimeError):
    """Raised when the graph cannot be rendered or injected safely."""


@dataclass(frozen=True, slots=True)
class GraphUpdateResult:
    analysis: RepositoryAnalysis
    changed: bool


def regenerate_graph(
    adr_dir: Path,
    *,
    focus: str | None = None,
    depth: int = 1,
    domain: ADRDomain | None = None,
) -> GraphUpdateResult:
    """Run the shared graph analysis, rendering, and README update path."""

    analysis = analyze_repository(adr_dir)
    mermaid = generate_mermaid(
        analysis,
        focus=focus,
        depth=depth,
        domain=domain,
    )
    changed = inject_mermaid_graph(adr_dir / "README.md", mermaid)
    return GraphUpdateResult(analysis, changed)


def generate_mermaid(
    analysis: RepositoryAnalysis,
    *,
    focus: str | None = None,
    depth: int = 1,
    domain: ADRDomain | None = None,
    _node_classes: dict[str, str] | None = None,
    _proposed_titles: tuple[str, ...] = (),
    _include_impact_classes: bool = False,
    clickable: bool = True,
) -> str:
    """Render a global, focused, or domain-specific Mermaid flowchart."""

    lines = ["flowchart TD"]
    selected_ids, normalized_focus = _select_ids(
        analysis,
        focus=focus,
        depth=depth,
        domain=domain,
    )
    selected_records = tuple(
        record
        for record in sorted(analysis.unique_records, key=_record_sort_key)
        if record.metadata.id in selected_ids
    )
    selected_unresolved = tuple(
        adr_id for adr_id in analysis.unresolved_ids if adr_id in selected_ids
    )

    for record in selected_records:
        label = _escape_label(f"{record.metadata.id}: {record.metadata.title}")
        lines.append(f'  {_node_id(record.metadata.id)}["{label}"]')
    for adr_id in selected_unresolved:
        lines.append(f'  {_node_id(adr_id)}["{adr_id}: unresolved"]')
    proposed_nodes = tuple(
        (f"PROPOSED_ADR_{index}", title)
        for index, title in enumerate(_proposed_titles, start=1)
    )
    for node_id, title in proposed_nodes:
        label = _escape_label(f"Proposed ADR: {title}")
        lines.append(f'  {node_id}["{label}"]')

    if not selected_records and not selected_unresolved and not proposed_nodes:
        empty_label = (
            f"No ADRs found in {domain.value} domain"
            if domain is not None
            else "No valid ADRs found"
        )
        lines.append(f'  no_adrs["{_escape_label(empty_label)}"]')

    edges: set[tuple[str, str, str, str | None]] = set()
    for record in analysis.unique_records:
        current_id = record.metadata.id
        for dependency in record.metadata.depends_on:
            edges.add((dependency.id, current_id, "depends_on", dependency.reason))
        for superseded in record.metadata.supersedes:
            edges.add((superseded.id, current_id, "supersedes", superseded.reason))

    selected_edges = tuple(
        edge
        for edge in sorted(
            edges,
            key=lambda edge: (
                _adr_number(edge[0]),
                _adr_number(edge[1]),
                edge[2],
            ),
        )
        if edge[0] in selected_ids and edge[1] in selected_ids
    )
    if selected_edges:
        lines.append("")
    for source, target, relationship, reason in selected_edges:
        if relationship == "depends_on":
            arrow = _render_edge("-->", reason)
            lines.append(f"  {_node_id(source)} {arrow} {_node_id(target)}")
        elif reason is None:
            lines.append(
                f"  {_node_id(source)} -.->|Superseded by| {_node_id(target)}"
            )
        else:
            label = f"Superseded by: {reason}"
            arrow = _render_edge("-.->", label)
            lines.append(f"  {_node_id(source)} {arrow} {_node_id(target)}")

    if selected_records and clickable:
        lines.append("")
        for record in selected_records:
            node_id = _node_id(record.metadata.id)
            url = quote(record.path.name)
            tooltip = f"Open {record.metadata.id}: {record.metadata.title}"
            lines.append(
                f"  click {node_id} {json.dumps(url)} {json.dumps(tooltip)}"
            )
    if selected_records:
        lines.append("")
        for record in selected_records:
            node_id = _node_id(record.metadata.id)
            if _node_classes and node_id in _node_classes:
                continue
            style = DOMAIN_STYLES[record.metadata.domain]
            lines.append(f"  style {node_id} {style}")
        if normalized_focus is not None:
            lines.append(f"  style {_node_id(normalized_focus)} stroke-width:4px")

    if selected_unresolved:
        lines.append("")
        for adr_id in selected_unresolved:
            lines.append(
                f"  style {_node_id(adr_id)} fill:#fff3cd,stroke:#b7791f,"
                "stroke-dasharray:5 5,color:#111827"
            )

    if _include_impact_classes:
        lines.append("")
        for class_name, definition in IMPACT_CLASS_DEFINITIONS.items():
            lines.append(f"  classDef {class_name} {definition}")
        class_assignments = dict(_node_classes or {})
        class_assignments.update(
            {node_id: "proposed" for node_id, _title in proposed_nodes}
        )
        for node_id, class_name in sorted(class_assignments.items()):
            lines.append(f"  class {node_id} {class_name}")

    return "\n".join(lines) + "\n"


def generate_impact_mermaid(
    analysis: RepositoryAnalysis,
    *,
    directly_affected_ids: tuple[str, ...],
    prerequisite_ids: tuple[str, ...],
    proposed_titles: tuple[str, ...],
) -> str:
    """Render the existing graph with impact classes and proposed ADR nodes."""

    node_classes = {
        _node_id(adr_id): "prerequisite" for adr_id in prerequisite_ids
    }
    # A direct change or conflict takes visual precedence when an ADR appears in
    # both AI result lists.
    node_classes.update(
        {_node_id(adr_id): "affected" for adr_id in directly_affected_ids}
    )
    return generate_mermaid(
        analysis,
        _node_classes=node_classes,
        _proposed_titles=proposed_titles,
        _include_impact_classes=True,
    )


def _select_ids(
    analysis: RepositoryAnalysis,
    *,
    focus: str | None,
    depth: int,
    domain: ADRDomain | None,
) -> tuple[frozenset[str], str | None]:
    if focus is not None and domain is not None:
        raise GraphError("--focus and --domain cannot be used together")
    if depth < 0:
        raise GraphError("--depth must be zero or greater")

    known_ids = {record.metadata.id for record in analysis.unique_records}
    if domain is not None:
        return (
            frozenset(
                record.metadata.id
                for record in analysis.unique_records
                if record.metadata.domain is domain
            ),
            None,
        )
    if focus is None:
        return frozenset(known_ids | set(analysis.unresolved_ids)), None

    normalized_focus = focus.strip().upper()
    match = ADR_ID_PATTERN.fullmatch(normalized_focus)
    if match is None or int(match.group(1)) == 0:
        raise GraphError("--focus must match ADR-0001 through ADR-9999")
    if normalized_focus not in known_ids:
        raise GraphError(f"focus ADR not found or not uniquely parseable: {normalized_focus}")

    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for record in analysis.unique_records:
        current_id = record.metadata.id
        relationships = (*record.metadata.depends_on, *record.metadata.supersedes)
        for relationship in relationships:
            parents.setdefault(current_id, set()).add(relationship.id)
            children.setdefault(relationship.id, set()).add(current_id)

    selected = {normalized_focus}
    selected.update(_walk_levels(normalized_focus, parents, depth))
    selected.update(_walk_levels(normalized_focus, children, depth))
    return frozenset(selected), normalized_focus


def _walk_levels(
    start: str, adjacency: dict[str, set[str]], depth: int
) -> frozenset[str]:
    visited: set[str] = set()
    frontier = {start}
    for _ in range(depth):
        next_frontier = {
            neighbor
            for node in frontier
            for neighbor in adjacency.get(node, set())
            if neighbor not in visited and neighbor != start
        }
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    return frozenset(visited)


def _render_edge(arrow: str, label: str | None) -> str:
    if label is None:
        return arrow
    compact_label = re.sub(r"\s+", " ", label).strip()
    escaped_label = _escape_label(compact_label).replace("|", "&#124;")
    return f'{arrow}|"{escaped_label}"|'


def inject_mermaid_graph(readme_path: Path, mermaid: str) -> bool:
    """Replace or append the managed graph block without disturbing other content."""

    if not readme_path.is_file():
        raise GraphError(f"{readme_path} does not exist; run 'decinav init' first")
    try:
        existing = readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GraphError(f"could not read {readme_path} as UTF-8: {exc}") from exc

    block = (
        f"{GRAPH_START_MARKER}\n"
        "```mermaid\n"
        f"{mermaid}"
        "```\n"
        f"{GRAPH_END_MARKER}"
    )
    original = existing
    if GRAPH_START_MARKER not in existing and GRAPH_END_MARKER not in existing:
        for legacy_start, legacy_end in LEGACY_GRAPH_MARKERS:
            if existing.count(legacy_start) == 1 and existing.count(legacy_end) == 1:
                existing = existing.replace(legacy_start, GRAPH_START_MARKER).replace(
                    legacy_end, GRAPH_END_MARKER
                )
                break
    start_count = existing.count(GRAPH_START_MARKER)
    end_count = existing.count(GRAPH_END_MARKER)

    if start_count == 0 and end_count == 0:
        if not existing:
            updated = block + "\n"
        else:
            if existing.endswith("\n\n"):
                separator = ""
            elif existing.endswith("\n"):
                separator = "\n"
            else:
                separator = "\n\n"
            updated = existing + separator + block + "\n"
    elif start_count == 1 and end_count == 1:
        start = existing.index(GRAPH_START_MARKER)
        end = existing.index(GRAPH_END_MARKER)
        if end < start:
            raise GraphError("README graph markers are in the wrong order; no changes made")
        end += len(GRAPH_END_MARKER)
        updated = existing[:start] + block + existing[end:]
    else:
        raise GraphError(
            "README contains missing or duplicate graph markers; no changes made"
        )

    if updated == original:
        return False
    _atomic_write_text(readme_path, updated)
    return True


def _escape_label(value: str) -> str:
    return html.escape(value, quote=True).replace("`", "&#96;")


def _node_id(adr_id: str) -> str:
    return adr_id.replace("-", "_")


def _adr_number(adr_id: str) -> int:
    return int(adr_id.removeprefix("ADR-"))


def _record_sort_key(record: ADRRecord) -> tuple[int, str]:
    return (_adr_number(record.metadata.id), record.path.name.casefold())


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        current_mode = stat.S_IMODE(path.stat().st_mode)
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
        temporary_path.chmod(current_mode)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise GraphError(f"could not update {path}: {exc}") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
