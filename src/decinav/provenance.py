"""Provenance sidecar and Git-derived freshness for ADR records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


PROVENANCE_DIRECTORY = ".decinav"
# Earlier releases (published as arch-cli) kept state in .arch/. Reads fall back
# to it so existing repositories keep their provenance; writes go to .decinav/.
LEGACY_STATE_DIRECTORY = ".arch"
PROVENANCE_FILENAME = "provenance.json"
PROVENANCE_SCHEMA_VERSION = 1
FRESHNESS_STATES = ("fresh", "aging", "sources-changed", "unknown")
_GIT_TIMEOUT_SECONDS = 10
_PROJECT_PATHSPEC = (
    "--",
    ".",
    ":(exclude)docs/adr",
    ":(exclude)docs/adr/**",
    f":(exclude){PROVENANCE_DIRECTORY}",
    f":(exclude){PROVENANCE_DIRECTORY}/**",
    f":(exclude){LEGACY_STATE_DIRECTORY}",
    f":(exclude){LEGACY_STATE_DIRECTORY}/**",
)


class ProvenanceError(RuntimeError):
    """Raised when the provenance sidecar cannot be read or written safely."""


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    generated_by: str
    generator: str
    generated_at: str
    commit: str | None
    sources: tuple[str, ...]
    accepted_at: str | None = None
    accepted_commit: str | None = None
    attested_at: str | None = None
    attested_commit: str | None = None

    @property
    def reference_commit(self) -> str | None:
        """The most recent commit at which a human or generator saw this ADR."""

        return self.attested_commit or self.accepted_commit or self.commit

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sources"] = list(self.sources)
        return payload


@dataclass(frozen=True, slots=True)
class Freshness:
    state: str
    reference_commit: str | None
    commits_since: int | None
    changed_sources: tuple[str, ...]
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reference_commit": self.reference_commit,
            "commits_since": self.commits_since,
            "changed_sources": list(self.changed_sources),
            "detail": self.detail,
        }

    def format(self) -> str:
        if self.state == "fresh":
            return f"fresh (no project commits since {_short(self.reference_commit)})"
        if self.state == "aging":
            return (
                f"aging ({self.commits_since} project commit(s) since "
                f"{_short(self.reference_commit)}; recorded sources unchanged)"
            )
        if self.state == "sources-changed":
            return (
                f"sources-changed ({self.commits_since} project commit(s) since "
                f"{_short(self.reference_commit)}; changed: "
                + ", ".join(self.changed_sources)
                + ")"
            )
        return f"unknown ({self.detail})"


def provenance_path(project_root: Path) -> Path:
    return project_root / PROVENANCE_DIRECTORY / PROVENANCE_FILENAME


def state_file(project_root: Path, filename: str) -> Path:
    """Where to read a state file: .decinav/ first, then the legacy .arch/."""

    current = project_root / PROVENANCE_DIRECTORY / filename
    if current.exists():
        return current
    legacy = project_root / LEGACY_STATE_DIRECTORY / filename
    return legacy if legacy.exists() else current


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_provenance(project_root: Path) -> dict[str, ProvenanceRecord]:
    """Load the sidecar; a missing file is an empty mapping, a malformed one is an error."""

    path = state_file(project_root, PROVENANCE_FILENAME)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ProvenanceError(
            f"{path} must be a JSON object with schema_version {PROVENANCE_SCHEMA_VERSION}"
        )
    records_value = raw.get("records")
    if not isinstance(records_value, dict):
        raise ProvenanceError(f"{path} field 'records' must be an object")
    records: dict[str, ProvenanceRecord] = {}
    for adr_id, value in records_value.items():
        if not isinstance(value, dict):
            raise ProvenanceError(f"{path} record {adr_id!r} must be an object")
        try:
            records[adr_id] = ProvenanceRecord(
                generated_by=str(value["generated_by"]),
                generator=str(value["generator"]),
                generated_at=str(value["generated_at"]),
                commit=_optional_string(value.get("commit")),
                sources=tuple(str(item) for item in value.get("sources", [])),
                accepted_at=_optional_string(value.get("accepted_at")),
                accepted_commit=_optional_string(value.get("accepted_commit")),
                attested_at=_optional_string(value.get("attested_at")),
                attested_commit=_optional_string(value.get("attested_commit")),
            )
        except (KeyError, TypeError) as exc:
            raise ProvenanceError(f"{path} record {adr_id!r} is incomplete: {exc}") from exc
    return records


def save_provenance(project_root: Path, records: dict[str, ProvenanceRecord]) -> Path:
    path = provenance_path(project_root)
    payload = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "records": {
            adr_id: records[adr_id].to_json() for adr_id in sorted(records)
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise ProvenanceError(f"could not write {path}: {exc}") from exc
    return path


def record_generation(
    project_root: Path,
    adr_ids: tuple[str, ...],
    *,
    generated_by: str,
    generator: str,
    sources: tuple[str, ...],
    commit: str | None = None,
) -> dict[str, ProvenanceRecord]:
    """Record that the listed ADRs were just created; existing entries are replaced."""

    return record_generations(
        project_root,
        {adr_id: sources for adr_id in adr_ids},
        generated_by=generated_by,
        generator=generator,
        commit=commit,
    )


def record_generations(
    project_root: Path,
    sources_by_id: dict[str, tuple[str, ...]],
    *,
    generated_by: str,
    generator: str,
    commit: str | None = None,
) -> dict[str, ProvenanceRecord]:
    """Record several ADRs at once, each with its own source list."""

    records = load_provenance(project_root)
    resolved_commit = commit if commit is not None else current_commit(project_root)
    stamp = now_iso()
    for adr_id, sources in sources_by_id.items():
        records[adr_id] = ProvenanceRecord(
            generated_by=generated_by,
            generator=generator,
            generated_at=stamp,
            commit=resolved_commit,
            sources=sources,
        )
    save_provenance(project_root, records)
    return records


def mark_accepted(project_root: Path, adr_ids: tuple[str, ...]) -> dict[str, ProvenanceRecord]:
    return _mark(project_root, adr_ids, "accepted")


def mark_attested(project_root: Path, adr_ids: tuple[str, ...]) -> dict[str, ProvenanceRecord]:
    return _mark(project_root, adr_ids, "attested")


def remove_records(project_root: Path, adr_ids: tuple[str, ...]) -> dict[str, ProvenanceRecord]:
    records = load_provenance(project_root)
    changed = False
    for adr_id in adr_ids:
        if adr_id in records:
            del records[adr_id]
            changed = True
    if changed:
        save_provenance(project_root, records)
    return records


def _mark(project_root: Path, adr_ids: tuple[str, ...], kind: str) -> dict[str, ProvenanceRecord]:
    records = load_provenance(project_root)
    commit = current_commit(project_root)
    stamp = now_iso()
    for adr_id in adr_ids:
        existing = records.get(adr_id)
        if existing is None:
            existing = ProvenanceRecord(
                generated_by="unknown",
                generator="unknown",
                generated_at=stamp,
                commit=None,
                sources=(),
            )
        if kind == "accepted":
            records[adr_id] = replace(existing, accepted_at=stamp, accepted_commit=commit)
        else:
            records[adr_id] = replace(existing, attested_at=stamp, attested_commit=commit)
    save_provenance(project_root, records)
    return records


def current_commit(project_root: Path) -> str | None:
    output = _git(project_root, "rev-parse", "HEAD")
    return output.strip() if output else None


class FreshnessCalculator:
    """Compute freshness for many ADRs while caching Git lookups per commit."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._is_repository: bool | None = None
        self._toplevel: Path | None = None
        self._count_cache: dict[str, int | None] = {}
        self._changed_cache: dict[tuple[str, tuple[str, ...]], tuple[str, ...] | None] = {}

    def compute(self, record: ProvenanceRecord | None) -> Freshness:
        if record is None:
            return Freshness("unknown", None, None, (), "no provenance recorded")
        reference = record.reference_commit
        if not reference:
            return Freshness("unknown", None, None, (), "no commit recorded")
        if not self._repository_available():
            return Freshness("unknown", reference, None, (), "not inside a Git repository")
        commits_since = self._commits_since(reference)
        if commits_since is None:
            return Freshness(
                "unknown",
                reference,
                None,
                (),
                f"commit {_short(reference)} is not in the current history (rebased, "
                "squashed, or from another clone); run 'decinav attest' after review",
            )
        if commits_since == 0:
            return Freshness("fresh", reference, 0, (), "no project commits since reference")
        changed = self._changed_sources(reference, record.sources)
        if changed is None:
            return Freshness(
                "unknown", reference, commits_since, (), "could not compare recorded sources"
            )
        if changed:
            return Freshness(
                "sources-changed", reference, commits_since, changed, "recorded sources changed"
            )
        return Freshness("aging", reference, commits_since, (), "project changed since reference")

    def _repository_available(self) -> bool:
        if self._is_repository is None:
            self._is_repository = _git(self.project_root, "rev-parse", "--git-dir") is not None
        return self._is_repository

    def _commits_since(self, reference: str) -> int | None:
        if reference in self._count_cache:
            return self._count_cache[reference]
        verified = _git(self.project_root, "rev-parse", "--verify", "--quiet", f"{reference}^{{commit}}")
        if verified is None:
            self._count_cache[reference] = None
            return None
        ancestor = _git(self.project_root, "merge-base", "--is-ancestor", reference, "HEAD")
        if ancestor is None:
            self._count_cache[reference] = None
            return None
        output = _git(
            self.project_root,
            "rev-list",
            "--count",
            f"{reference}..HEAD",
            *_PROJECT_PATHSPEC,
        )
        count = int(output.strip()) if output and output.strip().isdigit() else None
        self._count_cache[reference] = count
        return count

    def _changed_sources(
        self, reference: str, sources: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        if not sources:
            return ()
        key = (reference, sources)
        if key in self._changed_cache:
            return self._changed_cache[key]
        inside = self._sources_inside_repository(sources)
        if not inside:
            self._changed_cache[key] = ()
            return ()
        output = _git(
            self.project_root,
            "diff",
            "--name-only",
            "--relative",
            reference,
            "HEAD",
            "--",
            *inside,
        )
        if output is None:
            self._changed_cache[key] = None
            return None
        changed = tuple(line for line in output.splitlines() if line.strip())
        self._changed_cache[key] = changed
        return changed

    def _sources_inside_repository(self, sources: tuple[str, ...]) -> tuple[str, ...]:
        """Keep only sources that resolve inside the Git worktree; others cannot be diffed."""

        if self._toplevel is None:
            output = _git(self.project_root, "rev-parse", "--show-toplevel")
            self._toplevel = Path(output.strip()).resolve() if output else Path("/nonexistent")
        kept: list[str] = []
        for source in sources:
            candidate = (self.project_root / source).resolve()
            try:
                candidate.relative_to(self._toplevel)
            except ValueError:
                continue
            kept.append(source)
        return tuple(kept)


def compute_freshness(project_root: Path, record: ProvenanceRecord | None) -> Freshness:
    return FreshnessCalculator(project_root).compute(record)


def relative_source_paths(project_root: Path, paths: tuple[Path, ...]) -> tuple[str, ...]:
    """Render source paths relative to the project root when possible."""

    resolved_root = project_root.resolve()
    rendered: list[str] = []
    for path in paths:
        try:
            rendered.append(path.resolve().relative_to(resolved_root).as_posix())
        except ValueError:
            rendered.append(path.as_posix())
    return tuple(rendered)


def _git(project_root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _short(commit: str | None) -> str:
    if not commit:
        return "unknown"
    return commit[:7]


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
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
        temporary_path.chmod(0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
