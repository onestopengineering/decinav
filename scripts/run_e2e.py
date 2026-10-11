#!/usr/bin/env python3
"""Run decinav's deterministic brownfield onboarding scenario."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "legacy-monolith"
MOCK_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "mock-responses"
sys.path.insert(0, str(SOURCE_ROOT))

from decinav.repository import analyze_repository  # noqa: E402
from decinav.source_bundle import build_baseline_bundle  # noqa: E402


class E2EError(RuntimeError):
    """Raised when the deterministic brownfield scenario violates an expectation."""


def run_e2e(temporary_root: Path) -> Path:
    workspace = temporary_root / "legacy-monolith"
    shutil.copytree(FIXTURE_ROOT, workspace)
    print(f"Fixture workspace: {workspace}")

    _verify_baseline_security_filter(workspace)
    environment = _command_environment()
    decinav = [sys.executable, "-m", "decinav"]

    _run(["git", "init", "-q"], cwd=workspace, env=environment)
    _run([*decinav, "init", "--existing"], cwd=workspace, env=environment)

    baseline_environment = {
        **environment,
        "DECINAV_AI_CMD": "mock",
        "DECINAV_AI_MOCK_RESPONSE": str(MOCK_ROOT / "baseline.json"),
    }
    _run([*decinav, "baseline"], cwd=workspace, env=baseline_environment)

    ingest_environment = {
        **environment,
        "DECINAV_AI_CMD": "mock",
        "DECINAV_AI_MOCK_RESPONSE": str(MOCK_ROOT / "ingest.json"),
    }
    _run(
        [*decinav, "ingest", "informal-docs"],
        cwd=workspace,
        env=ingest_environment,
    )
    _run([*decinav, "graph"], cwd=workspace, env=environment)
    _run([*decinav, "validate"], cwd=workspace, env=environment)

    _verify_generated_repository(workspace)
    _verify_warning_hook(workspace, environment)
    print("E2E PASS: brownfield baseline, ingest, graph, validation, and hook flow")
    return workspace


def _verify_baseline_security_filter(workspace: Path) -> None:
    bundle = build_baseline_bundle(workspace)
    included = {source.relative_path for source in bundle.files}
    required = {
        "deploy/api-deployment.yaml",
        "docker-compose.yml",
        "infra/main.tf",
        "package.json",
        "services/catalog/pom.xml",
        "services/worker/requirements.txt",
    }
    missing = required - included
    if missing:
        raise E2EError("baseline context omitted structural files: " + ", ".join(missing))

    forbidden_paths = {
        ".env.production",
        "deploy/api-secret.yaml",
        "package-lock.json",
        "public/index.php",
        "services/catalog/src/main/java/com/example/App.java",
        "services/worker/worker.py",
        "src/server.py",
    }
    leaked_paths = forbidden_paths & included
    if leaked_paths:
        raise E2EError("baseline context included forbidden files: " + ", ".join(leaked_paths))

    forbidden_content = {
        "E2E_SECRET_ENV_MUST_NOT_ENTER_CONTEXT",
        "E2E_SECRET_API_KEY_MUST_NOT_ENTER_CONTEXT",
        "E2E_KUBERNETES_SECRET_MUST_NOT_ENTER_CONTEXT",
        "E2E_JAVA_SOURCE_MUST_NOT_ENTER_CONTEXT",
        "E2E_PYTHON_SOURCE_MUST_NOT_ENTER_CONTEXT",
        "E2E_PHP_SOURCE_MUST_NOT_ENTER_CONTEXT",
        "E2E_ROOT_PYTHON_SOURCE_MUST_NOT_ENTER_CONTEXT",
        "this-lockfile-must-not-enter-the-baseline-context",
    }
    leaked_content = sorted(value for value in forbidden_content if value in bundle.text)
    if leaked_content:
        raise E2EError("baseline context leaked protected content: " + ", ".join(leaked_content))
    if not any("Secret kind" in warning.reason for warning in bundle.warnings):
        raise E2EError("Kubernetes Secret exclusion did not produce an audit warning")
    print(
        f"Security filter PASS: {len(bundle.files)} structural files, "
        f"{bundle.size_bytes} context bytes"
    )


def _verify_generated_repository(workspace: Path) -> None:
    adr_dir = workspace / "docs" / "adr"
    generated = sorted(
        path.name for path in adr_dir.glob("*.md") if path.name != "README.md"
    )
    expected = [
        "0001-baseline.md",
        "0002-use-scheduled-import-polling.md",
        "0003-adopt-event-driven-imports.md",
    ]
    if generated != expected:
        raise E2EError(f"unexpected generated ADR set: {generated}")

    analysis = analyze_repository(adr_dir)
    if analysis.issues:
        rendered = "; ".join(issue.format(relative_to=workspace) for issue in analysis.issues)
        raise E2EError("generated repository is invalid: " + rendered)

    readme = (adr_dir / "README.md").read_text(encoding="utf-8")
    required_edges = {
        (
            'ADR_0001 -->|"The polling decision extends the recorded '
            'legacy-system baseline."| ADR_0002'
        ),
        (
            'ADR_0001 -->|"The event-driven design changes the import flow '
            'within the baseline system."| ADR_0003'
        ),
        (
            'ADR_0002 -.->|"Superseded by: Events remove the latency and '
            'repeated scans caused by scheduled polling."| ADR_0003'
        ),
    }
    missing_edges = sorted(edge for edge in required_edges if edge not in readme)
    if missing_edges:
        raise E2EError("generated graph is missing edges: " + ", ".join(missing_edges))


def _verify_warning_hook(workspace: Path, environment: dict[str, str]) -> None:
    _run(
        ["git", "add", "src/server.py"],
        cwd=workspace,
        env=environment,
    )
    message_path = workspace / "COMMIT_MESSAGE"
    message_path.write_text("Modify legacy worker without ADR token\n", encoding="utf-8")
    hooks_result = _run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=workspace,
        env=environment,
    )
    hooks_path = Path(hooks_result.stdout.strip())
    if not hooks_path.is_absolute():
        hooks_path = workspace / hooks_path
    hook_result = _run(
        [str(hooks_path.resolve() / "commit-msg"), str(message_path)],
        cwd=workspace,
        env=environment,
    )
    if "decinav warning" not in hook_result.stderr:
        raise E2EError("warning-only commit-msg hook did not emit its warning")


def _command_environment() -> dict[str, str]:
    environment = dict(os.environ)
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(SOURCE_ROOT)
        if not existing_python_path
        else str(SOURCE_ROOT) + os.pathsep + existing_python_path
    )
    environment["EDITOR"] = "true"
    return environment


def _run(
    command: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    print(f"$ {shlex.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    if result.returncode != 0:
        raise E2EError(
            f"command exited with status {result.returncode}: {shlex.join(command)}"
        )
    return result


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run decinav's deterministic brownfield end-to-end scenario."
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="retain the generated temporary repository for manual inspection",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    if arguments.keep:
        temporary_root = Path(tempfile.mkdtemp(prefix="decinav-e2e-"))
        try:
            workspace = run_e2e(temporary_root)
        except Exception:
            print(f"Failed workspace retained at: {temporary_root}", file=sys.stderr)
            raise
        print(f"Retained workspace: {workspace}")
        return 0

    with tempfile.TemporaryDirectory(prefix="decinav-e2e-") as temporary:
        run_e2e(Path(temporary))
    print("Temporary workspace removed. Re-run with --keep to inspect it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
