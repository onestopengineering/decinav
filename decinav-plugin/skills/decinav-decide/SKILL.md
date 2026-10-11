---
name: decinav-decide
description: Record new architecture decisions as ADRs through decinav's validation gate. Use after /decinav-plan lists proposed decisions, or when the user states a decision they want captured ("we decided to use X because Y").
argument-hint: "[path-to-prd.md or the decision in a sentence]"
---

# /decinav-decide

Write one or more ADRs as an envelope and publish them with `decinav propose`. They land as `proposed`; a human accepts them later with `/decinav-review`.

## Prerequisites

- `decinav` on `PATH`; run from the project root (contains `docs/adr`).
- Input: a PRD path whose "Proposed decisions" section lists titles, or a decision described in `$ARGUMENTS`.

## Steps

1. **Survey the decisions.** Run `decinav context --compact`. The highest existing ID plus one is the next ID; further records in the same envelope continue contiguously.
2. **Load only what you relate to.** From the compact listing pick the ADRs the new decision depends on or supersedes (usually 1 to 4), then run `decinav context --format json --ids <picked>` so every relationship reason is specific to what those ADRs actually say.
3. **Gather evidence.** For each decision: the PRD section, the related ADRs, and any code you read. If a decision replaces an existing one, it goes in `supersedes` with a reason; if it relies on one, `depends_on` with a reason. A relationship without evidence is left out.
4. **Write the envelope** to `.decinav/proposals/<slug>.json` (create the directory if needed):

   ```json
   {
     "records": [
       {
         "filename": "0007-generate-exports-in-background.md",
         "content": "---\nschema_version: 2\nid: \"ADR-0007\"\ntitle: \"Generate exports in a background job\"\nstatus: \"proposed\"\ndate: \"2026-09-28\"\ndomain: \"backend\"\ndepends_on:\n  - id: \"ADR-0002\"\n    reason: \"Exports reuse the asynchronous evaluation boundary.\"\nsupersedes: []\n---\n\n# Generate exports in a background job\n\n## Context\n\n...\n\n## Decision\n\n...\n\n## Consequences\n\n...\n\n## Sources\n\n- docs/prds/report-export.md\n- app/Jobs/EvaluateSubmission.php\n"
       }
     ],
     "provenance": {
       "generator": "claude-code",
       "sources": ["docs/prds/report-export.md"]
     }
   }
   ```

   Rules the gate enforces: `schema_version: 2`; exactly the eight frontmatter keys; `status: "proposed"`; the filename's four-digit prefix equals the ID; `domain` in frontend, backend, infrastructure, data, system; every relationship has `id` and a non-empty `reason`; the body has Context, Decision, Consequences, and a `## Sources` section; no cycles; every related ID exists. Keep each body to the point: a few sentences per section.
5. **Publish.** Run `decinav propose --from-file .decinav/proposals/<slug>.json`.
   - On exit 1, read the issue codes (`generated-status`, `missing-sources-section`, `candidate-id-sequence`, `dangling-reference`, `circular-relationship`, `malformed-frontmatter`, and others), fix the envelope, and rerun. Up to three attempts; then show the errors to the user.
   - On success it prints the created files and regenerates `docs/adr/README.md`.
6. **Report.** List the created ADR IDs and titles, state that they are `proposed` pending human review, and suggest `/decinav-review`.

## Token notes

- `--compact` first, then `--ids` for the related ADRs only. Never load every body.
- Do not echo the envelope or the new ADR text to the user; report IDs, titles, and file paths.

## Output

Created files, the provenance line printed by `decinav propose`, and the review hand-off.

## Never

- Never write or edit `docs/adr/*.md` directly, even to fix a rejected record; fix the envelope and rerun `propose`.
- Never set `status` to anything other than `proposed`.
- Never invent a relationship, a source, or a fact the evidence does not support; write the uncertainty into the ADR instead.
