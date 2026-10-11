"""Read-only AI intelligence workflows over validated ADR evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

import yaml

from decinav.ai_adapter import AIResponse, execute_ai
from decinav.frontmatter import ADRRelationship
from decinav.generation import EnvelopeError, parse_json_object
from decinav.graphing import generate_impact_mermaid
from decinav.prompts import build_impact_prompt, build_query_prompt
from decinav.provenance import (
    PROVENANCE_DIRECTORY,
    Freshness,
    FreshnessCalculator,
    ProvenanceError,
    ProvenanceRecord,
    current_commit,
    load_provenance,
    now_iso,
    state_file,
)
from decinav.repository import ADRRecord, RepositoryAnalysis, analyze_repository
from decinav.workflows import RepositoryPreflightError, WorkflowError


MAX_PRD_BYTES = 1024 * 1024
MAX_QUESTION_CHARACTERS = 16 * 1024
LAST_IMPACT_FILENAME = "last-impact.json"
QUERY_MAX_ATTEMPTS = 2
CLAIM_STATES = ("DOCUMENTED", "VERIFIED_IN_CODE", "CONTRADICTED", "UNKNOWN")
_CLAIM_STATE_PATTERN = re.compile(r"\[(DOCUMENTED|VERIFIED_IN_CODE|CONTRADICTED|UNKNOWN)\]")
_SECTION_HEADING_PATTERN = re.compile(
    r"^(?P<marks>#{1,6})[ \t]+(?P<title>[^\r\n#]+?)[ \t]*#*[ \t]*$",
    re.MULTILINE,
)
_ADR_CITATION_PATTERN = re.compile(r"\[(ADR-\d{4})\]")


@dataclass(frozen=True, slots=True)
class ImpactReference:
    id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImpactEvaluation:
    directly_affected_adrs: tuple[ImpactReference, ...]
    prerequisite_adrs: tuple[ImpactReference, ...]
    architectural_risks: tuple[str, ...]
    proposed_new_adrs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImpactResult:
    prd_path: Path
    repository: RepositoryAnalysis
    evaluation: ImpactEvaluation
    ai_response: AIResponse
    mermaid: str | None
    saved_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ContextDocument:
    id: str
    title: str
    status: str
    domain: str
    date: str
    path: str
    depends_on: tuple[ADRRelationship, ...]
    supersedes: tuple[ADRRelationship, ...]
    context: str | None
    decision: str | None
    consequences: str | None
    provenance: ProvenanceRecord | None
    freshness: Freshness

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "domain": self.domain,
            "date": self.date,
            "path": self.path,
            "depends_on": [{"id": r.id, "reason": r.reason} for r in self.depends_on],
            "supersedes": [{"id": r.id, "reason": r.reason} for r in self.supersedes],
            "context": self.context,
            "decision": self.decision,
            "consequences": self.consequences,
            "provenance": self.provenance.to_json() if self.provenance else None,
            "freshness": self.freshness.to_json(),
        }


@dataclass(frozen=True, slots=True)
class ContextResult:
    project_root: Path
    repository: RepositoryAnalysis
    documents: tuple[ContextDocument, ...]
    commit: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "project": str(self.project_root),
            "commit": self.commit,
            "adr_count": len(self.documents),
            "claim_states": list(CLAIM_STATES),
            "records": [document.to_json() for document in self.documents],
        }


@dataclass(frozen=True, slots=True)
class AnswerCheck:
    problems: tuple[str, ...]
    cited_ids: tuple[str, ...]
    factual_lines: int
    require_states: bool

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "cited_ids": list(self.cited_ids),
            "factual_lines": self.factual_lines,
            "require_states": self.require_states,
        }


@dataclass(frozen=True, slots=True)
class QueryResult:
    question: str
    repository: RepositoryAnalysis
    answer: str
    ai_response: AIResponse
    attempts: int = 1


class UntraceableAnswerError(WorkflowError):
    """Raised when the model could not produce a fully cited answer."""

    def __init__(self, reason: str, answer: str, attempts: int) -> None:
        self.reason = reason
        self.answer = answer
        self.attempts = attempts
        super().__init__(reason)


def build_condensed_adr_context(
    records: tuple[ADRRecord, ...],
    *,
    annotations: Mapping[str, str] | None = None,
) -> str:
    """Serialize only validated metadata plus Context and Decision evidence.

    ``annotations`` maps an ADR ID to one extra line (for example freshness) that
    is placed right after the frontmatter so the model sees it beside the record.
    """

    documents: list[str] = []
    for record in records:
        annotation = (annotations or {}).get(record.metadata.id)
        context = _extract_markdown_section(record.document.body, "Context")
        decision = _extract_markdown_section(record.document.body, "Decision")
        if decision is None and record.metadata.status.value == "baseline":
            decision = _extract_markdown_section(record.document.body, "Baseline")
        if decision is None and record.metadata.status.value == "baseline":
            decision = _extract_markdown_section_prefix(
                record.document.body, "Baseline"
            )

        documents.append(
            "\n".join(
                (
                    "ADR DOCUMENT START",
                    _render_frontmatter(record),
                    *((annotation,) if annotation else ()),
                    "",
                    "## Context",
                    "",
                    context or "(not present)",
                    "",
                    "## Decision",
                    "",
                    decision or "(not present)",
                    "ADR DOCUMENT END",
                )
            )
        )
    return "\n\n".join(documents)


def parse_impact_response(
    stdout: str,
    *,
    known_adr_ids: frozenset[str],
) -> ImpactEvaluation:
    """Validate the complete impact JSON contract and all referenced ADR IDs."""

    raw = parse_json_object(stdout, allow_markdown_fence=True)
    expected_fields = {
        "directly_affected_adrs",
        "prerequisite_adrs",
        "architectural_risks",
        "proposed_new_adrs",
    }
    if set(raw) != expected_fields:
        raise EnvelopeError(
            "impact JSON root must contain exactly: "
            + ", ".join(sorted(expected_fields))
        )

    affected = _parse_references(
        raw["directly_affected_adrs"],
        field="directly_affected_adrs",
        reason_field="reason_for_change",
        known_adr_ids=known_adr_ids,
    )
    prerequisites = _parse_references(
        raw["prerequisite_adrs"],
        field="prerequisite_adrs",
        reason_field="reason_required",
        known_adr_ids=known_adr_ids,
    )
    risks = _parse_string_list(raw["architectural_risks"], "architectural_risks")
    proposed = _parse_string_list(raw["proposed_new_adrs"], "proposed_new_adrs")
    return ImpactEvaluation(affected, prerequisites, risks, proposed)


def run_impact(
    project_root: Path,
    prd_path: Path,
    *,
    visual: bool = False,
    save: bool = False,
) -> ImpactResult:
    """Evaluate one PRD against the current validated ADR repository."""

    repository = _load_intelligence_repository(project_root)
    resolved_prd, prd_text = _read_prd(project_root, prd_path)
    result = _evaluate_impact(
        project_root,
        repository,
        prd_path=resolved_prd,
        prd_text=prd_text,
        visual=visual,
    )
    if save:
        saved = save_impact_result(project_root, result)
        result = ImpactResult(
            result.prd_path,
            result.repository,
            result.evaluation,
            result.ai_response,
            result.mermaid,
            saved,
        )
    return result


def save_impact_result(project_root: Path, result: ImpactResult) -> Path:
    """Persist the latest impact evaluation for the browser overlay."""

    try:
        display_path = result.prd_path.relative_to(project_root)
    except ValueError:
        display_path = result.prd_path
    payload = {
        "schema_version": 1,
        "prd": str(display_path),
        "generated_at": now_iso(),
        "commit": current_commit(project_root),
        "directly_affected_adrs": [
            {"id": item.id, "reason_for_change": item.reason}
            for item in result.evaluation.directly_affected_adrs
        ],
        "prerequisite_adrs": [
            {"id": item.id, "reason_required": item.reason}
            for item in result.evaluation.prerequisite_adrs
        ],
        "architectural_risks": list(result.evaluation.architectural_risks),
        "proposed_new_adrs": list(result.evaluation.proposed_new_adrs),
        "mermaid": result.mermaid,
    }
    target = project_root / PROVENANCE_DIRECTORY / LAST_IMPACT_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise WorkflowError(f"could not save impact result to {target}: {exc}") from exc
    return target


def load_last_impact(project_root: Path) -> dict[str, Any] | None:
    target = state_file(project_root, LAST_IMPACT_FILENAME)
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"could not read {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkflowError(f"{target} must contain a JSON object")
    return raw


def build_context(project_root: Path) -> ContextResult:
    """Assemble the agent-facing view: validated ADRs plus provenance and freshness."""

    repository = _load_intelligence_repository(project_root)
    provenance, freshness = load_freshness(project_root, repository)
    documents: list[ContextDocument] = []
    for record in repository.unique_records:
        metadata = record.metadata
        body = record.document.body
        decision = _extract_markdown_section(body, "Decision")
        if decision is None and metadata.status.value == "baseline":
            decision = _extract_markdown_section(body, "Baseline")
        if decision is None and metadata.status.value == "baseline":
            decision = _extract_markdown_section_prefix(body, "Baseline")
        try:
            display_path = str(record.path.relative_to(project_root))
        except ValueError:
            display_path = str(record.path)
        documents.append(
            ContextDocument(
                id=metadata.id,
                title=metadata.title,
                status=metadata.status.value,
                domain=metadata.domain.value,
                date=metadata.date.isoformat(),
                path=display_path,
                depends_on=metadata.depends_on,
                supersedes=metadata.supersedes,
                context=_extract_markdown_section(body, "Context"),
                decision=decision,
                consequences=_extract_markdown_section(body, "Consequences"),
                provenance=provenance.get(metadata.id),
                freshness=freshness[metadata.id],
            )
        )
    return ContextResult(
        project_root, repository, tuple(documents), current_commit(project_root)
    )


def render_context_text(result: ContextResult, *, ids: frozenset[str] | None = None) -> str:
    """The same condensed bundle the prompts use, with freshness annotations."""

    documents = select_context_documents(result, ids)
    selected_ids = {document.id for document in documents}
    annotations = {
        document.id: f"FRESHNESS: {document.freshness.format()}" for document in documents
    }
    header = (
        f"DECINAV_CONTEXT project={result.project_root} "
        f"commit={result.commit or 'unknown'} adrs={len(documents)} "
        f"of={len(result.documents)}\n"
        "Claim states for answers: " + ", ".join(CLAIM_STATES) + "\n\n"
    )
    records = tuple(
        record
        for record in result.repository.unique_records
        if record.metadata.id in selected_ids
    )
    return header + build_condensed_adr_context(records, annotations=annotations)


def render_context_compact(result: ContextResult) -> str:
    """One line per ADR so an agent can choose what to load in full."""

    lines = [
        f"DECINAV_CONTEXT compact project={result.project_root} "
        f"commit={result.commit or 'unknown'} adrs={len(result.documents)}",
        "id | status | domain | freshness | title | decision (first sentence) | relationships",
    ]
    for document in result.documents:
        relationships = ", ".join(
            [f"depends_on {item.id}" for item in document.depends_on]
            + [f"supersedes {item.id}" for item in document.supersedes]
        ) or "-"
        lines.append(
            f"{document.id} | {document.status} | {document.domain} | "
            f"{document.freshness.state} | {document.title} | "
            f"{_first_sentence(document.decision)} | {relationships}"
        )
    lines.append(
        "Next: `decinav context --format json --ids ADR-0001,ADR-0002` for full text."
    )
    return "\n".join(lines) + "\n"


def select_context_documents(
    result: ContextResult, ids: frozenset[str] | None
) -> tuple[ContextDocument, ...]:
    if ids is None:
        return result.documents
    known = {document.id for document in result.documents}
    unknown = sorted(ids - known)
    if unknown:
        raise WorkflowError("unknown ADR ID(s): " + ", ".join(unknown))
    return tuple(document for document in result.documents if document.id in ids)


def parse_id_list(value: str) -> frozenset[str]:
    ids: set[str] = set()
    for raw in value.split(","):
        candidate = raw.strip().upper()
        if not candidate:
            continue
        if not re.fullmatch(r"ADR-\d{4}", candidate):
            raise WorkflowError(f"invalid ADR ID {raw.strip()!r}; expected ADR-0001 form")
        ids.add(candidate)
    if not ids:
        raise WorkflowError("--ids must list at least one ADR ID")
    return frozenset(ids)


def _first_sentence(text: str | None) -> str:
    if not text:
        return "(no decision section)"
    compact = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"^(.{20,}?[.!?])(?:\s|$)", compact)
    sentence = match.group(1) if match else compact
    return sentence if len(sentence) <= 220 else sentence[:217] + "..."


def load_freshness(
    project_root: Path, repository: RepositoryAnalysis
) -> tuple[dict[str, ProvenanceRecord], dict[str, Freshness]]:
    """Load provenance and compute freshness for every validated record."""

    try:
        provenance = load_provenance(project_root)
    except ProvenanceError as exc:
        raise WorkflowError(str(exc)) from exc
    calculator = FreshnessCalculator(project_root)
    freshness = {
        record.metadata.id: calculator.compute(provenance.get(record.metadata.id))
        for record in repository.unique_records
    }
    return provenance, freshness


def freshness_annotations(project_root: Path, repository: RepositoryAnalysis) -> dict[str, str]:
    _provenance, freshness = load_freshness(project_root, repository)
    return {adr_id: f"FRESHNESS: {item.format()}" for adr_id, item in freshness.items()}


def run_impact_text(
    project_root: Path,
    prd_text: str,
    *,
    prd_name: str = "pasted-prd.md",
    visual: bool = False,
) -> ImpactResult:
    """Evaluate PRD text supplied by a local UI without persisting an upload."""

    repository = _load_intelligence_repository(project_root)
    _validate_prd_text(prd_text, source=prd_name)
    clean_name = Path(prd_name).name.strip() or "pasted-prd.md"
    if Path(clean_name).suffix.casefold() != ".md":
        clean_name += ".md"
    return _evaluate_impact(
        project_root,
        repository,
        prd_path=Path(clean_name),
        prd_text=prd_text,
        visual=visual,
    )


def _evaluate_impact(
    project_root: Path,
    repository: RepositoryAnalysis,
    *,
    prd_path: Path,
    prd_text: str,
    visual: bool,
) -> ImpactResult:
    adr_context = build_condensed_adr_context(
        repository.unique_records,
        annotations=freshness_annotations(project_root, repository),
    )
    prompt = build_impact_prompt(adr_context=adr_context, prd_text=prd_text)
    response = execute_ai(prompt, cwd=project_root, purpose="impact")
    known_ids = frozenset(record.metadata.id for record in repository.unique_records)
    evaluation = parse_impact_response(response.stdout, known_adr_ids=known_ids)
    mermaid = None
    if visual:
        mermaid = generate_impact_mermaid(
            repository,
            directly_affected_ids=tuple(
                reference.id for reference in evaluation.directly_affected_adrs
            ),
            prerequisite_ids=tuple(
                reference.id for reference in evaluation.prerequisite_adrs
            ),
            proposed_titles=evaluation.proposed_new_adrs,
        )
    return ImpactResult(prd_path, repository, evaluation, response, mermaid)


def run_query(project_root: Path, question: str) -> QueryResult:
    """Answer one natural-language question from condensed ADR evidence."""

    clean_question = question.strip()
    if not clean_question:
        raise WorkflowError("query question must not be empty")
    if len(clean_question) > MAX_QUESTION_CHARACTERS:
        raise WorkflowError(
            f"query question exceeds the {MAX_QUESTION_CHARACTERS}-character limit"
        )

    repository = _load_intelligence_repository(project_root)
    adr_context = build_condensed_adr_context(
        repository.unique_records,
        annotations=freshness_annotations(project_root, repository),
    )
    prompt = build_query_prompt(adr_context=adr_context, question=clean_question)
    attempt = 0
    while True:
        attempt += 1
        response = execute_ai(prompt, cwd=project_root, purpose="query")
        answer = response.stdout.strip()
        try:
            _validate_query_citations(answer, repository)
        except WorkflowError as exc:
            if attempt >= QUERY_MAX_ATTEMPTS:
                raise UntraceableAnswerError(str(exc), answer, attempt) from exc
            prompt = _query_retry_prompt(prompt, answer, str(exc))
            continue
        return QueryResult(clean_question, repository, answer, response, attempt)


def _query_retry_prompt(original_prompt: str, rejected_answer: str, reason: str) -> str:
    """One corrective round: the same evidence, the rejected draft, and the rule broken."""

    return (
        original_prompt
        + "\nREJECTED DRAFT START\n"
        + rejected_answer
        + "\nREJECTED DRAFT END\n\n"
        "The draft above was rejected by a deterministic check: "
        + reason
        + ". Rewrite the complete answer so that every non-heading line that states a "
        "fact ends with at least one inline citation such as [ADR-0002] to an ADR in "
        "ADR CONTEXT, or ends with [UNKNOWN] when the ADRs do not establish it, with no "
        "preamble and no closing remark. Return only the answer.\n"
    )


def check_answer(
    answer: str,
    repository: RepositoryAnalysis,
    *,
    require_states: bool = False,
) -> AnswerCheck:
    """Deterministically check an agent-written answer for traceability.

    Every factual line must cite a known ADR inline. With ``require_states`` every
    factual line must also carry one claim state; a line tagged ``[UNKNOWN]`` is an
    abstention and is exempt from the citation requirement.
    """

    known_ids = {record.metadata.id for record in repository.unique_records}
    text = answer.replace("\r\n", "\n").replace("\r", "\n")
    problems: list[str] = []
    cited_ids = sorted(set(_ADR_CITATION_PATTERN.findall(text)))
    unknown_ids = sorted(set(cited_ids) - known_ids)
    if unknown_ids:
        problems.append("cites unknown ADR IDs: " + ", ".join(unknown_ids))

    factual_lines = 0
    uncited: list[int] = []
    unstated: list[int] = []
    inside_code_fence = False
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("```"):
            inside_code_fence = not inside_code_fence
            continue
        if (
            not line
            or inside_code_fence
            or _is_short_heading(line)
            or re.fullmatch(r"[-*_]{3,}", line)
            or re.fullmatch(r"\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?", line)
        ):
            continue
        factual_lines += 1
        states = _CLAIM_STATE_PATTERN.findall(line)
        has_citation = _ADR_CITATION_PATTERN.search(line) is not None
        if require_states:
            if len(states) != 1:
                unstated.append(line_number)
            if states != ["UNKNOWN"] and not has_citation:
                uncited.append(line_number)
        elif not has_citation:
            uncited.append(line_number)

    if factual_lines == 0:
        problems.append("answer contains no factual lines")
    elif not cited_ids and not (require_states and not uncited):
        problems.append("answer contains no inline ADR citations")
    if uncited:
        problems.append(
            "factual line(s) without inline ADR citations: " + _render_lines(uncited)
        )
    if unstated:
        problems.append(
            "factual line(s) without exactly one claim state "
            + "/".join(CLAIM_STATES)
            + ": "
            + _render_lines(unstated)
        )
    return AnswerCheck(tuple(problems), tuple(cited_ids), factual_lines, require_states)


def _is_short_heading(line: str) -> bool:
    """Structural headings are exempt; a heading long enough to be a claim is not."""

    if not line.startswith("#"):
        return False
    words = line.lstrip("#").strip().split()
    return len(words) < 8


def _render_lines(numbers: list[int]) -> str:
    rendered = ", ".join(str(number) for number in numbers[:10])
    return rendered + ("..." if len(numbers) > 10 else "")


def render_impact_markdown(result: ImpactResult, *, project_root: Path) -> str:
    """Render stable, terminal-friendly Markdown for an impact evaluation."""

    try:
        display_path = result.prd_path.relative_to(project_root)
    except ValueError:
        display_path = result.prd_path

    lines = [
        "# PRD Impact Analysis",
        "",
        f"**PRD:** `{display_path}`",
        "",
        "## Directly Affected ADRs",
        "",
    ]
    lines.extend(
        _render_reference_list(
            result.evaluation.directly_affected_adrs,
            empty="No directly affected ADRs identified.",
        )
    )
    lines.extend(("", "## Prerequisite ADRs", ""))
    lines.extend(
        _render_reference_list(
            result.evaluation.prerequisite_adrs,
            empty="No prerequisite ADRs identified.",
        )
    )
    lines.extend(("", "## Architectural Risks", ""))
    lines.extend(
        _render_string_list(
            result.evaluation.architectural_risks,
            empty="No contradictions identified.",
        )
    )
    lines.extend(("", "## Proposed New ADRs", ""))
    lines.extend(
        _render_string_list(
            result.evaluation.proposed_new_adrs,
            empty="No new ADRs proposed.",
        )
    )
    if result.mermaid is not None:
        lines.extend(
            (
                "",
                "## Visual Impact Graph",
                "",
                "```mermaid",
                result.mermaid.rstrip(),
                "```",
            )
        )
    return "\n".join(lines) + "\n"


def _load_intelligence_repository(project_root: Path) -> RepositoryAnalysis:
    repository = analyze_repository(project_root / "docs" / "adr")
    if repository.issues:
        raise RepositoryPreflightError(repository)
    if not repository.unique_records:
        raise WorkflowError("architecture intelligence requires at least one ADR")
    return repository


def _read_prd(project_root: Path, prd_path: Path) -> tuple[Path, str]:
    candidate = prd_path if prd_path.is_absolute() else project_root / prd_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"could not resolve PRD path {candidate}: {exc}") from exc
    if not resolved.is_file():
        raise WorkflowError(f"PRD path is not a file: {resolved}")
    if resolved.suffix.casefold() != ".md":
        raise WorkflowError("impact requires a Markdown PRD with a .md extension")
    try:
        size = resolved.stat().st_size
        if size > MAX_PRD_BYTES:
            raise WorkflowError(
                f"PRD exceeds the {MAX_PRD_BYTES}-byte safety limit: {resolved}"
            )
        text = resolved.read_text(encoding="utf-8")
    except WorkflowError:
        raise
    except (OSError, UnicodeError) as exc:
        raise WorkflowError(f"could not read PRD as UTF-8: {resolved}: {exc}") from exc
    _validate_prd_text(text, source=str(resolved))
    return resolved, text


def _validate_prd_text(text: str, *, source: str) -> None:
    if len(text.encode("utf-8")) > MAX_PRD_BYTES:
        raise WorkflowError(f"PRD exceeds the {MAX_PRD_BYTES}-byte safety limit: {source}")
    if not text.strip():
        raise WorkflowError(f"PRD is empty: {source}")


def _render_frontmatter(record: ADRRecord) -> str:
    metadata = record.metadata
    raw: dict[str, object] = {}
    if metadata.schema_version == 2:
        raw["schema_version"] = 2
    raw.update(
        {
            "id": metadata.id,
            "title": metadata.title,
            "status": metadata.status.value,
            "date": metadata.date.isoformat(),
            "domain": metadata.domain.value,
            "depends_on": _serialize_relationships(
                metadata.depends_on, schema_version=metadata.schema_version
            ),
            "supersedes": _serialize_relationships(
                metadata.supersedes, schema_version=metadata.schema_version
            ),
        }
    )
    yaml_text = yaml.safe_dump(
        raw,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    return f"---\n{yaml_text}\n---"


def _serialize_relationships(
    relationships: tuple[ADRRelationship, ...],
    *,
    schema_version: int,
) -> list[object]:
    if schema_version == 1:
        return [relationship.id for relationship in relationships]
    return [
        {"id": relationship.id, "reason": relationship.reason}
        for relationship in relationships
    ]


def _extract_markdown_section(body: str, title: str) -> str | None:
    matches = tuple(_SECTION_HEADING_PATTERN.finditer(body))
    for index, match in enumerate(matches):
        if match.group("title").strip().casefold() != title.casefold():
            continue
        start = match.end()
        source_level = len(match.group("marks"))
        end = len(body)
        for later in matches[index + 1 :]:
            later_title = later.group("title").strip().casefold()
            later_level = len(later.group("marks"))
            if later_level <= source_level or later_title.startswith("alternative"):
                end = later.start()
                break
        return body[start:end].strip()
    return None


def _extract_markdown_section_prefix(body: str, title_prefix: str) -> str | None:
    matches = tuple(_SECTION_HEADING_PATTERN.finditer(body))
    for match in matches:
        title = match.group("title").strip()
        if title.casefold().startswith(title_prefix.casefold()):
            return _extract_markdown_section(body, title)
    return None


def _parse_references(
    value: object,
    *,
    field: str,
    reason_field: str,
    known_adr_ids: frozenset[str],
) -> tuple[ImpactReference, ...]:
    if not isinstance(value, list):
        raise EnvelopeError(f"impact JSON field {field!r} must be an array")
    references: list[ImpactReference] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or set(item) != {"id", reason_field}:
            raise EnvelopeError(
                f"impact JSON {field} item {index} must contain exactly "
                f"'id' and {reason_field!r}"
            )
        adr_id = _required_single_line(item["id"], f"{field} item {index} id")
        reason = _required_single_line(
            item[reason_field], f"{field} item {index} {reason_field}"
        )
        if adr_id not in known_adr_ids:
            raise EnvelopeError(
                f"impact JSON {field} item {index} references unknown ADR {adr_id}"
            )
        if adr_id in seen_ids:
            raise EnvelopeError(f"impact JSON {field} contains duplicate ID {adr_id}")
        seen_ids.add(adr_id)
        references.append(ImpactReference(adr_id, reason))
    return tuple(references)


def _parse_string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EnvelopeError(f"impact JSON field {field!r} must be an array")
    results: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        result = _required_single_line(item, f"{field} item {index}")
        if result in seen:
            raise EnvelopeError(f"impact JSON {field} contains a duplicate value")
        seen.add(result)
        results.append(result)
    return tuple(results)


def _required_single_line(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeError(f"impact JSON {location} must be a non-empty string")
    if value != value.strip() or "\n" in value or "\r" in value:
        raise EnvelopeError(
            f"impact JSON {location} must be a trimmed single-line string"
        )
    return value


def _validate_query_citations(answer: str, repository: RepositoryAnalysis) -> None:
    known_ids = {record.metadata.id for record in repository.unique_records}
    cited_ids = set(_ADR_CITATION_PATTERN.findall(answer))
    if not cited_ids:
        raise WorkflowError("AI query answer contains no inline ADR citations")
    unknown_ids = sorted(cited_ids - known_ids)
    if unknown_ids:
        raise WorkflowError(
            "AI query answer cites unknown ADR IDs: " + ", ".join(unknown_ids)
        )
    uncited_lines: list[int] = []
    inside_code_fence = False
    for line_number, raw_line in enumerate(answer.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("```"):
            inside_code_fence = not inside_code_fence
            continue
        if (
            not line
            or inside_code_fence
            or _is_short_heading(line)
            or re.fullmatch(r"[-*_]{3,}", line)
            or re.fullmatch(r"\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?", line)
            or "[UNKNOWN]" in line
        ):
            continue
        if not _ADR_CITATION_PATTERN.search(line):
            uncited_lines.append(line_number)
    if uncited_lines:
        rendered = ", ".join(str(number) for number in uncited_lines[:10])
        suffix = "..." if len(uncited_lines) > 10 else ""
        raise WorkflowError(
            "AI query answer contains factual line(s) without inline ADR "
            f"citations: {rendered}{suffix}"
        )


def _render_reference_list(
    references: tuple[ImpactReference, ...],
    *,
    empty: str,
) -> list[str]:
    if not references:
        return [empty]
    return [f"- **{reference.id}** — {reference.reason}" for reference in references]


def _render_string_list(values: tuple[str, ...], *, empty: str) -> list[str]:
    if not values:
        return [empty]
    return [f"- {value}" for value in values]
