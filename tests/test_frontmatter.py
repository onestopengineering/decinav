from datetime import date

import pytest

from decinav.frontmatter import (
    ADRDomain,
    ADRParseError,
    ADRRelationship,
    ADRStatus,
    parse_adr_text,
)


def valid_document(**overrides: str) -> str:
    fields = {
        "id": '"ADR-0004"',
        "title": '"Adopt PostgreSQL"',
        "status": '"accepted"',
        "date": '"2026-08-27"',
        "domain": '"data"',
        "depends_on": '["ADR-0001", "ADR-0002"]',
        "supersedes": '["ADR-0003"]',
    }
    fields.update(overrides)
    yaml_lines = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{yaml_lines}\n---\n\n# Adopt PostgreSQL\n"


def valid_v2_document(**overrides: str) -> str:
    fields = {
        "schema_version": "2",
        "id": '"ADR-0004"',
        "title": '"Adopt PostgreSQL"',
        "status": '"accepted"',
        "date": '"2026-08-27"',
        "domain": '"data"',
        "depends_on": (
            '[{"id": "ADR-0001", "reason": "The baseline establishes the system."}]'
        ),
        "supersedes": (
            '[{"id": "ADR-0003", "reason": "PostgreSQL replaces SQLite."}]'
        ),
    }
    fields.update(overrides)
    yaml_lines = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{yaml_lines}\n---\n\n# Adopt PostgreSQL\n"


def test_parse_valid_adr_into_typed_metadata() -> None:
    parsed = parse_adr_text(valid_document(), source="0004-postgres.md")

    assert parsed.metadata.id == "ADR-0004"
    assert parsed.metadata.title == "Adopt PostgreSQL"
    assert parsed.metadata.status is ADRStatus.ACCEPTED
    assert parsed.metadata.date == date(2026, 8, 27)
    assert parsed.metadata.domain is ADRDomain.DATA
    assert parsed.metadata.schema_version == 1
    assert parsed.metadata.dependency_ids == ("ADR-0001", "ADR-0002")
    assert parsed.metadata.superseded_ids == ("ADR-0003",)
    assert all(item.reason is None for item in parsed.metadata.depends_on)
    assert parsed.body == "\n# Adopt PostgreSQL\n"


def test_parse_v2_relationships_with_required_reasons() -> None:
    parsed = parse_adr_text(valid_v2_document(), source="0004-postgres.md")

    assert parsed.metadata.schema_version == 2
    assert parsed.metadata.depends_on == (
        ADRRelationship("ADR-0001", "The baseline establishes the system."),
    )
    assert parsed.metadata.supersedes == (
        ADRRelationship("ADR-0003", "PostgreSQL replaces SQLite."),
    )


def test_accepts_system_domain_for_cross_cutting_baselines() -> None:
    parsed = parse_adr_text(valid_document(domain='"system"'))

    assert parsed.metadata.domain is ADRDomain.SYSTEM


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": '"4"'}, "must match ADR-0001 through ADR-9999"),
        ({"status": '"draft"'}, "field 'status' must be one of"),
        ({"date": '"2026-02-30"'}, "not a real calendar date"),
        ({"date": "2026-08-27"}, "must be a non-empty string"),
        ({"domain": '"mobile"'}, "field 'domain' must be one of"),
        ({"depends_on": '"ADR-0001"'}, "must be a YAML list"),
        ({"depends_on": '["ADR-0004"]'}, "cannot reference its own ADR"),
        ({"depends_on": '["ADR-0000"]'}, "must match ADR-0001 through ADR-9999"),
    ],
)
def test_rejects_schema_violations(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ADRParseError, match=message):
        parse_adr_text(valid_document(**overrides))


def test_rejects_unknown_and_missing_fields() -> None:
    with pytest.raises(ADRParseError, match="unknown fields: owner"):
        parse_adr_text(valid_document(owner='"platform"'))

    missing_title = valid_document().replace('title: "Adopt PostgreSQL"\n', "")
    with pytest.raises(ADRParseError, match="missing required fields: title"):
        parse_adr_text(missing_title)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": "1"}, "must be integer 2 when present"),
        ({"schema_version": '"2"'}, "must be integer 2 when present"),
        (
            {"depends_on": '[{"id": "ADR-0001"}]'},
            "missing required fields: reason",
        ),
        (
            {
                "depends_on": (
                    '[{"id": "ADR-0001", "reason": "", "owner": "platform"}]'
                )
            },
            "unknown fields: owner",
        ),
        (
            {"depends_on": '[{"id": "ADR-0001", "reason": ""}]'},
            "reason must be a non-empty string",
        ),
        (
            {"depends_on": '["ADR-0001"]'},
            "must be a mapping with exactly 'id' and 'reason'",
        ),
    ],
)
def test_rejects_invalid_v2_relationships(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ADRParseError, match=message):
        parse_adr_text(valid_v2_document(**overrides))


def test_rejects_duplicate_yaml_keys() -> None:
    duplicate = valid_document().replace(
        'title: "Adopt PostgreSQL"',
        'title: "Adopt PostgreSQL"\ntitle: "Second title"',
    )

    with pytest.raises(ADRParseError, match="duplicate key 'title'"):
        parse_adr_text(duplicate)


def test_safe_loader_rejects_python_object_tags() -> None:
    unsafe = valid_document(title="!!python/object/apply:os.system ['echo unsafe']")

    with pytest.raises(ADRParseError, match="invalid YAML"):
        parse_adr_text(unsafe)


def test_requires_frontmatter_at_start_of_document() -> None:
    with pytest.raises(ADRParseError, match="document must start"):
        parse_adr_text("# Heading\n---\nid: ADR-0001\n---\n")
