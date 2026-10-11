"""Loopback-only browser UI over decinav's existing workflows."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser

from decinav.adr import ADRCreationError
from decinav.config import (
    PROVIDER_LABELS,
    SUGGESTED_MODELS,
    ConfigError,
    ModelConfig,
    choose_model,
    detect_providers,
    load_config,
    save_config,
)
from decinav.ai_adapter import AIAdapterError, execute_with_config
from decinav.generation import (
    BatchWriteError,
    CandidateValidationError,
    EnvelopeError,
)
from decinav.graphing import GraphError, generate_mermaid, regenerate_graph
from decinav.hooks import HookError, initialize_project
from decinav.intelligence import (
    load_freshness,
    load_last_impact,
    run_impact_text,
    run_query,
)
from decinav.provenance import (
    PROVENANCE_DIRECTORY,
    FreshnessCalculator,
    ProvenanceError,
    load_provenance,
    state_file,
)
from decinav.repository import (
    ADRRepositoryError,
    RepositoryAnalysis,
    ValidationIssue,
    analyze_repository,
)
from decinav.source_bundle import SourceBundleError
from decinav.workflows import (
    PostWriteGraphError,
    RepositoryPreflightError,
    WorkflowError,
    run_baseline,
    run_ingest,
)


MAX_REQUEST_BYTES = 2 * 1024 * 1024
UI_DIRECTORY = Path(__file__).with_name("ui")
UI_ASSET = UI_DIRECTORY / "index.html"
# Only these packaged files are ever served; anything else under /static/ is 404.
STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "vendor/dagre.min.js": ("vendor/dagre.min.js", "text/javascript; charset=utf-8"),
}
USER_ERRORS = (
    ADRCreationError,
    AIAdapterError,
    ADRRepositoryError,
    BatchWriteError,
    CandidateValidationError,
    ConfigError,
    EnvelopeError,
    GraphError,
    HookError,
    PostWriteGraphError,
    ProvenanceError,
    RepositoryPreflightError,
    SourceBundleError,
    WorkflowError,
)


class UIError(RuntimeError):
    """Raised for malformed or unsafe browser requests."""


HANDLED_UI_ERRORS = (UIError, *USER_ERRORS)


@dataclass(frozen=True, slots=True)
class UIServerInfo:
    server: ThreadingHTTPServer
    url: str
    token: str


class ArchitectureUIService:
    """Translate local UI actions into the already-tested domain workflows."""

    def __init__(self, default_project: Path) -> None:
        self.default_project = default_project.resolve()

    def status(self, project_value: object = None) -> dict[str, Any]:
        project = self.resolve_project(project_value)
        adr_dir = project / "docs" / "adr"
        base = {
            "project": str(project),
            "project_name": project.name,
            **_model_status(project),
        }
        if not adr_dir.is_dir():
            return {
                **base,
                "initialized": False,
                "valid": False,
                "adr_count": 0,
                "issues": [],
                "records": [],
                "mermaid": "flowchart TD\n  empty[\"Initialize ADRs to see the architecture\"]\n",
                "graph": {"nodes": [], "edges": []},
            }

        analysis = analyze_repository(adr_dir)
        provenance, freshness = load_freshness(project, analysis)
        return {
            **base,
            "initialized": True,
            "valid": not analysis.issues,
            "adr_count": len(analysis.unique_records),
            "issues": [
                _serialize_issue(issue, relative_to=project) for issue in analysis.issues
            ],
            "records": [
                {
                    "id": record.metadata.id,
                    "title": record.metadata.title,
                    "status": record.metadata.status.value,
                    "domain": record.metadata.domain.value,
                    "date": record.metadata.date.isoformat(),
                    "filename": record.path.name,
                    "depends_on": [
                        {"id": relation.id, "reason": relation.reason}
                        for relation in record.metadata.depends_on
                    ],
                    "supersedes": [
                        {"id": relation.id, "reason": relation.reason}
                        for relation in record.metadata.supersedes
                    ],
                    "provenance": (
                        provenance[record.metadata.id].to_json()
                        if record.metadata.id in provenance
                        else None
                    ),
                    "freshness": freshness[record.metadata.id].to_json(),
                }
                for record in analysis.unique_records
            ],
            "mermaid": generate_mermaid(analysis, clickable=False),
            "graph": _serialize_graph(analysis),
            "last_impact_available": state_file(project, "last-impact.json").is_file(),
        }

    def adr_detail(self, project_value: object, adr_id_value: object) -> dict[str, Any]:
        """Full decision text plus relationships in both directions."""

        project = self.resolve_project(project_value)
        if not isinstance(adr_id_value, str) or not adr_id_value.strip():
            raise UIError("query parameter 'id' must be an ADR ID")
        adr_id = adr_id_value.strip().upper()
        analysis = analyze_repository(project / "docs" / "adr")
        by_id = {record.metadata.id: record for record in analysis.unique_records}
        record = by_id.get(adr_id)
        if record is None:
            raise UIError(f"unknown or ambiguous ADR ID: {adr_id}")
        provenance = load_provenance(project)
        freshness = {adr_id: FreshnessCalculator(project).compute(provenance.get(adr_id))}
        dependents = [
            {"id": other.metadata.id, "type": "depends_on", "reason": relation.reason}
            for other in analysis.unique_records
            for relation in other.metadata.depends_on
            if relation.id == adr_id
        ] + [
            {"id": other.metadata.id, "type": "supersedes", "reason": relation.reason}
            for other in analysis.unique_records
            for relation in other.metadata.supersedes
            if relation.id == adr_id
        ]
        try:
            display_path = str(record.path.relative_to(project))
        except ValueError:
            display_path = str(record.path)
        return {
            "id": adr_id,
            "title": record.metadata.title,
            "status": record.metadata.status.value,
            "domain": record.metadata.domain.value,
            "date": record.metadata.date.isoformat(),
            "path": display_path,
            "body": record.document.body,
            "depends_on": [
                {"id": relation.id, "reason": relation.reason}
                for relation in record.metadata.depends_on
            ],
            "supersedes": [
                {"id": relation.id, "reason": relation.reason}
                for relation in record.metadata.supersedes
            ],
            "referenced_by": dependents,
            "provenance": provenance[adr_id].to_json() if adr_id in provenance else None,
            "freshness": freshness[adr_id].to_json(),
        }

    def last_impact(self, project_value: object) -> dict[str, Any]:
        project = self.resolve_project(project_value)
        payload = load_last_impact(project)
        if payload is None:
            raise UIError(
                "no saved impact result; run 'decinav impact <prd> --save' or the "
                "/decinav-impact skill first"
            )
        analysis = analyze_repository(project / "docs" / "adr")
        affected = {
            item.get("id") for item in payload.get("directly_affected_adrs", [])
            if isinstance(item, dict)
        }
        prerequisites = {
            item.get("id") for item in payload.get("prerequisite_adrs", [])
            if isinstance(item, dict)
        }
        proposed = tuple(
            str(title) for title in payload.get("proposed_new_adrs", []) if isinstance(title, str)
        )
        return {
            **payload,
            "graph": _serialize_graph(
                analysis,
                affected_ids={value for value in affected if isinstance(value, str)},
                prerequisite_ids={value for value in prerequisites if isinstance(value, str)},
                proposed_titles=proposed,
            ),
        }

    def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        project = self.resolve_project(payload.get("project"))
        if action == "setup":
            chosen = _candidate_model(payload)
            save_config(project, chosen)
            return {
                "message": f"Model saved: {chosen.describe()}.",
                "status": self.status(project),
            }
        if action == "test_model":
            chosen = _candidate_model(payload)
            if not chosen.configured:
                raise UIError("choose Claude CLI, Codex CLI, or a custom command to test")
            started = time.monotonic()
            response = execute_with_config(
                chosen, "Reply with exactly the word OK and nothing else.", cwd=project, timeout=120
            )
            seconds = round(time.monotonic() - started, 1)
            reply = " ".join(response.stdout.split())[:120]
            ok = "OK" in reply.upper()
            return {
                "message": f"{chosen.describe()} replied in {seconds} s." if ok else f"{chosen.describe()} replied, but not with OK.",
                "ok": ok,
                "seconds": seconds,
                "reply": reply,
            }
        if action == "init":
            result = initialize_project(
                project,
                existing_codebase=True,
                overwrite_hook=False,
            )
            return {
                "message": (
                    f"ADR repository ready; commit hook {result.hook.action} "
                    f"in {result.hook.mode} mode."
                    + (
                        " Updated .gitignore so ADRs remain trackable."
                        if result.gitignore_updated
                        else ""
                    )
                ),
                "status": self.status(result.project_root),
            }
        if action == "baseline":
            result = run_baseline(project)
            return {
                "message": f"Created {len(result.written_paths)} baseline ADR.",
                "files": [str(path.relative_to(project)) for path in result.written_paths],
                "warnings": [
                    warning.format(relative_to=result.bundle.root)
                    for warning in result.bundle.warnings
                ],
                "status": self.status(project),
            }
        if action == "ingest":
            source = self._resolve_source_folder(project, payload.get("folder"))
            result = run_ingest(project, source)
            return {
                "message": f"Created {len(result.written_paths)} ADRs from documentation.",
                "files": [str(path.relative_to(project)) for path in result.written_paths],
                "warnings": [
                    warning.format(relative_to=result.bundle.root)
                    for warning in result.bundle.warnings
                ],
                "status": self.status(project),
            }
        if action == "validate":
            status = self.status(project)
            message = (
                f"Validated {status['adr_count']} ADRs."
                if status["valid"]
                else f"Found {len(status['issues'])} validation errors."
            )
            return {"message": message, "status": status}
        if action == "graph":
            result = regenerate_graph(project / "docs" / "adr")
            return {
                "message": "Updated docs/adr/README.md." if result.changed else "Graph is already current.",
                "status": self.status(project),
            }
        if action == "impact":
            prd_text = _required_string(payload, "prd_text")
            prd_name_value = payload.get("prd_name", "pasted-prd.md")
            if not isinstance(prd_name_value, str):
                raise UIError("field 'prd_name' must be a string")
            result = run_impact_text(
                project,
                prd_text,
                prd_name=prd_name_value,
                visual=True,
            )
            evaluation = result.evaluation
            return {
                "message": "Impact analysis complete.",
                "directly_affected_adrs": [
                    {"id": item.id, "reason_for_change": item.reason}
                    for item in evaluation.directly_affected_adrs
                ],
                "prerequisite_adrs": [
                    {"id": item.id, "reason_required": item.reason}
                    for item in evaluation.prerequisite_adrs
                ],
                "architectural_risks": list(evaluation.architectural_risks),
                "proposed_new_adrs": list(evaluation.proposed_new_adrs),
                "mermaid": result.mermaid,
                "graph": _serialize_graph(
                    result.repository,
                    affected_ids={
                        item.id for item in evaluation.directly_affected_adrs
                    },
                    prerequisite_ids={
                        item.id for item in evaluation.prerequisite_adrs
                    },
                    proposed_titles=evaluation.proposed_new_adrs,
                ),
            }
        if action == "query":
            question = _required_string(payload, "question")
            result = run_query(project, question)
            return {
                "message": "Architecture answer ready.",
                "question": result.question,
                "answer": result.answer,
            }
        raise UIError(f"unknown UI action: {action}")

    def resolve_project(self, value: object) -> Path:
        if value is None or value == "":
            candidate = self.default_project
        elif isinstance(value, str):
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
        elif isinstance(value, Path):
            candidate = value.expanduser()
        else:
            raise UIError("field 'project' must be a filesystem path string")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise UIError(f"project directory does not exist: {candidate}") from exc
        if not resolved.is_dir():
            raise UIError(f"project path is not a directory: {resolved}")
        return resolved

    def _resolve_source_folder(self, project: Path, value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise UIError("field 'folder' must be a non-empty path")
        candidate = Path(value.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = project / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise UIError(f"documentation folder does not exist: {candidate}") from exc
        if not resolved.is_dir():
            raise UIError(f"documentation path is not a directory: {resolved}")
        return resolved


class _ArchUIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: ArchitectureUIService,
        token: str,
    ) -> None:
        self.service = service
        self.ui_token = token
        super().__init__(server_address, _ArchUIHandler)


class _ArchUIHandler(BaseHTTPRequestHandler):
    server: _ArchUIHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_index()
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path.removeprefix("/static/"))
            return
        if parsed.path in {"/api/status", "/api/adr", "/api/impact/last"}:
            if not self._authorized():
                return
            query = parse_qs(parsed.query)
            project = query.get("project", [None])[0]
            try:
                if parsed.path == "/api/status":
                    payload = self.server.service.status(project)
                elif parsed.path == "/api/adr":
                    payload = self.server.service.adr_detail(project, query.get("id", [None])[0])
                else:
                    payload = self.server.service.last_impact(project)
                self._send_json(HTTPStatus.OK, payload)
            except HANDLED_UI_ERRORS as exc:
                self._send_user_error(exc)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/") or parsed.path.count("/") != 2:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            return
        try:
            payload = self._read_json_body()
            action = parsed.path.removeprefix("/api/")
            result = self.server.service.dispatch(action, payload)
            self._send_json(HTTPStatus.OK, result)
        except HANDLED_UI_ERRORS as exc:
            self._send_user_error(exc)
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Unexpected local UI server error. Check the server terminal."},
            )
            raise

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        if secrets.compare_digest(
            self.headers.get("X-Decinav-UI-Token", ""), self.server.ui_token
        ):
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid local UI token."})
        return False

    def _serve_index(self) -> None:
        try:
            template = UI_ASSET.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Could not load packaged UI: {exc}"},
            )
            return
        config = json.dumps(
            {
                "token": self.server.ui_token,
                "defaultProject": str(self.server.service.default_project),
            }
        ).replace("<", "\\u003c")
        html = template.replace("__DECINAV_UI_CONFIG__", config)
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_static(self, name: str) -> None:
        """Serve one allowlisted packaged asset; the name is matched literally, never resolved."""

        asset = STATIC_ASSETS.get(name)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        relative_path, content_type = asset
        try:
            payload = (UI_DIRECTORY / relative_path).read_bytes()
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Could not load packaged UI asset {name}: {exc}"},
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise UIError("request Content-Type must be application/json")
        length_value = self.headers.get("Content-Length")
        try:
            length = int(length_value or "")
        except ValueError as exc:
            raise UIError("request Content-Length is missing or invalid") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise UIError(
                f"request body must be between 1 and {MAX_REQUEST_BYTES} bytes"
            )
        try:
            raw = json.loads(self.rfile.read(length))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UIError(f"request body must be valid UTF-8 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise UIError("request JSON root must be an object")
        return raw

    def _send_user_error(self, exc: Exception) -> None:
        payload: dict[str, Any] = {"error": str(exc)}
        rejected = getattr(exc, "answer", None)
        if isinstance(rejected, str):
            payload["rejected_answer"] = rejected
            payload["attempts"] = getattr(exc, "attempts", None)
        analysis = getattr(exc, "analysis", None)
        if analysis is not None:
            payload["issues"] = [
                _serialize_issue(issue, relative_to=analysis.adr_dir.parent.parent)
                for issue in analysis.issues
            ]
        issues = getattr(exc, "issues", None)
        if issues is not None:
            payload["issues"] = [
                _serialize_issue(issue, relative_to=Path.cwd()) for issue in issues
            ]
        self._send_json(HTTPStatus.BAD_REQUEST, payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)


def create_ui_server(
    default_project: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> UIServerInfo:
    """Create, but do not start, a loopback-only architecture UI server."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise UIError("decinav ui only binds to a loopback address")
    if port < 0 or port > 65535:
        raise UIError("UI port must be between 0 and 65535")
    service = ArchitectureUIService(default_project)
    token = secrets.token_urlsafe(32)
    server = _ArchUIHTTPServer((host, port), service, token)
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else bound_host
    url = f"http://{display_host}:{bound_port}/"
    return UIServerInfo(server, url, token)


def serve_ui(
    default_project: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the local UI until interrupted."""

    info = create_ui_server(default_project, host=host, port=port)
    print(f"decinav UI: {info.url}")
    print(f"Default project: {info.server.service.default_project}")
    if open_browser:
        webbrowser.open(info.url)
    try:
        info.server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        info.server.server_close()


def _model_status(project: Path) -> dict[str, Any]:
    override = os.environ.get("DECINAV_AI_CMD", "").strip()
    config: ModelConfig | None = None
    config_error: str | None = None
    try:
        config = load_config(project)
    except ConfigError as exc:
        config_error = str(exc)
    if override:
        description = "mock fixture" if override == "mock" else f"DECINAV_AI_CMD override: {override.split()[0]}"
        configured = True
        provider = "env"
    elif config is not None and config.configured:
        description = config.describe()
        configured = True
        provider = config.provider
    else:
        description = config_error or "not configured; run decinav setup or choose a model below"
        configured = False
        provider = "none"
    return {
        "ai_configured": configured,
        "model": {
            "provider": provider,
            "model": config.model if config else None,
            "command": config.command if config else None,
            "description": description,
            "available": list(detect_providers()),
            "suggestions": {name: list(values) for name, values in SUGGESTED_MODELS.items()},
            "labels": dict(PROVIDER_LABELS),
        },
    }


def _candidate_model(payload: dict[str, Any]) -> ModelConfig:
    provider = payload.get("provider")
    model = payload.get("model")
    command = payload.get("command")
    if not isinstance(provider, str):
        raise UIError("field 'provider' must be a string")
    for name, value in (("model", model), ("command", command)):
        if value is not None and not isinstance(value, str):
            raise UIError(f"field '{name}' must be a string")
    return choose_model(provider, model=model or None, command=command or None)


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise UIError(f"field {field!r} must be a non-empty string")
    return value


def _serialize_issue(issue: ValidationIssue, *, relative_to: Path) -> dict[str, str]:
    path = ""
    if issue.path is not None:
        try:
            path = str(issue.path.relative_to(relative_to))
        except ValueError:
            path = str(issue.path)
    return {"code": issue.code, "message": issue.message, "path": path}


def _serialize_graph(
    analysis: RepositoryAnalysis,
    *,
    affected_ids: set[str] | None = None,
    prerequisite_ids: set[str] | None = None,
    proposed_titles: tuple[str, ...] = (),
) -> dict[str, list[dict[str, Any]]]:
    affected = affected_ids or set()
    prerequisites = prerequisite_ids or set()
    nodes: list[dict[str, Any]] = []
    for record in analysis.unique_records:
        visual_class = "affected" if record.metadata.id in affected else None
        if visual_class is None and record.metadata.id in prerequisites:
            visual_class = "prerequisite"
        nodes.append(
            {
                "id": record.metadata.id,
                "title": record.metadata.title,
                "domain": record.metadata.domain.value,
                "status": record.metadata.status.value,
                "class": visual_class or record.metadata.domain.value,
            }
        )
    nodes.extend(
        {
            "id": f"PROPOSED-{index}",
            "title": title,
            "domain": "proposed",
            "status": "proposed",
            "class": "proposed",
        }
        for index, title in enumerate(proposed_titles, start=1)
    )

    known_ids = {node["id"] for node in nodes}
    edges: list[dict[str, Any]] = []
    for record in analysis.unique_records:
        target = record.metadata.id
        for relationship in record.metadata.depends_on:
            if relationship.id in known_ids:
                edges.append(
                    {
                        "source": relationship.id,
                        "target": target,
                        "type": "depends_on",
                        "reason": relationship.reason or "Dependency",
                    }
                )
        for relationship in record.metadata.supersedes:
            if relationship.id in known_ids:
                edges.append(
                    {
                        "source": relationship.id,
                        "target": target,
                        "type": "supersedes",
                        "reason": relationship.reason or "Superseded by later decision",
                    }
                )
    return {"nodes": nodes, "edges": edges}
