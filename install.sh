#!/usr/bin/env bash
# Install the decinav skills for a coding-agent host.
#
#   ./install.sh claude        # print the Claude Code plugin commands (verified)
#   ./install.sh claude-local  # symlink skills into ~/.claude/skills (verified path)
#   ./install.sh codex         # symlink skills into ~/.agents/skills (verified path)
#   ./install.sh cursor        # print instructions (untested)
#   ./install.sh --uninstall <platform>
#
# Verified paths come from the Claude Code skills reference
# (https://code.claude.com/docs/en/skills: ~/.claude/skills/<name>/SKILL.md) and the
# Codex skills documentation (https://developers.openai.com/codex/skills:
# $HOME/.agents/skills). Cursor is untested: no official skill path was confirmed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$REPO_DIR/decinav-plugin/skills"
MARKER="decinav-plugin/skills"

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

skills() {
  find "$SKILLS_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort
}

target_dir() {
  case "$1" in
    claude-local) printf '%s\n' "$HOME/.claude/skills" ;;
    codex) printf '%s\n' "$HOME/.agents/skills" ;;
    *) return 1 ;;
  esac
}

link_skills() {
  local target="$1"
  mkdir -p "$target"
  while IFS= read -r skill; do
    ln -sfn "$SKILLS_DIR/$skill" "$target/$skill"
    printf '  linked %s -> %s\n' "$target/$skill" "$SKILLS_DIR/$skill"
  done < <(skills)
}

unlink_skills() {
  local target="$1"
  [[ -d "$target" ]] || return 0
  while IFS= read -r skill; do
    local link="$target/$skill"
    if [[ -L "$link" ]] && [[ "$(readlink "$link")" == *"$MARKER"* ]]; then
      rm "$link"
      printf '  removed %s\n' "$link"
    fi
  done < <(skills)
}

require_decinav() {
  if ! command -v decinav >/dev/null 2>&1; then
    printf 'Warning: decinav is not on PATH. Install it first (pip install -e "%s").\n' "$REPO_DIR" >&2
  fi
}

install_platform() {
  local platform="$1"
  case "$platform" in
    claude)
      cat <<MSG
Claude Code (verified plugin format). Inside a Claude Code session run:

  /plugin marketplace add onestopengineering/decinav
  /plugin install decinav@decinav

For a local checkout instead:

  claude plugin marketplace add "$REPO_DIR"
  claude plugin install decinav@decinav

Skills appear as /decinav:decinav-init, /decinav:decinav-plan, and so on.
MSG
      ;;
    claude-local|codex)
      local target
      target="$(target_dir "$platform")"
      printf 'Linking skills for %s into %s\n' "$platform" "$target"
      link_skills "$target"
      if [[ "$platform" == "codex" ]]; then
        printf '\nCodex invokes skills with $ (for example $decinav-plan). Restart Codex to load them.\n'
      else
        printf '\nRestart Claude Code to load them as /decinav-init, /decinav-plan, and so on.\n'
      fi
      ;;
    cursor)
      cat <<MSG
Cursor (untested): no official skill directory was verified for this installer.
Options that Cursor documents for plugins in general:
  1. Open Cursor Settings -> Plugins and add this repository's URL.
  2. Or copy $SKILLS_DIR/<skill> into a location your Cursor version reads.
Please report what worked so this installer can be updated.
MSG
      ;;
    *)
      printf 'Unknown platform: %s\n\n' "$platform" >&2
      usage >&2
      exit 1
      ;;
  esac
}

uninstall_platform() {
  local platform="$1"
  case "$platform" in
    claude)
      printf 'Run inside Claude Code: /plugin uninstall decinav@decinav\n'
      ;;
    claude-local|codex)
      unlink_skills "$(target_dir "$platform")"
      ;;
    cursor)
      printf 'Cursor (untested): remove the plugin from Cursor Settings -> Plugins.\n'
      ;;
    *)
      printf 'Unknown platform: %s\n' "$platform" >&2
      exit 1
      ;;
  esac
}

main() {
  if [[ $# -eq 0 ]] || [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    usage
    exit 0
  fi
  if [[ "$1" == "--uninstall" ]]; then
    [[ $# -ge 2 ]] || { printf 'Usage: install.sh --uninstall <platform>\n' >&2; exit 1; }
    uninstall_platform "$2"
    exit 0
  fi
  require_decinav
  install_platform "$1"
}

main "$@"
