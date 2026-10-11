"""Safe parsing and strict validation for ADR YAML frontmatter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
import re
from typing import Any

import yaml
from yaml.constructor import ConstructorError


ADR_ID_PATTERN = re.compile(r"^ADR-(\d{4})$")
MAX_FRONTMATTER_BYTES = 64 * 1024
V1_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "title",
        "status",
        "date",
        "domain",
        "depends_on",
        "supersedes",
    }
)
V2_REQUIRED_FIELDS = V1_REQUIRED_FIELDS | {"schema_version"}
RELATIONSHIP_FIELDS = frozenset({"id", "reason"})


class ADRStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"
    BASELINE = "baseline"


class ADRDomain(StrEnum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    INFRASTRUCTURE = "infrastructure"
    DATA = "data"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ADRRelationship:
    id: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class ADRMetadata:
    schema_version: int
    id: str
    title: str
    status: ADRStatus
    date: date
    domain: ADRDomain
    depends_on: tuple[ADRRelationship, ...]
    supersedes: tuple[ADRRelationship, ...]

    @property
    def dependency_ids(self) -> tuple[str, ...]:
        return tuple(relationship.id for relationship in self.depends_on)

    @property
    def superseded_ids(self) -> tuple[str, ...]:
        return tuple(relationship.id for relationship in self.supersedes)


@dataclass(frozen=True, slots=True)
class ParsedADR:
    source: str
    metadata: ADRMetadata
    body: str


class ADRParseError(ValueError):
    """Raised when an ADR cannot be parsed without weakening the schema."""

    def __init__(self, source: str, reason: str) -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"{source}: {reason}")


class _StrictSafeLoader(yaml.SafeLoader):
    """PyYAML safe loader that also rejects duplicate mapping keys."""

    def construct_mapping(  # type: ignore[override]
        self, node: yaml.nodes.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        if not isinstance(node, yaml.nodes.MappingNode):
            raise ConstructorError(
                None,
                None,
                f"expected a mapping node, but found {node.id}",
                node.start_mark,
            )

        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse_adr(path: Path) -> ParsedADR:
    """Read and parse one ADR as UTF-8 text."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ADRParseError(str(path), f"could not read UTF-8 text: {exc}") from exc
    return parse_adr_text(text, source=str(path))


def parse_adr_text(text: str, *, source: str = "<memory>") -> ParsedADR:
    """Parse an ADR document and enforce its complete frontmatter schema."""

    yaml_text, body = _extract_frontmatter(text, source)
    if len(yaml_text.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise ADRParseError(
            source,
            f"frontmatter exceeds the {MAX_FRONTMATTER_BYTES}-byte safety limit",
        )

    try:
        raw = yaml.load(yaml_text, Loader=_StrictSafeLoader)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ADRParseError(source, f"invalid YAML: {problem}") from exc

    metadata = _validate_metadata(raw, source)
    return ParsedADR(source=source, metadata=metadata, body=body)


def _extract_frontmatter(text: str, source: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ADRParseError(source, "document must start with a YAML '---' delimiter")

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            closing_index = index
            break

    if closing_index is None:
        raise ADRParseError(source, "frontmatter is missing its closing '---' delimiter")

    yaml_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    return yaml_text, body


def _validate_metadata(raw: object, source: str) -> ADRMetadata:
    if not isinstance(raw, dict):
        raise ADRParseError(source, "frontmatter must be a YAML mapping")

    keys = set(raw)
    if not all(isinstance(key, str) for key in keys):
        raise ADRParseError(source, "frontmatter field names must be strings")

    schema_version_value = raw.get("schema_version")
    if schema_version_value is None:
        schema_version = 1
        required_fields = V1_REQUIRED_FIELDS
    else:
        if (
            isinstance(schema_version_value, bool)
            or not isinstance(schema_version_value, int)
            or schema_version_value != 2
        ):
            raise ADRParseError(
                source,
                "field 'schema_version' must be integer 2 when present",
            )
        schema_version = 2
        required_fields = V2_REQUIRED_FIELDS

    missing = required_fields - keys
    extra = keys - required_fields
    if missing:
        raise ADRParseError(source, f"missing required fields: {', '.join(sorted(missing))}")
    if extra:
        raise ADRParseError(source, f"unknown fields: {', '.join(sorted(extra))}")

    adr_id = _required_string(raw, "id", source)
    match = ADR_ID_PATTERN.fullmatch(adr_id)
    if match is None or int(match.group(1)) == 0:
        raise ADRParseError(source, "field 'id' must match ADR-0001 through ADR-9999")

    title = _required_string(raw, "title", source)
    if "\n" in title or "\r" in title:
        raise ADRParseError(source, "field 'title' must be a single line")

    status_value = _required_string(raw, "status", source)
    try:
        status = ADRStatus(status_value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ADRStatus)
        raise ADRParseError(source, f"field 'status' must be one of: {allowed}") from exc

    date_value = _required_string(raw, "date", source)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        raise ADRParseError(source, "field 'date' must use YYYY-MM-DD")
    try:
        decision_date = date.fromisoformat(date_value)
    except ValueError as exc:
        raise ADRParseError(source, "field 'date' is not a real calendar date") from exc

    domain_value = _required_string(raw, "domain", source)
    try:
        domain = ADRDomain(domain_value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ADRDomain)
        raise ADRParseError(source, f"field 'domain' must be one of: {allowed}") from exc

    depends_on = _validate_relationships(
        raw["depends_on"], "depends_on", adr_id, source, schema_version
    )
    supersedes = _validate_relationships(
        raw["supersedes"], "supersedes", adr_id, source, schema_version
    )

    return ADRMetadata(
        schema_version=schema_version,
        id=adr_id,
        title=title,
        status=status,
        date=decision_date,
        domain=domain,
        depends_on=depends_on,
        supersedes=supersedes,
    )


def _required_string(raw: dict[Any, Any], field: str, source: str) -> str:
    value = raw[field]
    if not isinstance(value, str) or not value.strip():
        raise ADRParseError(source, f"field '{field}' must be a non-empty string")
    if value != value.strip():
        raise ADRParseError(source, f"field '{field}' must not have surrounding whitespace")
    return value


def _validate_relationships(
    value: object,
    field: str,
    own_id: str,
    source: str,
    schema_version: int,
) -> tuple[ADRRelationship, ...]:
    if not isinstance(value, list):
        raise ADRParseError(source, f"field '{field}' must be a YAML list")

    result: list[ADRRelationship] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        reason: str | None = None
        if schema_version == 1:
            related_id = item
        else:
            if not isinstance(item, dict):
                raise ADRParseError(
                    source,
                    f"field '{field}' item {index + 1} must be a mapping with "
                    "exactly 'id' and 'reason'",
                )
            item_keys = set(item)
            if not all(isinstance(key, str) for key in item_keys):
                raise ADRParseError(
                    source,
                    f"field '{field}' item {index + 1} field names must be strings",
                )
            missing = RELATIONSHIP_FIELDS - item_keys
            extra = item_keys - RELATIONSHIP_FIELDS
            if missing:
                raise ADRParseError(
                    source,
                    f"field '{field}' item {index + 1} missing required fields: "
                    + ", ".join(sorted(missing)),
                )
            if extra:
                raise ADRParseError(
                    source,
                    f"field '{field}' item {index + 1} unknown fields: "
                    + ", ".join(sorted(extra)),
                )
            related_id = item["id"]
            reason_value = item["reason"]
            if not isinstance(reason_value, str) or not reason_value.strip():
                raise ADRParseError(
                    source,
                    f"field '{field}' item {index + 1} reason must be a non-empty string",
                )
            if reason_value != reason_value.strip():
                raise ADRParseError(
                    source,
                    f"field '{field}' item {index + 1} reason must not have "
                    "surrounding whitespace",
                )
            reason = reason_value

        match = ADR_ID_PATTERN.fullmatch(related_id) if isinstance(related_id, str) else None
        if match is None or int(match.group(1)) == 0:
            raise ADRParseError(
                source,
                f"field '{field}' item {index + 1} must match ADR-0001 through ADR-9999",
            )
        if related_id == own_id:
            raise ADRParseError(source, f"field '{field}' cannot reference its own ADR")
        if related_id in seen_ids:
            raise ADRParseError(source, f"field '{field}' contains duplicate ID {related_id}")
        seen_ids.add(related_id)
        result.append(ADRRelationship(related_id, reason))
    return tuple(result)
