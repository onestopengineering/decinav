---
name: decinav-impact
description: Analyze which architecture decisions a PRD affects, relies on, contradicts, or needs, using decinav's impact analysis. Use when the user has a PRD or feature document and asks what it touches, its blast radius, or its risks.
argument-hint: "<path-to-prd.md>"
---

# /decinav-impact

Assess the blast radius of the PRD at "$ARGUMENTS" against the decision graph.

## Prerequisites

- `decinav` on `PATH`; run from the project root (contains `docs/adr`).
- The PRD must be a Markdown file. If the user gave text instead of a path, save it under `docs/prds/` first (`decinav prd new "<title>"`, then fill it).

## Steps

1. **Lint.** Run `decinav prd lint <path>`. If it fails only because sections are empty, tell the user which ones and ask whether to continue with the analysis anyway; unknown ADR references must be fixed first.
2. **Analyze.**
   - If a model is configured via `decinav setup` or `DECINAV_AI_CMD`: run `decinav impact <path> --visual --save`. Show its Markdown report (without the Mermaid block unless asked). It also writes `.decinav/last-impact.json` for the browser overlay.
   - If it is not set: run `decinav context --compact`, pick the ADRs the PRD could touch (by domain, title, first sentence, relationships), run `decinav context --format json --ids <picked>`, read the PRD, and produce the same four lists yourself: directly affected ADRs (the feature changes, conflicts with, or forces a revisit of the decision) with a reason each; prerequisite ADRs (relied on but unchanged) with a reason each; architectural risks (contradictions between the PRD and a current rule); proposed new ADR titles. Use only IDs from the compact listing. Say clearly that this assessment was made by you, not by `decinav impact`, and do not write `.decinav/last-impact.json`.
3. **Flag freshness.** For every ADR you list, note if its freshness is `aging` or `sources-changed`; the decision may be outdated and the risk may be stale.
4. **Offer next steps.** `decinav ui` shows the overlay (red affected, blue prerequisites, green dashed proposed) when a saved result exists; `/decinav-decide` records the proposed decisions.

## Token notes

- Manual mode uses the two-step pattern: `--compact`, then `--ids` for the picked ADRs. Never load every body.
- Report the four lists with one line per item; do not paste ADR bodies or the PRD back.

## Output

The four lists, freshness flags, and the next-step offer.

## Never

- Never cite an ADR ID absent from the context.
- Never modify the PRD or any ADR during analysis.
- Never claim the analysis proves the full runtime blast radius; it lists evidence-supported candidate effects.
