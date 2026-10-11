import json
import os
from pathlib import Path

import pytest

from decinav.generation import (
    BatchWriteError,
    CandidateValidationError,
    EnvelopeError,
    parse_envelope,
    validate_candidates,
    write_candidate_batch,
)
from test_repository import write_adr


def generated_adr(
    adr_id: str,
    title: str,
    *,
    status: str = "proposed",
    domain: str = "backend",
    depends_on: tuple[str, ...] = (),
    supersedes: tuple[str, ...] = (),
) -> str:
    dependency_objects = [
        {"id": adr_id, "reason": f"{title} requires {adr_id}."}
        for adr_id in depends_on
    ]
    supersession_objects = [
        {"id": adr_id, "reason": f"{title} replaces {adr_id}."}
        for adr_id in supersedes
    ]
    return (
        "---\n"
        "schema_version: 2\n"
        f'id: "{adr_id}"\n'
        f"title: {json.dumps(title)}\n"
        f'status: "{status}"\n'
        'date: "2026-08-27"\n'
        f'domain: "{domain}"\n'
        f"depends_on: {json.dumps(dependency_objects)}\n"
        f"supersedes: {json.dumps(supersession_objects)}\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Sources\n\n- test-fixture.md\n"
    )


def envelope(*records: tuple[str, str]) -> str:
    return json.dumps(
        {
            "records": [
                {"filename": filename, "content": content}
                for filename, content in records
            ]
        }
    )


@pytest.mark.parametrize(
    "stdout",
    [
        '```json\n{"records": []}\n```',
        '{"records": []} trailing',
        '{"records": [], "comment": "chatty"}',
        '{"records": [], "records": []}',
        '[{"records": []}]',
    ],
)
def test_envelope_parser_rejects_non_exact_ai_stdout(stdout: str) -> None:
    with pytest.raises(EnvelopeError):
        parse_envelope(stdout)


def test_envelope_rejects_unsafe_filename() -> None:
    stdout = envelope(("../0001-escape.md", generated_adr("ADR-0001", "Escape")))

    with pytest.raises(EnvelopeError, match="filename must match"):
        parse_envelope(stdout)


def test_prospective_validation_and_batch_write_use_contiguous_ids(
    tmp_path: Path,
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(adr_dir / "0001-baseline.md", adr_id="ADR-0001", domain="system")
    records = parse_envelope(
        envelope(
            (
                "0002-queue.md",
                generated_adr(
                    "ADR-0002", "Use queue", depends_on=("ADR-0001",)
                ),
            ),
            (
                "0003-replace-queue.md",
                generated_adr(
                    "ADR-0003", "Replace queue", supersedes=("ADR-0002",)
                ),
            ),
        )
    )

    batch = validate_candidates(
        records,
        adr_dir=adr_dir,
        expected_start=2,
        operation="ingest",
    )

    assert not (adr_dir / "0002-queue.md").exists()
    written = write_candidate_batch(batch)
    assert [path.name for path in written] == [
        "0002-queue.md",
        "0003-replace-queue.md",
    ]


def test_validation_rejects_id_gaps_without_writing(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    records = parse_envelope(
        envelope(("0002-gap.md", generated_adr("ADR-0002", "Gap")))
    )

    with pytest.raises(CandidateValidationError) as error:
        validate_candidates(
            records,
            adr_dir=adr_dir,
            expected_start=1,
            operation="ingest",
        )

    assert "candidate-id-sequence" in {issue.code for issue in error.value.issues}
    assert list(adr_dir.glob("*.md")) == []


def test_prospective_validation_detects_cycle_across_existing_and_candidate(
    tmp_path: Path,
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    write_adr(
        adr_dir / "0001-first.md",
        adr_id="ADR-0001",
        depends_on=("ADR-0002",),
    )
    records = parse_envelope(
        envelope(
            (
                "0002-second.md",
                generated_adr(
                    "ADR-0002", "Second", depends_on=("ADR-0001",)
                ),
            )
        )
    )

    with pytest.raises(CandidateValidationError) as error:
        validate_candidates(
            records,
            adr_dir=adr_dir,
            expected_start=2,
            operation="ingest",
        )

    assert "circular-relationship" in {issue.code for issue in error.value.issues}
    assert not (adr_dir / "0002-second.md").exists()


def test_baseline_contract_requires_exact_system_baseline(tmp_path: Path) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    records = parse_envelope(
        envelope(
            (
                "0001-wrong.md",
                generated_adr("ADR-0001", "Wrong", status="accepted", domain="backend"),
            )
        )
    )

    with pytest.raises(CandidateValidationError) as error:
        validate_candidates(
            records,
            adr_dir=adr_dir,
            expected_start=1,
            operation="baseline",
        )

    assert {
        "baseline-filename",
        "baseline-status",
        "baseline-domain",
    }.issubset({issue.code for issue in error.value.issues})


def test_batch_write_rolls_back_first_file_when_second_publish_fails(
    tmp_path: Path, monkeypatch
) -> None:
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    records = parse_envelope(
        envelope(
            ("0001-first.md", generated_adr("ADR-0001", "First")),
            ("0002-second.md", generated_adr("ADR-0002", "Second")),
        )
    )
    batch = validate_candidates(
        records,
        adr_dir=adr_dir,
        expected_start=1,
        operation="ingest",
    )
    real_link = os.link
    calls = 0

    def fail_second_link(source, target) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated publish failure")
        real_link(source, target)

    monkeypatch.setattr("decinav.generation.os.link", fail_second_link)

    with pytest.raises(BatchWriteError, match="simulated publish failure"):
        write_candidate_batch(batch)

    assert list(adr_dir.glob("*.md")) == []
