# decinav walkthrough: how it works inside, and how to try it

Written 2026-09-28 for the v0.8 working tree. Read this before explaining the
system to someone else, then run the "Try it in 15 minutes" section yourself.

## 1. The one-paragraph pitch

Every important architecture decision becomes a small Markdown file with strict
metadata. Deterministic Python validates the whole set as a graph: unique IDs,
real references, no cycles, reasons on every edge. A coding agent can read that
graph, propose new decisions, and answer questions, but it can never write trusted
state directly: everything goes through a gate, and a human accepts. Provenance
records where each decision came from, and Git tells you how stale it is.

Three roles, never mixed:

```text
agent or model      proposes   (baseline, ingest, propose, impact, query drafts)
deterministic code  decides    (parse, validate, publish, graph, freshness, check-answer)
human               approves   (accept, reject, attest)
```

## 2. The internal pipeline, module by module

### Reading an ADR

`frontmatter.py` splits the file at the two `---` lines, loads the YAML with a
safe loader that also rejects duplicate keys, and checks the exact key set
(schema version 2 adds `reason` on every relationship). Anything unknown is an
error, never a warning. Output: an `ADRMetadata` value plus the Markdown body.

### Reading the repository

`repository.py` loads every `NNNN-*.md` under `docs/adr`, keeps malformed files
as issues, then checks filename/ID agreement, duplicate IDs, dangling references,
and causal cycles (iterative strongly connected components, so deep graphs cannot
hit the recursion limit). Output: `RepositoryAnalysis` with `unique_records` and
`issues`. `validate` exits 1 on any issue; `graph` renders what it can and warns.

### Writing generated ADRs (the gate)

`generation.py` is the gate every model or agent output passes:

```text
stdout or envelope file
  -> exactly one JSON object with `records` (optional `provenance`)
  -> safe filenames, no duplicates, no null bytes
  -> each record parses as strict V2
  -> status must be proposed (baseline op: baseline)
  -> a "## Sources" section must exist
  -> IDs contiguous from the next free number
  -> the complete prospective repository (existing + candidates) is analyzed
  -> stage all files in a temp dir, fsync, hard-link into place, roll back on failure
```

`workflows.py` wires it: `run_baseline`, `run_ingest`, `run_propose`. Order after
validation is write, record provenance, regenerate graph, and only then report a
provenance failure, so a bad sidecar can never leave ADRs on disk without a graph.

### Provenance and freshness

`provenance.py` keeps `.decinav/provenance.json` (tracked in Git, not derived). Per
ADR: `generated_by`, `generator`, `generated_at`, `commit`, `sources`, plus
`accepted_*` and `attested_*` stamps. Freshness is computed on read:

| State | Rule |
|---|---|
| fresh | zero project commits since the reference commit (attested, else accepted, else generated), ignoring `docs/adr` and `.decinav` |
| aging | commits since, none touching the recorded sources |
| sources-changed | at least one commit since touched a recorded source (`git diff --name-only --relative`) |
| unknown | no provenance, no Git, or the commit is not an ancestor of HEAD (rebase/squash) |

Sources per ADR come from the record's own Sources section, narrowed to files that
were actually in the bundle. Freshness is commit distance, not semantic verification.

### Human review

`review.py`: `accept` rewrites only the `status:` line inside the frontmatter
(preserving line endings), re-parses to prove validity, records the reviewer commit,
regenerates the graph. `reject` deletes a proposed ADR only when nothing references
it. `attest` records "reviewed as current at this commit" without changing the file.

### Intelligence

`intelligence.py` builds the condensed context (frontmatter, Context, Decision, and
a FRESHNESS line per ADR) that both the prompts and `decinav context` use.
`context --compact` is one line per ADR so an agent can choose what to load with
`--ids`. `check_answer` enforces: every factual line cites a known `[ADR-XXXX]`,
and with `--require-states` carries exactly one of `[DOCUMENTED]`
`[VERIFIED_IN_CODE]` `[CONTRADICTED]` `[UNKNOWN]` (UNKNOWN lines need no citation).
`run_query` retries once with the rejection reason before failing. `impact --save`
writes `.decinav/last-impact.json` for the browser overlay.

### The model boundary

`ai_adapter.py` runs whatever `DECINAV_AI_CMD` names, without a shell, with the prompt
as an argument, a temp file, or stdin. `mock` reads a fixture. The agent-first path
does not use it at all: the agent inside Claude Code or Codex reads `context` and
writes an envelope for `propose`.

### The browser

`web_ui.py` is a standard-library HTTP server bound to loopback with a random token
embedded as JSON in the page (CSP forbids inline scripts). It serves three static
assets from an allowlist and exposes `status`, `adr`, `impact/last`, and the action
endpoints, all calling the same Python workflows. `ui/app.js` lays out the visible
subset with vendored dagre, draws SVG itself, and switches to collapsed domain
clusters above 60 decisions so large systems stay readable.

## 3. State models worth memorizing

- ADR status: `proposed -> accepted`, `accepted -> deprecated | superseded`, plus
  `baseline` for ADR-0001. Generated records always start proposed.
- Freshness: fresh, aging, sources-changed, unknown.
- Claim states in answers: DOCUMENTED, VERIFIED_IN_CODE, CONTRADICTED, UNKNOWN.
- Validation issue codes: malformed-frontmatter, filename-id-mismatch, duplicate-id,
  dangling-reference, circular-relationship, candidate-id-sequence, file-collision,
  generated-status, missing-sources-section, baseline-*.

## 4. Try it in 15 minutes

Use any Git repository that has a few ADRs, or the fixture under `tests/fixtures/legacy-monolith` after running `python scripts/run_e2e.py --keep`.

```bash
cd /path/to/decinav
source .venv/bin/activate
cd /path/to/your-repo

decinav validate                     # 17 ADRs
decinav context --compact            # one line per decision with freshness
decinav context --format json --ids ADR-0005,ADR-0017 | head -60
decinav prd new "Course export"      # then edit docs/prds/course-export.md
decinav prd lint docs/prds/course-export.md
decinav ui --port 8790               # click a node, try search, Timeline, How to read this
```

Propose a decision the way an agent would:

```bash
cat > /tmp/envelope.json <<'JSON'
{"records":[{"filename":"0018-example.md","content":"---\nschema_version: 2\nid: \"ADR-0018\"\ntitle: \"Example decision\"\nstatus: \"proposed\"\ndate: \"2026-09-28\"\ndomain: \"backend\"\ndepends_on:\n  - id: \"ADR-0017\"\n    reason: \"Builds on the read-time leaderboard.\"\nsupersedes: []\n---\n\n# Example decision\n\n## Context\n\nWhy.\n\n## Decision\n\nWhat.\n\n## Consequences\n\nSo what.\n\n## Sources\n\n- docs/prds/course-export.md\n"}],"provenance":{"generator":"me","sources":["docs/prds/course-export.md"]}}
JSON
decinav propose --from-file /tmp/envelope.json
decinav accept ADR-0018
decinav reject ADR-0018      # refused: accepted ADRs cannot be rejected
```

Check an answer the way the query skill does:

```bash
printf -- '- Go alone updates submission status [ADR-0005] [DOCUMENTED]\n- Recovery after a failed dispatch is not defined [UNKNOWN]\n' \
  | decinav check-answer --require-states
```

Run the model path once (about 1 to 5 minutes per call on Sonnet):

```bash
decinav setup                 # finds claude or codex; no environment variable needed
decinav query "What happens if Redis XADD fails after a submission row is created?"
decinav impact docs/prds/course-export.md --visual --save
```

Inside Claude Code, the same flow is `/decinav-plan`, `/decinav-decide`, `/decinav-query`,
`/decinav-review` after installing the plugin from this repository.

## 5. How to explain it to different people

- To an engineer: "ADRs as a validated graph with an agent gate. The model
  proposes, Python validates and publishes, a human accepts. Git gives freshness."
- To a product manager: "Every decision has a why, a date, what it replaced, and
  how stale it is. Before a feature, impact tells you which decisions it touches."
- Against code-graph tools: they map what the code is. This records why it is
  that way and what a change would break. Point one at the other; do not merge them.

## 6. Known limits (be honest when asked)

- Freshness is commit distance. It does not prove an ADR is still true.
- Generated ADRs reflect their source documents; stale documents produce confident
  but outdated decisions. That is why everything starts proposed.
- Two concurrent writers to the same repository can still allocate the same ID.
- Cursor install is untested; Claude Code and Codex paths were verified from docs.
- `decinav setup` runs the model from an empty temporary directory with no tools; running `claude -p` inside a repo you have not opened in Claude Code fails on workspace trust, which is why the built-in runner does not do that.
