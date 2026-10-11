# Changelog

All notable changes to `decinav` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.8.0 - 2026-09-28

The tool becomes agent-first. A coding agent (Claude Code, Codex, Cursor, or any
CLI that can run shell commands) reads the validated decision corpus, proposes
PRDs and ADRs, and publishes them only through deterministic gates that a human
then accepts. The subprocess path through `DECINAV_AI_CMD` remains for CI and
headless use.

### Added

- `decinav setup`: detects `claude` or `codex` on PATH and saves `.decinav/config.json`; every model-backed command and the browser use it. Built-in isolated runners (empty directory, no tools, prompt on stdin). `DECINAV_AI_CMD` remains an override for CI.
- Browser Setup tab: model selector that writes the same config; model-backed buttons stay disabled until a model exists.
- `init` states that nothing is committed and how to undo.
- The commit-msg hook watches the code folders that exist (`src`, `app`, `lib`, `cmd`, `infra`, ...) instead of a fixed `src|infra`; override with `init --watch <folder>`.
- Model answers may end a line with `[UNKNOWN]` to abstain; such lines need no citation.

- `decinav context [--format text|json]`: the validated ADR bundle, with
  provenance and freshness, for an agent to read.
- `decinav propose --from-file <envelope.json>`: publish agent-authored ADRs
  through the same strict envelope, prospective validation, and atomic publish
  used by `ingest`. An optional `provenance` object records the generator and
  sources.
- `decinav accept <ids>`, `decinav reject <id>`, `decinav attest <ids>`: the
  human review gate. Accept promotes proposed ADRs and regenerates the graph.
  Reject deletes a proposed ADR only when nothing references it. Attest records
  that a human reviewed an ADR as current at the present commit.
- `decinav check-answer [file] [--require-states]`: deterministic check that an
  agent-written answer cites known ADRs on every factual line and, when
  required, carries exactly one claim state per line: `[DOCUMENTED]`,
  `[VERIFIED_IN_CODE]`, `[CONTRADICTED]`, or `[UNKNOWN]`.
- `decinav prd new <title>` and `decinav prd lint <file>`: a grounded PRD
  template with required sections and a lint that checks structure and that
  every referenced ADR exists.
- `decinav baseline --from-brief <file>`: the new-product path. The Day 0
  record is derived from a product brief and labeled as intended architecture,
  never as observed code.
- `decinav impact --save`: also writes `.decinav/last-impact.json` so the browser
  workbench can overlay the last blast-radius result.
- Provenance sidecar at `.decinav/provenance.json`: for every ADR, who generated
  it, from which sources, at which commit, and when it was accepted or
  attested.
- Freshness computed from Git at read time: `fresh`, `aging`,
  `sources-changed`, or `unknown`. Shown in `context`, in the UI, and inside the
  prompt context for `impact` and `query`.
- Skill pack under `decinav-plugin/` with `/decinav-init`, `/decinav-plan`,
  `/decinav-decide`, `/decinav-query`, `/decinav-review`, and `/decinav-impact`, plus a plugin
  marketplace manifest and an install script.
- Interactive browser workbench: layered layout, click a node to read the full
  decision with relationships and freshness, search, focus, status and domain
  filters, and an impact overlay loaded from the saved result.
- UI service endpoints `GET /api/adr` and `GET /api/impact/last`; the status
  payload now carries provenance and freshness per record.

### Changed

- The ingest and baseline prompts require a `## Sources` section and instruct
  the model to use `status: proposed` for every extracted decision.
- The query and impact prompts explain freshness and ask the model to say when a
  decision is proposed or possibly outdated.
- `decinav new` records human provenance at the current commit.

### Breaking

- Ingested and proposed ADRs are always written with `status: proposed`. The
  gate rejects any other status with `generated-status`. Promote with
  `decinav accept` after review. Previously the model could emit `accepted`.
- Generated and proposed ADRs must contain a `## Sources` section. The gate
  rejects records without one with `missing-sources-section`.
- `decinav new` rejects `--depends-on` and `--supersedes` IDs that do not exist
  in the repository. Previously it created a dangling reference that only
  `validate` caught later.

## 0.7.0 - 2026-08-27

- Loopback-only browser workbench for onboarding, validation, graph refresh,
  PRD impact analysis, and architecture queries.

## 0.6.0 - 2026-08-27

- `decinav impact` with optional Mermaid blast-radius visual.
- `decinav query` with mandatory inline ADR citations.

## 0.5.0 and earlier - 2026-08-27

- Strict V1 and V2 frontmatter parsing, deterministic ID allocation,
  `decinav new`, Mermaid graph generation with focus and domain filters,
  strict repository validation, commit-message hook installation, Day 0
  baseline generation, and atomic ingestion of informal documents.
