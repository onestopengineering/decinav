import os
from pathlib import Path
import stat
import subprocess

from typer.testing import CliRunner

from decinav.cli import app
from decinav.hooks import HOOK_MARKER


runner = CliRunner()


def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def initialize_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")


def run_hook(hook: Path, message: Path, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(hook), str(message)],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def test_init_scaffolds_project_and_strict_hook_enforces_staged_paths(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_git(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    readme = tmp_path / "docs" / "adr" / "README.md"
    hook = tmp_path / ".git" / "hooks" / "commit-msg"
    assert readme.read_text(encoding="utf-8") == ""
    assert HOOK_MARKER in hook.read_text(encoding="utf-8")
    assert hook.stat().st_mode & stat.S_IXUSR

    notes = tmp_path / "notes.md"
    notes.write_text("Documentation only\n", encoding="utf-8")
    git(tmp_path, "add", "notes.md")
    message = tmp_path / "COMMIT_MESSAGE"
    message.write_text("Update notes\n", encoding="utf-8")
    assert run_hook(hook, message, cwd=tmp_path).returncode == 0

    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('hello')\n", encoding="utf-8")
    git(tmp_path, "add", "src/app.py")

    message.write_text("Implement service\n", encoding="utf-8")
    rejected = run_hook(hook, message, cwd=tmp_path)
    assert rejected.returncode == 1
    assert "require [ADR-XXXX] or [skip-adr]" in rejected.stderr

    message.write_text("Implement service [ADR-0001]\n", encoding="utf-8")
    assert run_hook(hook, message, cwd=tmp_path).returncode == 0
    message.write_text("Emergency repair [skip-adr]\n", encoding="utf-8")
    assert run_hook(hook, message, cwd=tmp_path).returncode == 0


def test_existing_mode_warns_without_rejecting_commit(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_git(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--existing"])
    assert result.exit_code == 0, result.output

    infrastructure = tmp_path / "infra" / "main.tf"
    infrastructure.parent.mkdir()
    infrastructure.write_text("resource {}\n", encoding="utf-8")
    git(tmp_path, "add", "infra/main.tf")
    message = tmp_path / "COMMIT_MESSAGE"
    message.write_text("Legacy infrastructure change\n", encoding="utf-8")

    hook_result = run_hook(
        tmp_path / ".git" / "hooks" / "commit-msg", message, cwd=tmp_path
    )

    assert hook_result.returncode == 0
    assert "decinav warning" in hook_result.stderr


def test_init_refuses_foreign_hook_before_scaffolding(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_git(tmp_path)
    foreign_hook = tmp_path / ".git" / "hooks" / "commit-msg"
    foreign_hook.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    foreign_hook.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "refusing to overwrite foreign hook" in result.output
    assert "Manual chaining option" in result.output
    assert "--overwrite-hook" in result.output
    assert not (tmp_path / "docs").exists()
    assert foreign_hook.read_text(encoding="utf-8") == "#!/bin/sh\necho foreign\n"


def test_overwrite_hook_creates_timestamped_backup(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_git(tmp_path)
    foreign_hook = tmp_path / ".git" / "hooks" / "commit-msg"
    foreign_content = "#!/bin/sh\necho foreign\n"
    foreign_hook.write_text(foreign_content, encoding="utf-8")
    foreign_hook.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--overwrite-hook"])

    assert result.exit_code == 0, result.output
    backups = list(foreign_hook.parent.glob("commit-msg.backup-*") )
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == foreign_content
    assert HOOK_MARKER in foreign_hook.read_text(encoding="utf-8")
    assert "Foreign hook backup:" in result.output


def test_init_is_idempotent_for_managed_hook_and_can_change_mode(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_git(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0

    unchanged = runner.invoke(app, ["init"])
    changed = runner.invoke(app, ["init", "--existing"])

    assert unchanged.exit_code == 0
    assert "Hook unchanged" in unchanged.output
    assert changed.exit_code == 0
    assert "Hook updated" in changed.output
    hook_content = (tmp_path / ".git" / "hooks" / "commit-msg").read_text(
        encoding="utf-8"
    )
    assert "# mode: warn" in hook_content


def test_init_respects_custom_core_hooks_path(tmp_path: Path, monkeypatch) -> None:
    initialize_git(tmp_path)
    git(tmp_path, "config", "core.hooksPath", ".project-hooks")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    custom_hook = tmp_path / ".project-hooks" / "commit-msg"
    assert custom_hook.is_file()
    assert os.access(custom_hook, os.X_OK)
    assert not (tmp_path / ".git" / "hooks" / "commit-msg").exists()


def test_init_keeps_adr_markdown_trackable_when_docs_is_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    initialize_git(tmp_path)
    (tmp_path / ".gitignore").write_text("/docs\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--existing"])

    assert result.exit_code == 0, result.output
    assert "Updated .gitignore" in result.output
    ignored = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "check-ignore",
            "--quiet",
            "--no-index",
            "docs/adr/README.md",
        ],
        check=False,
    )
    assert ignored.returncode == 1
    status = git(tmp_path, "status", "--short", "--untracked-files=all").stdout
    assert "docs/adr/README.md" in status
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count("# decinav:adr-tracking:start") == 1

    second = runner.invoke(app, ["init", "--existing"])

    assert second.exit_code == 0
    assert "Updated .gitignore" not in second.output
    assert (
        tmp_path / ".gitignore"
    ).read_text(encoding="utf-8").count("# decinav:adr-tracking:start") == 1


def test_init_resolves_linked_git_worktree_paths(tmp_path: Path, monkeypatch) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    initialize_git(primary)
    tracked = primary / "tracked.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    git(primary, "add", "tracked.txt")
    git(
        primary,
        "-c",
        "user.name=decinav tests",
        "-c",
        "user.email=decinav@example.invalid",
        "commit",
        "-q",
        "-m",
        "Initial commit",
    )
    git(primary, "worktree", "add", "-q", "-b", "linked-test", str(linked))
    nested = linked / "packages" / "service"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (linked / "docs" / "adr" / "README.md").is_file()
    hooks_path = Path(git(linked, "rev-parse", "--git-path", "hooks").stdout.strip())
    if not hooks_path.is_absolute():
        hooks_path = linked / hooks_path
    assert (hooks_path.resolve() / "commit-msg").is_file()


def test_init_detects_code_folders_and_accepts_watch_override(tmp_path: Path, monkeypatch) -> None:
    import subprocess as _sp

    _sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "database").mkdir()
    monkeypatch.chdir(tmp_path)

    detected = runner.invoke(app, ["init", "--existing"])
    assert detected.exit_code == 0, detected.output
    assert "Hook watches: app/" in detected.output
    hook = (tmp_path / ".git" / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert "grep -Eq '^(app)/'" in hook
    assert "# paths: app" in hook

    overridden = runner.invoke(app, ["init", "--existing", "--watch", "app", "--watch", "database/migrations"])
    assert overridden.exit_code == 0, overridden.output
    hook = (tmp_path / ".git" / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert "grep -Eq '^(app|database/migrations)/'" in hook

    bad = runner.invoke(app, ["init", "--existing", "--watch", "../etc"])
    assert bad.exit_code == 1
    assert "invalid watched path" in bad.output
