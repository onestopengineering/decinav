---
name: decinav-init
description: Set up architecture decision records (using decinav) for a new product (from a product brief) or an existing codebase (from its structural files). Use when a project has no docs/adr yet, when the user asks to start recording decisions, or when they say "init ADRs".
argument-hint: "[path-to-brief.md]"
---

# /decinav-init

Initialize `docs/adr`, create the Day 0 baseline, validate it, and explain the loop.

## Prerequisites

- `decinav` on `PATH`. Check with `decinav --help`; if missing, stop and tell the user to install it.
- Run every command from the project root (the directory that will contain `docs/adr`).

## Steps

1. **Detect the situation.**
   - Run `git rev-parse --show-toplevel`. If it fails, tell the user the tool needs a Git repository and stop.
   - Check whether `docs/adr` exists and contains `*.md` files other than `README.md`. If it does, run `decinav context --compact`, say the project is already initialized with that many ADRs, and offer `/decinav-review` or `/decinav-query` instead. Do not run `baseline` on an initialized repository.
   - Count source files (for example `git ls-files | grep -vE '\.md$' | wc -l`). Fewer than about 5 non-Markdown files means a new product.
2. **Initialize.** Run `decinav init --existing`. This creates `docs/adr/README.md` and installs a warning-only `commit-msg` hook. If it refuses because of a foreign hook, show the message and ask the user how to proceed; never pass `--overwrite-hook` without an explicit yes.
3. **Create the baseline.**
   - New product, or the user passed a brief path in `$ARGUMENTS`: confirm the brief path (ask if none was given; a product brief, PRD, or README describing intent is fine), then run `decinav baseline --from-brief <path>`. Tell the user the result is *intended* architecture, not observed code.
   - Existing product: run `decinav baseline`. It reads only manifests, Dockerfiles, Compose, Terraform, CI, and Kubernetes files; it never sends source code or secrets.
   - Both forms need a model. If the command reports `No model is configured`, run `decinav setup --show` to see which CLIs are installed, ask the user which one (claude or codex) and which model they want, then run `decinav setup --provider <claude|codex> --model <name>` (omit `--model` to keep that CLI's default) and retry. Never choose the provider or model for them. Do not write the baseline yourself.
4. **Validate.** Run `decinav validate`. It must print `Validated 1 ADR(s).` If it fails, show the issues verbatim and stop.
5. **Explain the loop** in a short message:
   - `/decinav-plan <feature>` writes a PRD grounded in decisions and checks its impact.
   - `/decinav-decide` turns proposed decisions into ADRs; they stay `proposed` until a human runs `/decinav-review`.
   - Commits that touch `src/` or `infra/` should reference `[ADR-XXXX]` (or `[skip-adr]`); the installed hook warns when they do not.
   - `decinav ui` opens a local browser workbench for the graph; `/decinav-query` answers questions with checked citations.

## Token notes

- Use `decinav context --compact` (one line per ADR) when you need to look at an existing repository; never load full bodies here.
- Do not paste the generated baseline back to the user; report its path and title.

## Output

Report which mode ran (brief or structural), the created files, the validate result, and the three next commands.

## Never

- Never create or edit files under `docs/adr/` yourself; only `decinav` writes there.
- Never label a brief-derived baseline as observed architecture.
- Never overwrite a foreign Git hook without an explicit yes.
