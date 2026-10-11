"""Git repository discovery and non-destructive commit-msg hook installation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


HOOK_MARKER = "# decinav:managed-commit-msg-hook"
LEGACY_HOOK_MARKERS = ("# arch-cli:managed-commit-msg-hook",)
DEFAULT_WATCHED_PATHS = ("src", "infra")
# Top-level folders that hold application code in common stacks; used when the
# project has no src/ so the hook still fires where the code actually lives.
_CANDIDATE_CODE_PATHS = (
    "src", "app", "lib", "cmd", "internal", "pkg", "services", "packages",
    "infra", "infrastructure", "terraform", "deploy", "k8s", "helm",
)
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
GITIGNORE_START_MARKER = "# decinav:adr-tracking:start"
GITIGNORE_END_MARKER = "# decinav:adr-tracking:end"
GITIGNORE_ADR_BLOCK = (
    f"{GITIGNORE_START_MARKER}\n"
    "!/docs/\n"
    "/docs/*\n"
    "!/docs/adr/\n"
    "!/docs/adr/*.md\n"
    f"{GITIGNORE_END_MARKER}"
)


class HookError(RuntimeError):
    """Raised when project initialization or hook installation is unsafe."""


@dataclass(frozen=True, slots=True)
class GitContext:
    project_root: Path
    hooks_dir: Path


@dataclass(frozen=True, slots=True)
class HookInstallResult:
    path: Path
    mode: str
    action: str
    backup_path: Path | None = None
    watched_paths: tuple[str, ...] = DEFAULT_WATCHED_PATHS


@dataclass(frozen=True, slots=True)
class InitResult:
    project_root: Path
    adr_dir: Path
    readme_created: bool
    hook: HookInstallResult
    gitignore_updated: bool


def initialize_project(
    start: Path,
    *,
    existing_codebase: bool,
    overwrite_hook: bool,
    watched_paths: tuple[str, ...] | None = None,
) -> InitResult:
    """Create docs/adr and install the requested commit-message policy.

    ``watched_paths`` are the top-level folders whose staged changes must cite an
    ADR. When omitted they are detected from the repository layout.
    """

    context = resolve_git_context(start)
    paths = (
        normalize_watched_paths(watched_paths)
        if watched_paths
        else detect_watched_paths(context.project_root)
    )
    hook_path = context.hooks_dir / "commit-msg"
    _ensure_hook_can_be_installed(hook_path, overwrite_hook=overwrite_hook)
    gitignore_updated = _ensure_adr_trackable(context.project_root)

    adr_dir = context.project_root / "docs" / "adr"
    readme_path = adr_dir / "README.md"
    try:
        adr_dir.mkdir(parents=True, exist_ok=True)
        readme_created = False
        if not readme_path.exists():
            with readme_path.open("x", encoding="utf-8", newline="\n"):
                pass
            readme_created = True
        elif not readme_path.is_file():
            raise HookError(f"{readme_path} exists but is not a regular file")
    except OSError as exc:
        raise HookError(f"could not scaffold {adr_dir}: {exc}") from exc

    hook = install_commit_msg_hook(
        context.hooks_dir,
        warn_only=existing_codebase,
        overwrite_hook=overwrite_hook,
        watched_paths=paths,
    )
    return InitResult(
        project_root=context.project_root,
        adr_dir=adr_dir,
        readme_created=readme_created,
        hook=hook,
        gitignore_updated=gitignore_updated,
    )


def _ensure_adr_trackable(project_root: Path) -> bool:
    relative_readme = Path("docs") / "adr" / "README.md"
    if not _git_path_is_ignored(project_root, relative_readme):
        return False

    gitignore = project_root / ".gitignore"
    try:
        if gitignore.exists() and not gitignore.is_file():
            raise HookError(f"{gitignore} exists but is not a regular file")
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise HookError(f"could not inspect {gitignore}: {exc}") from exc

    start_count = existing.count(GITIGNORE_START_MARKER)
    end_count = existing.count(GITIGNORE_END_MARKER)
    if start_count != end_count or start_count > 1:
        raise HookError(
            f"{gitignore} contains malformed decinav ADR tracking markers"
        )
    if start_count == 1:
        start = existing.index(GITIGNORE_START_MARKER)
        end = existing.index(GITIGNORE_END_MARKER) + len(GITIGNORE_END_MARKER)
        existing = (existing[:start] + existing[end:]).strip("\n")

    prefix = existing.rstrip("\n")
    updated = f"{prefix}\n\n{GITIGNORE_ADR_BLOCK}\n" if prefix else f"{GITIGNORE_ADR_BLOCK}\n"
    _atomic_write_text_file(gitignore, updated)
    if _git_path_is_ignored(project_root, relative_readme):
        raise HookError(
            "docs/adr remains ignored after updating .gitignore; inspect repository "
            "or global Git ignore rules"
        )
    return True


def _git_path_is_ignored(project_root: Path, relative_path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "check-ignore",
                "--quiet",
                "--no-index",
                relative_path.as_posix(),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise HookError(f"could not execute Git ignore check: {exc}") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or f"git check-ignore exited {result.returncode}"
    raise HookError(f"could not inspect Git ignore rules: {detail}")


def resolve_git_context(start: Path) -> GitContext:
    """Resolve the worktree root and effective hooks path through Git itself."""

    project_root_output = _run_git(start, "rev-parse", "--show-toplevel")
    project_root = Path(project_root_output).resolve()
    hooks_output = _run_git(project_root, "rev-parse", "--git-path", "hooks")
    hooks_path = Path(hooks_output)
    if not hooks_path.is_absolute():
        hooks_path = project_root / hooks_path
    return GitContext(project_root=project_root, hooks_dir=hooks_path.resolve())


def install_commit_msg_hook(
    hooks_dir: Path,
    *,
    warn_only: bool,
    overwrite_hook: bool,
    now: datetime | None = None,
    watched_paths: tuple[str, ...] = DEFAULT_WATCHED_PATHS,
) -> HookInstallResult:
    """Install or update decinav's standalone POSIX commit-msg hook."""

    hook_path = hooks_dir / "commit-msg"
    state = _ensure_hook_can_be_installed(hook_path, overwrite_hook=overwrite_hook)
    mode = "warn" if warn_only else "strict"
    paths = normalize_watched_paths(watched_paths)
    content = render_commit_msg_hook(warn_only=warn_only, watched_paths=paths)

    backup_path: Path | None = None
    if state == "foreign":
        backup_path = _backup_hook(hook_path, now=now)

    if state == "managed":
        try:
            current = hook_path.read_text(encoding="utf-8")
            is_executable = bool(hook_path.stat().st_mode & stat.S_IXUSR)
        except (OSError, UnicodeError) as exc:
            raise HookError(f"could not inspect managed hook {hook_path}: {exc}") from exc
        if current == content and is_executable:
            return HookInstallResult(hook_path, mode, "unchanged", watched_paths=paths)

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_hook(hook_path, content)
    except OSError as exc:
        raise HookError(f"could not install commit-msg hook at {hook_path}: {exc}") from exc

    action = "updated" if state == "managed" else "installed"
    if state == "foreign":
        action = "overwritten"
    return HookInstallResult(hook_path, mode, action, backup_path, watched_paths=paths)


def detect_watched_paths(project_root: Path) -> tuple[str, ...]:
    """Pick the top-level code and infrastructure folders that exist in this repo."""

    found = tuple(name for name in _CANDIDATE_CODE_PATHS if (project_root / name).is_dir())
    return found or DEFAULT_WATCHED_PATHS


def normalize_watched_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw in paths:
        candidate = raw.strip().strip("/")
        if not candidate:
            continue
        if not _PATH_PATTERN.fullmatch(candidate) or ".." in candidate.split("/"):
            raise HookError(f"invalid watched path {raw!r}; use folder names like app or infra/terraform")
        if candidate not in cleaned:
            cleaned.append(candidate)
    if not cleaned:
        raise HookError("at least one watched path is required")
    return tuple(cleaned)


def render_commit_msg_hook(
    *, warn_only: bool, watched_paths: tuple[str, ...] = DEFAULT_WATCHED_PATHS
) -> str:
    """Render a dependency-free POSIX hook for the selected enforcement mode."""

    paths = normalize_watched_paths(watched_paths)
    display = " or ".join(f"{path}/" for path in paths)
    pattern = "|".join(re.escape(path) for path in paths)
    policy_message = (
        f"decinav warning: staged {display} changes should include "
        "[ADR-XXXX] or [skip-adr] in the commit message"
        if warn_only
        else f"decinav: staged {display} changes require "
        "[ADR-XXXX] or [skip-adr] in the commit message"
    )
    outcome = f'printf "%s\\n" "{policy_message}" >&2\nexit {0 if warn_only else 1}'
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        f"# mode: {'warn' if warn_only else 'strict'}\n"
        f"# paths: {' '.join(paths)}\n"
        "\n"
        "message_file=$1\n"
        "\n"
        "if ! git diff --cached --name-only --diff-filter=ACMRD -- 2>/dev/null "
        f"| grep -Eq '^({pattern})/'; then\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "if grep -Eq '\\[(ADR-[0-9]{4}|skip-adr)\\]' \"$message_file\"; then\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        f"{outcome}\n"
    )


def _run_git(cwd: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise HookError(f"could not execute Git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "not inside a Git worktree"
        raise HookError(f"Git repository discovery failed: {detail}")
    output = result.stdout.strip()
    if not output:
        raise HookError("Git repository discovery returned an empty path")
    return output


def _ensure_hook_can_be_installed(hook_path: Path, *, overwrite_hook: bool) -> str:
    if not os.path.lexists(hook_path):
        return "absent"
    if not hook_path.is_file():
        raise HookError(f"{hook_path} exists but is not a regular file")

    try:
        existing = hook_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        existing = ""
    if HOOK_MARKER in existing or any(marker in existing for marker in LEGACY_HOOK_MARKERS):
        return "managed"
    if overwrite_hook:
        return "foreign"

    previous_path = hook_path.with_name("commit-msg.previous")
    raise HookError(
        f"refusing to overwrite foreign hook: {hook_path}\n"
        "Manual chaining option:\n"
        f"  1. Move it to {previous_path}\n"
        "  2. Run decinav init again\n"
        f"  3. Add '\"{previous_path}\" \"$1\" || exit $?' to the generated hook\n"
        "Or rerun with --overwrite-hook; decinav will create a timestamped backup."
    )


def _backup_hook(hook_path: Path, *, now: datetime | None) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    suffix = timestamp.strftime("%Y%m%dT%H%M%SZ")
    candidate = hook_path.with_name(f"commit-msg.backup-{suffix}")
    counter = 1
    while candidate.exists():
        candidate = hook_path.with_name(f"commit-msg.backup-{suffix}-{counter}")
        counter += 1
    try:
        shutil.copy2(hook_path, candidate)
    except OSError as exc:
        raise HookError(f"could not back up foreign hook {hook_path}: {exc}") from exc
    return candidate


def _atomic_write_hook(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".commit-msg.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o755)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _atomic_write_text_file(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise HookError(f"could not update {path}: {exc}") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
