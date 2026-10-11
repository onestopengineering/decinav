import json
from pathlib import Path

from typer.testing import CliRunner

from decinav.cli import app
from decinav.repository import analyze_repository


runner = CliRunner()


def write_adr(
    path: Path,
    *,
    adr_id: str,
    title: str = "Decision",
    domain: str = "backend",
    depends_on: tuple[str, ...] = (),
    supersedes: tuple[str, ...] = (),
) -> None:
    content = (
        "---\n"
        f'id: "{adr_id}"\n'
        f"title: {json.dumps(title)}\n"
        'status: "accepted"\n'
        'date: "2026-08-27"\n'
        f'domain: "{domain}"\n'
        f"depends_on: {json.dumps(depends_on)}\n"
        f"supersedes: {json.dumps(supersedes)}\n"
        "---\n\n"
        f"# {title}\n"
    )
    path.write_text(content, encoding="utf-8")


def write_v2_adr(
    path: Path,
    *,
    adr_id: str,
    title: str = "Decision",
    domain: str = "backend",
    depends_on: tuple[tuple[str, str], ...] = (),
    supersedes: tuple[tuple[str, str], ...] = (),
) -> None:
    dependency_objects = [
        {"id": related_id, "reason": reason}
        for related_id, reason in depends_on
    ]
    supersession_objects = [
        {"id": related_id, "reason": reason}
        for related_id, reason in supersedes
    ]
    content = (
        "---\n"
        "schema_version: 2\n"
        f'id: "{adr_id}"\n'
        f"title: {json.dumps(title)}\n"
        'status: "accepted"\n'
        'date: "2026-08-27"\n'
        f'domain: "{domain}"\n'
        f"depends_on: {json.dumps(dependency_objects)}\n"
        f"supersedes: {json.dumps(supersession_objects)}\n"
        "---\n\n"
        f"# {title}\n"
    )
    path.write_text(content, encoding="utf-8")


def test_analysis_aggregates_all_repository_integrity_errors(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(
        adr_dir / "0001-first.md",
        adr_id="ADR-0001",
        depends_on=("ADR-0002",),
    )
    write_adr(
        adr_dir / "0002-second.md",
        adr_id="ADR-0002",
        depends_on=("ADR-0001",),
    )
    write_adr(adr_dir / "0003-wrong-prefix.md", adr_id="ADR-0004")
    write_adr(adr_dir / "0004-duplicate.md", adr_id="ADR-0004")
    write_adr(
        adr_dir / "0005-dangling.md",
        adr_id="ADR-0005",
        depends_on=("ADR-9999",),
    )
    (adr_dir / "0006-malformed.md").write_text("# Missing YAML\n", encoding="utf-8")

    analysis = analyze_repository(adr_dir)

    assert {issue.code for issue in analysis.issues} == {
        "malformed-frontmatter",
        "filename-id-mismatch",
        "duplicate-id",
        "dangling-reference",
        "circular-relationship",
    }
    assert analysis.duplicate_ids == frozenset({"ADR-0004"})
    assert analysis.unresolved_ids == ("ADR-9999",)
    assert [record.metadata.id for record in analysis.unique_records] == [
        "ADR-0001",
        "ADR-0002",
        "ADR-0005",
    ]


def test_validate_command_reports_all_errors_and_exits_one(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(
        adr_dir / "0001-dangling.md",
        adr_id="ADR-0001",
        depends_on=("ADR-9999",),
    )
    (adr_dir / "0002-broken.md").write_text("not frontmatter\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate"])

    assert result.exit_code == 1
    assert "Validation failed with 2 error(s)" in result.output
    assert "[malformed-frontmatter]" in result.output
    assert "[dangling-reference]" in result.output


def test_validate_command_succeeds_for_consistent_repository(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(adr_dir / "0001-baseline.md", adr_id="ADR-0001", domain="system")
    write_adr(
        adr_dir / "0002-api.md",
        adr_id="ADR-0002",
        depends_on=("ADR-0001",),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate"])

    assert result.exit_code == 0, result.output
    assert "Validated 2 ADR(s)." in result.output


def test_converging_causal_graph_is_not_misclassified_as_cycle(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(adr_dir / "0001-baseline.md", adr_id="ADR-0001", domain="system")
    write_adr(
        adr_dir / "0002-old.md",
        adr_id="ADR-0002",
        depends_on=("ADR-0001",),
    )
    write_adr(
        adr_dir / "0003-new.md",
        adr_id="ADR-0003",
        depends_on=("ADR-0001",),
        supersedes=("ADR-0002",),
    )

    analysis = analyze_repository(adr_dir)

    assert "circular-relationship" not in {issue.code for issue in analysis.issues}
