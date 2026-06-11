from __future__ import annotations

from pathlib import Path

import pytest

from ccr.schemas.refactor import RefactorOutcome, RefactorResult
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


class _EditingProvider:
    name = "editing"

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on

    def retrieve(self, *, unit, references, workspace):
        return RetrievalResult(unit_id=unit.unit_id, ideas=[])

    def refactor(self, *, unit, retrieval, summary, workspace, instructions):
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

    def judge(self, *, unit, diff, summary, workspace):
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
