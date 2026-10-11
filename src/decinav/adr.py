"""ADR creation and local editor integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import unicodedata

from decinav.frontmatter import (
    ADRDomain,
    ADRParseError,
    ADRRelationship,
    parse_adr,
    parse_adr_text,
)


FILENAME_ID_PATTERN = re.compile(r"^(\d{4})-")
MAX_ADR_NUMBER = 9999


class ADRCreationError(RuntimeError):
    """Raised when a new ADR cannot be created safely."""


class EditorError(RuntimeError):
    """Raised when a configured editor cannot be started successfully."""


def create_adr(
    adr_dir: Path,
    title: str,
    domain: ADRDomain,
    *,
    depends_on: tuple[ADRRelationship, ...] = (),
    supersedes: tuple[ADRRelationship, ...] = (),
    today: date | None = None,
    warn: Callable[[str], None] | None = None,
    known_ids: frozenset[str] | None = None,
) -> Path:
    """Create one proposed ADR using the next available numeric ID.

    When ``known_ids`` is given, every relationship must target one of them so a
    manual ADR can never introduce a dangling reference.
    """

    clean_title = title.strip()
    if not clean_title:
        raise ADRCreationError("title must not be empty")
    if "\n" in clean_title or "\r" in clean_title:
        raise ADRCreationError("title must be a single line")
    if not adr_dir.is_dir():
        raise ADRCreationError(f"{adr_dir} does not exist; run 'decinav init' first")
    if known_ids is not None:
        for field, relationships in (("depends_on", depends_on), ("supersedes", supersedes)):
            missing = [item.id for item in relationships if item.id not in known_ids]
            if missing:
                raise ADRCreationError(
                    f"{field} references unknown ADR(s): {', '.join(missing)}; "
                    "create the referenced decision first"
                )

    number = find_next_adr_number(adr_dir, warn=warn)
    adr_id = f"ADR-{number:04d}"
    filename = f"{number:04d}-{slugify(clean_title)}.md"
    path = adr_dir / filename
    content = render_adr(
        adr_id=adr_id,
        title=clean_title,
        domain=domain,
        decision_date=today or date.today(),
        depends_on=depends_on,
        supersedes=supersedes,
    )
    try:
        parse_adr_text(content, source=str(path))
    except ADRParseError as exc:
        raise ADRCreationError(exc.reason) from exc

    try:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
    except FileExistsError as exc:
        raise ADRCreationError(f"refusing to overwrite existing file: {path}") from exc
    except OSError as exc:
        raise ADRCreationError(f"could not create {path}: {exc}") from exc
    return path


def find_next_adr_number(
    adr_dir: Path, *, warn: Callable[[str], None] | None = None
) -> int:
    """Find the next ID from valid metadata and reserved filename prefixes."""

    highest = 0
    for path in sorted(adr_dir.glob("*.md")):
        if path.name.casefold() == "readme.md":
            continue

        filename_match = FILENAME_ID_PATTERN.match(path.name)
        if filename_match:
            highest = max(highest, int(filename_match.group(1)))

        try:
            parsed = parse_adr(path)
        except ADRParseError as exc:
            if warn is not None:
                warn(f"ignoring malformed ADR metadata in {path}: {exc.reason}")
            continue
        highest = max(highest, int(parsed.metadata.id.removeprefix("ADR-")))

    if highest >= MAX_ADR_NUMBER:
        raise ADRCreationError("ADR ID space is exhausted at ADR-9999")
    return highest + 1


def slugify(title: str) -> str:
    """Create a portable lowercase filename component from an ADR title."""

    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug or "decision"


def render_adr(
    *,
    adr_id: str,
    title: str,
    domain: ADRDomain,
    decision_date: date,
    depends_on: tuple[ADRRelationship, ...] = (),
    supersedes: tuple[ADRRelationship, ...] = (),
) -> str:
    """Render strict YAML frontmatter and a minimal decision template."""

    # JSON strings are valid YAML strings and correctly escape arbitrary titles.
    yaml_title = json.dumps(title, ensure_ascii=False)
    return (
        "---\n"
        "schema_version: 2\n"
        f'id: "{adr_id}"\n'
        f"title: {yaml_title}\n"
        'status: "proposed"\n'
        f'date: "{decision_date.isoformat()}"\n'
        f'domain: "{domain.value}"\n'
        f"depends_on: {_render_relationships(depends_on)}\n"
        f"supersedes: {_render_relationships(supersedes)}\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Context\n\n"
        "Describe the forces and constraints that require a decision.\n\n"
        "## Decision\n\n"
        "Describe the chosen approach.\n\n"
        "## Consequences\n\n"
        "Describe the positive, negative, and operational consequences.\n"
    )


def _render_relationships(relationships: tuple[ADRRelationship, ...]) -> str:
    if not relationships:
        return "[]"
    return "\n" + "\n".join(
        (
            f'  - id: "{relationship.id}"\n'
            f"    reason: {json.dumps(relationship.reason, ensure_ascii=False)}"
        )
        for relationship in relationships
    )


def open_in_editor(path: Path, *, editor: str | None = None) -> bool:
    """Open a path without invoking a shell; return False when no editor is set."""

    editor_command = editor if editor is not None else os.environ.get("EDITOR")
    if not editor_command or not editor_command.strip():
        return False

    try:
        command = shlex.split(editor_command)
    except ValueError as exc:
        raise EditorError(f"could not parse $EDITOR: {exc}") from exc
    if not command:
        return False

    try:
        result = subprocess.run([*command, str(path)], check=False)
    except OSError as exc:
        raise EditorError(f"could not start $EDITOR command {command[0]!r}: {exc}") from exc
    if result.returncode != 0:
        raise EditorError(
            f"$EDITOR exited with status {result.returncode}; ADR remains at {path}"
        )
    return True
