"""Typer command-line interface for decinav."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import json
from pathlib import Path
import sys
from typing import Annotated

import typer

from decinav.adr import ADRCreationError, EditorError, create_adr, open_in_editor
from decinav.config import ConfigError, choose_model, config_path, detect_providers, save_config
from decinav.ai_adapter import AIAdapterError
from decinav.frontmatter import ADRDomain, ADR_ID_PATTERN, ADRRelationship
from decinav.generation import (
    BatchWriteError,
    CandidateValidationError,
    EnvelopeError,
)
from decinav.graphing import GraphError, regenerate_graph
from decinav.hooks import HookError, initialize_project
from decinav.intelligence import (
    UntraceableAnswerError,
    build_context,
    check_answer,
    parse_id_list,
    render_context_compact,
    render_context_text,
    select_context_documents,
    render_impact_markdown,
    run_impact,
    run_query,
)
from decinav.prd import PRDError, create_prd, lint_prd
from decinav.provenance import ProvenanceError, record_generation
from decinav.repository import (
    ADRRepositoryError,
    ValidationIssue,
    analyze_repository,
    load_repository_records,
)
from decinav.review import ReviewError, accept_adrs, attest_adrs, reject_adr
from decinav.source_bundle import SourceBundleError
from decinav.workflows import (
    GenerationResult,
    PostWriteGraphError,
    PostWriteProvenanceError,
    RepositoryPreflightError,
    WorkflowError,
    run_baseline,
    run_ingest,
    run_propose,
)
from decinav.web_ui import UIError, serve_ui


app = typer.Typer(
    name="decinav",
    help="Treat Architecture Decision Records as executable data.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def main() -> None:
    """Manage Architecture Decision Records in the current project."""


@app.command("init")
def init_project(
    existing: Annotated[
        bool,
        typer.Option(
            "--existing",
            help="Warn instead of rejecting commits that lack an ADR reference.",
        ),
    ] = False,
    overwrite_hook: Annotated[
        bool,
        typer.Option(
            "--overwrite-hook",
            help="Back up and replace a foreign commit-msg hook.",
        ),
    ] = False,
    watch: Annotated[
        list[str] | None,
        typer.Option(
            "--watch",
            help="Top-level folder whose staged changes must cite an ADR; repeatable. "
            "Default: detected from the repository (src, app, lib, infra, ...).",
        ),
    ] = None,
) -> None:
    """Initialize docs/adr and install the local commit-msg policy."""

    try:
        result = initialize_project(
            Path.cwd(),
            existing_codebase=existing,
            overwrite_hook=overwrite_hook,
            watched_paths=tuple(watch) if watch else None,
        )
    except HookError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    readme = result.adr_dir / "README.md"
    if result.readme_created:
        typer.secho(
            f"Created {_display_path(readme, result.project_root)}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.echo(f"Preserved {_display_path(readme, result.project_root)}")
    if result.gitignore_updated:
        typer.secho(
            "Updated .gitignore so docs/adr Markdown remains trackable.",
            fg=typer.colors.GREEN,
        )

    hook = result.hook
    typer.secho(
        f"Hook {hook.action}: {hook.path} ({hook.mode} mode)",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        "Hook watches: " + ", ".join(f"{path}/" for path in hook.watched_paths)
        + " (change with --watch)"
    )
    if hook.backup_path is not None:
        typer.echo(f"Foreign hook backup: {hook.backup_path}")
    typer.echo(
        "Nothing was committed: docs/adr is a new untracked folder and the hook lives in "
        ".git. Remove both to undo. Next: 'decinav setup' to choose a model."
    )


@app.command("setup")
def setup(
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="claude, codex, command, or none. Default: first of claude/codex found on PATH.",
        ),
    ] = None,
    claude_model: Annotated[
        str | None,
        typer.Option("--claude-model", help="Claude model alias for the built-in runner (default sonnet)."),
    ] = None,
    command: Annotated[
        str | None,
        typer.Option("--command", help="Custom command template for --model command (same syntax as DECINAV_AI_CMD)."),
    ] = None,
) -> None:
    """Choose the local model once; saved to .decinav/config.json for every command and the UI."""

    project_root = Path.cwd()
    try:
        chosen = choose_model(model, claude_model=claude_model, command=command)
        path = save_config(project_root, chosen)
    except ConfigError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    detected = detect_providers()
    typer.echo("Found on PATH: " + (", ".join(detected) if detected else "no model CLI"))
    typer.secho(f"Model: {chosen.describe()}", fg=typer.colors.GREEN)
    typer.echo(f"Saved to {_display_path(path, project_root)}")
    if not chosen.configured:
        typer.echo(
            "Without a model, init, new, validate, graph, and ui still work. Install claude or "
            "codex and rerun 'decinav setup' to enable baseline, ingest, impact, and query."
        )
    else:
        typer.echo("The model runs isolated: empty directory, no tools, prompt on stdin.")


@app.command("new")
def new_adr(
    title: Annotated[str, typer.Argument(help="Short decision title.")],
    domain: Annotated[
        ADRDomain,
        typer.Option("--domain", help="Architecture domain for this decision."),
    ],
    depends_on: Annotated[
        list[str] | None,
        typer.Option(
            "--depends-on",
            help="Existing ADR dependency; repeat for multiple dependencies.",
        ),
    ] = None,
    supersedes: Annotated[
        list[str] | None,
        typer.Option(
            "--supersedes",
            help="Existing ADR replaced by this decision; repeat for multiple ADRs.",
        ),
    ] = None,
) -> None:
    """Create the next proposed ADR and open it in $EDITOR."""

    adr_dir = Path.cwd() / "docs" / "adr"

    def warn(message: str) -> None:
        typer.secho(f"Warning: {message}", fg=typer.colors.YELLOW, err=True)

    try:
        dependency_relationships = _prompt_relationships(
            depends_on or [], relationship_name="dependency"
        )
        supersession_relationships = _prompt_relationships(
            supersedes or [], relationship_name="supersession"
        )
        known_ids: frozenset[str] | None = None
        if (dependency_relationships or supersession_relationships) and adr_dir.is_dir():
            try:
                existing_records, _issues = load_repository_records(adr_dir)
            except ADRRepositoryError as exc:
                raise ADRCreationError(str(exc)) from exc
            known_ids = frozenset(record.metadata.id for record in existing_records)
        path = create_adr(
            adr_dir,
            title,
            domain,
            depends_on=dependency_relationships,
            supersedes=supersession_relationships,
            warn=warn,
            known_ids=known_ids,
        )
    except ADRCreationError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    try:
        record_generation(
            Path.cwd(),
            (f"ADR-{path.name[:4]}",),
            generated_by="new",
            generator="human",
            sources=(),
        )
    except ProvenanceError as exc:
        warn(f"ADR was created but provenance could not be recorded: {exc}")

    try:
        display_path = path.relative_to(Path.cwd())
    except ValueError:
        display_path = path
    typer.secho(f"Created {display_path}", fg=typer.colors.GREEN)

    try:
        opened = open_in_editor(path)
    except EditorError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if not opened:
        warn(f"$EDITOR is not set; ADR was created but not opened: {display_path}")


def _prompt_relationships(
    related_ids: list[str], *, relationship_name: str
) -> tuple[ADRRelationship, ...]:
    clean_ids: list[str] = []
    for raw_id in related_ids:
        related_id = raw_id.strip().upper()
        match = ADR_ID_PATTERN.fullmatch(related_id)
        if match is None or int(match.group(1)) == 0:
            raise ADRCreationError(
                f"{relationship_name} ID must match ADR-0001 through ADR-9999"
            )
        if related_id in clean_ids:
            raise ADRCreationError(
                f"duplicate {relationship_name} ID: {related_id}"
            )
        clean_ids.append(related_id)

    relationships: list[ADRRelationship] = []
    for related_id in clean_ids:
        while True:
            reason = typer.prompt(
                f"Reason for {relationship_name} relationship with {related_id}"
            ).strip()
            if reason:
                break
            typer.secho("Reason must not be empty.", fg=typer.colors.RED, err=True)
        relationships.append(ADRRelationship(related_id, reason))
    return tuple(relationships)


@app.command("graph")
def graph(
    focus: Annotated[
        str | None,
        typer.Option(
            "--focus",
            help="Render one ADR with its ancestor and descendant decisions.",
        ),
    ] = None,
    depth: Annotated[
        int,
        typer.Option(
            "--depth",
            min=0,
            help="Number of parent and child levels included with --focus.",
        ),
    ] = 1,
    domain: Annotated[
        ADRDomain | None,
        typer.Option(
            "--domain",
            help="Render only ADRs in one architecture domain.",
        ),
    ] = None,
) -> None:
    """Generate a forgiving global, focused, or domain Mermaid graph."""

    project_root = Path.cwd()
    adr_dir = project_root / "docs" / "adr"
    try:
        result = regenerate_graph(
            adr_dir,
            focus=focus,
            depth=depth,
            domain=domain,
        )
    except (ADRRepositoryError, GraphError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for issue in result.analysis.issues:
        typer.secho(
            f"Warning: {issue.format(relative_to=project_root)}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    outcome = "Updated" if result.changed else "Already current"
    typer.secho(f"{outcome}: docs/adr/README.md", fg=typer.colors.GREEN)


@app.command("validate")
def validate() -> None:
    """Strictly validate all ADR schema and graph integrity for CI."""

    project_root = Path.cwd()
    try:
        analysis = analyze_repository(project_root / "docs" / "adr")
    except ADRRepositoryError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if analysis.issues:
        typer.secho(
            f"Validation failed with {len(analysis.issues)} error(s):",
            fg=typer.colors.RED,
            err=True,
        )
        for issue in analysis.issues:
            typer.echo(f"- {issue.format(relative_to=project_root)}", err=True)
        raise typer.Exit(code=1)

    typer.secho(
        f"Validated {len(analysis.unique_records)} ADR(s).",
        fg=typer.colors.GREEN,
    )


@app.command("baseline")
def baseline(
    from_brief: Annotated[
        Path | None,
        typer.Option(
            "--from-brief",
            help=(
                "Derive an intended-architecture baseline from a product brief "
                "instead of structural repository files (new products)."
            ),
        ),
    ] = None,
) -> None:
    """Generate the Day 0 baseline from code structure or a product brief."""

    _run_generation(lambda: run_baseline(Path.cwd(), brief_path=from_brief))


@app.command("propose")
def propose(
    from_file: Annotated[
        Path,
        typer.Option(
            "--from-file",
            help="JSON envelope written by a coding agent: {\"records\": [...]}.",
        ),
    ],
) -> None:
    """Publish agent-authored ADRs through the strict validation gate."""

    project_root = Path.cwd()
    try:
        result = run_propose(project_root, from_file)
    except RepositoryPreflightError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.analysis.issues, project_root)
        raise typer.Exit(code=1) from exc
    except CandidateValidationError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.issues, project_root)
        raise typer.Exit(code=1) from exc
    except PostWriteGraphError as exc:
        for path in exc.written_paths:
            typer.secho(
                f"Created {_display_path(path, project_root)}", fg=typer.colors.GREEN
            )
        typer.secho(
            f"Error: ADR files were retained, but graph regeneration failed: {exc.reason}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except PostWriteProvenanceError as exc:
        _report_post_write_provenance_error(exc, project_root)
        raise typer.Exit(code=1) from exc
    except (
        EnvelopeError,
        BatchWriteError,
        WorkflowError,
        ADRRepositoryError,
        GraphError,
    ) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for path in result.written_paths:
        typer.secho(f"Created {_display_path(path, project_root)}", fg=typer.colors.GREEN)
    typer.echo(
        f"Recorded provenance: generator={result.generator}, "
        f"sources={len(result.sources)}. Status is proposed until 'decinav accept'."
    )
    graph_outcome = "Updated" if result.graph.changed else "Already current"
    typer.secho(f"{graph_outcome}: docs/adr/README.md", fg=typer.colors.GREEN)


@app.command("accept")
def accept(
    adr_ids: Annotated[list[str], typer.Argument(help="Proposed ADR IDs to accept.")],
) -> None:
    """Promote proposed ADRs to accepted after human review."""

    project_root = Path.cwd()
    result = _run_review(lambda: accept_adrs(project_root, tuple(adr_ids)), project_root)
    for adr_id, path in zip(result.adr_ids, result.paths):
        typer.secho(
            f"Accepted {adr_id}: {_display_path(path, project_root)}", fg=typer.colors.GREEN
        )
    _report_graph(result.graph)


@app.command("reject")
def reject(
    adr_id: Annotated[str, typer.Argument(help="Proposed ADR ID to delete.")],
) -> None:
    """Delete a proposed ADR that no other decision references."""

    project_root = Path.cwd()
    result = _run_review(lambda: reject_adr(project_root, adr_id), project_root)
    typer.secho(
        f"Rejected {result.adr_ids[0]}: removed {_display_path(result.paths[0], project_root)}",
        fg=typer.colors.GREEN,
    )
    _report_graph(result.graph)


@app.command("attest")
def attest(
    adr_ids: Annotated[list[str], typer.Argument(help="ADR IDs reviewed as current.")],
) -> None:
    """Record that a human reviewed these ADRs as current at the present commit."""

    project_root = Path.cwd()
    result = _run_review(lambda: attest_adrs(project_root, tuple(adr_ids)), project_root)
    for adr_id in result.adr_ids:
        typer.secho(f"Attested {adr_id} at the current commit.", fg=typer.colors.GREEN)


@app.command("context")
def context(
    output_format: Annotated[
        str,
        typer.Option("--format", help="text (prompt bundle) or json (agent payload)."),
    ] = "text",
    compact: Annotated[
        bool,
        typer.Option(
            "--compact",
            help="One line per ADR (id, status, domain, freshness, title, first decision "
            "sentence) so an agent can pick what to load in full.",
        ),
    ] = False,
    ids: Annotated[
        str | None,
        typer.Option("--ids", help="Comma-separated ADR IDs to include in full."),
    ] = None,
) -> None:
    """Print the validated ADR bundle with provenance and freshness for an agent."""

    if output_format not in {"text", "json"}:
        typer.secho("Error: --format must be text or json", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if compact and ids:
        typer.secho("Error: --compact and --ids cannot be combined", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    project_root = Path.cwd()
    try:
        selected_ids = parse_id_list(ids) if ids else None
        result = build_context(project_root)
        documents = select_context_documents(result, selected_ids)
    except RepositoryPreflightError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.analysis.issues, project_root)
        raise typer.Exit(code=1) from exc
    except (WorkflowError, ADRRepositoryError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if compact:
        typer.echo(render_context_compact(result), nl=False)
    elif output_format == "json":
        payload = result.to_json()
        payload["records"] = [document.to_json() for document in documents]
        payload["selected"] = len(documents)
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        typer.echo(render_context_text(result, ids=selected_ids), nl=False)


@app.command("check-answer")
def check_answer_command(
    answer_path: Annotated[
        Path | None,
        typer.Argument(help="File containing the answer; omit to read stdin."),
    ] = None,
    require_states: Annotated[
        bool,
        typer.Option(
            "--require-states",
            help="Every factual line must carry one claim state tag.",
        ),
    ] = False,
    output_format: Annotated[
        str, typer.Option("--format", help="text or json.")
    ] = "text",
) -> None:
    """Check an agent-written answer for inline citations and claim states."""

    if output_format not in {"text", "json"}:
        typer.secho("Error: --format must be text or json", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    project_root = Path.cwd()
    try:
        if answer_path is None:
            answer = sys.stdin.read()
        else:
            answer = answer_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        typer.secho(f"Error: could not read answer: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        analysis = analyze_repository(project_root / "docs" / "adr")
    except ADRRepositoryError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if analysis.issues:
        typer.secho(
            f"Error: existing ADR repository has {len(analysis.issues)} validation error(s)",
            fg=typer.colors.RED,
            err=True,
        )
        _print_validation_issues(analysis.issues, project_root)
        raise typer.Exit(code=1)
    result = check_answer(answer, analysis, require_states=require_states)
    if output_format == "json":
        typer.echo(json.dumps(result.to_json(), indent=2))
    elif result.ok:
        typer.secho(
            f"Answer OK: {result.factual_lines} factual line(s), cites "
            + (", ".join(result.cited_ids) or "nothing")
            + ".",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho("Answer rejected:", fg=typer.colors.RED, err=True)
        for problem in result.problems:
            typer.echo(f"- {problem}", err=True)
    if not result.ok:
        raise typer.Exit(code=1)


prd_app = typer.Typer(help="Create and lint grounded product requirement documents.")
app.add_typer(prd_app, name="prd")


@prd_app.command("new")
def prd_new(
    title: Annotated[str, typer.Argument(help="Feature or change title.")],
) -> None:
    """Create docs/prds/<slug>.md from the grounded PRD template."""

    project_root = Path.cwd()
    try:
        path = create_prd(project_root, title)
    except PRDError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Created {_display_path(path, project_root)}", fg=typer.colors.GREEN)


@prd_app.command("lint")
def prd_lint(
    prd_path: Annotated[Path, typer.Argument(help="PRD Markdown file to check.")],
) -> None:
    """Check PRD structure and that every referenced ADR exists."""

    project_root = Path.cwd()
    try:
        result = lint_prd(project_root, prd_path)
    except PRDError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if result.ok:
        typer.secho(
            f"PRD OK: {_display_path(result.path, project_root)} references "
            + (", ".join(result.referenced_ids) or "no ADRs")
            + ".",
            fg=typer.colors.GREEN,
        )
        return
    typer.secho(
        f"PRD lint failed with {len(result.issues)} issue(s):", fg=typer.colors.RED, err=True
    )
    for issue in result.issues:
        typer.echo(f"- {issue}", err=True)
    raise typer.Exit(code=1)


@app.command("ingest")
def ingest(
    folder_path: Annotated[
        Path,
        typer.Argument(help="Folder containing informal UTF-8 decision documents."),
    ],
) -> None:
    """Extract atomic ADRs from informal project documentation."""

    _run_generation(lambda: run_ingest(Path.cwd(), folder_path))


@app.command("impact")
def impact(
    prd_path: Annotated[
        Path,
        typer.Argument(help="Path to the proposed feature PRD in Markdown."),
    ],
    visual: Annotated[
        bool,
        typer.Option(
            "--visual",
            help="Append a color-coded Mermaid blast-radius graph.",
        ),
    ] = False,
    save: Annotated[
        bool,
        typer.Option(
            "--save",
            help="Also write .decinav/last-impact.json for the browser overlay.",
        ),
    ] = False,
) -> None:
    """Analyze a proposed PRD against current architecture decisions."""

    project_root = Path.cwd()
    try:
        result = run_impact(project_root, prd_path, visual=visual, save=save)
    except RepositoryPreflightError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.analysis.issues, project_root)
        raise typer.Exit(code=1) from exc
    except (
        AIAdapterError,
        EnvelopeError,
        WorkflowError,
        ADRRepositoryError,
        GraphError,
    ) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(render_impact_markdown(result, project_root=project_root), nl=False)
    if result.saved_path is not None:
        typer.secho(
            f"Saved {_display_path(result.saved_path, project_root)}",
            fg=typer.colors.GREEN,
            err=True,
        )


@app.command("query")
def query(
    question: Annotated[
        str,
        typer.Argument(help="Natural-language question about the architecture."),
    ],
) -> None:
    """Answer an architecture question with inline ADR citations."""

    project_root = Path.cwd()
    try:
        result = run_query(project_root, question)
    except RepositoryPreflightError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.analysis.issues, project_root)
        raise typer.Exit(code=1) from exc
    except UntraceableAnswerError as exc:
        typer.secho(
            f"Error: after {exc.attempts} attempt(s), {exc.reason}", fg=typer.colors.RED, err=True
        )
        typer.echo("Rejected answer, shown for inspection only (not validated):", err=True)
        typer.echo(exc.answer, err=True)
        raise typer.Exit(code=1) from exc
    except (AIAdapterError, WorkflowError, ADRRepositoryError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("# Architecture Query\n")
    typer.echo(f"**Question:** {result.question}\n")
    typer.echo("## Answer\n")
    typer.echo(result.answer)


@app.command("ui")
def ui(
    project: Annotated[
        Path,
        typer.Option(
            "--project",
            help="Repository initially loaded in the browser workbench.",
        ),
    ] = Path("."),
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=0,
            max=65535,
            help="Loopback TCP port; use 0 to select an available port.",
        ),
    ] = 8765,
    no_open: Annotated[
        bool,
        typer.Option(
            "--no-open",
            help="Start the server without opening the default browser.",
        ),
    ] = False,
) -> None:
    """Open the local browser workbench; no project CLI commands required."""

    try:
        serve_ui(project, port=port, open_browser=not no_open)
    except (OSError, UIError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _run_review(operation: Callable[[], object], project_root: Path):
    try:
        return operation()
    except RepositoryPreflightError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.analysis.issues, project_root)
        raise typer.Exit(code=1) from exc
    except (ReviewError, ADRRepositoryError, GraphError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _report_graph(graph) -> None:
    if graph is None:
        return
    for issue in graph.analysis.issues:
        typer.secho(f"Warning: {issue.format(relative_to=Path.cwd())}", fg=typer.colors.YELLOW, err=True)
    outcome = "Updated" if graph.changed else "Already current"
    typer.secho(f"{outcome}: docs/adr/README.md", fg=typer.colors.GREEN)


def _run_generation(operation: Callable[[], GenerationResult]) -> None:
    project_root = Path.cwd()
    try:
        result = operation()
    except RepositoryPreflightError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.analysis.issues, project_root)
        raise typer.Exit(code=1) from exc
    except CandidateValidationError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        _print_validation_issues(exc.issues, project_root)
        raise typer.Exit(code=1) from exc
    except PostWriteGraphError as exc:
        for path in exc.written_paths:
            typer.secho(
                f"Created {_display_path(path, project_root)}", fg=typer.colors.GREEN
            )
        typer.secho(
            "Error: ADR files were retained, but graph regeneration failed: "
            f"{exc.reason}",
            fg=typer.colors.RED,
            err=True,
        )
        typer.echo("Run 'decinav graph' after resolving the README or filesystem issue.")
        raise typer.Exit(code=1) from exc
    except PostWriteProvenanceError as exc:
        _report_post_write_provenance_error(exc, project_root)
        raise typer.Exit(code=1) from exc
    except (
        AIAdapterError,
        EnvelopeError,
        BatchWriteError,
        SourceBundleError,
        WorkflowError,
        ADRRepositoryError,
        GraphError,
    ) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _report_generation_result(result, project_root)


def _report_post_write_provenance_error(
    exc: PostWriteProvenanceError, project_root: Path
) -> None:
    for path in exc.written_paths:
        typer.secho(f"Created {_display_path(path, project_root)}", fg=typer.colors.GREEN)
    typer.secho("Updated: docs/adr/README.md", fg=typer.colors.GREEN)
    typer.secho(f"Error: {exc.reason}", fg=typer.colors.RED, err=True)


def _report_generation_result(result: GenerationResult, project_root: Path) -> None:
    for warning in result.bundle.warnings:
        typer.secho(
            f"Warning: {warning.format(relative_to=result.bundle.root)}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(
        f"Bundled {len(result.bundle.files)} source file(s), "
        f"{result.bundle.size_bytes} bytes; AI transport: {result.ai_response.transport}."
    )
    for path in result.written_paths:
        typer.secho(
            f"Created {_display_path(path, project_root)}", fg=typer.colors.GREEN
        )
    for issue in result.graph.analysis.issues:
        typer.secho(
            f"Warning: {issue.format(relative_to=project_root)}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    graph_outcome = "Updated" if result.graph.changed else "Already current"
    typer.secho(f"{graph_outcome}: docs/adr/README.md", fg=typer.colors.GREEN)


def _print_validation_issues(
    issues: Iterable[ValidationIssue], project_root: Path
) -> None:
    for issue in issues:
        typer.echo(f"- {issue.format(relative_to=project_root)}", err=True)


def _display_path(path: Path, relative_to: Path) -> Path:
    try:
        return path.relative_to(relative_to)
    except ValueError:
        return path


if __name__ == "__main__":
    app()
