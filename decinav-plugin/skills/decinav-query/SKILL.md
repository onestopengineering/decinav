---
name: decinav-query
description: Answer a question about the system's architecture from the recorded ADRs plus code you verify, with every line cited and labelled, checked by decinav. Use when the user asks why something is designed a certain way, what a decision says, or whether the code still follows it.
argument-hint: "<question>"
---

# /decinav-query

Answer "$ARGUMENTS" so that every factual line is traceable and its verification state is explicit.

## Prerequisites

- `decinav` on `PATH`; run from the project root (contains `docs/adr`).

## Steps

1. **Survey the decisions.** Run `decinav context --compact`. If it exits 1, show the issues and stop.
2. **Pick the evidence.** From the compact lines choose the ADRs whose title, first sentence, domain, or relationships bear on the question (usually 1 to 5). Run `decinav context --format json --ids <picked>` and read their `context`, `decision`, `status`, and `freshness`.
3. **Verify in code where it matters.** For each claim you will make, try to confirm it with Grep or Read in the repository. Record the file path.
4. **Write the answer** to a temporary file (for example `.decinav/answers/<timestamp>.md`). Keep it short: answer the question, do not summarize every ADR. Rules:
   - One sentence per line. Headings and blank lines are allowed and are not checked.
   - Each factual line ends with exactly one citation `[ADR-XXXX]` and exactly one state tag:
     - `[VERIFIED_IN_CODE]` when you confirmed it in the code; add the path in the sentence.
     - `[DOCUMENTED]` when the ADR states it and you did not or could not verify it.
     - `[CONTRADICTED]` when the code disagrees with the ADR; say what the code does.
     - `[UNKNOWN]` when neither the ADRs nor the code establish the answer. An `[UNKNOWN]` line needs no citation.
   - Cite only IDs present in the compact listing. Do not answer from general knowledge.
   - When a cited ADR is `proposed`, say "proposed" in the sentence. When its freshness is `aging` or `sources-changed`, add a final line: `Freshness: ADR-XXXX is <state> (<commits_since> commits since <reference_commit>) [ADR-XXXX] [DOCUMENTED]`.
5. **Check.** Run `decinav check-answer --require-states <file>`. On exit 1, fix the listed lines and rerun. Show the answer only after it passes.
6. **Present** the checked answer verbatim, then one line stating how many claims were verified in code versus documented only.

## Token notes

- Two-step context only: `--compact`, then `--ids` for the ADRs you picked. Never load all bodies for one question.
- Do not paste ADR bodies into the answer; cite them.

## Output

The checked answer, then `Verified in code: N, documented only: M, contradicted: K, unknown: U`.

## Never

- Never show an answer that failed `check-answer`.
- Never cite an ADR ID that is not in the context.
- Never upgrade a `[DOCUMENTED]` claim to `[VERIFIED_IN_CODE]` without a file you opened.
