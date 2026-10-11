# decinav plugin

Skills that let a coding agent use `decinav` as the source of truth for
architecture decisions. The agent reads the validated ADR graph, reasons with the
code it can inspect, and writes only through `decinav` commands that validate
before publishing. It never edits `docs/adr/*.md` directly and never marks a
decision accepted; a human does that.

## Requirements

- `decinav` on `PATH` (from this repository: `pip install -e .`, then
  `decinav --help` must work).
- Run skills from the project root, the directory that contains `docs/adr`.
- `DECINAV_AI_CMD` is optional. When it is set (for example
  `DECINAV_AI_CMD='claude -p --tools ""'`), `decinav-impact` and `decinav-plan` call
  `decinav impact`. When it is unset, the skills reason about impact themselves
  from `decinav context` and do not write an impact file.

## Skills

| Skill | Purpose |
|---|---|
| `/decinav:decinav-init` | Set up ADRs for a new product (from a brief) or an existing one (from code structure). |
| `/decinav:decinav-plan <feature>` | Write a grounded PRD, lint it, and assess which decisions the feature touches. |
| `/decinav:decinav-decide` | Turn proposed decisions into ADRs through `decinav propose`. |
| `/decinav:decinav-query <question>` | Answer from the ADRs plus verified code, every line cited and labelled, checked by `decinav check-answer`. |
| `/decinav:decinav-review` | Walk proposed ADRs with the human and run `accept` or `reject` on their say-so. |
| `/decinav:decinav-impact <prd.md>` | Blast-radius analysis of an existing PRD. |

The loop these skills implement:

```text
decinav-init  ->  decinav-plan  ->  decinav-decide  ->  decinav-review  ->  implement with [ADR-XXXX] commits
                 ^                                                 |
                 +----------------- decinav-query / decinav-impact ---------+
```

## Token discipline

These skills run inside your own Claude or Codex subscription, so they load as
little as possible. Every skill first runs `decinav context --compact` (one line
per ADR), picks the few decisions relevant to the task, and only then runs
`decinav context --format json --ids <picked>` for full text. Only `decinav-review`
loads several bodies, and only the proposed ones. Skills report paths, IDs, and
short lists rather than pasting ADR bodies back.

## Install

Claude Code (verified format, marketplace at this repository's root):

```text
/plugin marketplace add onestopengineering/decinav
/plugin install decinav@decinav
```

Other hosts: run `./install.sh <platform>` from the repository root. See
`./install.sh --help` for which platforms are verified.
