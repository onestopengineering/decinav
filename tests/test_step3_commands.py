import json
from pathlib import Path

from typer.testing import CliRunner

from decinav.cli import app
from decinav.frontmatter import ADRDomain, ADRStatus, parse_adr
from decinav.graphing import GRAPH_START_MARKER
from test_generation import envelope, generated_adr
from test_repository import write_adr


runner = CliRunner()


def setup_project(tmp_path: Path) -> Path:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    return adr_dir


def configure_mock(monkeypatch, fixture: Path, response: str) -> None:
    fixture.write_text(response, encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_RESPONSE", str(fixture))


def test_baseline_command_writes_valid_adr_and_regenerates_graph(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_project(tmp_path)
    (tmp_path / "package.json").write_text('{"name":"legacy"}\n', encoding="utf-8")
    response = envelope(
        (
            "0001-baseline.md",
            generated_adr(
                "ADR-0001",
                "Legacy system baseline",
                status="baseline",
                domain="system",
            ),
        )
    )
    configure_mock(monkeypatch, tmp_path / "response.json", response)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["baseline"])

    assert result.exit_code == 0, result.output
    parsed = parse_adr(adr_dir / "0001-baseline.md")
    assert parsed.metadata.status is ADRStatus.BASELINE
    assert parsed.metadata.domain is ADRDomain.SYSTEM
    readme = (adr_dir / "README.md").read_text(encoding="utf-8")
    assert "ADR_0001" in readme
    assert "AI transport: mock" in result.output


def test_baseline_refuses_existing_adr_before_calling_agent(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_project(tmp_path)
    write_adr(adr_dir / "0001-existing.md", adr_id="ADR-0001")
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["baseline"])

    assert result.exit_code == 1
    assert "baseline requires an empty ADR repository" in result.output
    assert "No model is configured" not in result.output


def test_ingest_command_uses_next_contiguous_ids_and_updates_graph(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_project(tmp_path)
    write_adr(adr_dir / "0001-baseline.md", adr_id="ADR-0001", domain="system")
    source = tmp_path / "informal"
    source.mkdir()
    (source / "decisions.md").write_text(
        "We chose a queue, then replaced it with streams.\n", encoding="utf-8"
    )
    response = envelope(
        (
            "0002-use-queue.md",
            generated_adr("ADR-0002", "Use queue", depends_on=("ADR-0001",)),
        ),
        (
            "0003-use-streams.md",
            generated_adr("ADR-0003", "Use streams", supersedes=("ADR-0002",)),
        ),
    )
    configure_mock(monkeypatch, tmp_path / "response.json", response)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ingest", str(source)])

    assert result.exit_code == 0, result.output
    assert (adr_dir / "0002-use-queue.md").is_file()
    assert (adr_dir / "0003-use-streams.md").is_file()
    graph = (adr_dir / "README.md").read_text(encoding="utf-8")
    assert (
        'ADR_0001 -->|"Use queue requires ADR-0001."| ADR_0002' in graph
    )
    assert (
        'ADR_0002 -.->|"Superseded by: Use streams replaces ADR-0002."| '
        "ADR_0003" in graph
    )


def test_invalid_generated_record_causes_zero_candidate_writes(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_project(tmp_path)
    source = tmp_path / "informal"
    source.mkdir()
    (source / "decisions.md").write_text("Two decisions.\n", encoding="utf-8")
    response = json.dumps(
        {
            "records": [
                {
                    "filename": "0001-valid.md",
                    "content": generated_adr("ADR-0001", "Valid"),
                },
                {
                    "filename": "0002-invalid.md",
                    "content": "# Missing frontmatter\n",
                },
            ]
        }
    )
    configure_mock(monkeypatch, tmp_path / "response.json", response)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ingest", str(source)])

    assert result.exit_code == 1
    assert "malformed-frontmatter" in result.output
    assert not (adr_dir / "0001-valid.md").exists()
    assert not (adr_dir / "0002-invalid.md").exists()


def test_post_write_graph_failure_retains_valid_generated_adr(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_project(tmp_path)
    (adr_dir / "README.md").write_text(
        f"{GRAPH_START_MARKER}\nunfinished\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    response = envelope(
        (
            "0001-baseline.md",
            generated_adr(
                "ADR-0001", "Baseline", status="baseline", domain="system"
            ),
        )
    )
    configure_mock(monkeypatch, tmp_path / "response.json", response)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["baseline"])

    assert result.exit_code == 1
    assert (adr_dir / "0001-baseline.md").is_file()
    assert "ADR files were retained" in result.output
    assert "Run 'decinav graph'" in result.output


def test_missing_ai_configuration_is_actionable(tmp_path: Path, monkeypatch) -> None:
    setup_project(tmp_path)
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["baseline"])

    assert result.exit_code == 1
    assert "No model is configured" in result.output
    assert "claude -p" in result.output


def test_ingest_blocks_invalid_existing_repository_before_agent_execution(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_project(tmp_path)
    (adr_dir / "0001-broken.md").write_text("# no frontmatter\n", encoding="utf-8")
    source = tmp_path / "informal"
    source.mkdir()
    (source / "decision.md").write_text("Use PostgreSQL.\n", encoding="utf-8")
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ingest", str(source)])

    assert result.exit_code == 1
    assert "existing ADR repository has 1 validation error" in result.output
    assert "[malformed-frontmatter]" in result.output
    assert "No model is configured" not in result.output
