from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ccr.env import load_environment_files
from ccr.extraction.token_budget import (
    DEFAULT_MODEL_LIMITS_PATH,
    model_limits_for_model,
    refresh_model_limits_from_openai_docs,
)
from ccr.knowledge.references import DEFAULT_REFERENCES_ROOT, sync_references
from ccr.langchain_utils.parsers import schema_names
from ccr.langfuse_related.prompts import (
    DEFAULT_PROMPT_BACKUP_FILE,
    check_required_prompts,
    seed_prompts_from_file,
    sync_prompt_backups_to_langfuse,
)
from ccr.langfuse_related.sync import (
    diff_schema_bundle,
    sync_schemas_to_langfuse,
    write_schema_bundle,
)
from ccr.schemas.refactor import RefactorIntensity
from ccr.schemas.unit import CodeUnit
from ccr.snapshots.git import GitRepo
from ccr.snapshots.workspace import WORKSPACE_WARNING
from ccr.verification.commands import detect_verification_commands, parse_command
from ccr.verification.runner import run_commands
from ccr.workflow.run import (
    RefactorRunConfig,
    analyze_project,
    preview_refactor_units,
    resume_refactor,
    run_refactor,
    unit_value_score,
)
from ccr.workflow.state import RunState


def main(argv: list[str] | None = None) -> int:
    load_environment_files()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ccr: error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccr")
    subparsers = parser.add_subparsers(required=True)

    references = subparsers.add_parser("references")
    references_sub = references.add_subparsers(required=True)
    references_sync = references_sub.add_parser("sync")
    references_sync.add_argument("--languages", default="python")
    references_sync.add_argument("--references-root", type=Path, default=DEFAULT_REFERENCES_ROOT)
    references_sync.add_argument("--no-test-targets", action="store_true")
    references_sync.set_defaults(func=_references_sync)

    models = subparsers.add_parser("models")
    models_sub = models.add_subparsers(required=True)
    models_limits = models_sub.add_parser("limits")
    models_limits.add_argument("--model", action="append", default=[])
    models_limits.set_defaults(func=_models_limits)
    models_refresh = models_sub.add_parser("refresh-limits")
    models_refresh.add_argument("--model", action="append", default=[])
    models_refresh.add_argument("--output", type=Path, default=DEFAULT_MODEL_LIMITS_PATH)
    models_refresh.set_defaults(func=_models_refresh_limits)

    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("project", type=Path)
    analyze.add_argument("--language", default="python")
    analyze.add_argument("--model")
    analyze.add_argument("--include-methods", action="store_true")
    analyze.add_argument(
        "--unit-mode",
        choices=["code", "package", "file", "cluster"],
        default="code",
    )
    analyze.add_argument("--unit-sort", choices=["value", "source"], default="value")
    analyze.add_argument("--target-unit-count", type=int, default=5)
    analyze.set_defaults(func=_analyze)

    plan = subparsers.add_parser("plan")
    plan.add_argument("project", type=Path)
    plan.add_argument("--language", default="python")
    plan.add_argument("--model")
    plan.add_argument("--include-methods", action="store_true")
    plan.add_argument("--unit-mode", choices=["code", "package", "file", "cluster"], default="code")
    plan.add_argument("--unit-sort", choices=["value", "source"], default="value")
    plan.add_argument("--target-unit-count", type=int, default=5)
    plan.set_defaults(func=_plan)

    refactor = subparsers.add_parser("refactor")
    refactor.add_argument("project", type=Path)
    refactor.add_argument("--language", default="python")
    refactor.add_argument("--provider", default="codex", choices=["codex", "heuristic", "openai"])
    refactor.add_argument("--model")
    refactor.add_argument(
        "--reasoning-effort",
        help=(
            "Override Codex reasoning effort for this run, for example minimal, low, medium, "
            "high, or xhigh."
        ),
    )
    refactor.add_argument("--max-units", type=int)
    refactor.add_argument("--run-root", type=Path, default=Path("/tmp/ccr/runs"))
    refactor.add_argument("--references-root", type=Path, default=DEFAULT_REFERENCES_ROOT)
    refactor.add_argument("--include-methods", action="store_true")
    refactor.add_argument(
        "--unit-mode",
        choices=["code", "package", "file", "cluster"],
        help=(
            "Select refactoring unit granularity. code keeps the default class/function behavior; "
            "cluster builds model-budgeted file groups; package groups direct files in Python "
            "packages and falls back to files; file uses whole Python files."
        ),
    )
    refactor.add_argument(
        "--target-unit-count",
        type=int,
        default=RefactorRunConfig.model_fields["target_unit_count"].default,
        help=(
            "Soft target for cluster unit count. CCR may produce fewer or more units to preserve "
            "cohesion and fit the selected model context."
        ),
    )
    refactor.add_argument(
        "--unit-sort",
        choices=["value", "source"],
        default="value",
        help=(
            "Order units before refactoring. value prioritizes units that look most in need of "
            "cleanup; source preserves source-order traversal."
        ),
    )
    refactor.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Use a speed-oriented preset: package units, no generated tests, and staged "
            "verification. Explicit --unit-mode still wins."
        ),
    )
    refactor.add_argument("--min-unit-lines", type=int)
    refactor.add_argument("--skip-low-value-units", action="store_true")
    refactor.add_argument("--include-unit", action="append", default=[])
    refactor.add_argument("--exclude-unit", action="append", default=[])
    refactor.add_argument("--verify-command", action="append", default=[])
    refactor.add_argument("--characterization-command", action="append", default=[])
    refactor.add_argument(
        "--staged-verification",
        action="store_true",
        help=(
            "Run quick changed-file syntax checks per unit and the full configured verification "
            "once before completing the run."
        ),
    )
    refactor.add_argument(
        "--no-test-generation",
        action="store_true",
        help="Disable automatic pre-refactor test audit and test generation.",
    )
    refactor.add_argument("--judge", action="store_true")
    refactor.add_argument(
        "--print-units",
        action="store_true",
        help=(
            "Analyze and print the selected refactor units, then exit without creating a run "
            "or refactoring files."
        ),
    )
    refactor.add_argument(
        "--judge-retries",
        type=int,
        default=RefactorRunConfig.model_fields["judge_retries"].default,
        help=(
            "Number of additional refactor attempts to make when the judge rejects a "
            "passing refactor."
        ),
    )
    refactor.add_argument(
        "--refactor-intensity",
        choices=[intensity.value for intensity in RefactorIntensity],
        default=RefactorRunConfig.model_fields["refactor_intensity"].default.value,
        help=(
            "Choose how much design freedom Codex has. conservative favors smaller safe edits; "
            "structural allows renames, moved logic, and repository-local integration updates."
        ),
    )
    refactor.add_argument(
        "--instructions",
        help=(
            "Override the Langfuse-backed intensity instructions for this run. If omitted, CCR "
            "fetches the selected intensity instructions from Langfuse."
        ),
    )
    refactor.set_defaults(func=_refactor)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--run", type=Path, required=True)
    resume.set_defaults(func=_resume)

    verify = subparsers.add_parser("verify")
    verify.add_argument("workspace", type=Path)
    verify.add_argument("--command", action="append", default=[])
    verify.set_defaults(func=_verify)

    apply = subparsers.add_parser("apply")
    apply.add_argument("original", type=Path)
    apply.add_argument("--run", type=Path, required=True)
    apply.add_argument("--yes", action="store_true")
    apply.set_defaults(func=_apply)

    schemas = subparsers.add_parser("schemas")
    schemas_sub = schemas.add_subparsers(required=True)
    schemas_sync = schemas_sub.add_parser("sync")
    schemas_sync.add_argument(
        "--output", type=Path, default=Path(".ccr/langfuse/schema_bundle.json")
    )
    schemas_sync.add_argument(
        "--local-only",
        action="store_true",
        help="Write the local schema snapshot without publishing Langfuse inspection artifacts.",
    )
    schemas_sync.set_defaults(func=_schemas_sync)
    schemas_diff = schemas_sub.add_parser("diff")
    schemas_diff.add_argument(
        "--snapshot", type=Path, default=Path(".ccr/langfuse/schema_bundle.json")
    )
    schemas_diff.set_defaults(func=_schemas_diff)

    langfuse = subparsers.add_parser("langfuse")
    langfuse_sub = langfuse.add_subparsers(required=True)
    langfuse_seed = langfuse_sub.add_parser("seed-prompts")
    langfuse_seed.add_argument("--input", type=Path, default=DEFAULT_PROMPT_BACKUP_FILE)
    langfuse_seed.set_defaults(func=_langfuse_seed_prompts)
    langfuse_sync = langfuse_sub.add_parser("sync-prompts")
    langfuse_sync.add_argument("--input", type=Path, default=DEFAULT_PROMPT_BACKUP_FILE)
    langfuse_sync.set_defaults(func=_langfuse_sync_prompts)
    langfuse_check = langfuse_sub.add_parser("check-prompts")
    langfuse_check.set_defaults(func=_langfuse_check_prompts)

    langchain = subparsers.add_parser("langchain")
    langchain_sub = langchain.add_subparsers(required=True)
    langchain_export = langchain_sub.add_parser("export-documents")
    langchain_export.add_argument("project", type=Path)
    langchain_export.add_argument("--language", default="python")
    langchain_export.add_argument("--model")
    langchain_export.add_argument("--include-methods", action="store_true")
    langchain_export.add_argument(
        "--unit-mode",
        choices=["code", "package", "file", "cluster"],
        default="code",
    )
    langchain_export.add_argument("--unit-sort", choices=["value", "source"], default="value")
    langchain_export.add_argument("--target-unit-count", type=int, default=5)
    langchain_export.add_argument("--references-root", type=Path, default=DEFAULT_REFERENCES_ROOT)
    langchain_export.add_argument("--no-references", action="store_true")
    langchain_export.add_argument(
        "--output",
        type=Path,
        help="Write JSONL documents to this path. The command fails if the path already exists.",
    )
    langchain_export.set_defaults(func=_langchain_export_documents)

    langchain_parsers = langchain_sub.add_parser("parser-diagnostics")
    langchain_parsers.add_argument("--schema", choices=schema_names(), required=True)
    langchain_parsers.set_defaults(func=_langchain_parser_diagnostics)

    langchain_run = langchain_sub.add_parser("export-run-documents")
    langchain_run.add_argument("--run", type=Path, required=True)
    langchain_run.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Write additive run-analysis documents to this new directory. If omitted, CCR creates "
            "a unique directory under <run>/analysis/."
        ),
    )
    langchain_run.set_defaults(func=_langchain_export_run_documents)

    prompts = subparsers.add_parser("prompts")
    prompts.set_defaults(func=_prompts_moved)

    return parser


def _references_sync(args: argparse.Namespace) -> int:
    languages = _split_csv(args.languages)
    records = sync_references(
        languages=languages,
        references_root=args.references_root,
        include_test_targets=not args.no_test_targets,
    )
    print(json.dumps([record.model_dump() for record in records], indent=2))
    return 0


def _models_limits(args: argparse.Namespace) -> int:
    payload = {}
    for model in args.model or ["gpt-5.5"]:
        limits, approximate = model_limits_for_model(model)
        payload[model] = {
            "normalized_model": limits.model,
            "context_window_tokens": limits.context_window_tokens,
            "max_output_tokens": limits.max_output_tokens,
            "source": limits.source,
            "verified_at": limits.verified_at,
            "approximate": approximate,
        }
    print(json.dumps(payload, indent=2))
    return 0


def _models_refresh_limits(args: argparse.Namespace) -> int:
    output = refresh_model_limits_from_openai_docs(
        models=args.model or ["gpt-5.5"],
        output=args.output,
    )
    print(f"Wrote model limits to {output}")
    return 0


def _analyze(args: argparse.Namespace) -> int:
    units = analyze_project(
        args.project.resolve(),
        language=args.language,
        include_methods=args.include_methods,
        unit_mode=args.unit_mode,
        unit_sort=args.unit_sort,
        model=args.model,
        target_unit_count=args.target_unit_count,
    )
    print(json.dumps([unit.model_dump() for unit in units], indent=2))
    return 0


def _plan(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    units = analyze_project(
        project,
        language=args.language,
        include_methods=args.include_methods,
        unit_mode=args.unit_mode,
        unit_sort=args.unit_sort,
        model=args.model,
        target_unit_count=args.target_unit_count,
    )
    commands = detect_verification_commands(project)
    payload = {
        "project": str(project),
        "language": args.language,
        "unit_mode": args.unit_mode,
        "unit_sort": args.unit_sort,
        "units": [
            {
                "unit_id": unit.unit_id,
                "kind": unit.kind,
                "location": unit.location,
                "owned_paths": unit.owned_paths,
                "context_paths": unit.context_paths,
                "estimated_tokens": unit.estimated_tokens,
                "source_token_budget": unit.source_token_budget,
                "model_context_window_tokens": unit.model_context_window_tokens,
                "model_max_output_tokens": unit.model_max_output_tokens,
                "response_reserve_tokens": unit.response_reserve_tokens,
                "budget_notes": unit.budget_notes,
                "value_score": unit_value_score(unit),
            }
            for unit in units
        ],
        "verification_commands": commands,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _unit_preview_payload(config: RefactorRunConfig, units: list[CodeUnit]) -> dict[str, object]:
    return {
        "project": str(config.project.resolve()),
        "language": config.language,
        "unit_mode": config.unit_mode,
        "unit_sort": config.unit_sort,
        "target_unit_count": config.target_unit_count,
        "max_units": config.max_units,
        "units_total": len(units),
        "units": [
            {
                "unit_id": unit.unit_id,
                "kind": unit.kind,
                "qualified_name": unit.qualified_name,
                "location": unit.location,
                "owned_paths": unit.owned_paths,
                "context_paths": unit.context_paths,
                "member_paths": unit.member_paths,
                "estimated_tokens": unit.estimated_tokens,
                "source_token_budget": unit.source_token_budget,
                "model_context_window_tokens": unit.model_context_window_tokens,
                "model_max_output_tokens": unit.model_max_output_tokens,
                "response_reserve_tokens": unit.response_reserve_tokens,
                "budget_notes": unit.budget_notes,
                "value_score": unit_value_score(unit),
            }
            for unit in units
        ],
    }


def _refactor(args: argparse.Namespace) -> int:
    refactor_intensity = RefactorIntensity(args.refactor_intensity)
    unit_mode = args.unit_mode or (
        "cluster"
        if refactor_intensity == RefactorIntensity.STRUCTURAL
        else ("package" if args.fast else "code")
    )
    test_generation_enabled = not args.no_test_generation and not args.fast
    config = RefactorRunConfig(
        project=args.project,
        language=args.language,
        provider=args.provider,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        run_root=args.run_root,
        references_root=args.references_root,
        max_units=args.max_units,
        include_methods=args.include_methods,
        unit_mode=unit_mode,
        unit_sort=args.unit_sort,
        target_unit_count=args.target_unit_count,
        fast_mode=args.fast,
        min_unit_lines=args.min_unit_lines,
        skip_low_value_units=args.skip_low_value_units,
        include_units=args.include_unit,
        exclude_units=args.exclude_unit,
        verification_commands=args.verify_command,
        characterization_commands=args.characterization_command,
        staged_verification=args.staged_verification or args.fast,
        test_generation_enabled=test_generation_enabled,
        judge=args.judge,
        judge_retries=args.judge_retries,
        refactor_intensity=refactor_intensity,
        instructions=args.instructions,
    )
    if args.print_units:
        units = preview_refactor_units(config)
        print(json.dumps(_unit_preview_payload(config, units), indent=2))
        return 0

    summary = run_refactor(config)
    print(summary.model_dump_json(indent=2))
    return 0


def _resume(args: argparse.Namespace) -> int:
    summary = resume_refactor(args.run)
    print(summary.model_dump_json(indent=2))
    return 0


def _verify(args: argparse.Namespace) -> int:
    commands = (
        [parse_command(command) for command in args.command]
        if args.command
        else detect_verification_commands(args.workspace)
    )
    report = run_commands(commands, cwd=args.workspace)
    print(report.model_dump_json(indent=2))
    return 0 if report.ok else 1


def _apply(args: argparse.Namespace) -> int:
    run_dir = args.run.resolve()
    workspace = run_dir / "workspace"
    state = RunState.load(run_dir)
    repo = GitRepo(workspace)
    diff = repo.diff(state.baseline_commit, "HEAD")
    stat = repo.show_stat(state.baseline_commit, "HEAD")

    print(WORKSPACE_WARNING)
    if stat.strip():
        print("\nDiff preview:\n" + stat)
    else:
        print("\nDiff preview: no committed changes to apply.")

    if not args.yes:
        print(f"\nRe-run with --yes to apply these edits to {args.original.resolve()}.")
        return 0

    patch_path = run_dir / "apply.patch"
    patch_path.write_text(diff, encoding="utf-8")
    completed = subprocess.run(
        ["git", "apply", "--binary", str(patch_path)],
        cwd=args.original.resolve(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode
    print(f"Applied run {run_dir} to {args.original.resolve()}.")
    return 0


def _schemas_sync(args: argparse.Namespace) -> int:
    path = write_schema_bundle(args.output)
    print(f"Wrote schema bundle to {path}")
    if not args.local_only:
        artifacts = sync_schemas_to_langfuse()
        print(json.dumps({"langfuse_schema_artifacts": artifacts}, indent=2))
    return 0


def _schemas_diff(args: argparse.Namespace) -> int:
    diff = diff_schema_bundle(args.snapshot)
    print(diff if diff else "No schema differences.")
    return 0


def _langfuse_seed_prompts(args: argparse.Namespace) -> int:
    names = seed_prompts_from_file(args.input)
    print(json.dumps({"seeded_prompts": names}, indent=2))
    return 0


def _langfuse_sync_prompts(args: argparse.Namespace) -> int:
    names = sync_prompt_backups_to_langfuse(args.input)
    print(json.dumps({"synced_prompts": names}, indent=2))
    return 0


def _langfuse_check_prompts(args: argparse.Namespace) -> int:
    statuses = check_required_prompts()
    print(json.dumps(statuses, indent=2))
    return 0 if all(status == "ok" for status in statuses.values()) else 1


def _langchain_export_documents(args: argparse.Namespace) -> int:
    from ccr.langchain_utils.documents import build_project_documents, write_jsonl_documents

    documents = build_project_documents(
        args.project,
        language=args.language,
        include_methods=args.include_methods,
        unit_mode=args.unit_mode,
        unit_sort=args.unit_sort,
        model=args.model,
        target_unit_count=args.target_unit_count,
        references_root=args.references_root,
        include_references=not args.no_references,
    )
    if args.output:
        path = write_jsonl_documents(documents, args.output)
        print(json.dumps({"documents_path": str(path), "document_count": len(documents)}, indent=2))
        return 0
    for document in documents:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0


def _langchain_parser_diagnostics(args: argparse.Namespace) -> int:
    from ccr.langchain_utils.parsers import parser_diagnostics

    print(json.dumps(parser_diagnostics(args.schema), indent=2, sort_keys=True))
    return 0


def _langchain_export_run_documents(args: argparse.Namespace) -> int:
    from ccr.langchain_utils.documents import export_run_documents

    result = export_run_documents(args.run, output_dir=args.output_dir)
    print(json.dumps(result.as_json(), indent=2, sort_keys=True))
    return 0


def _prompts_moved(args: argparse.Namespace) -> int:
    print(
        "Prompts are stored in Langfuse and fetched automatically at runtime. "
        "Use `ccr langfuse check-prompts` to verify required prompt availability, "
        "`ccr langfuse sync-prompts` to publish bundled YAML backup prompts as new Langfuse "
        "versions, and `ccr schemas sync` for JSON schema inspection artifacts."
    )
    return 0


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _split_csv(value: str) -> list[str]:
    return [part.strip().lower() for part in value.split(",") if part.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
