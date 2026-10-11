"""Project configuration: which local model runs, stored in .decinav/config.json."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any

from decinav.provenance import PROVENANCE_DIRECTORY, _atomic_write, state_file


CONFIG_FILENAME = "config.json"
CONFIG_SCHEMA_VERSION = 1
PROVIDERS = ("claude", "codex", "command", "none")
DEFAULT_CLAUDE_MODEL = "sonnet"


class ConfigError(RuntimeError):
    """Raised when .decinav/config.json is unreadable or invalid."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    provider: str
    model: str | None = None
    command: str | None = None

    @property
    def configured(self) -> bool:
        return self.provider in {"claude", "codex", "command"}

    def describe(self) -> str:
        if self.provider == "claude":
            return f"claude ({self.model or DEFAULT_CLAUDE_MODEL}), isolated: no tools, empty directory"
        if self.provider == "codex":
            return "codex (read-only sandbox, empty directory)"
        if self.provider == "command":
            return f"custom command: {self.command}"
        return "none"

    def to_json(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "command": self.command}


def config_path(project_root: Path) -> Path:
    return project_root / PROVENANCE_DIRECTORY / CONFIG_FILENAME


def load_config(project_root: Path) -> ModelConfig | None:
    """Return the saved model config, or None when the project has none."""

    path = state_file(project_root, CONFIG_FILENAME)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"{path} must be an object with schema_version {CONFIG_SCHEMA_VERSION}")
    model_value = raw.get("model")
    if not isinstance(model_value, dict):
        raise ConfigError(f"{path} field 'model' must be an object")
    provider = model_value.get("provider")
    if provider not in PROVIDERS:
        raise ConfigError(f"{path} model.provider must be one of: {', '.join(PROVIDERS)}")
    model = model_value.get("model")
    command = model_value.get("command")
    if model is not None and not isinstance(model, str):
        raise ConfigError(f"{path} model.model must be a string")
    if provider == "command" and (not isinstance(command, str) or not command.strip()):
        raise ConfigError(f"{path} model.command must be a non-empty string for provider 'command'")
    return ModelConfig(provider, model, command if isinstance(command, str) else None)


def save_config(project_root: Path, model: ModelConfig) -> Path:
    path = config_path(project_root)
    payload = {"schema_version": CONFIG_SCHEMA_VERSION, "model": model.to_json()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        raise ConfigError(f"could not write {path}: {exc}") from exc
    return path


def detect_providers() -> tuple[str, ...]:
    """Local model CLIs found on PATH, in preference order."""

    return tuple(name for name in ("claude", "codex") if shutil.which(name))


def choose_model(
    requested: str | None,
    *,
    claude_model: str | None = None,
    command: str | None = None,
) -> ModelConfig:
    """Resolve the model to save: an explicit choice, or the first CLI on PATH."""

    if requested == "command":
        if not command or not command.strip():
            raise ConfigError("--command is required with --model command")
        return ModelConfig("command", None, command.strip())
    if requested == "none":
        return ModelConfig("none")
    if requested in {"claude", "codex"}:
        if not shutil.which(requested):
            raise ConfigError(
                f"'{requested}' was not found on PATH; install it or choose another provider"
            )
        return ModelConfig(requested, claude_model or (DEFAULT_CLAUDE_MODEL if requested == "claude" else None))
    if requested is not None:
        raise ConfigError(f"unknown provider {requested!r}; choose from {', '.join(PROVIDERS)}")
    detected = detect_providers()
    if not detected:
        return ModelConfig("none")
    provider = detected[0]
    return ModelConfig(provider, claude_model or (DEFAULT_CLAUDE_MODEL if provider == "claude" else None))
