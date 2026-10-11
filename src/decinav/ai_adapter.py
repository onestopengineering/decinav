"""Shell-free subprocess adapter for local AI command-line agents."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Mapping


DEFAULT_TIMEOUT_SECONDS = 300
BUILTIN_TIMEOUT_SECONDS = 900
MAX_AI_STDOUT_BYTES = 10 * 1024 * 1024
MAX_INLINE_PROMPT_BYTES = 128 * 1024
_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class AIAdapterError(RuntimeError):
    """Raised when AI configuration or local subprocess execution fails."""


@dataclass(frozen=True, slots=True)
class AIResponse:
    stdout: str
    stderr: str
    transport: str


def execute_ai(
    prompt: str,
    *,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
    purpose: str | None = None,
) -> AIResponse:
    """Execute the configured local agent without invoking a command shell."""

    settings = os.environ if environ is None else environ
    command_template = settings.get("DECINAV_AI_CMD", "").strip()
    if not command_template:
        return _execute_from_project_config(prompt, cwd=cwd, settings=settings, purpose=purpose)

    if command_template == "mock":
        return _read_mock_response(cwd, settings, purpose=purpose)

    timeout = _parse_timeout(settings.get("DECINAV_AI_TIMEOUT_SECONDS"))
    return _execute_template(command_template, prompt, cwd=cwd, timeout=timeout, environ=environ)


def _execute_from_project_config(
    prompt: str, *, cwd: Path, settings: Mapping[str, str], purpose: str | None
) -> AIResponse:
    """Use .decinav/config.json written by `decinav setup` when no env override exists."""

    from decinav.config import ConfigError, load_config

    try:
        config = load_config(cwd)
    except ConfigError as exc:
        raise AIAdapterError(str(exc)) from exc
    if config is None or not config.configured:
        raise AIAdapterError(_missing_configuration_message())
    timeout = _parse_timeout(settings.get("DECINAV_AI_TIMEOUT_SECONDS"), default=BUILTIN_TIMEOUT_SECONDS)
    return execute_with_config(config, prompt, cwd=cwd, timeout=timeout)


def execute_with_config(config, prompt: str, *, cwd: Path, timeout: int) -> AIResponse:
    """Run one prompt through a saved or candidate ModelConfig."""

    if config.provider == "command":
        return _execute_template(config.command or "", prompt, cwd=cwd, timeout=timeout, environ=None)
    if config.provider in {"claude", "codex"}:
        return run_builtin_provider(config.provider, prompt, model=config.model, timeout=timeout)
    raise AIAdapterError(_missing_configuration_message())


def run_builtin_provider(
    provider: str, prompt: str, *, model: str | None, timeout: int
) -> AIResponse:
    """Run claude or codex isolated from the repository and from the user's own setup.

    Both run in an empty temporary directory with no tools and the prompt on stdin.
    Neither loads the user's personal instructions (~/.claude/CLAUDE.md or
    ~/.codex/AGENTS.md): those would color every answer and, measured on one machine,
    cost Codex about 7,500 extra tokens per call. Logins keep working.
    """

    with tempfile.TemporaryDirectory(prefix="decinav-model-") as sandbox:
        sandbox_path = Path(sandbox)
        if provider == "claude":
            command = [
                "claude", "-p", *(["--model", model] if model else []), "--tools", "",
                "--setting-sources", "", "--max-turns", "4", "--no-session-persistence",
                "--output-format", "text",
            ]
            return _run_process(
                command, cwd=sandbox_path, timeout=timeout, process_input=prompt,
                process_environment=None, transport="builtin-claude",
            )
        if provider == "codex":
            work = sandbox_path / "work"
            work.mkdir()
            last_message = sandbox_path / "last-message.txt"
            command = [
                "codex", "exec", "--skip-git-repo-check", "-s", "read-only",
                *(["-m", model] if model else []), "-o", str(last_message), "-",
            ]
            response = _run_process(
                command, cwd=work, timeout=timeout, process_input=prompt,
                process_environment=_isolated_codex_environment(sandbox_path / "codex-home"),
                transport="builtin-codex",
            )
            try:
                final = last_message.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                final = response.stdout
            if not final.strip():
                raise AIAdapterError("codex returned an empty final message")
            return AIResponse(final, response.stderr, response.transport)
    raise AIAdapterError(f"unsupported built-in provider {provider!r}")


def _isolated_codex_environment(home: Path) -> dict[str, str]:
    """A throwaway CODEX_HOME holding only links to the user's login and config.

    Codex reads global instructions from $CODEX_HOME/AGENTS.md; leaving that file
    out keeps personal instructions out of decinav prompts. Sessions and logs land
    in the throwaway home and are deleted with it.
    """

    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    home.mkdir()
    for name in ("auth.json", "config.toml"):
        target = source / name
        if target.exists():
            (home / name).symlink_to(target)
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    return environment


def _execute_template(
    command_template: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    environ: Mapping[str, str] | None,
) -> AIResponse:
    try:
        arguments = shlex.split(command_template)
    except ValueError as exc:
        raise AIAdapterError(f"could not parse DECINAV_AI_CMD with shlex: {exc}") from exc
    if not arguments:
        raise AIAdapterError("DECINAV_AI_CMD produced an empty command")

    placeholder_names = [
        name for argument in arguments for name in _PLACEHOLDER_PATTERN.findall(argument)
    ]
    unknown = sorted(set(placeholder_names) - {"prompt", "prompt_file"})
    if unknown:
        raise AIAdapterError(
            "DECINAV_AI_CMD contains unsupported placeholder(s): " + ", ".join(unknown)
        )
    prompt_count = placeholder_names.count("prompt")
    prompt_file_count = placeholder_names.count("prompt_file")
    if prompt_count > 1 or prompt_file_count > 1 or (prompt_count and prompt_file_count):
        raise AIAdapterError(
            "DECINAV_AI_CMD must contain at most one of {prompt} or {prompt_file}"
        )

    process_environment = None
    if environ is not None:
        process_environment = dict(os.environ)
        process_environment.update(environ)

    if prompt_count:
        if len(prompt.encode("utf-8")) > MAX_INLINE_PROMPT_BYTES:
            raise AIAdapterError(
                "prompt is too large for {prompt}; use {prompt_file} or omit the "
                "placeholder to send it through stdin"
            )
        command = [argument.replace("{prompt}", prompt) for argument in arguments]
        return _run_process(
            command,
            cwd=cwd,
            timeout=timeout,
            process_input=None,
            process_environment=process_environment,
            transport="argument",
        )

    if prompt_file_count:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix="decinav-prompt-",
                suffix=".txt",
            ) as prompt_file:
                prompt_file.write(prompt)
                prompt_file.flush()
                command = [
                    argument.replace("{prompt_file}", prompt_file.name)
                    for argument in arguments
                ]
                return _run_process(
                    command,
                    cwd=cwd,
                    timeout=timeout,
                    process_input=None,
                    process_environment=process_environment,
                    transport="prompt_file",
                )
        except OSError as exc:
            raise AIAdapterError(f"could not create temporary prompt file: {exc}") from exc

    return _run_process(
        arguments,
        cwd=cwd,
        timeout=timeout,
        process_input=prompt,
        process_environment=process_environment,
        transport="stdin",
    )


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    process_input: str | None,
    process_environment: dict[str, str] | None,
    transport: str,
) -> AIResponse:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=process_input,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=process_environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise AIAdapterError(
            f"AI command {command[0]!r} timed out after {timeout} seconds"
        ) from exc
    except OSError as exc:
        raise AIAdapterError(f"could not execute AI command {command[0]!r}: {exc}") from exc

    if result.returncode != 0:
        diagnostic = _diagnostic(result.stderr or result.stdout)
        suffix = f": {diagnostic}" if diagnostic else ""
        raise AIAdapterError(
            f"AI command {command[0]!r} exited with status {result.returncode}{suffix}"
        )
    if len(result.stdout.encode("utf-8")) > MAX_AI_STDOUT_BYTES:
        raise AIAdapterError(
            f"AI stdout exceeds the {MAX_AI_STDOUT_BYTES}-byte safety limit"
        )
    if not result.stdout.strip():
        raise AIAdapterError(f"AI command {command[0]!r} returned empty stdout")
    return AIResponse(result.stdout, result.stderr, transport)


def _read_mock_response(
    cwd: Path,
    settings: Mapping[str, str],
    *,
    purpose: str | None,
) -> AIResponse:
    purpose_variable: str | None = None
    fixture_value = ""
    if purpose is not None:
        normalized_purpose = re.sub(r"[^A-Za-z0-9]+", "_", purpose).strip("_").upper()
        if normalized_purpose:
            purpose_variable = f"DECINAV_AI_MOCK_{normalized_purpose}_RESPONSE"
            fixture_value = settings.get(purpose_variable, "").strip()
    if not fixture_value:
        fixture_value = settings.get("DECINAV_AI_MOCK_RESPONSE", "").strip()
    if not fixture_value:
        accepted = "DECINAV_AI_MOCK_RESPONSE"
        if purpose_variable is not None:
            accepted = f"{purpose_variable} or {accepted}"
        raise AIAdapterError(
            f"DECINAV_AI_CMD=mock requires {accepted}=/path/to/fixture"
        )
    fixture_path = Path(fixture_value)
    if not fixture_path.is_absolute():
        fixture_path = cwd / fixture_path
    try:
        if fixture_path.stat().st_size > MAX_AI_STDOUT_BYTES:
            raise AIAdapterError(
                f"mock response exceeds the {MAX_AI_STDOUT_BYTES}-byte safety limit"
            )
        stdout = fixture_path.read_text(encoding="utf-8")
    except AIAdapterError:
        raise
    except (OSError, UnicodeError) as exc:
        raise AIAdapterError(f"could not read mock response {fixture_path}: {exc}") from exc
    if not stdout.strip():
        raise AIAdapterError(f"mock response is empty: {fixture_path}")
    return AIResponse(stdout, "", "mock")


def _parse_timeout(value: str | None, *, default: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    if value is None or not value.strip():
        return default
    try:
        timeout = int(value)
    except ValueError as exc:
        raise AIAdapterError("DECINAV_AI_TIMEOUT_SECONDS must be a positive integer") from exc
    if timeout <= 0:
        raise AIAdapterError("DECINAV_AI_TIMEOUT_SECONDS must be a positive integer")
    return timeout


def _diagnostic(output: str) -> str:
    cleaned = " ".join(output.strip().split())
    if len(cleaned) > 500:
        return cleaned[:497] + "..."
    return cleaned


def _missing_configuration_message() -> str:
    return (
        "No model is configured for this project. Run 'decinav setup' once (it finds "
        "claude or codex on your PATH and saves .decinav/config.json).\n"
        "For CI or a custom runner, set DECINAV_AI_CMD instead, for example:\n"
        "  DECINAV_AI_CMD='claude -p \"{prompt}\"'\n"
        "  DECINAV_AI_CMD='sol run --prompt \"{prompt}\"'\n"
        "  DECINAV_AI_CMD='agent --prompt-file \"{prompt_file}\"'\n"
        "  DECINAV_AI_CMD='agent-that-reads-stdin'\n"
        "For tests: DECINAV_AI_CMD=mock and DECINAV_AI_MOCK_RESPONSE=/path/to/fixture"
    )
