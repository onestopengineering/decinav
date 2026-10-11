"""Contract tests for the agent skill pack: manifests, frontmatter, and command references."""

import json
from pathlib import Path
import re
import subprocess

import pytest
import yaml

from decinav.cli import app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "decinav-plugin"
SKILLS_ROOT = PLUGIN_ROOT / "skills"
EXPECTED_SKILLS = {
    "decinav-init",
    "decinav-plan",
    "decinav-decide",
    "decinav-query",
    "decinav-review",
    "decinav-impact",
}
_FRONTMATTER_PATTERN = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n", re.DOTALL)
_COMMAND_PATTERN = re.compile(r"decinav\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?")


def registered_commands() -> tuple[set[str], dict[str, set[str]]]:
    commands = {command.name for command in app.registered_commands}
    groups = {
        group.name: {command.name for command in group.typer_instance.registered_commands}
        for group in app.registered_groups
    }
    return commands, groups


def skill_files() -> list[Path]:
    return sorted(SKILLS_ROOT.glob("*/SKILL.md"))


def frontmatter(path: Path) -> dict:
    match = _FRONTMATTER_PATTERN.match(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path} must start with YAML frontmatter"
    data = yaml.safe_load(match.group("yaml"))
    assert isinstance(data, dict), f"{path} frontmatter must be a mapping"
    return data


def test_every_expected_skill_exists() -> None:
    assert {path.parent.name for path in skill_files()} == EXPECTED_SKILLS


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_skill_frontmatter_names_its_directory(path: Path) -> None:
    data = frontmatter(path)
    assert data.get("name") == path.parent.name
    assert isinstance(data.get("description"), str) and data["description"].strip()
    assert len(data["description"]) <= 1536


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_skill_references_only_real_decinav_commands(path: Path) -> None:
    commands, groups = registered_commands()
    text = path.read_text(encoding="utf-8")
    for match in _COMMAND_PATTERN.finditer(text):
        first, second = match.group(1), match.group(2)
        if first == "--help":
            continue
        assert first in commands or first in groups, f"{path}: unknown command 'decinav {first}'"
        if first in groups:
            assert second in groups[first], f"{path}: unknown subcommand 'decinav {first} {second}'"


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_skill_states_never_rules_and_prerequisites(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "## Prerequisites" in text
    assert "## Never" in text
    assert "## Token notes" in text
    assert "docs/adr" in text


@pytest.mark.parametrize("path", skill_files(), ids=lambda path: path.parent.name)
def test_skill_uses_compact_then_ids_context_pattern(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "decinav context --compact" in text
    if path.parent.name != "decinav-init":
        assert "--ids" in text, f"{path}: must narrow full bodies with --ids"
    for line in text.splitlines():
        if "decinav context --format json" in line and "--ids" not in line:
            assert "Never" in line or "never" in line, (
                f"{path}: full-body context without --ids: {line.strip()}"
            )


def test_plugin_manifest_is_valid() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["name"] == "decinav"
    assert manifest["description"].strip()
    assert manifest["author"]["name"].strip()
    assert (PLUGIN_ROOT / "README.md").is_file()


def test_marketplace_points_at_plugin_directory() -> None:
    marketplace = json.loads(
        (REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8")
    )
    assert marketplace["name"]
    assert marketplace["owner"]["name"]
    entries = marketplace["plugins"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "decinav"
    source = entry["source"]
    assert source.startswith("./") and ".." not in source
    assert (REPOSITORY_ROOT / source).joinpath(".claude-plugin", "plugin.json").is_file()
    manifest = json.loads(
        (REPOSITORY_ROOT / source / ".claude-plugin" / "plugin.json").read_text("utf-8")
    )
    assert manifest["name"] == entry["name"]


def test_install_script_parses_and_documents_platforms() -> None:
    script = REPOSITORY_ROOT / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    result = subprocess.run(
        ["bash", str(script), "--help"], check=True, capture_output=True, text=True
    )
    for platform in ("claude", "codex", "cursor", "--uninstall"):
        assert platform in result.stdout
