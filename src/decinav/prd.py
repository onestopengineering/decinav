"""Grounded PRD template and deterministic lint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from decinav.adr import slugify
from decinav.frontmatter import ADR_ID_PATTERN
from decinav.repository import ADRRepositoryError, load_repository_records


PRD_DIRECTORY = Path("docs") / "prds"
MAX_PRD_BYTES = 1024 * 1024
REQUIRED_SECTIONS = (
    "Problem",
    "Users",
    "Requirements",
    "Affected decisions",
    "Proposed decisions",
    "Open questions",
    "Sources",
)
_H1_PATTERN = re.compile(r"^#[ \t]+\S", re.MULTILINE)
_H2_PATTERN = re.compile(r"^##[ \t]+(?P<title>[^\r\n#]+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_ADR_REFERENCE_PATTERN = re.compile(r"\bADR-(\d{4})\b")


class PRDError(RuntimeError):
    """Raised when a PRD cannot be created or read."""


@dataclass(frozen=True, slots=True)
class PRDLintResult:
    path: Path
    issues: tuple[str, ...]
    referenced_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def render_prd_template(title: str) -> str:
    clean_title = title.strip()
    return (
        f"# {clean_title}\n\n"
        "<!-- decinav PRD. Every section below is required by `decinav prd lint`. "
        "Delete the comments once you have written the section. -->\n\n"
        "## Problem\n\n"
        "<!-- What is wrong or missing today, for whom, and what evidence shows it. -->\n\n"
        "## Users\n\n"
        "<!-- Who experiences the problem and what they are trying to do. -->\n\n"
        "## Requirements\n\n"
        "<!-- Numbered, testable behaviors. Say what must be true, not how to build it. -->\n\n"
        "## Affected decisions\n\n"
        "<!-- Existing ADRs this feature changes or relies on, one per line, as ADR-XXXX "
        "with one sentence on why. Run `decinav impact <this file>` to check. Write "
        "`None` only after checking. -->\n\n"
        "## Proposed decisions\n\n"
        "<!-- New architectural decisions this feature needs, as titles. Each becomes an "
        "ADR through `decinav propose`. Write `None` if no new decision is needed. -->\n\n"
        "## Open questions\n\n"
        "<!-- Facts that would change the design and are not yet known. Write `None` "
        "only when every material question is resolved. -->\n\n"
        "## Sources\n\n"
        "<!-- Documents, code paths, tickets, or conversations this PRD is grounded in. -->\n"
    )


def create_prd(project_root: Path, title: str) -> Path:
    clean_title = title.strip()
    if not clean_title or "\n" in clean_title or "\r" in clean_title:
        raise PRDError("PRD title must be a single non-empty line")
    directory = project_root / PRD_DIRECTORY
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PRDError(f"could not create {directory}: {exc}") from exc
    path = directory / f"{slugify(clean_title)}.md"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(render_prd_template(clean_title))
    except FileExistsError as exc:
        raise PRDError(f"refusing to overwrite existing PRD: {path}") from exc
    except OSError as exc:
        raise PRDError(f"could not create {path}: {exc}") from exc
    return path


def lint_prd(project_root: Path, prd_path: Path) -> PRDLintResult:
    """Check structure and that every referenced ADR exists in the repository."""

    candidate = prd_path if prd_path.is_absolute() else project_root / prd_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PRDError(f"PRD does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise PRDError(f"PRD path is not a file: {resolved}")
    if resolved.suffix.casefold() != ".md":
        raise PRDError("PRD must be a Markdown file with a .md extension")
    try:
        if resolved.stat().st_size > MAX_PRD_BYTES:
            raise PRDError(f"PRD exceeds the {MAX_PRD_BYTES}-byte safety limit: {resolved}")
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PRDError(f"could not read PRD as UTF-8: {resolved}: {exc}") from exc

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    issues: list[str] = []
    if not text.strip():
        return PRDLintResult(resolved, ("PRD is empty",), ())
    if _H1_PATTERN.search(text) is None:
        issues.append("missing a top-level '# Title' heading")

    sections = _split_sections(text)
    for required in REQUIRED_SECTIONS:
        body = sections.get(required.casefold())
        if body is None:
            issues.append(f"missing required section '## {required}'")
            continue
        content = _COMMENT_PATTERN.sub("", body).strip()
        if not content:
            issues.append(f"section '## {required}' is empty (write content or 'None')")

    referenced = tuple(
        sorted({f"ADR-{number}" for number in _ADR_REFERENCE_PATTERN.findall(text)})
    )
    if referenced:
        adr_dir = project_root / "docs" / "adr"
        try:
            records, _load_issues = load_repository_records(adr_dir)
        except ADRRepositoryError:
            issues.append(
                "PRD references ADRs but docs/adr does not exist; run 'decinav init' first"
            )
        else:
            known = {record.metadata.id for record in records}
            unknown = [adr_id for adr_id in referenced if adr_id not in known]
            if unknown:
                issues.append("references unknown ADR(s): " + ", ".join(unknown))
    return PRDLintResult(resolved, tuple(issues), referenced)


def _split_sections(text: str) -> dict[str, str]:
    matches = list(_H2_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group("title").strip().casefold()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if title not in sections:
            sections[title] = text[start:end]
    return sections
