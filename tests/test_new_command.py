from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from decinav.adr import create_adr, find_next_adr_number, slugify
from decinav.cli import app
from decinav.frontmatter import ADRDomain, ADRRelationship, ADRStatus, parse_adr


runner = CliRunner()


def test_new_command_creates_next_adr_and_opens_configured_editor(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EDITOR", "true")

    first = create_adr(
        adr_dir,
        "Initial platform",
        ADRDomain.INFRASTRUCTURE,
        today=date(2026, 1, 2),
    )
    assert first.name == "0001-initial-platform.md"

    result = runner.invoke(app, ["new", "Adopt PostgreSQL", "--domain", "data"])

    assert result.exit_code == 0, result.output
    created = adr_dir / "0002-adopt-postgresql.md"
    assert created.is_file()
    assert "Created docs/adr/0002-adopt-postgresql.md" in result.output

    parsed = parse_adr(created)
    assert parsed.metadata.schema_version == 2
    assert parsed.metadata.id == "ADR-0002"
    assert parsed.metadata.title == "Adopt PostgreSQL"
    assert parsed.metadata.status is ADRStatus.PROPOSED
    assert parsed.metadata.domain is ADRDomain.DATA
    assert parsed.metadata.depends_on == ()
    assert parsed.metadata.supersedes == ()


def test_new_prompts_for_v2_dependency_and_supersession_reasons(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    create_adr(adr_dir, "Foundation", ADRDomain.SYSTEM)
    create_adr(adr_dir, "Old queue", ADRDomain.BACKEND)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EDITOR", "true")

    result = runner.invoke(
        app,
        [
            "new",
            "Reliable dispatch",
            "--domain",
            "backend",
            "--depends-on",
            "ADR-0001",
            "--supersedes",
            "ADR-0002",
        ],
        input=(
            "The foundation defines the service boundary.\n"
            "The old queue cannot provide durable redelivery.\n"
        ),
    )

    assert result.exit_code == 0, result.output
    parsed = parse_adr(adr_dir / "0003-reliable-dispatch.md")
    assert parsed.metadata.depends_on == (
        ADRRelationship(
            "ADR-0001", "The foundation defines the service boundary."
        ),
    )
    assert parsed.metadata.supersedes == (
        ADRRelationship(
            "ADR-0002", "The old queue cannot provide durable redelivery."
        ),
    )


def test_new_warns_but_succeeds_when_editor_is_not_configured(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EDITOR", raising=False)

    result = runner.invoke(app, ["new", "Queue policy", "--domain", "backend"])

    assert result.exit_code == 0
    assert "$EDITOR is not set" in result.output
    assert (tmp_path / "docs" / "adr" / "0001-queue-policy.md").is_file()


def test_new_command_accepts_system_domain(tmp_path: Path, monkeypatch) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EDITOR", "true")

    result = runner.invoke(app, ["new", "Legacy baseline", "--domain", "system"])

    assert result.exit_code == 0, result.output
    parsed = parse_adr(adr_dir / "0001-legacy-baseline.md")
    assert parsed.metadata.domain is ADRDomain.SYSTEM


def test_new_fails_cleanly_before_init(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EDITOR", "true")

    result = runner.invoke(app, ["new", "Queue policy", "--domain", "backend"])

    assert result.exit_code == 1
    assert "run 'decinav init' first" in result.output


def test_id_allocation_reserves_prefix_of_malformed_file(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0007-incomplete.md").write_text("# no frontmatter\n", encoding="utf-8")
    warnings: list[str] = []

    number = find_next_adr_number(adr_dir, warn=warnings.append)

    assert number == 8
    assert len(warnings) == 1
    assert "ignoring malformed ADR metadata" in warnings[0]


def test_slugify_is_portable_and_has_a_fallback() -> None:
    assert slugify("Use Café / Redis?") == "use-cafe-redis"
    assert slugify("数据库") == "decision"
