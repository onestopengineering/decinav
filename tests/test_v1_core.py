"""Behavior tests for the agent-first v1 core: provenance, propose, review, context."""

import json
import os
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from decinav.cli import app
from decinav.frontmatter import ADRStatus, parse_adr
from decinav.intelligence import build_context, check_answer
from decinav.prd import lint_prd
from decinav.provenance import (
    FreshnessCalculator,
    load_provenance,
    provenance_path,
    record_generation,
)
from decinav.repository import analyze_repository
from decinav.web_ui import ArchitectureUIService, UIError
from test_generation import generated_adr
from test_intelligence import setup_architecture_project


runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def git(repo: Path, *arguments: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "HOME": str(repo),
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def git_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("# project\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def proposal_envelope(*records: tuple[str, str], provenance: dict | None = None) -> str:
    payload: dict = {
        "records": [
            {"filename": filename, "content": content} for filename, content in records
        ]
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return json.dumps(payload)


# --- provenance and freshness -------------------------------------------------


def test_freshness_tracks_project_commits_and_source_changes(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "spec.md").write_text("spec v1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "add spec")

    record_generation(
        repo,
        ("ADR-0002",),
        generated_by="ingest",
        generator="mock",
        sources=("docs/spec.md",),
    )
    records = load_provenance(repo)
    assert records["ADR-0002"].commit == git(repo, "rev-parse", "HEAD")
    assert FreshnessCalculator(repo).compute(records["ADR-0002"]).state == "fresh"

    (repo / "src.py").write_text("print('unrelated')\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "unrelated change")
    aging = FreshnessCalculator(repo).compute(records["ADR-0002"])
    assert aging.state == "aging"
    assert aging.commits_since == 1

    (repo / "docs" / "spec.md").write_text("spec v2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change spec")
    changed = FreshnessCalculator(repo).compute(records["ADR-0002"])
    assert changed.state == "sources-changed"
    assert changed.changed_sources == ("docs/spec.md",)
    assert changed.commits_since == 2


def test_adr_commits_do_not_age_other_adrs_and_missing_provenance_is_unknown(
    tmp_path: Path,
) -> None:
    repo = git_repo(tmp_path)
    record_generation(repo, ("ADR-0001",), generated_by="new", generator="human", sources=())
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0001-x.md").write_text("adr text\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "only adr and provenance files")

    records = load_provenance(repo)
    assert FreshnessCalculator(repo).compute(records["ADR-0001"]).state == "fresh"
    assert FreshnessCalculator(repo).compute(None).state == "unknown"


def test_freshness_is_unknown_outside_git(tmp_path: Path) -> None:
    record_generation(
        tmp_path, ("ADR-0001",), generated_by="new", generator="human", sources=(), commit=None
    )
    assert FreshnessCalculator(tmp_path).compute(load_provenance(tmp_path)["ADR-0001"]).state == "unknown"


# --- propose ------------------------------------------------------------------


def test_propose_publishes_proposed_adrs_and_records_agent_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    envelope = repo / "proposal.json"
    envelope.write_text(
        proposal_envelope(
            (
                "0003-cache-results.md",
                generated_adr("ADR-0003", "Cache evaluation results", depends_on=("ADR-0002",)),
            ),
            provenance={"generator": "claude-code", "sources": ["docs/prds/cache.md"]},
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["propose", "--from-file", "proposal.json"])

    assert result.exit_code == 0, result.output
    created = repo / "docs" / "adr" / "0003-cache-results.md"
    assert parse_adr(created).metadata.status is ADRStatus.PROPOSED
    provenance = load_provenance(repo)["ADR-0003"]
    assert provenance.generated_by == "propose"
    assert provenance.generator == "claude-code"
    assert provenance.sources == ("docs/prds/cache.md",)
    assert provenance.commit == git(repo, "rev-parse", "HEAD")
    assert "ADR_0003" in (repo / "docs" / "adr" / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("content_status", "strip_sources", "expected_code"),
    [
        ("accepted", False, "generated-status"),
        ("proposed", True, "missing-sources-section"),
    ],
)
def test_propose_rejects_accepted_status_and_missing_sources(
    tmp_path: Path, monkeypatch, content_status: str, strip_sources: bool, expected_code: str
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    content = generated_adr("ADR-0003", "Cache results", status=content_status)
    if strip_sources:
        content = content.split("## Sources")[0]
    (repo / "proposal.json").write_text(
        proposal_envelope(("0003-cache-results.md", content)), encoding="utf-8"
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["propose", "--from-file", "proposal.json"])

    assert result.exit_code == 1
    assert expected_code in result.output
    assert not (repo / "docs" / "adr" / "0003-cache-results.md").exists()
    assert "ADR-0003" not in load_provenance(repo)


def test_propose_rejects_unknown_provenance_fields(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    (repo / "proposal.json").write_text(
        proposal_envelope(
            ("0003-x.md", generated_adr("ADR-0003", "X")),
            provenance={"generator": "agent", "confidence": 0.9},
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["propose", "--from-file", "proposal.json"])

    assert result.exit_code == 1
    assert "unknown fields: confidence" in result.output


# --- review gate --------------------------------------------------------------


def _propose_two(repo: Path) -> None:
    (repo / "proposal.json").write_text(
        proposal_envelope(
            ("0003-cache.md", generated_adr("ADR-0003", "Cache", depends_on=("ADR-0002",))),
            ("0004-ttl.md", generated_adr("ADR-0004", "TTL", depends_on=("ADR-0003",))),
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["propose", "--from-file", "proposal.json"])
    assert result.exit_code == 0, result.output


def test_accept_flips_status_records_commit_and_refuses_non_proposed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    monkeypatch.chdir(repo)
    _propose_two(repo)

    result = runner.invoke(app, ["accept", "ADR-0003"])

    assert result.exit_code == 0, result.output
    assert parse_adr(repo / "docs" / "adr" / "0003-cache.md").metadata.status is ADRStatus.ACCEPTED
    provenance = load_provenance(repo)["ADR-0003"]
    assert provenance.accepted_commit == git(repo, "rev-parse", "HEAD")
    assert provenance.accepted_at is not None
    assert analyze_repository(repo / "docs" / "adr").issues == ()

    again = runner.invoke(app, ["accept", "ADR-0003"])
    assert again.exit_code == 1
    assert "only proposed ADRs can be accepted" in again.output


def test_reject_deletes_unreferenced_proposed_adr_only(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    monkeypatch.chdir(repo)
    _propose_two(repo)

    blocked = runner.invoke(app, ["reject", "ADR-0003"])
    assert blocked.exit_code == 1
    assert "referenced by ADR-0004" in blocked.output
    assert (repo / "docs" / "adr" / "0003-cache.md").exists()

    allowed = runner.invoke(app, ["reject", "ADR-0004"])
    assert allowed.exit_code == 0, allowed.output
    assert not (repo / "docs" / "adr" / "0004-ttl.md").exists()
    assert "ADR-0004" not in load_provenance(repo)
    assert "ADR_0004" not in (repo / "docs" / "adr" / "README.md").read_text(encoding="utf-8")

    accepted = runner.invoke(app, ["reject", "ADR-0002"])
    assert accepted.exit_code == 1
    assert "only proposed ADRs can be rejected" in accepted.output


def test_attest_records_reviewer_commit_and_refreshes_freshness(
    tmp_path: Path, monkeypatch
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    monkeypatch.chdir(repo)
    assert build_context(repo).documents[0].freshness.state == "unknown"

    result = runner.invoke(app, ["attest", "ADR-0001", "ADR-0002"])

    assert result.exit_code == 0, result.output
    records = load_provenance(repo)
    assert records["ADR-0001"].attested_commit == git(repo, "rev-parse", "HEAD")
    assert {document.freshness.state for document in build_context(repo).documents} == {"fresh"}

    unknown = runner.invoke(app, ["attest", "ADR-0009"])
    assert unknown.exit_code == 1
    assert "unknown ADR ID(s): ADR-0009" in unknown.output


# --- context ------------------------------------------------------------------


def test_context_json_and_text_include_freshness_and_decision_sections(
    tmp_path: Path, monkeypatch
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    monkeypatch.chdir(repo)
    runner.invoke(app, ["attest", "ADR-0002"])

    as_json = runner.invoke(app, ["context", "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    by_id = {record["id"]: record for record in payload["records"]}
    assert by_id["ADR-0002"]["freshness"]["state"] == "fresh"
    assert by_id["ADR-0001"]["freshness"]["state"] == "unknown"
    assert by_id["ADR-0002"]["decision"] == "The API dispatches evaluation work asynchronously."
    assert by_id["ADR-0002"]["provenance"]["attested_commit"] == payload["commit"]
    assert payload["claim_states"] == ["DOCUMENTED", "VERIFIED_IN_CODE", "CONTRADICTED", "UNKNOWN"]

    as_text = runner.invoke(app, ["context"])
    assert as_text.exit_code == 0
    assert "FRESHNESS: fresh" in as_text.output
    assert "FRESHNESS: unknown (no provenance recorded)" in as_text.output
    assert "rejected historical alternative" not in as_text.output


def test_context_refuses_invalid_repository(tmp_path: Path, monkeypatch) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-broken.md").write_text("# no frontmatter\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["context", "--format", "json"])

    assert result.exit_code == 1
    assert "[malformed-frontmatter]" in result.output


# --- check-answer -------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "require_states", "ok", "fragment"),
    [
        ("Evaluation is asynchronous [ADR-0002].", False, True, ""),
        ("Evaluation is asynchronous.", False, False, "no inline ADR citations"),
        ("Evaluation is asynchronous [ADR-0002].", True, False, "without exactly one claim state"),
        (
            "- Evaluation is asynchronous [ADR-0002] [VERIFIED_IN_CODE]\n"
            "- The ADRs do not establish the queue topology [UNKNOWN]",
            True,
            True,
            "",
        ),
        (
            "- Evaluation is asynchronous [DOCUMENTED]",
            True,
            False,
            "without inline ADR citations: 1",
        ),
        ("Uses Kafka [ADR-0042] [DOCUMENTED]", True, False, "unknown ADR IDs: ADR-0042"),
    ],
)
def test_check_answer_enforces_citations_and_claim_states(
    tmp_path: Path, answer: str, require_states: bool, ok: bool, fragment: str
) -> None:
    adr_dir = setup_architecture_project(tmp_path)
    repository = analyze_repository(adr_dir)

    result = check_answer(answer, repository, require_states=require_states)

    assert result.ok is ok, result.problems
    if fragment:
        assert any(fragment in problem for problem in result.problems), result.problems


def test_check_answer_command_reads_stdin_and_exits_nonzero_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    setup_architecture_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    good = runner.invoke(
        app,
        ["check-answer", "--require-states", "--format", "json"],
        input="Evaluation is asynchronous [ADR-0002] [DOCUMENTED]\n",
    )
    assert good.exit_code == 0, good.output
    assert json.loads(good.output)["ok"] is True

    bad = runner.invoke(app, ["check-answer"], input="Evaluation is asynchronous.\n")
    assert bad.exit_code == 1
    assert "Answer rejected" in bad.output


# --- PRD ----------------------------------------------------------------------


def test_prd_template_fails_lint_until_filled_and_checks_references(
    tmp_path: Path, monkeypatch
) -> None:
    setup_architecture_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    created = runner.invoke(app, ["prd", "new", "Report export"])
    assert created.exit_code == 0, created.output
    prd_path = tmp_path / "docs" / "prds" / "report-export.md"
    assert prd_path.is_file()

    empty = runner.invoke(app, ["prd", "lint", str(prd_path)])
    assert empty.exit_code == 1
    assert "section '## Problem' is empty" in empty.output

    prd_path.write_text(
        "# Report export\n\n"
        "## Problem\n\nUsers cannot export reports.\n\n"
        "## Users\n\nCourse admins.\n\n"
        "## Requirements\n\n1. Export as CSV.\n\n"
        "## Affected decisions\n\n- ADR-0002: exports must not block the API.\n\n"
        "## Proposed decisions\n\n- Generate exports in a background job\n\n"
        "## Open questions\n\nNone\n\n"
        "## Sources\n\n- support tickets\n",
        encoding="utf-8",
    )
    filled = runner.invoke(app, ["prd", "lint", str(prd_path)])
    assert filled.exit_code == 0, filled.output
    assert "references ADR-0002" in filled.output

    prd_path.write_text(
        prd_path.read_text(encoding="utf-8").replace("ADR-0002", "ADR-0077"), encoding="utf-8"
    )
    unknown = runner.invoke(app, ["prd", "lint", str(prd_path)])
    assert unknown.exit_code == 1
    assert "references unknown ADR(s): ADR-0077" in unknown.output

    duplicate = runner.invoke(app, ["prd", "new", "Report export"])
    assert duplicate.exit_code == 1
    assert "refusing to overwrite" in duplicate.output


def test_prd_lint_result_lists_missing_sections(tmp_path: Path) -> None:
    prd = tmp_path / "loose.md"
    prd.write_text("# Loose\n\n## Problem\n\nSomething.\n", encoding="utf-8")

    result = lint_prd(tmp_path, prd)

    assert not result.ok
    assert "missing required section '## Users'" in result.issues
    assert "missing required section '## Sources'" in result.issues


# --- new ----------------------------------------------------------------------


def test_new_rejects_missing_dependency_before_writing(tmp_path: Path, monkeypatch) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EDITOR", "true")

    result = runner.invoke(
        app,
        ["new", "Dispatch", "--domain", "backend", "--depends-on", "ADR-0009"],
        input="Some reason.\n",
    )

    assert result.exit_code == 1
    assert "depends_on references unknown ADR(s): ADR-0009" in result.output
    assert list(adr_dir.glob("*.md")) == []


def test_new_records_human_provenance(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    (repo / "docs" / "adr").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("EDITOR", "true")

    result = runner.invoke(app, ["new", "Queue policy", "--domain", "backend"])

    assert result.exit_code == 0, result.output
    record = load_provenance(repo)["ADR-0001"]
    assert record.generated_by == "new"
    assert record.generator == "human"
    assert record.commit == git(repo, "rev-parse", "HEAD")


# --- baseline from brief ------------------------------------------------------


def test_baseline_from_brief_marks_intended_architecture(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    brief = repo / "docs" / "brief.md"
    brief.write_text("# New product\n\nA study planner with a Postgres backend.\n", encoding="utf-8")
    content = generated_adr(
        "ADR-0001", "Intended architecture baseline", status="baseline", domain="system"
    )
    response = json.dumps({"records": [{"filename": "0001-baseline.md", "content": content}]})
    (repo / "response.json").write_text(response, encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_RESPONSE", str(repo / "response.json"))
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["baseline", "--from-brief", "docs/brief.md"])

    assert result.exit_code == 0, result.output
    assert parse_adr(adr_dir / "0001-baseline.md").metadata.status is ADRStatus.BASELINE
    record = load_provenance(repo)["ADR-0001"]
    assert record.generated_by == "baseline-brief"
    assert record.sources == ("docs/brief.md",)
    assert record.generator == "mock"


def test_baseline_from_brief_requires_readable_document(tmp_path: Path, monkeypatch) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["baseline", "--from-brief", "missing.md"])

    assert result.exit_code == 1
    assert "product brief does not exist" in result.output


def test_ingest_records_provenance_with_relative_sources(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    source = repo / "notes"
    source.mkdir()
    (source / "decisions.md").write_text("We chose a queue.\n", encoding="utf-8")
    response = json.dumps(
        {
            "records": [
                {"filename": "0001-use-queue.md", "content": generated_adr("ADR-0001", "Use queue")}
            ]
        }
    )
    (repo / "response.json").write_text(response, encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_RESPONSE", str(repo / "response.json"))
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["ingest", "notes"])

    assert result.exit_code == 0, result.output
    record = load_provenance(repo)["ADR-0001"]
    assert record.generated_by == "ingest"
    assert record.sources == ("notes/decisions.md",)
    assert provenance_path(repo).is_file()


# --- impact save and UI endpoints ---------------------------------------------


def test_impact_save_writes_overlay_and_ui_serves_it(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    prd = repo / "feature.md"
    prd.write_text("# Live results\n\nReturn evaluation synchronously.\n", encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_IMPACT_RESPONSE", str(FIXTURES / "mock_impact.json"))
    monkeypatch.delenv("DECINAV_AI_MOCK_RESPONSE", raising=False)
    monkeypatch.chdir(repo)
    service = ArchitectureUIService(repo)
    with pytest.raises(UIError, match="no saved impact result"):
        service.last_impact(str(repo))

    result = runner.invoke(app, ["impact", "feature.md", "--visual", "--save"])

    assert result.exit_code == 0, result.output
    saved = json.loads((repo / ".decinav" / "last-impact.json").read_text(encoding="utf-8"))
    assert saved["prd"] == "feature.md"
    assert saved["directly_affected_adrs"][0]["id"] == "ADR-0002"
    assert saved["commit"] == git(repo, "rev-parse", "HEAD")

    status = service.status(str(repo))
    assert status["last_impact_available"] is True
    assert status["records"][0]["freshness"]["state"] == "unknown"
    overlay = service.last_impact(str(repo))
    classes = {node["id"]: node["class"] for node in overlay["graph"]["nodes"]}
    assert classes["ADR-0002"] == "affected"
    assert classes["ADR-0001"] == "prerequisite"
    assert classes["PROPOSED-1"] == "proposed"


def test_ui_adr_detail_returns_body_relationships_and_freshness(
    tmp_path: Path, monkeypatch
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    monkeypatch.chdir(repo)
    runner.invoke(app, ["attest", "ADR-0001"])
    service = ArchitectureUIService(repo)

    detail = service.adr_detail(str(repo), "adr-0001")

    assert detail["id"] == "ADR-0001"
    assert "The platform has an API and an evaluation worker." in detail["body"]
    assert detail["referenced_by"] == [
        {"id": "ADR-0002", "type": "depends_on", "reason": "The worker is defined by the baseline."}
    ]
    assert detail["freshness"]["state"] == "fresh"
    assert detail["path"] == "docs/adr/0001-baseline.md"

    with pytest.raises(UIError, match="unknown or ambiguous ADR ID"):
        service.adr_detail(str(repo), "ADR-0099")


def test_context_compact_and_ids_select_subsets(tmp_path: Path, monkeypatch) -> None:
    setup_architecture_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    compact = runner.invoke(app, ["context", "--compact"])
    assert compact.exit_code == 0, compact.output
    lines = [line for line in compact.output.splitlines() if line.startswith("ADR-")]
    assert len(lines) == 2
    assert "ADR-0002 | accepted | backend | unknown | Evaluate asynchronously" in lines[1]
    assert "depends_on ADR-0001" in lines[1]
    assert "The platform has an API" not in compact.output

    subset = runner.invoke(app, ["context", "--format", "json", "--ids", "adr-0002"])
    assert subset.exit_code == 0, subset.output
    payload = json.loads(subset.output)
    assert [record["id"] for record in payload["records"]] == ["ADR-0002"]
    assert payload["selected"] == 1 and payload["adr_count"] == 2

    text = runner.invoke(app, ["context", "--ids", "ADR-0001"])
    assert "adrs=1 of=2" in text.output
    assert "Evaluate asynchronously" not in text.output

    unknown = runner.invoke(app, ["context", "--ids", "ADR-0009"])
    assert unknown.exit_code == 1
    assert "unknown ADR ID(s): ADR-0009" in unknown.output


# --- review fixes: write ordering, CRLF, sources, ancestry --------------------


def test_propose_keeps_files_and_graph_when_provenance_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    (repo / ".decinav").write_text("not a directory\n", encoding="utf-8")
    (repo / "proposal.json").write_text(
        proposal_envelope(("0003-cache.md", generated_adr("ADR-0003", "Cache"))), encoding="utf-8"
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["propose", "--from-file", "proposal.json"])

    assert result.exit_code == 1
    assert "Created docs/adr/0003-cache.md" in result.output
    assert "decinav attest ADR-0003" in result.output
    assert "ADR_0003" in (repo / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    again = runner.invoke(app, ["propose", "--from-file", "proposal.json"])
    assert "file-collision" in again.output or "candidate-id-sequence" in again.output


def test_malformed_provenance_blocks_propose_before_any_write(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    (repo / ".decinav").mkdir()
    (repo / ".decinav" / "provenance.json").write_text("{}", encoding="utf-8")
    (repo / "proposal.json").write_text(
        proposal_envelope(("0003-cache.md", generated_adr("ADR-0003", "Cache"))), encoding="utf-8"
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["propose", "--from-file", "proposal.json"])

    assert result.exit_code == 1
    assert "schema_version" in result.output
    assert not (repo / "docs" / "adr" / "0003-cache.md").exists()


def test_accept_handles_crlf_line_endings(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    monkeypatch.chdir(repo)
    _propose_two(repo)
    path = repo / "docs" / "adr" / "0004-ttl.md"
    path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))

    result = runner.invoke(app, ["accept", "ADR-0004"])

    assert result.exit_code == 0, result.output
    raw = path.read_bytes()
    assert b'status: "accepted"\r\n' in raw
    assert parse_adr(path).metadata.status is ADRStatus.ACCEPTED


def test_prd_lint_handles_crlf(tmp_path: Path) -> None:
    prd = tmp_path / "crlf.md"
    body = (
        "# Title\r\n\r\n## Problem\r\n\r\nP\r\n\r\n## Users\r\n\r\nU\r\n\r\n## Requirements\r\n\r\nR\r\n\r\n"
        "## Affected decisions\r\n\r\nNone\r\n\r\n## Proposed decisions\r\n\r\nNone\r\n\r\n"
        "## Open questions\r\n\r\nNone\r\n\r\n## Sources\r\n\r\nS\r\n"
    )
    prd.write_bytes(body.encode("utf-8"))

    assert lint_prd(tmp_path, prd).ok


def test_ingest_records_only_sources_named_in_each_record(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "README.md").write_text("", encoding="utf-8")
    source = repo / "notes"
    source.mkdir()
    (source / "queue.md").write_text("We chose a queue.\n", encoding="utf-8")
    (source / "cache.md").write_text("We chose a cache.\n", encoding="utf-8")
    first = generated_adr("ADR-0001", "Use queue").replace("- test-fixture.md", "- queue.md")
    second = generated_adr("ADR-0002", "Use cache").replace("- test-fixture.md", "- notes/cache.md")
    response = json.dumps(
        {
            "records": [
                {"filename": "0001-use-queue.md", "content": first},
                {"filename": "0002-use-cache.md", "content": second},
            ]
        }
    )
    (repo / "response.json").write_text(response, encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_RESPONSE", str(repo / "response.json"))
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["ingest", "notes"])

    assert result.exit_code == 0, result.output
    records = load_provenance(repo)
    assert records["ADR-0001"].sources == ("notes/queue.md",)
    assert records["ADR-0002"].sources == ("notes/cache.md",)


def test_freshness_unknown_when_recorded_commit_is_not_an_ancestor(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "feature work")
    feature_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-")
    record_generation(
        repo, ("ADR-0001",), generated_by="new", generator="human", sources=(), commit=feature_commit
    )

    freshness = FreshnessCalculator(repo).compute(load_provenance(repo)["ADR-0001"])

    assert freshness.state == "unknown"
    assert "not in the current history" in freshness.detail


def test_freshness_ignores_sources_outside_the_repository(tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo") if False else git_repo(_mk(tmp_path / "repo"))
    outside = tmp_path / "outside.md"
    outside.write_text("brief\n", encoding="utf-8")
    record_generation(
        repo,
        ("ADR-0001",),
        generated_by="baseline-brief",
        generator="mock",
        sources=(str(outside), "README.md"),
    )
    (repo / "other.txt").write_text("y\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "unrelated")

    freshness = FreshnessCalculator(repo).compute(load_provenance(repo)["ADR-0001"])

    assert freshness.state == "aging"
    assert freshness.commits_since == 1


def _mk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_check_answer_counts_long_headings_and_ignores_loose_table_separators(
    tmp_path: Path,
) -> None:
    adr_dir = setup_architecture_project(tmp_path)
    repository = analyze_repository(adr_dir)

    heading_claim = check_answer(
        "\n\n## The API dispatches evaluation asynchronously through a worker boundary\n",
        repository,
    )
    assert not heading_claim.ok
    assert any("citations: 3" in problem for problem in heading_claim.problems)

    table = check_answer(
        "| Area | Rule |\n|---|---\n| Eval | Async [ADR-0002] |\n", repository
    )
    assert not table.ok
    assert any("citations: 1" in problem for problem in table.problems), table.problems


def test_query_retries_once_with_the_rejection_reason_then_surfaces_draft(
    tmp_path: Path, monkeypatch
) -> None:
    from decinav.ai_adapter import AIResponse
    from decinav.intelligence import UntraceableAnswerError, run_query

    setup_architecture_project(tmp_path)
    prompts: list[str] = []
    answers = iter(["The API owns evaluation.", "The API dispatches evaluation [ADR-0002]."])

    def fake_execute(prompt: str, *, cwd: Path, purpose: str) -> AIResponse:
        prompts.append(prompt)
        return AIResponse(next(answers), "", "mock")

    monkeypatch.setattr("decinav.intelligence.execute_ai", fake_execute)

    result = run_query(tmp_path, "Who owns evaluation?")

    assert result.attempts == 2
    assert result.answer.endswith("[ADR-0002].")
    assert "REJECTED DRAFT START" in prompts[1]
    assert "no inline ADR citations" in prompts[1]

    always_bad = iter(["Uncited one.", "Uncited two."])
    monkeypatch.setattr(
        "decinav.intelligence.execute_ai",
        lambda prompt, *, cwd, purpose: AIResponse(next(always_bad), "", "mock"),
    )
    with pytest.raises(UntraceableAnswerError) as error:
        run_query(tmp_path, "Who owns evaluation?")
    assert error.value.attempts == 2
    assert error.value.answer == "Uncited two."


# --- setup and project model config ---------------------------------------------


def _only_on_path(*names):
    return lambda name: "/usr/bin/" + name if name in names else None


def test_setup_without_flags_off_a_terminal_picks_first_cli_and_keeps_its_default_model(
    tmp_path: Path, monkeypatch
) -> None:
    from decinav import ai_adapter
    from decinav.ai_adapter import AIResponse, execute_ai
    from decinav.config import load_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.setattr("decinav.config.shutil.which", _only_on_path("claude"))

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0, result.output
    assert "Chose claude" in result.output and "--provider" in result.output
    assert "Model: claude · CLI default model" in result.output
    config = load_config(tmp_path)
    assert config.provider == "claude" and config.model is None

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(ai_adapter, "run_builtin_provider",
                        lambda provider, prompt, *, model, timeout: calls.append((provider, model)) or AIResponse("ok", "", "builtin"))
    execute_ai("hello", cwd=tmp_path)
    assert calls == [("claude", None)]

    explicit = runner.invoke(app, ["setup", "--provider", "claude", "--model", "opus"])
    assert explicit.exit_code == 0, explicit.output
    assert load_config(tmp_path).model == "opus"
    shown = runner.invoke(app, ["setup", "--show"])
    assert "Model: claude · opus" in shown.output

    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_RESPONSE", str(tmp_path / "fixture.txt"))
    (tmp_path / "fixture.txt").write_text("fixture wins", encoding="utf-8")
    assert execute_ai("hello", cwd=tmp_path).stdout == "fixture wins"


def test_setup_in_a_terminal_asks_for_provider_and_model(tmp_path: Path, monkeypatch) -> None:
    from decinav.config import load_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("decinav.config.shutil.which", _only_on_path("claude", "codex"))
    monkeypatch.setattr("decinav.cli._stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["setup"], input="2\ngpt-5-codex\n")

    assert result.exit_code == 0, result.output
    assert "Claude CLI" in result.output and "Codex CLI" in result.output
    config = load_config(tmp_path)
    assert (config.provider, config.model) == ("codex", "gpt-5-codex")

    kept_default = runner.invoke(app, ["setup"], input="1\n\n")
    assert kept_default.exit_code == 0, kept_default.output
    assert (load_config(tmp_path).provider, load_config(tmp_path).model) == ("claude", None)


def test_setup_rejects_missing_cli_and_unsafe_model_names(tmp_path: Path, monkeypatch) -> None:
    from decinav.ai_adapter import AIAdapterError, execute_ai

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.setattr("decinav.config.shutil.which", _only_on_path("claude"))

    missing = runner.invoke(app, ["setup", "--provider", "codex"])
    assert missing.exit_code == 1 and "not found on PATH" in missing.output
    unsafe = runner.invoke(app, ["setup", "--provider", "claude", "--model", "opus; rm -rf /"])
    assert unsafe.exit_code == 1 and "invalid model name" in unsafe.output

    monkeypatch.setattr("decinav.config.shutil.which", _only_on_path())
    none = runner.invoke(app, ["setup"])
    assert none.exit_code == 0 and "Without a model" in none.output
    with pytest.raises(AIAdapterError, match="decinav setup"):
        execute_ai("hello", cwd=tmp_path)


def test_builtin_runner_passes_the_chosen_model_or_none(monkeypatch) -> None:
    from decinav import ai_adapter
    from decinav.ai_adapter import AIResponse, run_builtin_provider

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return AIResponse("OK", "", kwargs["transport"])

    monkeypatch.setattr(ai_adapter, "_run_process", fake_run)
    run_builtin_provider("claude", "p", model=None, timeout=5)
    run_builtin_provider("claude", "p", model="opus", timeout=5)
    run_builtin_provider("codex", "p", model=None, timeout=5)
    run_builtin_provider("codex", "p", model="gpt-5-codex", timeout=5)

    assert "--model" not in commands[0] and "--tools" in commands[0]
    assert commands[0][commands[0].index("--setting-sources") + 1] == ""
    assert commands[1][commands[1].index("--model") + 1] == "opus"
    assert "-m" not in commands[2] and "read-only" in commands[2]
    assert commands[3][commands[3].index("-m") + 1] == "gpt-5-codex"


def test_ui_setup_saves_provider_and_model_and_tests_a_candidate(tmp_path: Path, monkeypatch) -> None:
    from decinav import ai_adapter
    from decinav.ai_adapter import AIResponse

    setup_architecture_project(tmp_path)
    monkeypatch.delenv("DECINAV_AI_CMD", raising=False)
    monkeypatch.setattr("decinav.config.shutil.which", _only_on_path("codex"))
    service = ArchitectureUIService(tmp_path)

    before = service.status(str(tmp_path))
    assert before["ai_configured"] is False
    assert before["model"]["available"] == ["codex"]
    assert before["model"]["suggestions"]["claude"] == ["sonnet", "opus", "fable"]

    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(ai_adapter, "run_builtin_provider",
                        lambda provider, prompt, *, model, timeout: seen.append((provider, model)) or AIResponse("OK", "", "builtin"))
    tested = service.dispatch("test_model", {"project": str(tmp_path), "provider": "codex", "model": "gpt-5-codex"})
    assert tested["ok"] is True and seen == [("codex", "gpt-5-codex")]
    assert service.status(str(tmp_path))["ai_configured"] is False

    saved = service.dispatch("setup", {"project": str(tmp_path), "provider": "codex", "model": "gpt-5-codex"})
    assert "codex · gpt-5-codex" in saved["message"]
    assert saved["status"]["model"]["provider"] == "codex"
    assert saved["status"]["model"]["model"] == "gpt-5-codex"

    with pytest.raises(UIError):
        service.dispatch("setup", {"project": str(tmp_path), "provider": 7})
    with pytest.raises(UIError, match="choose Claude CLI"):
        service.dispatch("test_model", {"project": str(tmp_path), "provider": "none"})


def test_init_explains_that_nothing_is_committed(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["init", "--existing"])

    assert result.exit_code == 0, result.output
    assert "Nothing was committed" in result.output


def test_query_citation_rule_allows_unknown_abstention_lines(tmp_path: Path) -> None:
    from decinav.intelligence import _validate_query_citations

    repository = analyze_repository(setup_architecture_project(tmp_path))
    _validate_query_citations(
        "Evaluation is asynchronous [ADR-0002].\nNo ADR defines the retry policy [UNKNOWN]",
        repository,
    )
    with pytest.raises(Exception, match="without inline ADR citations: 2"):
        _validate_query_citations(
            "Evaluation is asynchronous [ADR-0002].\nNo ADR defines the retry policy.", repository
        )


# --- legacy state written by earlier releases (published as arch-cli) ----------


def _legacy_repo(tmp_path: Path) -> Path:
    repo = git_repo(tmp_path)
    setup_architecture_project(repo)
    legacy = repo / ".arch"
    legacy.mkdir()
    head = git(repo, "rev-parse", "HEAD")
    (legacy / "provenance.json").write_text(json.dumps({
        "schema_version": 1,
        "records": {"ADR-0001": {"generated_by": "baseline", "generator": "claude", "generated_at": "2026-10-11T08:00:00Z",
                                  "commit": head, "sources": [], "accepted_at": None, "accepted_commit": None,
                                  "attested_at": None, "attested_commit": None}},
    }), encoding="utf-8")
    (legacy / "config.json").write_text(json.dumps({"schema_version": 1, "model": {"provider": "codex", "model": None, "command": None}}), encoding="utf-8")
    (legacy / "last-impact.json").write_text(json.dumps({"schema_version": 1, "prd": "x.md", "directly_affected_adrs": [], "prerequisite_adrs": [], "architectural_risks": [], "proposed_new_adrs": []}), encoding="utf-8")
    return repo


def test_legacy_state_is_read_and_migrates_on_first_write(tmp_path: Path, monkeypatch) -> None:
    from decinav.config import load_config
    from decinav.intelligence import load_last_impact

    repo = _legacy_repo(tmp_path)
    monkeypatch.chdir(repo)

    assert load_provenance(repo)["ADR-0001"].generator == "claude"
    assert load_config(repo).provider == "codex"
    assert load_last_impact(repo)["prd"] == "x.md"
    assert build_context(repo).documents[0].freshness.state == "fresh"

    result = runner.invoke(app, ["attest", "ADR-0002"])

    assert result.exit_code == 0, result.output
    migrated = json.loads((repo / ".decinav" / "provenance.json").read_text(encoding="utf-8"))
    assert set(migrated["records"]) == {"ADR-0001", "ADR-0002"}


def test_graph_replaces_legacy_markers_instead_of_appending(tmp_path: Path) -> None:
    from decinav.graphing import GRAPH_START_MARKER, regenerate_graph

    adr_dir = setup_architecture_project(tmp_path)
    readme = adr_dir / "README.md"
    readme.write_text("# Decisions\n\n<!-- arch-cli:graph:start -->\nold graph\n<!-- arch-cli:graph:end -->\n\nFooter.\n", encoding="utf-8")

    assert regenerate_graph(adr_dir).changed is True

    text = readme.read_text(encoding="utf-8")
    assert "arch-cli:graph" not in text
    assert text.count(GRAPH_START_MARKER) == 1
    assert "old graph" not in text and text.endswith("Footer.\n")


def test_init_updates_a_legacy_hook_instead_of_refusing_it(tmp_path: Path, monkeypatch) -> None:
    repo = git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.write_text("#!/bin/sh\n# arch-cli:managed-commit-msg-hook\nexit 0\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["init", "--existing"])

    assert result.exit_code == 0, result.output
    assert "Hook updated" in result.output
    assert "# decinav:managed-commit-msg-hook" in hook.read_text(encoding="utf-8")


def test_question_headings_are_not_claims(tmp_path: Path) -> None:
    """Regression: a real Sonnet answer was rejected for a long question heading."""
    from decinav.intelligence import _validate_query_citations

    repository = analyze_repository(setup_architecture_project(tmp_path))
    answer = (
        "## Can you trust that the code still works this way?\n\n"
        "Evaluation is asynchronous [ADR-0002].\n"
        "Whether the worker still retries is not recorded [UNKNOWN]\n"
    )
    _validate_query_citations(answer, repository)
    assert check_answer(answer, repository).ok
    assert not check_answer(
        "## The API dispatches evaluation asynchronously through a worker boundary\n", repository
    ).ok


def test_impact_retries_once_with_the_rejection_reason(tmp_path: Path, monkeypatch) -> None:
    """Regression: a real Sonnet impact run used the wrong item keys and failed outright."""
    from decinav.ai_adapter import AIResponse
    from decinav.generation import EnvelopeError
    from decinav.intelligence import run_impact

    setup_architecture_project(tmp_path)
    prd = tmp_path / "feature.md"
    prd.write_text("# Feature\n\nDo something.\n", encoding="utf-8")
    wrong = json.dumps({"directly_affected_adrs": [{"id": "ADR-0002", "reason": "x"}],
                        "prerequisite_adrs": [], "architectural_risks": [], "proposed_new_adrs": []})
    right = (FIXTURES / "mock_impact.json").read_text(encoding="utf-8")
    prompts: list[str] = []
    replies = iter([wrong, right])

    def fake_execute(prompt: str, *, cwd: Path, purpose: str) -> AIResponse:
        prompts.append(prompt)
        return AIResponse(next(replies), "", "mock")

    monkeypatch.setattr("decinav.intelligence.execute_ai", fake_execute)
    result = run_impact(tmp_path, prd)
    assert result.evaluation.directly_affected_adrs[0].id == "ADR-0002"
    assert len(prompts) == 2 and "REJECTED JSON START" in prompts[1]
    assert "reason_for_change" in prompts[1]

    always_wrong = iter([wrong, wrong])
    monkeypatch.setattr("decinav.intelligence.execute_ai",
                        lambda prompt, *, cwd, purpose: AIResponse(next(always_wrong), "", "mock"))
    with pytest.raises(EnvelopeError, match="after 2 attempt"):
        run_impact(tmp_path, prd)


def test_bold_headings_pass_but_bold_or_bare_verdicts_do_not(tmp_path: Path) -> None:
    """Regression: two real Sonnet answers were rejected for bold section titles."""
    from decinav.intelligence import _uncited_query_lines, _validate_query_citations

    repository = analyze_repository(setup_architecture_project(tmp_path))
    good = (
        "**Why it is asynchronous**\n\n"
        "Evaluation takes longer than a request [ADR-0002].\n\n"
        "**Can you trust that this is still how the code works?**\n\n"
        "Whether the worker retries is not recorded [UNKNOWN]\n"
    )
    _validate_query_citations(good, repository)
    assert check_answer(good, repository).ok

    verdicts = "## Is it current?\n\nNot fully.\n**Not fully.**\nEvaluation is async [ADR-0002].\n"
    assert [text for _n, text in _uncited_query_lines(verdicts)] == ["Not fully.", "**Not fully.**"]


def test_query_retry_quotes_the_offending_lines(tmp_path: Path, monkeypatch) -> None:
    from decinav.ai_adapter import AIResponse
    from decinav.intelligence import run_query

    setup_architecture_project(tmp_path)
    prompts: list[str] = []
    replies = iter(["## Is it async?\n\nNot fully.\nEvaluation is async [ADR-0002].",
                    "Evaluation is async [ADR-0002]."])

    def fake_execute(prompt: str, *, cwd: Path, purpose: str) -> AIResponse:
        prompts.append(prompt)
        return AIResponse(next(replies), "", "mock")

    monkeypatch.setattr("decinav.intelligence.execute_ai", fake_execute)
    assert run_query(tmp_path, "Is evaluation async?").attempts == 2
    assert "These lines carry neither a citation nor [UNKNOWN]:\n- Not fully.\n" in prompts[1]
    assert "never bold-only lines" in prompts[0]


def test_http_query_failure_returns_the_rejected_draft(tmp_path: Path, monkeypatch) -> None:
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from decinav.web_ui import create_ui_server

    setup_architecture_project(tmp_path)
    fixture = tmp_path / "uncited.txt"
    fixture.write_text("The API owns evaluation.\n", encoding="utf-8")
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv("DECINAV_AI_MOCK_QUERY_RESPONSE", str(fixture))
    try:
        info = create_ui_server(tmp_path, port=0)
    except PermissionError:
        pytest.skip("test sandbox does not permit binding a loopback socket")
    thread = Thread(target=info.server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            info.url + "api/query",
            data=json.dumps({"project": str(tmp_path), "question": "Who owns evaluation?"}).encode(),
            headers={"X-Decinav-UI-Token": info.token, "Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        payload = json.loads(error.value.read())
        assert error.value.code == 400
        assert payload["rejected_answer"] == "The API owns evaluation."
        assert payload["attempts"] == 2
    finally:
        info.server.shutdown()
        info.server.server_close()
        thread.join(timeout=2)


def test_codex_runs_with_a_home_that_excludes_personal_instructions(tmp_path: Path, monkeypatch) -> None:
    from decinav import ai_adapter
    from decinav.ai_adapter import AIResponse, run_builtin_provider

    real_home = tmp_path / "real-codex"
    real_home.mkdir()
    for name, text in (("auth.json", "{}"), ("config.toml", "model = 'x'"), ("AGENTS.md", "Call the user boss.")):
        (real_home / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(real_home))
    seen: dict = {}

    def fake_run(command, **kwargs):
        home = Path(kwargs["process_environment"]["CODEX_HOME"])
        seen["files"] = sorted(item.name for item in home.iterdir())
        seen["auth_target"] = (home / "auth.json").resolve()
        seen["cwd_entries"] = list(kwargs["cwd"].iterdir())
        (Path(command[command.index("-o") + 1])).write_text("OK", encoding="utf-8")
        return AIResponse("", "", kwargs["transport"])

    monkeypatch.setattr(ai_adapter, "_run_process", fake_run)
    assert run_builtin_provider("codex", "p", model=None, timeout=5).stdout == "OK"
    assert seen["files"] == ["auth.json", "config.toml"]
    assert seen["auth_target"] == (real_home / "auth.json").resolve()
    assert seen["cwd_entries"] == []
