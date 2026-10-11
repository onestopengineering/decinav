"""Deterministic, size-bounded source discovery for AI prompts."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

import yaml


MAX_SOURCE_FILE_BYTES = 128 * 1024
MAX_SOURCE_BUNDLE_BYTES = 1024 * 1024
INGEST_SUFFIXES = frozenset({".md", ".txt", ".rst", ".adoc"})

_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "coverage",
        ".next",
        ".cache",
        "__pycache__",
    }
)
_LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "composer.lock",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "gemfile.lock",
        "go.sum",
        "gradle.lockfile",
    }
)
_STRUCTURAL_FILENAMES = frozenset(
    {
        "package.json",
        "pom.xml",
        "composer.json",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "go.mod",
        "cargo.toml",
        "gemfile",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "makefile",
        "cmakelists.txt",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "chart.yaml",
        "tsconfig.json",
        "nx.json",
        "turbo.json",
        "angular.json",
        ".gitlab-ci.yml",
        ".gitlab-ci.yaml",
        "azure-pipelines.yml",
        "azure-pipelines.yaml",
        "jenkinsfile",
        "procfile",
        "serverless.yml",
        "serverless.yaml",
        "wrangler.toml",
    }
)
_REQUIREMENTS_PATTERN = re.compile(r"^requirements(?:[-.][a-z0-9_.-]+)?\.txt$")
_COMPOSE_PATTERN = re.compile(r"^(?:docker-)?compose(?:\.[a-z0-9_.-]+)?\.ya?ml$")


class SourceBundleError(RuntimeError):
    """Raised when no safe, supported prompt sources can be assembled."""


@dataclass(frozen=True, slots=True)
class SourceWarning:
    path: Path
    reason: str

    def format(self, *, relative_to: Path) -> str:
        try:
            display_path = self.path.relative_to(relative_to)
        except ValueError:
            display_path = self.path
        return f"{display_path}: {self.reason}"


@dataclass(frozen=True, slots=True)
class BundledSource:
    relative_path: str
    content: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SourceBundle:
    root: Path
    files: tuple[BundledSource, ...]
    warnings: tuple[SourceWarning, ...]
    text: str
    size_bytes: int


def build_baseline_bundle(project_root: Path) -> SourceBundle:
    """Bundle only recognized structural files; never include application source."""

    root = _require_directory(project_root)
    candidates: list[tuple[Path, str]] = []
    for path in _walk_files(root):
        relative = path.relative_to(root)
        classification = _baseline_classification(relative)
        if classification is not None:
            candidates.append((path, classification))

    sources: list[BundledSource] = []
    warnings: list[SourceWarning] = []
    sections: list[str] = []
    total_bytes = 0
    for path, classification in sorted(
        candidates, key=lambda item: item[0].relative_to(root).as_posix().casefold()
    ):
        loaded = _load_text(path, warnings)
        if loaded is None:
            continue
        content, content_size = loaded

        if classification == "possible-kubernetes":
            kubernetes, contains_secret = _inspect_kubernetes_yaml(content)
            if contains_secret:
                warnings.append(
                    SourceWarning(path, "excluded Kubernetes manifest containing a Secret kind")
                )
                continue
            if not kubernetes:
                continue

        relative_path = path.relative_to(root).as_posix()
        section = _render_section(relative_path, content)
        section_size = len(section.encode("utf-8"))
        if total_bytes + section_size > MAX_SOURCE_BUNDLE_BYTES:
            warnings.append(
                SourceWarning(path, "excluded because the 1 MiB source bundle limit was reached")
            )
            continue
        sources.append(BundledSource(relative_path, content, content_size))
        sections.append(section)
        total_bytes += section_size

    if not sources:
        raise SourceBundleError(
            "no supported structural files were found within the filtering and size limits"
        )
    return SourceBundle(root, tuple(sources), tuple(warnings), "".join(sections), total_bytes)


def build_ingest_bundle(folder: Path) -> SourceBundle:
    """Bundle supported informal decision documents as strict UTF-8 text."""

    root = _require_directory(folder)
    sources: list[BundledSource] = []
    warnings: list[SourceWarning] = []
    sections: list[str] = []
    total_bytes = 0
    candidates = [
        path for path in _walk_files(root) if path.suffix.casefold() in INGEST_SUFFIXES
    ]
    for path in sorted(
        candidates, key=lambda candidate: candidate.relative_to(root).as_posix().casefold()
    ):
        loaded = _load_text(path, warnings)
        if loaded is None:
            continue
        content, content_size = loaded
        if not content.strip():
            warnings.append(SourceWarning(path, "excluded empty document"))
            continue
        relative_path = path.relative_to(root).as_posix()
        section = _render_section(relative_path, content)
        section_size = len(section.encode("utf-8"))
        if total_bytes + section_size > MAX_SOURCE_BUNDLE_BYTES:
            warnings.append(
                SourceWarning(path, "excluded because the 1 MiB source bundle limit was reached")
            )
            continue
        sources.append(BundledSource(relative_path, content, content_size))
        sections.append(section)
        total_bytes += section_size

    if not sources:
        raise SourceBundleError(
            "no supported UTF-8 .md, .txt, .rst, or .adoc documents were found "
            "within the size limits"
        )
    return SourceBundle(root, tuple(sources), tuple(warnings), "".join(sections), total_bytes)


def build_brief_bundle(brief_path: Path) -> SourceBundle:
    """Bundle one product brief as evidence for an intended-architecture baseline."""

    try:
        resolved = brief_path.resolve(strict=True)
    except OSError as exc:
        raise SourceBundleError(f"product brief does not exist: {brief_path}") from exc
    if not resolved.is_file():
        raise SourceBundleError(f"product brief is not a file: {brief_path}")
    if resolved.suffix.casefold() not in INGEST_SUFFIXES:
        raise SourceBundleError(
            "product brief must be a UTF-8 .md, .txt, .rst, or .adoc document"
        )
    warnings: list[SourceWarning] = []
    loaded = _load_text(resolved, warnings)
    if loaded is None:
        raise SourceBundleError(warnings[-1].format(relative_to=resolved.parent))
    content, content_size = loaded
    if not content.strip():
        raise SourceBundleError(f"product brief is empty: {brief_path}")
    section = _render_section(resolved.name, content)
    return SourceBundle(
        resolved.parent,
        (BundledSource(resolved.name, content, content_size),),
        tuple(warnings),
        section,
        len(section.encode("utf-8")),
    )


def _require_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceBundleError(f"source folder does not exist: {path}") from exc
    if not resolved.is_dir():
        raise SourceBundleError(f"source path is not a directory: {path}")
    return resolved


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, child_directories, child_files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        child_directories[:] = sorted(
            (
                name
                for name in child_directories
                if not _exclude_directory(root, directory_path / name)
            ),
            key=str.casefold,
        )
        for filename in sorted(child_files, key=str.casefold):
            path = directory_path / filename
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path)
    return files


def _exclude_directory(root: Path, path: Path) -> bool:
    if path.is_symlink() or path.name.casefold() in _EXCLUDED_DIRECTORIES:
        return True
    absolute_parts = tuple(part.casefold() for part in path.parts)
    if len(absolute_parts) >= 2 and absolute_parts[-2:] == ("docs", "adr"):
        return True
    try:
        relative_parts = tuple(part.casefold() for part in path.relative_to(root).parts)
    except ValueError:
        return True
    return len(relative_parts) >= 2 and relative_parts[-2:] == ("docs", "adr")


def _baseline_classification(relative: Path) -> str | None:
    name = relative.name.casefold()
    if name in _LOCKFILES or name == ".env" or name.startswith(".env."):
        return None
    if name in _STRUCTURAL_FILENAMES:
        return "structural"
    if _REQUIREMENTS_PATTERN.fullmatch(name):
        return "structural"
    if _COMPOSE_PATTERN.fullmatch(name):
        return "structural"
    if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return "structural"
    if name.endswith(".tf") and not name.endswith(".tfvars"):
        return "structural"

    parts = tuple(part.casefold() for part in relative.parts)
    if len(parts) >= 3 and parts[0:2] == (".github", "workflows"):
        if relative.suffix.casefold() in {".yml", ".yaml"}:
            return "structural"
    if relative.suffix.casefold() in {".yml", ".yaml"}:
        return "possible-kubernetes"
    return None


def _load_text(
    path: Path, warnings: list[SourceWarning]
) -> tuple[str, int] | None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        warnings.append(SourceWarning(path, f"could not inspect file: {exc}"))
        return None
    if size > MAX_SOURCE_FILE_BYTES:
        warnings.append(
            SourceWarning(path, f"excluded because it exceeds {MAX_SOURCE_FILE_BYTES} bytes")
        )
        return None
    try:
        raw = path.read_bytes()
        content = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        warnings.append(SourceWarning(path, f"excluded non-UTF-8 or unreadable file: {exc}"))
        return None
    return content, len(raw)


def _inspect_kubernetes_yaml(content: str) -> tuple[bool, bool]:
    try:
        documents = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return False, False
    is_manifest = any(
        isinstance(document, dict)
        and "apiVersion" in document
        and isinstance(document.get("kind"), str)
        for document in documents
    )
    contains_secret = any(_contains_secret_kind(document) for document in documents)
    return is_manifest, contains_secret


def _contains_secret_kind(value: object) -> bool:
    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str) and "secret" in kind.casefold():
            return True
        return any(_contains_secret_kind(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_secret_kind(child) for child in value)
    return False


def _render_section(relative_path: str, content: str) -> str:
    normalized = content if content.endswith("\n") else content + "\n"
    return (
        f"===== BEGIN FILE: {relative_path} =====\n"
        f"{normalized}"
        f"===== END FILE: {relative_path} =====\n\n"
    )
