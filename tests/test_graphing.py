from pathlib import Path

import pytest
from typer.testing import CliRunner

from decinav.cli import app
from decinav.frontmatter import ADRDomain
from decinav.graphing import (
    GRAPH_END_MARKER,
    GRAPH_START_MARKER,
    GraphError,
    generate_mermaid,
    inject_mermaid_graph,
)
from decinav.repository import analyze_repository
from test_repository import write_adr, write_v2_adr


runner = CliRunner()


def test_mermaid_uses_causal_edges_domain_colors_clicks_and_chronological_order(
    tmp_path: Path,
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(
        adr_dir / "0001-baseline.md",
        adr_id="ADR-0001",
        title="System baseline",
        domain="system",
    )
    write_adr(
        adr_dir / "0002-api.md",
        adr_id="ADR-0002",
        title="Build API",
        depends_on=("ADR-0001",),
    )
    write_adr(
        adr_dir / "0003-replace-api.md",
        adr_id="ADR-0003",
        title='Replace "API" `safely`',
        domain="frontend",
        supersedes=("ADR-0002",),
    )

    mermaid = generate_mermaid(analyze_repository(adr_dir))

    assert mermaid.startswith("flowchart TD\n")
    assert "subgraph" not in mermaid
    assert mermaid.index('ADR_0001["ADR-0001') < mermaid.index(
        'ADR_0002["ADR-0002'
    )
    assert mermaid.index('ADR_0002["ADR-0002') < mermaid.index(
        'ADR_0003["ADR-0003'
    )
    assert "ADR_0001 --> ADR_0002" in mermaid
    assert "ADR_0002 -.->|Superseded by| ADR_0003" in mermaid
    assert "&quot;API&quot; &#96;safely&#96;" in mermaid
    assert 'click ADR_0002 "0002-api.md" "Open ADR-0002: Build API"' in mermaid
    assert "style ADR_0001 fill:#f3e8ff" in mermaid
    assert "style ADR_0002 fill:#dbeafe" in mermaid
    assert "style ADR_0003 fill:#ffedd5" in mermaid


def test_mermaid_renders_dangling_reference_as_unresolved_node(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(
        adr_dir / "0001-service.md",
        adr_id="ADR-0001",
        depends_on=("ADR-9999",),
    )

    analysis = analyze_repository(adr_dir)
    mermaid = generate_mermaid(analysis)

    assert "subgraph" not in mermaid
    assert 'ADR_9999["ADR-9999: unresolved"]' in mermaid
    assert "ADR_9999 --> ADR_0001" in mermaid
    assert "style ADR_9999 fill:#fff3cd" in mermaid


def test_v2_relationship_reasons_are_rendered_on_edges(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_v2_adr(adr_dir / "0001-base.md", adr_id="ADR-0001")
    write_v2_adr(
        adr_dir / "0002-worker.md",
        adr_id="ADR-0002",
        depends_on=(
            (
                "ADR-0001",
                "The submission must exist before Redis | consumers process it.",
            ),
        ),
    )

    mermaid = generate_mermaid(analyze_repository(adr_dir))

    assert (
        'ADR_0001 -->|"The submission must exist before Redis &#124; consumers '
        'process it."| ADR_0002' in mermaid
    )


def test_focus_walks_ancestors_and_descendants_without_including_siblings(
    tmp_path: Path,
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(adr_dir / "0001-root.md", adr_id="ADR-0001")
    write_adr(
        adr_dir / "0002-parent.md",
        adr_id="ADR-0002",
        depends_on=("ADR-0001",),
    )
    write_adr(
        adr_dir / "0003-focus.md",
        adr_id="ADR-0003",
        depends_on=("ADR-0002",),
    )
    write_adr(
        adr_dir / "0004-child.md",
        adr_id="ADR-0004",
        depends_on=("ADR-0003",),
    )
    write_adr(
        adr_dir / "0005-sibling.md",
        adr_id="ADR-0005",
        depends_on=("ADR-0002",),
    )
    write_adr(
        adr_dir / "0006-grandchild.md",
        adr_id="ADR-0006",
        depends_on=("ADR-0004",),
    )

    mermaid = generate_mermaid(
        analyze_repository(adr_dir), focus="adr-0003", depth=2
    )

    for included in ("ADR_0001", "ADR_0002", "ADR_0003", "ADR_0004", "ADR_0006"):
        assert included in mermaid
    assert "ADR_0005" not in mermaid
    assert "style ADR_0003 stroke-width:4px" in mermaid


def test_domain_view_hides_other_domains_and_cross_domain_edges(
    tmp_path: Path,
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(
        adr_dir / "0001-system.md",
        adr_id="ADR-0001",
        domain="system",
    )
    write_adr(
        adr_dir / "0002-api.md",
        adr_id="ADR-0002",
        domain="backend",
        depends_on=("ADR-0001",),
    )
    write_adr(
        adr_dir / "0003-auth.md",
        adr_id="ADR-0003",
        domain="backend",
        depends_on=("ADR-0002",),
    )
    write_adr(
        adr_dir / "0004-data.md",
        adr_id="ADR-0004",
        domain="data",
        depends_on=("ADR-0002",),
    )

    mermaid = generate_mermaid(
        analyze_repository(adr_dir), domain=ADRDomain.BACKEND
    )

    assert "ADR_0001" not in mermaid
    assert "ADR_0002" in mermaid
    assert "ADR_0003" in mermaid
    assert "ADR_0004" not in mermaid
    assert "ADR_0002 --> ADR_0003" in mermaid
    assert "ADR_0001 --> ADR_0002" not in mermaid


def test_readme_injection_preserves_content_and_replaces_only_managed_block(
    tmp_path: Path,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Decisions\n\nHuman-owned introduction.\n", encoding="utf-8")

    assert inject_mermaid_graph(readme, "flowchart TD\n  A --> B\n") is True
    first = readme.read_text(encoding="utf-8")
    assert first.startswith("# Decisions\n\nHuman-owned introduction.\n")
    assert first.count(GRAPH_START_MARKER) == 1
    assert first.count(GRAPH_END_MARKER) == 1

    assert inject_mermaid_graph(readme, "flowchart TD\n  B --> C\n") is True
    second = readme.read_text(encoding="utf-8")
    assert "A --> B" not in second
    assert "B --> C" in second
    assert second.startswith("# Decisions\n\nHuman-owned introduction.\n")
    assert inject_mermaid_graph(readme, "flowchart TD\n  B --> C\n") is False


def test_readme_injection_fails_without_modifying_mismatched_markers(
    tmp_path: Path,
) -> None:
    readme = tmp_path / "README.md"
    original = f"# Decisions\n\n{GRAPH_START_MARKER}\nunfinished\n"
    readme.write_text(original, encoding="utf-8")

    with pytest.raises(GraphError, match="missing or duplicate graph markers"):
        inject_mermaid_graph(readme, "flowchart TD\n")

    assert readme.read_text(encoding="utf-8") == original


def test_graph_command_warns_but_still_updates_readme(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    write_adr(
        adr_dir / "0001-service.md",
        adr_id="ADR-0001",
        depends_on=("ADR-9999",),
    )
    (adr_dir / "0002-broken.md").write_text("# no YAML\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph"])

    assert result.exit_code == 0, result.output
    assert "Warning: [malformed-frontmatter]" in result.output
    assert "Warning: [dangling-reference]" in result.output
    assert "Updated: docs/adr/README.md" in result.output
    assert "ADR_9999 --> ADR_0001" in (adr_dir / "README.md").read_text(
        encoding="utf-8"
    )


def test_graph_command_applies_focus_depth_and_domain_options(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    write_adr(
        adr_dir / "0001-system.md",
        adr_id="ADR-0001",
        domain="system",
    )
    write_adr(
        adr_dir / "0002-backend.md",
        adr_id="ADR-0002",
        domain="backend",
        depends_on=("ADR-0001",),
    )
    write_adr(
        adr_dir / "0003-backend.md",
        adr_id="ADR-0003",
        domain="backend",
        depends_on=("ADR-0002",),
    )
    monkeypatch.chdir(tmp_path)

    focused = runner.invoke(
        app, ["graph", "--focus", "ADR-0002", "--depth", "0"]
    )

    assert focused.exit_code == 0, focused.output
    focused_readme = (adr_dir / "README.md").read_text(encoding="utf-8")
    assert "ADR_0002" in focused_readme
    assert "ADR_0001" not in focused_readme
    assert "ADR_0003" not in focused_readme

    domain = runner.invoke(app, ["graph", "--domain", "backend"])

    assert domain.exit_code == 0, domain.output
    domain_readme = (adr_dir / "README.md").read_text(encoding="utf-8")
    assert "ADR_0001" not in domain_readme
    assert "ADR_0002 --> ADR_0003" in domain_readme


def test_graph_command_rejects_combined_focus_and_domain(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    write_adr(adr_dir / "0001-system.md", adr_id="ADR-0001", domain="system")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["graph", "--focus", "ADR-0001", "--domain", "system"],
    )

    assert result.exit_code == 1
    assert "--focus and --domain cannot be used together" in result.output
