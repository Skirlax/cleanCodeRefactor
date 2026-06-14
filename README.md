# Clean Code Refactor

Clean Code Refactor, or CCR, is a command-line tool for running Codex-backed refactors on Python projects without letting the agent edit the original working tree directly.

A run starts by copying the project into an isolated workspace. CCR then selects refactoring units, checks or adds tests, gives Codex a limited scope, verifies the result, commits accepted unit changes in the copy, and writes dashboards and diffs for review. The original repository changes only after you run `ccr apply --yes`.

CCR treats cleanup as a series of smaller attempts, each with logs, verification, and a patch you can inspect before applying.

## Why CCR Exists

LLMs are useful for local refactoring. Refactors can still go wrong in small ways that are easy to miss. A method rename can leak across a boundary, a helper can change behavior, or a test can pass while an edge case is lost.

CCR adds a control loop around the model. The copied workspace gives every attempt a rollback point. Unit selection keeps the scope readable. Verification commands catch obvious breakage. Optional judge mode gives passing diffs one more review pass. The dashboard keeps the diff, logs, and decisions in one place.

This gives AI-assisted refactoring an audit trail and enough context to decide whether a change belongs in the real project.

## What It Does

- Copies the target project into `/tmp/ccr/runs/<run-id>/workspace` by default.
- Extracts Python refactoring units as classes/functions, whole files, packages, or model-budgeted clusters.
- Ranks units with a maintenance-value score so the more useful cleanup targets can be handled first.
- Uses Langfuse-backed runtime prompts for retrieval, test audit, test writing, refactoring, and judging.
- Runs Codex CLI in scoped sandboxes for read-only analysis and workspace-write edits.
- Can generate characterization tests before refactoring when coverage is missing.
- Runs automatic or custom verification commands after edits.
- Commits accepted unit-level refactors inside the copied workspace.
- Produces `dashboard.html`, `diffs.html`, JSONL event logs, schema-bound Codex call logs, and a final summary.
- Applies the final patch to the original project only when you explicitly run `ccr apply --yes`.

## Current Scope

CCR is currently focused on Python projects. The CLI already has provider and language boundaries, but the implemented MVP path is Python extraction together with the `codex` and `heuristic` providers.

Support for an OpenAI API-key provider is reserved behind the provider interface, but it is not implemented yet.

## Installation

CCR requires Python 3.11 or newer.

```bash
git clone <this-repo-url>
cd cleanCodeRefactor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For development, install the extra test and lint tools:

```bash
pip install -e ".[dev]"
```

For normal Codex-backed refactoring, make sure the `codex` CLI is installed and available on `PATH`.

CCR also reads `.env`, `.env.local`, or a file pointed to by `CCR_ENV_FILE`. Start from the example:

```bash
cp .env.example .env
```

Then fill in the values you need:

```env
LANGFUSE_BASE_URL=http://localhost:3001
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
OPENAI_API_KEY=...
```

Runtime prompts are stored in Langfuse. The bundled YAML file serves as a backup and bootstrap source:

```bash
ccr langfuse sync-prompts --input ccr/langfuse_related/prompt_backups.yaml
ccr langfuse check-prompts
```

## Quick Start

First, preview what CCR would work on:

```bash
ccr plan /path/to/python/project
```

Then run a small refactor that is easy to review:

```bash
ccr refactor /path/to/python/project \
  --max-units 3 \
  --judge \
  --verify-command "python -m pytest"
```

CCR prints a run directory and writes a dashboard:

```text
/tmp/ccr/runs/run-YYYYMMDDTHHMMSSZ/dashboard.html
```

Open the dashboard and diff viewer, inspect the accepted commits in the copied workspace, and run any extra checks you care about. When you are ready, apply the patch back to the original project:

```bash
ccr apply /path/to/python/project \
  --run /tmp/ccr/runs/run-YYYYMMDDTHHMMSSZ \
  --yes
```

## Everyday Commands

Analyze a project and print extracted units as JSON:

```bash
ccr analyze /path/to/project --unit-mode code
```

Print the exact units that would be selected by a refactor command, without creating a run:

```bash
ccr refactor /path/to/project --refactor-intensity structural --max-units 5 --print-units
```

Run a faster pass over package-sized units with staged verification:

```bash
ccr refactor /path/to/project --fast
```

Use larger, cross-file clusters for structural cleanup:

```bash
ccr refactor /path/to/project \
  --refactor-intensity structural \
  --target-unit-count 6 \
  --judge
```

Resume an interrupted run:

```bash
ccr resume --run /tmp/ccr/runs/run-YYYYMMDDTHHMMSSZ
```

Verify a copied workspace manually:

```bash
ccr verify /tmp/ccr/runs/run-YYYYMMDDTHHMMSSZ/workspace
ccr verify /tmp/ccr/runs/run-YYYYMMDDTHHMMSSZ/workspace --command "python -m pytest"
```

## The Run Lifecycle

1. **Copy**: CCR copies your project into a fresh run workspace and initializes a git baseline there.
2. **Select**: It extracts units and orders them by source order or value score.
3. **Protect**: It checks whether tests are adequate and can ask Codex to add characterization tests first.
4. **Retrieve**: It gathers clean-code guidance and example context for the current unit.
5. **Refactor**: Codex edits only the copied workspace.
6. **Verify**: CCR runs configured checks and rolls back failed attempts.
7. **Judge**: Optional judge mode reviews passing diffs and can retry rejected attempts.
8. **Commit**: Accepted changes are committed unit by unit in the copied workspace.
9. **Inspect**: Dashboards, diffs, ledgers, summaries, and Codex call logs show what happened.
10. **Apply**: Only `ccr apply --yes` modifies the original project.

## Verification

If you do not pass `--verify-command`, CCR detects a conservative default:

- `python -m compileall -q .`
- `python -m pytest` when tests are present

You can add one or more explicit commands:

```bash
ccr refactor /path/to/project \
  --verify-command "python -m pytest" \
  --verify-command "python -m ruff check ."
```

For legacy systems, characterization commands can capture behavior before the refactor and compare it after:

```bash
ccr refactor /path/to/project \
  --characterization-command "python texttest_fixture.py 30"
```

## Choosing Refactor Units

CCR supports several unit modes:

- `code`: class/function units, which is the default conservative mode.
- `file`: whole Python files.
- `package`: direct files in Python packages, with file fallback.
- `cluster`: model-budgeted file groups for broader structural refactors.

Useful filters:

```bash
ccr refactor /path/to/project \
  --include-unit "pkg/core*" \
  --exclude-unit "*legacy_adapter*" \
  --min-unit-lines 40 \
  --skip-low-value-units
```

## Providers

The default provider is `codex`:

```bash
ccr refactor /path/to/project --provider codex --model gpt-5.5 --reasoning-effort medium
```

There is also a deterministic `heuristic` provider used by tests and kata-style examples:

```bash
ccr refactor /path/to/gilded-rose --provider heuristic --judge
```

## Development

Install development dependencies:

```bash
pip install -e ".[dev]"
```

Run tests:

```bash
python -m pytest
```

Run linting:

```bash
python -m ruff check .
```

When prompt backups change, publish and verify them:

```bash
python -m ccr.cli langfuse sync-prompts --input ccr/langfuse_related/prompt_backups.yaml
python -m ccr.cli langfuse check-prompts
```

## Project Philosophy

CCR is intentionally conservative. Each change is tied to a bounded unit, a reason, a verification trail, a rollback point, and a diff.

The hard part of agentic refactoring is often the review: what changed, why it changed, and whether the behavior still matches the original system. CCR keeps that information visible enough that applying the patch feels closer to a normal code-review decision.
