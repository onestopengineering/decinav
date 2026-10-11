from pathlib import Path
import shlex
import sys

import pytest

from decinav.ai_adapter import AIAdapterError, execute_ai


def python_command(script: str, argument: str | None = None) -> str:
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    if argument is not None:
        command += f" {shlex.quote(argument)}"
    return command


def test_missing_configuration_fails_with_actionable_examples(tmp_path: Path) -> None:
    with pytest.raises(AIAdapterError, match="No model is configured") as error:
        execute_ai("prompt", cwd=tmp_path, environ={})

    assert "claude -p" in str(error.value)
    assert "sol run" in str(error.value)
    assert "DECINAV_AI_CMD=mock" in str(error.value)


def test_mock_driver_reads_deterministic_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "response.json"
    fixture.write_text('{"records": []}\n', encoding="utf-8")

    response = execute_ai(
        "ignored prompt",
        cwd=tmp_path,
        environ={
            "DECINAV_AI_CMD": "mock",
            "DECINAV_AI_MOCK_RESPONSE": str(fixture),
        },
    )

    assert response.stdout == '{"records": []}\n'
    assert response.transport == "mock"


def test_mock_driver_uses_purpose_specific_fixture(tmp_path: Path) -> None:
    generic_fixture = tmp_path / "generic.json"
    generic_fixture.write_text("generic\n", encoding="utf-8")
    impact_fixture = tmp_path / "impact.json"
    impact_fixture.write_text("impact\n", encoding="utf-8")

    response = execute_ai(
        "ignored prompt",
        cwd=tmp_path,
        purpose="impact",
        environ={
            "DECINAV_AI_CMD": "mock",
            "DECINAV_AI_MOCK_RESPONSE": str(generic_fixture),
            "DECINAV_AI_MOCK_IMPACT_RESPONSE": str(impact_fixture),
        },
    )

    assert response.stdout == "impact\n"
    assert response.transport == "mock"


def test_adapter_uses_stdin_when_template_has_no_placeholder(tmp_path: Path) -> None:
    command = python_command("import sys; sys.stdout.write(sys.stdin.read())")

    response = execute_ai(
        "prompt over stdin",
        cwd=tmp_path,
        environ={"DECINAV_AI_CMD": command},
    )

    assert response.stdout == "prompt over stdin"
    assert response.transport == "stdin"


def test_prompt_placeholder_is_one_argument_and_never_shell_evaluated(
    tmp_path: Path,
) -> None:
    command = python_command(
        "import sys; sys.stdout.write(sys.argv[1])",
        "{prompt}",
    )
    marker = tmp_path / "must-not-exist"
    prompt = f"hello; touch {marker}"

    response = execute_ai(
        prompt,
        cwd=tmp_path,
        environ={"DECINAV_AI_CMD": command},
    )

    assert response.stdout == prompt
    assert response.transport == "argument"
    assert not marker.exists()


def test_prompt_file_transport_keeps_large_prompt_out_of_arguments(
    tmp_path: Path,
) -> None:
    command = python_command(
        "from pathlib import Path; import sys; sys.stdout.write(Path(sys.argv[1]).read_text())",
        "{prompt_file}",
    )
    prompt = "large prompt\n" * 20_000

    response = execute_ai(
        prompt,
        cwd=tmp_path,
        environ={"DECINAV_AI_CMD": command},
    )

    assert response.stdout == prompt
    assert response.transport == "prompt_file"


def test_large_inline_prompt_is_rejected_with_safer_transport_guidance(
    tmp_path: Path,
) -> None:
    command = python_command("print('unused')", "{prompt}")

    with pytest.raises(AIAdapterError, match=r"use \{prompt_file\} or omit"):
        execute_ai(
            "x" * (129 * 1024),
            cwd=tmp_path,
            environ={"DECINAV_AI_CMD": command},
        )
