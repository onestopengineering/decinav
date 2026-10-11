"""Project configuration: which local model runs, stored in .decinav/config.json."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
import shutil
from typing import Any

from decinav.provenance import PROVENANCE_DIRECTORY, _atomic_write, state_file


CONFIG_FILENAME = "config.json"
CONFIG_SCHEMA_VERSION = 1
PROVIDERS = ("claude", "codex", "command", "none")
# Suggestions only; the user picks. An empty model means "the CLI's own default".
SUGGESTED_MODELS = {"claude": ("sonnet", "opus", "fable"), "codex": ()}
PROVIDER_LABELS = {
    "claude": "Claude CLI",
    "codex": "Codex CLI",
    "command": "Custom command",
    "none": "No model",
}
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


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
        if self.provider in {"claude", "codex"}:
            return f"{self.provider} · {self.model or 'CLI default model'}"
        if self.provider == "command":
            return f"custom command · {self.command}"
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


def validate_model_name(model: str | None) -> str | None:
    if model is None:
        return None
    cleaned = model.strip()
    if not cleaned:
        return None
    if not _MODEL_NAME_PATTERN.fullmatch(cleaned):
        raise ConfigError(
            f"invalid model name {model!r}; use an alias such as sonnet or a full model ID"
        )
    return cleaned


def choose_model(
    provider: str | None,
    *,
    model: str | None = None,
    command: str | None = None,
) -> ModelConfig:
    """Resolve the configuration to save from the user's choice.

    ``provider`` None means "first local CLI on PATH" for non-interactive use. The
    model is always the user's: None keeps the CLI's own default.
    """

    model_name = validate_model_name(model)
    if provider == "command":
        if not command or not command.strip():
            raise ConfigError("a command is required for the custom command provider")
        return ModelConfig("command", None, command.strip())
    if provider == "none":
        return ModelConfig("none")
    if provider in {"claude", "codex"}:
        if not shutil.which(provider):
            raise ConfigError(
                f"'{provider}' was not found on PATH; install it or choose another provider"
            )
        return ModelConfig(provider, model_name)
    if provider is not None:
        raise ConfigError(f"unknown provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    detected = detect_providers()
    if not detected:
        return ModelConfig("none")
    return ModelConfig(detected[0], model_name)
