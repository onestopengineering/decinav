from pathlib import Path

import pytest
from typer.testing import CliRunner

from decinav.ai_adapter import AIResponse
from decinav.cli import app
from decinav.generation import EnvelopeError
from decinav.graphing import generate_impact_mermaid
from decinav.intelligence import (
    build_condensed_adr_context,
    parse_impact_response,
    run_impact,
    run_query,
)
from decinav.workflows import WorkflowError
from decinav.repository import analyze_repository
from test_repository import write_v2_adr


runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def setup_architecture_project(tmp_path: Path) -> Path:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("# Architecture decisions\n", encoding="utf-8")
    _write_adr_with_sections(
        adr_dir / "0001-baseline.md",
        adr_id="ADR-0001",
        title="System baseline",
        domain="system",
        context="The platform has an API and an evaluation worker.",
        decision="The API and worker remain separate runtime boundaries.",
    )
    _write_adr_with_sections(
        adr_dir / "0002-async-evaluation.md",
        adr_id="ADR-0002",
        title="Evaluate asynchronously",
        depends_on=(("ADR-0001", "The worker is defined by the baseline."),),
        context="Evaluation can take longer than an HTTP request.",
        decision="The API dispatches evaluation work asynchronously.",
    )
    return adr_dir


def _write_adr_with_sections(
    path: Path,
    *,
    adr_id: str,
    title: str,
    context: str,
    decision: str,
    domain: str = "backend",
    depends_on: tuple[tuple[str, str], ...] = (),
) -> None:
    write_v2_adr(
        path,
        adr_id=adr_id,
        title=title,
        domain=domain,
        depends_on=depends_on,
    )
    existing = path.read_text(encoding="utf-8")
    path.write_text(
        existing
        + f"\n## Context\n\n{context}\n\n"
        + f"## Decision\n\n{decision}\n\n"
        + "### Alternatives\n\nA lengthy rejected historical alternative.\n\n"
        + "## Consequences\n\nOperational history that is not required here.\n",
        encoding="utf-8",
    )


def test_condensed_context_contains_only_frontmatter_context_and_decision(
    tmp_path: Path,
) -> None:
    adr_dir = setup_architecture_project(tmp_path)
    repository = analyze_repository(adr_dir)

    context = build_condensed_adr_context(repository.unique_records)

    assert 'id: ADR-0001' in context
    assert "The platform has an API and an evaluation worker." in context
    assert "The API dispatches evaluation work asynchronously." in context
    assert "Alternatives" not in context
    assert "rejected historical alternative" not in context
    assert "Operational history" not in context


def test_condensed_context_treats_named_baseline_section_as_decision(
    tmp_path: Path,
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    path = adr_dir / "0001-baseline.md"
    path.write_text(
        "---\n"
        "schema_version: 2\n"
        'id: "ADR-0001"\n'
        'title: "System baseline"\n'
        'status: "baseline"\n'
        'date: "2026-08-27"\n'
        'domain: "system"\n'
        "depends_on: []\n"
        "supersedes: []\n"
        "---\n\n"
        "# Context\n\nThe observed system has two services.\n\n"
        "# Baseline architecture\n\nThe services communicate asynchronously.\n\n"
        "# Consequences\n\nHistorical detail.\n",
        encoding="utf-8",
    )

    context = build_condensed_adr_context(analyze_repository(adr_dir).unique_records)

    assert "The services communicate asynchronously." in context
    assert "Historical detail" not in context


def test_impact_workflow_loads_prd_and_minimized_adr_context(
    tmp_path: Path, monkeypatch
) -> None:
    setup_architecture_project(tmp_path)
    prd = tmp_path / "feature.md"
    prd.write_text("# Live results\n\nShow evaluation immediately.\n", encoding="utf-8")
    fixture = (FIXTURES / "mock_impact.json").read_text(encoding="utf-8")
    captured_prompt = ""

    def fake_execute(prompt: str, *, cwd: Path, purpose: str) -> AIResponse:
        nonlocal captured_prompt
        captured_prompt = prompt
        assert cwd == tmp_path
        assert purpose == "impact"
        return AIResponse(fixture, "", "mock")

    monkeypatch.setattr("decinav.intelligence.execute_ai", fake_execute)

    result = run_impact(tmp_path, prd)

    assert result.evaluation.directly_affected_adrs[0].id == "ADR-0002"
    assert "Show evaluation immediately." in captured_prompt
    assert "The API dispatches evaluation work asynchronously." in captured_prompt
    assert "rejected historical alternative" not in captured_prompt
    assert "Operational history" not in captured_prompt


def test_impact_parser_accepts_one_json_fence_and_rejects_unknown_adr() -> None:
    valid = """```json
{
  "directly_affected_adrs": [],
  "prerequisite_adrs": [],
  "architectural_risks": [],
  "proposed_new_adrs": []
}
```"""

    parsed = parse_impact_response(valid, known_adr_ids=frozenset({"ADR-0001"}))

    assert parsed.directly_affected_adrs == ()

    unknown = """{
      "directly_affected_adrs": [
        {"id": "ADR-9999", "reason_for_change": "Invented reference."}
      ],
      "prerequisite_adrs": [],
      "architectural_risks": [],
      "proposed_new_adrs": []
    }"""
    with pytest.raises(EnvelopeError, match="unknown ADR ADR-9999"):
        parse_impact_response(unknown, known_adr_ids=frozenset({"ADR-0001"}))


def test_impact_command_prints_markdown_and_visual_mermaid(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = setup_architecture_project(tmp_path)
    original_readme = (adr_dir / "README.md").read_text(encoding="utf-8")
    prd = tmp_path / "live-results.md"
    prd.write_text("# Live results\n\nReturn evaluation synchronously.\n", encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv(
        "DECINAV_AI_MOCK_IMPACT_RESPONSE",
        str(FIXTURES / "mock_impact.json"),
    )
    monkeypatch.delenv("DECINAV_AI_MOCK_RESPONSE", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["impact", str(prd), "--visual"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# PRD Impact Analysis\n")
    assert "## Directly Affected ADRs" in result.output
    assert "**ADR-0002**" in result.output
    assert "## Visual Impact Graph" in result.output
    assert "classDef affected fill:#f9dbdb,stroke:#c0392b" in result.output
    assert "classDef prerequisite fill:#d4e6f1,stroke:#2980b9" in result.output
    assert (
        "classDef proposed fill:#d5f5e3,stroke:#27ae60,"
        "stroke-dasharray: 5 5" in result.output
    )
    assert "class ADR_0002 affected" in result.output
    assert "class ADR_0001 prerequisite" in result.output
    assert 'PROPOSED_ADR_1["Proposed ADR: Define the feature evaluation' in result.output
    assert "class PROPOSED_ADR_1 proposed" in result.output
    assert (adr_dir / "README.md").read_text(encoding="utf-8") == original_readme


def test_impact_visual_keeps_all_class_defs_and_gives_affected_precedence(
    tmp_path: Path,
) -> None:
    adr_dir = setup_architecture_project(tmp_path)

    mermaid = generate_impact_mermaid(
        analyze_repository(adr_dir),
        directly_affected_ids=("ADR-0001",),
        prerequisite_ids=("ADR-0001",),
        proposed_titles=(),
    )

    assert "classDef affected fill:#f9dbdb,stroke:#c0392b" in mermaid
    assert "classDef prerequisite fill:#d4e6f1,stroke:#2980b9" in mermaid
    assert "classDef proposed fill:#d5f5e3,stroke:#27ae60" in mermaid
    assert "class ADR_0001 affected" in mermaid
    assert "class ADR_0001 prerequisite" not in mermaid


def test_impact_rejects_invalid_repository_before_calling_ai(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-broken.md").write_text("# no frontmatter\n", encoding="utf-8")
    prd = tmp_path / "feature.md"
    prd.write_text("# Feature\n", encoding="utf-8")
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["impact", str(prd)])

    assert result.exit_code == 1
    assert "existing ADR repository has 1 validation error" in result.output
    assert "No model is configured" not in result.output


def test_query_workflow_uses_principal_architect_prompt_and_minimized_context(
    tmp_path: Path, monkeypatch
) -> None:
    setup_architecture_project(tmp_path)
    fixture = (FIXTURES / "mock_query.txt").read_text(encoding="utf-8")
    captured_prompt = ""

    def fake_execute(prompt: str, *, cwd: Path, purpose: str) -> AIResponse:
        nonlocal captured_prompt
        captured_prompt = prompt
        assert cwd == tmp_path
        assert purpose == "query"
        return AIResponse(fixture, "", "mock")

    monkeypatch.setattr("decinav.intelligence.execute_ai", fake_execute)

    result = run_query(tmp_path, "Where does evaluation execute?")

    assert result.answer.startswith("Evaluation is separated")
    assert "Act as a Principal Architect" in captured_prompt
    assert "Every factual architectural assertion must cite" in captured_prompt
    assert "Where does evaluation execute?" in captured_prompt
    assert "The API dispatches evaluation work asynchronously." in captured_prompt
    assert "rejected historical alternative" not in captured_prompt


def test_query_command_prints_fixture_answer_with_inline_citations(
    tmp_path: Path, monkeypatch
) -> None:
    setup_architecture_project(tmp_path)
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv(
        "DECINAV_AI_MOCK_QUERY_RESPONSE",
        str(FIXTURES / "mock_query.txt"),
    )
    monkeypatch.delenv("DECINAV_AI_MOCK_RESPONSE", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["query", "Where does evaluation execute?"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Architecture Query\n")
    assert "**Question:** Where does evaluation execute?" in result.output
    assert "## Answer" in result.output
    assert "[ADR-0002]" in result.output
    assert "[ADR-0001]" in result.output


@pytest.mark.parametrize(
    ("answer", "expected_error"),
    [
        ("The API owns evaluation.", "contains no inline ADR citations"),
        (
            "The API owns evaluation [ADR-9999].",
            "cites unknown ADR IDs: ADR-9999",
        ),
        (
            "The API accepts work.\n- A worker evaluates it [ADR-0002].",
            "factual line.*without inline ADR citations: 1",
        ),
    ],
)
def test_query_rejects_untraceable_answers(
    tmp_path: Path, monkeypatch, answer: str, expected_error: str
) -> None:
    setup_architecture_project(tmp_path)

    def fake_execute(prompt: str, *, cwd: Path, purpose: str) -> AIResponse:
        return AIResponse(answer, "", "mock")

    monkeypatch.setattr("decinav.intelligence.execute_ai", fake_execute)

    with pytest.raises(WorkflowError, match=expected_error):
        run_query(tmp_path, "Who owns evaluation?")
