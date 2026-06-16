from __future__ import annotations

import ast
import fnmatch
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ccr.codex.provider import build_provider
from ccr.extraction.units import extract_units
from ccr.knowledge.loaders import load_reference_context
from ccr.schemas.judge import JudgeResult
from ccr.schemas.refactor import RefactorIntensity, RefactorOutcome, RefactorResult
from ccr.schemas.summary import AcceptedRefactor, CumulativeSummary, RunSummary
from ccr.schemas.unit import CodeUnit
from ccr.snapshots.git import GitRepo
from ccr.snapshots.workspace import WORKSPACE_WARNING, create_workspace_copy
from ccr.verification.commands import detect_verification_commands, parse_command
from ccr.verification.runner import CommandResult, VerificationReport, run_commands
from ccr.workflow.dashboard import RunEventLog, record_run_event, write_run_dashboard
from ccr.workflow.ledger import LedgerEntry, RunLedger
from ccr.workflow.state import RunState

RUN_CONFIG_FILE = "config.json"
CHARACTERIZATION_BASELINE_FILE = "characterization-baseline.json"
FINAL_UNIT_OUTCOMES = {
    "accepted",
    "verification_failed",
    "unchanged",
    "skipped",
    "judge_rejected",
    "test_generation_failed",
}


class RefactorRunConfig(BaseModel):
    project: Path
    language: str = "python"
    provider: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    run_root: Path = Path("/tmp/ccr/runs")
    references_root: Path = Path("/home/vvlcek/Code/CodeReferences")
    max_units: int | None = None
    include_methods: bool = False
    unit_mode: str = "code"
    unit_sort: str = "value"
    target_unit_count: int = Field(default=5, ge=1)
    fast_mode: bool = False
    min_unit_lines: int | None = None
    skip_low_value_units: bool = False
    include_units: list[str] = Field(default_factory=list)
    exclude_units: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    characterization_commands: list[str] = Field(default_factory=list)
    staged_verification: bool = False
    test_generation_enabled: bool = True
    judge: bool = False
    judge_retries: int = Field(default=1, ge=0)
    refactor_intensity: RefactorIntensity = RefactorIntensity.CONSERVATIVE
    instructions: str | None = None


def analyze_project(
    project: Path,
    *,
    language: str = "python",
    include_methods: bool = False,
    unit_mode: str = "code",
    unit_sort: str = "value",
    model: str | None = None,
    target_unit_count: int = 5,
    min_unit_lines: int | None = None,
    skip_low_value_units: bool = False,
    include_units: list[str] | None = None,
    exclude_units: list[str] | None = None,
) -> list[CodeUnit]:
    units = extract_units(
        project.resolve(),
        language=language,
        include_methods=include_methods,
        unit_mode=unit_mode,
        model=model,
        target_unit_count=target_unit_count,
    )
    filtered = _filter_units(
        units,
        min_unit_lines=min_unit_lines,
        skip_low_value_units=skip_low_value_units,
        include_units=include_units or [],
        exclude_units=exclude_units or [],
    )
    return _sort_units(filtered, unit_sort=unit_sort)


def preview_refactor_units(config: RefactorRunConfig) -> list[CodeUnit]:
    units = _analyze_project_for_config(config.project.resolve(), config)
    return units[: config.max_units] if config.max_units else units


def run_refactor(config: RefactorRunConfig) -> RunSummary:
    project = config.project.resolve()
    run_id, workspace = create_workspace_copy(project, run_root=config.run_root)
    run_dir = workspace.parent
    repo = GitRepo(workspace)
    baseline = repo.ensure_baseline()

    units = _analyze_project_for_config(workspace, config)
    selected_units = units[: config.max_units] if config.max_units else units
    state = RunState(
        run_id=run_id,
        original_path=str(project),
        copied_workspace=str(workspace),
        language=config.language,
        provider=config.provider,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        baseline_commit=baseline,
        units_total=len(selected_units),
        current_head=baseline,
    )
    state.save(run_dir)
    _write_run_config(run_dir, config)

    event_log = RunEventLog(run_dir / "events.jsonl")
    _record_event(
        run_dir,
        event_log,
        "run_started",
        run_id=run_id,
        original_path=str(project),
        copied_workspace=str(workspace),
        provider=config.provider,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        units_total=len(selected_units),
    )
    print(f"CCR dashboard: {run_dir / 'dashboard.html'}", file=sys.stderr)
    return _continue_refactor(config, run_dir)


def resume_refactor(run_dir: Path) -> RunSummary:
    run_dir = run_dir.resolve()
    config = _load_run_config(run_dir)
    state = RunState.load(run_dir)
    workspace = Path(state.copied_workspace)
    repo = GitRepo(workspace)
    if not workspace.exists():
        msg = f"Run workspace does not exist: {workspace}"
        raise FileNotFoundError(msg)
    if not repo.is_repo():
        msg = f"Run workspace is not a git repository: {workspace}"
        raise RuntimeError(msg)

    _rollback_workspace(repo, repo.head())
    state.status = "running"
    state.current_head = repo.head()
    state.current_stage = "resuming"
    state.error = None
    state.save(run_dir)

    event_log = RunEventLog(run_dir / "events.jsonl")
    _record_event(
        run_dir,
        event_log,
        "run_resumed",
        run_id=state.run_id,
        copied_workspace=str(workspace),
        current_head=state.current_head,
    )
    print(f"CCR dashboard: {run_dir / 'dashboard.html'}", file=sys.stderr)
    return _continue_refactor(config, run_dir)


def _continue_refactor(config: RefactorRunConfig, run_dir: Path) -> RunSummary:
    state = RunState.load(run_dir)
    project = Path(state.original_path)
    workspace = Path(state.copied_workspace)
    repo = GitRepo(workspace)
    repo.install_excludes()

    units = _analyze_project_for_config(workspace, config)
    selected_units = units[: config.max_units] if config.max_units else units
    state.units_total = len(selected_units)
    state.status = "running"
    state.error = None
    state.save(run_dir)

    ledger = RunLedger(run_dir / "ledger.jsonl")
    event_log = RunEventLog(run_dir / "events.jsonl")
    provider = build_provider(
        config.provider,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        run_dir=run_dir,
    )
    references = load_reference_context(
        language=config.language,
        references_root=config.references_root,
    )
    characterization_baseline = _load_or_capture_characterization_baseline(
        run_dir,
        workspace,
        config.characterization_commands,
    )
    summary = _summary_from_ledger(ledger.read())
    generated_test_commands = _generated_test_commands_from_events(run_dir)

    try:
        for original_unit in selected_units:
            completed_unit_ids = _finalized_unit_ids(ledger.read())
            if original_unit.unit_id in completed_unit_ids:
                continue
            unit = _refresh_unit(workspace, original_unit, config=config)
            state.current_unit_id = unit.unit_id
            state.current_stage = "unit_started"
            state.save(run_dir)
            _record_event(run_dir, event_log, "unit_started", unit=_dump_model(unit))
            effective_verification_commands = _merge_commands(
                config.verification_commands,
                generated_test_commands,
            )
            if config.test_generation_enabled:
                state.current_stage = "test_generation"
                state.save(run_dir)
                tests_ready, new_test_commands = _ensure_unit_tests(
                    provider=provider,
                    unit=unit,
                    summary=summary,
                    workspace=workspace,
                    repo=repo,
                    ledger=ledger,
                    run_dir=run_dir,
                    event_log=event_log,
                    state=state,
                    verification_commands=effective_verification_commands,
                    characterization_commands=config.characterization_commands,
                    characterization_baseline=characterization_baseline,
                    staged_verification=config.staged_verification,
                )
                generated_test_commands = _merge_commands(
                    generated_test_commands,
                    new_test_commands,
                )
                if not tests_ready:
                    _record_event(
                        run_dir,
                        event_log,
                        "unit_skipped",
                        unit_id=unit.unit_id,
                        reason="Required tests were not ready.",
                    )
                    _sync_state_progress(state, ledger, repo, run_dir)
                    continue
                unit = _refresh_unit(workspace, unit, config=config)

            state.current_stage = "retrieval"
            state.save(run_dir)
            retrieval = provider.retrieve(unit=unit, references=references, workspace=workspace)
            _record_event(
                run_dir,
                event_log,
                "retrieval_completed",
                unit_id=unit.unit_id,
                ideas=[_dump_model(idea) for idea in retrieval.ideas],
            )
            judge_feedback: str | None = None
            max_attempts = _max_refactor_attempts(config)
            for attempt in range(1, max_attempts + 1):
                before_head = repo.head()
                state.current_stage = "refactor"
                state.save(run_dir)
                refactor_result = provider.refactor(
                    unit=unit,
                    retrieval=retrieval,
                    summary=summary,
                    workspace=workspace,
                    instructions=_instructions_with_judge_feedback(
                        config.instructions,
                        judge_feedback,
                    ),
                    refactor_intensity=config.refactor_intensity,
                )
                _format_workspace(workspace, repo.changed_files())
                changed_files = repo.changed_files()
                state.current_stage = "verification"
                state.save(run_dir)
                verification = _verify_workspace(
                    workspace,
                    _merge_commands(config.verification_commands, generated_test_commands),
                    characterization_baseline,
                    staged=config.staged_verification,
                    changed_files=changed_files,
                )
                diff = repo.diff()
                _record_event(
                    run_dir,
                    event_log,
                    "refactor_completed",
                    unit_id=unit.unit_id,
                    result=_dump_model(refactor_result),
                    changed_files=changed_files,
                    diff=diff,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if not verification.ok:
                    _rollback_workspace(repo, before_head)
                    _record_event(
                        run_dir,
                        event_log,
                        "refactor_verification_failed",
                        unit_id=unit.unit_id,
                        changed_files=changed_files,
                        verification=_dump_model(verification),
                        message=_verification_failure_message(verification),
                    )
                    ledger.append(
                        _ledger_entry(
                            unit=unit,
                            outcome="verification_failed",
                            changed_files=changed_files,
                            retrieval=retrieval,
                            verification=verification,
                            message=_verification_failure_message(verification),
                        )
                    )
                    _sync_state_progress(state, ledger, repo, run_dir)
                    break
                if not changed_files or refactor_result.outcome in {
                    RefactorOutcome.UNCHANGED,
                    RefactorOutcome.SKIPPED,
                }:
                    _rollback_workspace(repo, before_head)
                    _record_event(
                        run_dir,
                        event_log,
                        "refactor_skipped",
                        unit_id=unit.unit_id,
                        outcome=refactor_result.outcome.value,
                        message=refactor_result.message,
                    )
                    ledger.append(
                        _ledger_entry(
                            unit=unit,
                            outcome=refactor_result.outcome.value,
                            changed_files=[],
                            retrieval=retrieval,
                            verification=verification,
                            message=refactor_result.message,
                        )
                    )
                    _sync_state_progress(state, ledger, repo, run_dir)
                    break
                if config.judge:
                    state.current_stage = "judge"
                    state.save(run_dir)
                    judge_result = provider.judge(
                        unit=unit,
                        diff=diff,
                        refactor_result=refactor_result,
                        summary=summary,
                        workspace=workspace,
                        refactor_intensity=config.refactor_intensity,
                    )
                    _record_event(
                        run_dir,
                        event_log,
                        "judge_completed",
                        unit_id=unit.unit_id,
                        result=_dump_model(judge_result),
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    if not judge_result.accepted:
                        _rollback_workspace(repo, before_head)
                        if attempt < max_attempts:
                            judge_feedback = _judge_retry_feedback(
                                judge_result,
                                config.refactor_intensity,
                            )
                            _record_event(
                                run_dir,
                                event_log,
                                "judge_retrying",
                                unit_id=unit.unit_id,
                                issues=judge_result.issues,
                                summary=judge_result.summary,
                                attempt=attempt,
                                retries_remaining=max_attempts - attempt,
                            )
                            continue
                        _record_event(
                            run_dir,
                            event_log,
                            "judge_rejected",
                            unit_id=unit.unit_id,
                            issues=judge_result.issues,
                            summary=judge_result.summary,
                            attempt=attempt,
                            max_attempts=max_attempts,
                        )
                        ledger.append(
                            _ledger_entry(
                                unit=unit,
                                outcome="judge_rejected",
                                changed_files=changed_files,
                                retrieval=retrieval,
                                verification=verification,
                                message=(
                                    "; ".join(judge_result.issues)
                                    or judge_result.summary
                                ),
                            )
                        )
                        _sync_state_progress(state, ledger, repo, run_dir)
                        break

                repo.add_all(force=True)
                repo.commit(f"ccr refactor {unit.qualified_name}")
                commit_hash = repo.head()
                _record_event(
                    run_dir,
                    event_log,
                    "refactor_accepted",
                    unit_id=unit.unit_id,
                    changed_files=changed_files,
                    commit=commit_hash,
                    attempt=attempt,
                )
                accepted = AcceptedRefactor(
                    unit=unit.qualified_name,
                    files_changed=changed_files,
                    renames=refactor_result.renames,
                    signature_changes=refactor_result.signature_changes,
                    moved_logic=refactor_result.moved_logic,
                    integration_points=_integration_point_paths(unit, refactor_result),
                    integration_points_updated=refactor_result.integration_points_updated,
                    constraints_to_preserve=[
                        "Preserve observable behavior verified by configured checks."
                    ],
                    verification=verification.descriptions(),
                    behavior_changes=refactor_result.behavior_changes,
                )
                summary.accepted_refactors.append(accepted)
                ledger.append(
                    _ledger_entry(
                        unit=unit,
                        outcome="accepted",
                        changed_files=changed_files,
                        retrieval=retrieval,
                        verification=verification,
                        message=refactor_result.message,
                        commit=commit_hash,
                        refactor_result=refactor_result,
                    )
                )
                _sync_state_progress(state, ledger, repo, run_dir)
                break
    except KeyboardInterrupt as exc:
        _mark_run_interrupted(
            run_dir,
            event_log,
            state,
            repo,
            message="Interrupted by user.",
            error=repr(exc),
        )
        raise
    except Exception as exc:
        _mark_run_interrupted(
            run_dir,
            event_log,
            state,
            repo,
            message=str(exc),
            error=repr(exc),
        )
        raise

    if config.staged_verification:
        state.current_unit_id = None
        state.current_stage = "final_verification"
        state.save(run_dir)
        final_verification = _verify_workspace_full(
            workspace,
            _merge_commands(config.verification_commands, generated_test_commands),
            characterization_baseline,
        )
        _record_event(
            run_dir,
            event_log,
            "final_verification_completed",
            verification=_dump_model(final_verification),
        )
        if not final_verification.ok:
            message = _verification_failure_message(final_verification)
            state.status = "interrupted"
            state.error = message
            state.save(run_dir)
            _record_event(
                run_dir,
                event_log,
                "final_verification_failed",
                verification=_dump_model(final_verification),
                message=message,
            )
            raise RuntimeError(f"Final staged verification failed: {message}")

    state.status = "complete"
    state.current_unit_id = None
    state.current_stage = "complete"
    state.error = None
    _sync_state_progress(state, ledger, repo, run_dir, write_dashboard=False)
    state.save(run_dir)
    run_summary = _build_run_summary(
        run_id=state.run_id,
        original=project,
        workspace=workspace,
        ledger=ledger,
    )
    (run_dir / "summary.json").write_text(
        json.dumps(run_summary.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )
    _record_event(
        run_dir,
        event_log,
        "run_completed",
        run_id=state.run_id,
        applied_changes=run_summary.applied_changes,
        skipped_changes=run_summary.skipped_changes,
    )
    return run_summary


def _write_run_config(run_dir: Path, config: RefactorRunConfig) -> None:
    (run_dir / RUN_CONFIG_FILE).write_text(
        config.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def _load_run_config(run_dir: Path) -> RefactorRunConfig:
    path = run_dir / RUN_CONFIG_FILE
    if not path.exists():
        msg = f"Run config does not exist: {path}"
        raise FileNotFoundError(msg)
    return RefactorRunConfig.model_validate_json(path.read_text(encoding="utf-8"))


def _max_refactor_attempts(config: RefactorRunConfig) -> int:
    if not config.judge:
        return 1
    return config.judge_retries + 1


def _judge_retry_feedback(
    judge_result: JudgeResult,
    refactor_intensity: RefactorIntensity,
) -> str:
    issues = "\n".join(f"- {issue}" for issue in judge_result.issues)
    if not issues:
        issues = f"- {judge_result.summary}"
    revision_goal = (
        "Revise the refactor to address these judge findings. In structural mode, "
        "intentional behavior changes are allowed only when they are explicit in "
        "behavior_changes, justified by the diff, and covered by verification or tests. "
        "Preserve behavior that is not explicitly changed."
        if refactor_intensity == RefactorIntensity.STRUCTURAL
        else "Revise the refactor to address these judge findings while preserving behavior."
    )
    return "\n".join(
        [
            "The previous refactor attempt passed verification but was rejected by the judge.",
            revision_goal,
            issues,
            f"Judge summary: {judge_result.summary}",
        ]
    )


def _instructions_with_judge_feedback(
    instructions: str | None,
    judge_feedback: str | None,
) -> str | None:
    if judge_feedback is None:
        return instructions
    if instructions:
        return "\n\n".join([instructions, judge_feedback])
    return judge_feedback


def _load_or_capture_characterization_baseline(
    run_dir: Path,
    workspace: Path,
    commands: list[str],
) -> list[CommandResult]:
    path = run_dir / CHARACTERIZATION_BASELINE_FILE
    if path.exists():
        return [
            CommandResult.model_validate(item)
            for item in json.loads(path.read_text(encoding="utf-8"))
        ]
    baseline = _capture_characterization_baseline(workspace, commands)
    if baseline:
        path.write_text(
            json.dumps([result.model_dump() for result in baseline], indent=2) + "\n",
            encoding="utf-8",
        )
    return baseline


def _summary_from_ledger(entries: list[LedgerEntry]) -> CumulativeSummary:
    summary = CumulativeSummary()
    for entry in entries:
        if entry.outcome != "accepted":
            continue
        path, _, qualified_name = entry.unit_id.partition("::")
        summary.accepted_refactors.append(
            AcceptedRefactor(
                unit=qualified_name or entry.unit_id,
                files_changed=entry.changed_files,
                renames=entry.renames,
                signature_changes=entry.signature_changes,
                moved_logic=entry.moved_logic,
                integration_points=_ledger_integration_point_paths(path, entry),
                integration_points_updated=entry.integration_points_updated,
                constraints_to_preserve=[
                    "Preserve observable behavior verified by configured checks."
                ],
                verification=entry.checks_run,
                behavior_changes=entry.behavior_changes,
            )
        )
    return summary


def _generated_test_commands_from_events(run_dir: Path) -> list[str]:
    commands: list[str] = []
    for event in _read_events(run_dir):
        if event.get("event") == "tests_added":
            commands = _merge_commands(commands, event.get("test_commands") or [])
    return commands


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _analyze_project_for_config(project: Path, config: RefactorRunConfig) -> list[CodeUnit]:
    return analyze_project(
        project,
        language=config.language,
        include_methods=config.include_methods,
        unit_mode=config.unit_mode,
        unit_sort=config.unit_sort,
        model=config.model,
        target_unit_count=config.target_unit_count,
        min_unit_lines=config.min_unit_lines,
        skip_low_value_units=config.skip_low_value_units,
        include_units=config.include_units,
        exclude_units=config.exclude_units,
    )


def _filter_units(
    units: list[CodeUnit],
    *,
    min_unit_lines: int | None,
    skip_low_value_units: bool,
    include_units: list[str],
    exclude_units: list[str],
) -> list[CodeUnit]:
    filtered = units
    if include_units:
        filtered = [unit for unit in filtered if _matches_any_unit_pattern(unit, include_units)]
    if exclude_units:
        filtered = [unit for unit in filtered if not _matches_any_unit_pattern(unit, exclude_units)]
    if min_unit_lines is not None:
        filtered = [
            unit for unit in filtered if unit.end_line - unit.start_line + 1 >= min_unit_lines
        ]
    if skip_low_value_units:
        filtered = [unit for unit in filtered if not _is_low_value_unit(unit)]
    return filtered


def _sort_units(units: list[CodeUnit], *, unit_sort: str) -> list[CodeUnit]:
    normalized = unit_sort.lower()
    if normalized == "source":
        return units
    if normalized == "value":
        return [
            unit
            for _, unit in sorted(
                enumerate(units),
                key=lambda item: (-unit_value_score(item[1]), item[0]),
            )
        ]
    msg = f"Unknown unit sort {unit_sort!r}. Expected source or value."
    raise ValueError(msg)


def unit_value_score(unit: CodeUnit) -> int:
    meaningful_lines = _meaningful_source_lines(unit.text)
    line_count = len(meaningful_lines)
    score = line_count
    score += max(0, line_count - 40) * 2
    score += max(0, line_count - 120) * 3
    score += sum(2 for line in meaningful_lines if len(line) > 100)
    score += sum(8 for line in meaningful_lines if _has_maintenance_marker(line))

    try:
        tree = ast.parse(unit.text)
    except SyntaxError:
        score += _lexical_complexity_score(meaningful_lines)
    else:
        metrics = _AstValueMetrics()
        metrics.visit(tree)
        score += metrics.branch_count * 12
        score += metrics.max_control_depth * 18
        score += metrics.function_count * 5
        score += metrics.class_count * 8
        score += metrics.broad_exception_count * 20
        score += metrics.long_argument_list_count * 12
        score += max(0, metrics.longest_block_lines - 80) * 2

    if unit.kind.value in {"file", "package"}:
        score += max(0, unit.text.count("\ndef ") + unit.text.count("\nclass ") - 3) * 4
    if _is_low_value_unit(unit):
        score -= 100
    return max(score, 0)


class _AstValueMetrics(ast.NodeVisitor):
    def __init__(self) -> None:
        self.branch_count = 0
        self.class_count = 0
        self.function_count = 0
        self.broad_exception_count = 0
        self.long_argument_list_count = 0
        self.longest_block_lines = 0
        self.max_control_depth = 0
        self._control_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_count += 1
        self._record_block_lines(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_If(self, node: ast.If) -> None:
        self._visit_control_node(node)

    def visit_For(self, node: ast.For) -> None:
        self._visit_control_node(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_control_node(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_control_node(node)

    def visit_With(self, node: ast.With) -> None:
        self._visit_control_node(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_control_node(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_control_node(node)

    def visit_Match(self, node: ast.Match) -> None:
        self._visit_control_node(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.branch_count += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.branch_count += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None or (
            isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
        ):
            self.broad_exception_count += 1
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_count += 1
        self._record_block_lines(node)
        arg_count = (
            len(node.args.posonlyargs)
            + len(node.args.args)
            + len(node.args.kwonlyargs)
            + int(node.args.vararg is not None)
            + int(node.args.kwarg is not None)
        )
        if arg_count >= 6:
            self.long_argument_list_count += 1
        self.generic_visit(node)

    def _visit_control_node(self, node: ast.AST) -> None:
        self.branch_count += 1
        self._control_depth += 1
        self.max_control_depth = max(self.max_control_depth, self._control_depth)
        self.generic_visit(node)
        self._control_depth -= 1

    def _record_block_lines(self, node: ast.AST) -> None:
        start_line = getattr(node, "lineno", None)
        end_line = getattr(node, "end_lineno", None)
        if start_line is not None and end_line is not None:
            self.longest_block_lines = max(self.longest_block_lines, end_line - start_line + 1)


def _matches_any_unit_pattern(unit: CodeUnit, patterns: list[str]) -> bool:
    candidates = [
        unit.unit_id,
        unit.path,
        unit.name,
        unit.qualified_name,
        unit.location,
        *unit.member_paths,
        *unit.owned_paths,
        *unit.context_paths,
    ]
    return any(
        fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns for candidate in candidates
    )


def _has_maintenance_marker(line: str) -> bool:
    normalized = line.lower()
    return any(marker in normalized for marker in ("todo", "fixme", "hack", "xxx"))


def _lexical_complexity_score(meaningful_lines: list[str]) -> int:
    branch_prefixes = (
        "if ",
        "elif ",
        "for ",
        "async for ",
        "while ",
        "except",
        "try:",
        "with ",
        "async with ",
        "match ",
        "case ",
    )
    branch_count = 0
    max_indent_depth = 0
    for line in meaningful_lines:
        stripped = line.lstrip()
        if stripped.startswith(branch_prefixes) or " and " in stripped or " or " in stripped:
            branch_count += 1
        max_indent_depth = max(max_indent_depth, (len(line) - len(stripped)) // 4)
    return branch_count * 10 + max_indent_depth * 14


def _is_low_value_unit(unit: CodeUnit) -> bool:
    meaningful_lines = _meaningful_source_lines(unit.text)
    if len(meaningful_lines) <= 3:
        return True
    if unit.kind.value in {"file", "package"}:
        return not any(
            line.startswith(("class ", "def ", "async def ")) for line in meaningful_lines
        )
    try:
        tree = ast.parse(unit.text)
    except SyntaxError:
        return False
    if not tree.body:
        return True
    first_statement = tree.body[0]
    if not isinstance(
        first_statement,
        ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ):
        return False
    return all(_is_placeholder_statement(statement) for statement in first_statement.body)


def _meaningful_source_lines(text: str) -> list[str]:
    return [
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


def _is_placeholder_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str | type(Ellipsis))
    ):
        return True
    if isinstance(statement, ast.Raise) and statement.exc is not None:
        exception = statement.exc
        if isinstance(exception, ast.Call):
            exception = exception.func
        return isinstance(exception, ast.Name) and exception.id in {
            "NotImplementedError",
            "NotImplemented",
        }
    return False


def _finalized_unit_ids(entries: list[LedgerEntry]) -> set[str]:
    return {entry.unit_id for entry in entries if entry.outcome in FINAL_UNIT_OUTCOMES}


def _sync_state_progress(
    state: RunState,
    ledger: RunLedger,
    repo: GitRepo,
    run_dir: Path,
    *,
    write_dashboard: bool = True,
) -> None:
    state.units_done = len(_finalized_unit_ids(ledger.read()))
    state.current_head = repo.head()
    state.save(run_dir)
    if write_dashboard:
        write_run_dashboard(run_dir)


def _mark_run_interrupted(
    run_dir: Path,
    event_log: RunEventLog,
    state: RunState,
    repo: GitRepo,
    *,
    message: str,
    error: str,
) -> None:
    _rollback_workspace(repo, repo.head())
    state.status = "interrupted"
    state.current_head = repo.head()
    state.error = error
    state.save(run_dir)
    _record_event(
        run_dir,
        event_log,
        "run_interrupted",
        run_id=state.run_id,
        unit_id=state.current_unit_id,
        stage=state.current_stage,
        message=message,
        error=error,
        current_head=state.current_head,
    )


def _verify_workspace(
    workspace: Path,
    configured_commands: list[str],
    characterization_baseline: list[object],
    *,
    staged: bool = False,
    changed_files: list[str] | None = None,
) -> VerificationReport:
    if staged:
        return _verify_changed_python_files(workspace, changed_files or [])
    return _verify_workspace_full(
        workspace,
        configured_commands,
        characterization_baseline,
    )


def _verify_workspace_full(
    workspace: Path,
    configured_commands: list[str],
    characterization_baseline: list[object],
) -> VerificationReport:
    if configured_commands:
        commands = [[sys.executable, "-m", "compileall", "-q", "."]]
        commands.extend(parse_command(command) for command in configured_commands)
    else:
        commands = detect_verification_commands(workspace)
    report = run_commands(commands, cwd=workspace)
    if not report.ok:
        return report

    if characterization_baseline:
        characterization_results = []
        for expected in characterization_baseline:
            current = run_commands([expected.command], cwd=workspace).results[0]
            if (
                current.returncode == expected.returncode
                and current.stdout == expected.stdout
                and current.stderr == expected.stderr
            ):
                characterization_results.append(
                    CommandResult(
                        command=["characterization", "matches-baseline", *current.command],
                        returncode=0,
                        stdout=(
                            f"Matched baseline stdout bytes={len(current.stdout)} "
                            f"stderr bytes={len(current.stderr)}."
                        ),
                        stderr="",
                    )
                )
            else:
                characterization_results.append(
                    CommandResult(
                        command=["characterization", "changed", *current.command],
                        returncode=1,
                        stdout=current.stdout,
                        stderr=(
                            "Characterization output changed for command: "
                            f"{' '.join(current.command)}"
                        ),
                    )
                )
                break
        report.results.extend(characterization_results)
    return report


def _verify_changed_python_files(
    workspace: Path,
    changed_files: list[str],
) -> VerificationReport:
    python_files = [
        path for path in changed_files if path.endswith(".py") and (workspace / path).is_file()
    ]
    if not python_files:
        return VerificationReport()
    return run_commands([[sys.executable, "-m", "py_compile", *python_files]], cwd=workspace)


def _ensure_unit_tests(
    *,
    provider: object,
    unit: CodeUnit,
    summary: CumulativeSummary,
    workspace: Path,
    repo: GitRepo,
    ledger: RunLedger,
    run_dir: Path,
    event_log: RunEventLog,
    state: RunState,
    verification_commands: list[str],
    characterization_commands: list[str],
    characterization_baseline: list[object],
    staged_verification: bool,
) -> tuple[bool, list[str]]:
    state.current_stage = "test_audit"
    state.save(run_dir)
    assessment = provider.assess_tests(
        unit=unit,
        summary=summary,
        workspace=workspace,
        verification_commands=verification_commands,
        characterization_commands=characterization_commands,
    )
    _record_event(
        run_dir,
        event_log,
        "test_audit_completed",
        unit_id=unit.unit_id,
        adequate=assessment.adequate,
        reason=assessment.reason,
        recommendations=[
            _dump_model(recommendation) for recommendation in assessment.recommendations
        ],
    )
    if assessment.adequate:
        return True, []

    before_head = repo.head()
    try:
        state.current_stage = "test_write"
        state.save(run_dir)
        test_result = provider.write_tests(
            unit=unit,
            assessment=assessment,
            summary=summary,
            workspace=workspace,
            verification_commands=verification_commands,
            characterization_commands=characterization_commands,
        )
        _format_workspace(workspace, repo.changed_files())
        changed_files = repo.changed_files()
        test_diff = repo.diff()
        _record_event(
            run_dir,
            event_log,
            "test_write_completed",
            unit_id=unit.unit_id,
            result=_dump_model(test_result),
            changed_files=changed_files,
            diff=test_diff,
        )
        if not changed_files:
            _record_event(
                run_dir,
                event_log,
                "test_generation_failed",
                unit_id=unit.unit_id,
                reason="Test writer did not change the workspace.",
                result=_dump_model(test_result),
            )
            ledger.append(
                _ledger_entry(
                    unit=unit,
                    outcome="test_generation_failed",
                    changed_files=[],
                    retrieval=assessment,
                    verification=VerificationReport(),
                    message=(
                        "Test audit requested more coverage, but the test writer did not change "
                        "the workspace. " + test_result.message
                    ),
                )
            )
            return False, []

        new_commands = test_result.test_commands
        state.current_stage = "test_verification"
        state.save(run_dir)
        verification = _verify_workspace(
            workspace,
            _merge_commands(verification_commands, new_commands),
            characterization_baseline,
            staged=staged_verification,
            changed_files=changed_files,
        )
        if not verification.ok:
            _rollback_workspace(repo, before_head)
            _record_event(
                run_dir,
                event_log,
                "test_generation_verification_failed",
                unit_id=unit.unit_id,
                changed_files=changed_files,
                verification=_dump_model(verification),
                message=_verification_failure_message(verification),
            )
            ledger.append(
                _ledger_entry(
                    unit=unit,
                    outcome="test_generation_failed",
                    changed_files=changed_files,
                    retrieval=assessment,
                    verification=verification,
                    message=_verification_failure_message(verification),
                )
            )
            return False, []

        repo.add_all(force=True)
        repo.commit(f"ccr add tests for {unit.qualified_name}")
        state.current_head = repo.head()
        state.save(run_dir)
        commit_hash = repo.head()
        _record_event(
            run_dir,
            event_log,
            "tests_added",
            unit_id=unit.unit_id,
            changed_files=changed_files,
            test_commands=new_commands,
            commit=commit_hash,
        )
        ledger.append(
            _ledger_entry(
                unit=unit,
                outcome="tests_added",
                changed_files=changed_files,
                retrieval=assessment,
                verification=verification,
                message=test_result.message,
                commit=commit_hash,
            )
        )
        return True, new_commands
    except Exception as exc:
        _rollback_workspace(repo, before_head)
        _record_event(
            run_dir,
            event_log,
            "test_generation_error",
            unit_id=unit.unit_id,
            message=str(exc),
        )
        raise


def _capture_characterization_baseline(workspace: Path, commands: list[str]) -> list[object]:
    if not commands:
        return []
    parsed_commands = [parse_command(command) for command in commands]
    report = run_commands(parsed_commands, cwd=workspace)
    if not report.ok:
        failed = _verification_failure_message(report)
        msg = f"Characterization baseline command failed before refactoring: {failed}"
        raise RuntimeError(msg)
    return report.results


def _format_workspace(workspace: Path, changed_files: list[str]) -> None:
    if shutil.which("ruff") is None:
        return
    targets = [
        path for path in changed_files if path.endswith(".py") and (workspace / path).is_file()
    ]
    if not targets:
        return
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", *targets],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )


def _rollback_workspace(repo: GitRepo, revision: str) -> None:
    repo.reset_hard(revision)
    repo.clean_untracked()


def _refresh_unit(
    workspace: Path,
    original_unit: CodeUnit,
    *,
    config: RefactorRunConfig,
) -> CodeUnit:
    try:
        current_units = _analyze_project_for_config(workspace, config)
    except Exception:
        return original_unit
    for unit in current_units:
        if unit.unit_id == original_unit.unit_id:
            return unit
    return original_unit


def _ledger_entry(
    *,
    unit: CodeUnit,
    outcome: str,
    changed_files: list[str],
    retrieval: object,
    verification: VerificationReport,
    message: str,
    commit: str | None = None,
    refactor_result: RefactorResult | None = None,
) -> LedgerEntry:
    ideas = getattr(retrieval, "ideas", [])
    examples = [idea.code_example[:200] for idea in ideas]
    recommendations = getattr(retrieval, "recommendations", [])
    examples.extend(
        f"test:{recommendation.name}: {recommendation.behavior[:160]}"
        for recommendation in recommendations
    )
    return LedgerEntry(
        unit_id=unit.unit_id,
        outcome=outcome,
        changed_files=changed_files,
        member_paths=unit.member_paths,
        owned_paths=unit.owned_paths,
        context_paths=unit.context_paths,
        examples_used=examples,
        checks_run=verification.descriptions(),
        renames=refactor_result.renames if refactor_result else [],
        signature_changes=refactor_result.signature_changes if refactor_result else [],
        moved_logic=refactor_result.moved_logic if refactor_result else [],
        integration_points_updated=(
            refactor_result.integration_points_updated if refactor_result else []
        ),
        behavior_changes=refactor_result.behavior_changes if refactor_result else [],
        commit=commit,
        message=message,
    )


def _integration_point_paths(unit: CodeUnit, refactor_result: RefactorResult) -> list[str]:
    paths = set(unit.owned_paths or unit.member_paths or [unit.path])
    paths.update(update.path for update in refactor_result.integration_points_updated)
    return sorted(paths)


def _ledger_integration_point_paths(path: str, entry: LedgerEntry) -> list[str]:
    paths = set(entry.owned_paths or entry.member_paths or ([path] if path else []))
    paths.update(update.path for update in entry.integration_points_updated)
    return sorted(paths)


def _merge_commands(*command_groups: list[str]) -> list[str]:
    commands: list[str] = []
    for group in command_groups:
        for command in group:
            if command not in commands:
                commands.append(command)
    return commands


def _verification_failure_message(report: VerificationReport) -> str:
    failed = [result for result in report.results if not result.ok]
    if not failed:
        return "Verification failed."
    result = failed[0]
    output = _verification_failure_output(result)
    return output[-2_000:] if output else result.describe()


def _verification_failure_output(result: CommandResult) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    if stdout and _is_wrapper_error(stderr):
        return stdout
    return stderr or stdout


def _is_wrapper_error(stderr: str) -> bool:
    return "See above for error" in stderr


def _build_run_summary(
    *,
    run_id: str,
    original: Path,
    workspace: Path,
    ledger: RunLedger,
) -> RunSummary:
    entries = ledger.read()
    applied = [entry for entry in entries if entry.outcome in {"accepted", "tests_added"}]
    skipped = [entry for entry in entries if entry.outcome not in {"accepted", "tests_added"}]
    apply_command = f"ccr apply {original} --run {workspace.parent}"
    return RunSummary(
        run_id=run_id,
        original_path=str(original),
        copied_workspace=str(workspace),
        applied_changes=[
            f"{entry.unit_id}: {entry.outcome}: {', '.join(entry.changed_files)}"
            for entry in applied
        ],
        skipped_changes=[f"{entry.unit_id}: {entry.outcome}" for entry in skipped],
        examples_used=[example for entry in entries for example in entry.examples_used],
        verification_results=[check for entry in entries for check in entry.checks_run],
        apply_command=apply_command,
        warning=WORKSPACE_WARNING,
    )


def _record_event(
    run_dir: Path,
    event_log: RunEventLog,
    event_type: str,
    **payload: Any,
) -> None:
    record_run_event(run_dir, event_log, event_type, **payload)


def _dump_model(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
