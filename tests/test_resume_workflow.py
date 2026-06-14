from __future__ import annotations

from pathlib import Path

import pytest

from ccr.schemas.judge import JudgeResult
from ccr.schemas.refactor import RefactorIntensity, RefactorOutcome, RefactorResult
from ccr.schemas.retrieval import RetrievalResult
from ccr.workflow import run as workflow_run
from ccr.workflow.ledger import RunLedger
from ccr.workflow.run import RefactorRunConfig, resume_refactor, run_refactor
from ccr.workflow.state import RunState


def test_resume_continues_after_interrupted_unit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "sample.py"
    source.write_text(
        "\n".join(
            [
                "class Alpha:",
                '    value = "alpha"',
                "",
                "",
                "class Beta:",
                '    value = "beta"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    providers = [
        _EditingProvider(fail_on="Beta"),
        _EditingProvider(),
    ]
    monkeypatch.setattr(
        workflow_run,
        "build_provider",
        lambda *args, **kwargs: providers.pop(0),
    )

    config = RefactorRunConfig(
        project=project,
        provider="codex",
        run_root=tmp_path / "runs",
        max_units=2,
        test_generation_enabled=False,
    )
    with pytest.raises(RuntimeError, match="usage limit"):
        run_refactor(config)

    run_dir = next((tmp_path / "runs").iterdir())
    interrupted_state = RunState.load(run_dir)
    interrupted_entries = RunLedger(run_dir / "ledger.jsonl").read()

    assert interrupted_state.status == "interrupted"
    assert interrupted_state.current_unit_id == "sample.py::Beta"
    assert interrupted_state.current_stage == "refactor"
    assert interrupted_state.units_done == 1
    assert [entry.unit_id for entry in interrupted_entries] == ["sample.py::Alpha"]
    assert 'value = "beta dirty"' not in (run_dir / "workspace" / "sample.py").read_text(
        encoding="utf-8"
    )

    summary = resume_refactor(run_dir)
    resumed_state = RunState.load(run_dir)
    resumed_entries = RunLedger(run_dir / "ledger.jsonl").read()
    resumed_source = (run_dir / "workspace" / "sample.py").read_text(encoding="utf-8")
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")

    assert resumed_state.status == "complete"
    assert resumed_state.units_done == 2
    assert [entry.unit_id for entry in resumed_entries] == [
        "sample.py::Alpha",
        "sample.py::Beta",
    ]
    assert resumed_source.count("refactored") == 2
    assert "run_resumed" in events
    assert "run_interrupted" in events
    assert len(summary.applied_changes) == 2


def test_judge_rejection_retries_refactor_with_feedback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "sample.py"
    source.write_text(
        "\n".join(
            [
                "class Alpha:",
                '    value = "alpha"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    provider = _JudgeRetryProvider()
    monkeypatch.setattr(
        workflow_run,
        "build_provider",
        lambda *args, **kwargs: provider,
    )

    summary = run_refactor(
        RefactorRunConfig(
            project=project,
            provider="codex",
            run_root=tmp_path / "runs",
            max_units=1,
            test_generation_enabled=False,
            judge=True,
            judge_retries=1,
        )
    )

    run_dir = next((tmp_path / "runs").iterdir())
    entries = RunLedger(run_dir / "ledger.jsonl").read()
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    copied_source = (run_dir / "workspace" / "sample.py").read_text(encoding="utf-8")

    assert provider.refactor_calls == 2
    assert len(provider.judge_diffs) == 2
    assert "new file mode" in provider.judge_diffs[0]
    assert "helper.py" in provider.judge_diffs[0]
    assert "missing helper module" in (provider.instructions[1] or "")
    assert [entry.outcome for entry in entries] == ["accepted"]
    assert entries[0].commit
    assert summary.applied_changes == ["sample.py::Alpha: accepted: sample.py"]
    assert 'value = "alpha retry"' in copied_source
    assert "judge_retrying" in events
    assert "judge_rejected" not in events


def test_judge_retry_feedback_is_conditional_by_refactor_intensity() -> None:
    judge_result = JudgeResult(
        unit_id="sample.py::Alpha",
        accepted=False,
        issues=["behavior changed"],
        summary="Needs revision.",
    )

    conservative = workflow_run._judge_retry_feedback(
        judge_result,
        RefactorIntensity.CONSERVATIVE,
    )
    structural = workflow_run._judge_retry_feedback(
        judge_result,
        RefactorIntensity.STRUCTURAL,
    )

    assert "while preserving behavior" in conservative
    assert "behavior_changes" in structural
    assert "Preserve behavior that is not explicitly changed" in structural


class _EditingProvider:
    name = "editing"

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on

    def retrieve(self, *, unit, references, workspace):
        return RetrievalResult(unit_id=unit.unit_id, ideas=[])

    def refactor(
        self,
        *,
        unit,
        retrieval,
        summary,
        workspace,
        instructions,
        refactor_intensity,
    ):
        source = workspace / unit.path
        text = source.read_text(encoding="utf-8")
        marker = unit.name.lower()
        if unit.name == self.fail_on:
            source.write_text(
                text.replace(f'value = "{marker}"', f'value = "{marker} dirty"'),
                encoding="utf-8",
            )
            raise RuntimeError("usage limit")
        source.write_text(
            text.replace(f'value = "{marker}"', f'value = "{marker} refactored"'),
            encoding="utf-8",
        )
        return RefactorResult(
            unit_id=unit.unit_id,
            outcome=RefactorOutcome.CHANGED,
            changed_files=[unit.path],
            message=f"Refactored {unit.name}.",
        )

    def judge(
        self,
        *,
        unit,
        diff,
        refactor_result,
        summary,
        workspace,
        refactor_intensity,
    ):
        raise AssertionError("judge should not run in this test")

    def assess_tests(
        self,
        *,
        unit,
        summary,
        workspace,
        verification_commands,
        characterization_commands,
    ):
        raise AssertionError("test audit should not run in this test")

    def write_tests(
        self,
        *,
        unit,
        assessment,
        summary,
        workspace,
        verification_commands,
        characterization_commands,
    ):
        raise AssertionError("test writing should not run in this test")


class _JudgeRetryProvider:
    name = "judge-retry"

    def __init__(self) -> None:
        self.refactor_calls = 0
        self.judge_diffs: list[str] = []
        self.instructions: list[str | None] = []

    def retrieve(self, *, unit, references, workspace):
        return RetrievalResult(unit_id=unit.unit_id, ideas=[])

    def refactor(
        self,
        *,
        unit,
        retrieval,
        summary,
        workspace,
        instructions,
        refactor_intensity,
    ):
        self.refactor_calls += 1
        self.instructions.append(instructions)
        source = workspace / unit.path
        if self.refactor_calls == 1:
            source.write_text(
                "\n".join(
                    [
                        "from helper import VALUE",
                        "",
                        "",
                        "class Alpha:",
                        "    value = VALUE",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (workspace / "helper.py").write_text(
                'VALUE = "alpha first"\n',
                encoding="utf-8",
            )
        else:
            source.write_text(
                "\n".join(
                    [
                        "class Alpha:",
                        '    value = "alpha retry"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

        return RefactorResult(
            unit_id=unit.unit_id,
            outcome=RefactorOutcome.CHANGED,
            changed_files=[unit.path],
            message=f"Refactor attempt {self.refactor_calls}.",
        )

    def judge(
        self,
        *,
        unit,
        diff,
        refactor_result,
        summary,
        workspace,
        refactor_intensity,
    ):
        self.judge_diffs.append(diff)
        if len(self.judge_diffs) == 1:
            return JudgeResult(
                unit_id=unit.unit_id,
                accepted=False,
                issues=["missing helper module"],
                summary="Rejected first attempt.",
            )
        return JudgeResult(
            unit_id=unit.unit_id,
            accepted=True,
            issues=[],
            summary="Accepted retry.",
        )

    def assess_tests(
        self,
        *,
        unit,
        summary,
        workspace,
        verification_commands,
        characterization_commands,
    ):
        raise AssertionError("test audit should not run in this test")

    def write_tests(
        self,
        *,
        unit,
        assessment,
        summary,
        workspace,
        verification_commands,
        characterization_commands,
    ):
        raise AssertionError("test writing should not run in this test")
