import os
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPOSITORY_ROOT / "scripts" / "run_e2e.py"


def runner_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source_root = REPOSITORY_ROOT / "src"
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root) if not existing else str(source_root) + os.pathsep + existing
    )
    return environment


def test_brownfield_runner_executes_complete_deterministic_workflow() -> None:
    result = subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=REPOSITORY_ROOT,
        env=runner_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Security filter PASS" in result.stdout
    assert "Created docs/adr/0001-baseline.md" in result.stdout
    assert "Created docs/adr/0002-use-scheduled-import-polling.md" in result.stdout
    assert "Created docs/adr/0003-adopt-event-driven-imports.md" in result.stdout
    assert "Validated 3 ADR(s)." in result.stdout
    assert "E2E PASS" in result.stdout
    assert "Temporary workspace removed" in result.stdout
    assert "decinav warning" in result.stderr


def test_brownfield_runner_documents_keep_mode() -> None:
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        cwd=REPOSITORY_ROOT,
        env=runner_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "--keep" in result.stdout
    assert "retain the generated temporary repository" in result.stdout
