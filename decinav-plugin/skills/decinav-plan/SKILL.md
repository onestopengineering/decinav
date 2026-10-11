---
name: decinav-plan
description: Plan a feature or change against the recorded architecture decisions. Writes a grounded PRD with decinav, lints it, and assesses which ADRs the change affects. Use when the user says "I want to add/change X", asks for a PRD, or asks what a feature would touch.
argument-hint: "<feature or change, in a sentence>"
---

# /decinav-plan

Turn "$ARGUMENTS" into a PRD that is grounded in the decision graph and the code.

## Prerequisites

- `decinav` on `PATH`; run from the project root (contains `docs/adr`).
- If `docs/adr` has no ADRs, stop and suggest `/decinav-init`.

## Steps

1. **Survey the decisions.** Run `decinav context --compact`. Each line is `id | status | domain | freshness | title | decision (first sentence) | relationships`. If it exits 1, show the validation issues and stop; the repository must validate first.
2. **Pick the relevant ADRs** by domain, title, first sentence, and relationships: the ones the change could alter, rely on, or contradict. Typically 2 to 6. Then run `decinav context --format json --ids <picked, comma-separated>` and read only those bodies (`context`, `decision`, `consequences`, `freshness`, relationships).
3. **Investigate the code.** Use Grep and Read on the parts of the repository the change touches: entry points, validation, state ownership, persistence, background jobs, failure handling. Record file paths you actually opened; you will cite them.
4. **Create the PRD.** Run `decinav prd new "<short title>"`. It prints the path under `docs/prds/`. If it refuses because the file exists, stop and ask the user whether to reuse that PRD or pick a different title; never overwrite a PRD the user wrote.
5. **Fill every section** of the PRD (edit the file; it is yours to write). Keep it concise: a sentence or a short list per point.
   - Problem, Users, Requirements: numbered, testable, from the user's request and what you read.
   - Affected decisions: one line per relevant ADR as `ADR-XXXX: <why it is affected or relied on>`. Only IDs present in the compact listing.
   - Proposed decisions: titles of new architectural decisions this change needs, or `None`.
   - Open questions: facts you could not establish from ADRs or code. Never fill a gap with a guess.
   - Sources: the ADR IDs and code paths you used.
   Delete the template comments.
6. **Lint until clean.** Run `decinav prd lint <path>`. Fix every reported issue and rerun until it prints `PRD OK`.
7. **Assess impact.**
   - If a model is configured via `decinav setup` or `DECINAV_AI_CMD` (`printenv DECINAV_AI_CMD`): run `decinav impact <path> --visual --save` and use its output.
   - Otherwise: reason yourself from the bodies you loaded. Produce the same four lists (directly affected ADRs with reasons, prerequisite ADRs with reasons, architectural risks as contradictions between the PRD and a current rule, proposed new ADR titles). Do not write `.decinav/last-impact.json` yourself. Update the PRD's Affected decisions if the assessment changed it, and lint again.
8. **Report and hand off.** Summarize the four lists with one sentence per item. Flag any cited ADR whose freshness is `aging` or `sources-changed`. Ask whether to proceed to `/decinav-decide` for the proposed decisions.

## Token notes

- Always the two-step pattern: `--compact` first, then `--format json --ids` for the few ADRs you picked. Never run plain `decinav context --format json` here.
- Do not paste ADR bodies or the whole PRD back to the user; give the path and the four lists.

## Output

The PRD path, the lint result line, the four impact lists, freshness warnings, and the hand-off question.

## Never

- Never cite an ADR ID that is not in the `decinav context` output.
- Never edit `docs/adr/*.md`; the PRD is the only file this skill writes.
- Never present the manual impact assessment as if `decinav impact` ran.
