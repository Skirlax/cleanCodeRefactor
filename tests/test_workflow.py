from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ccr.workflow.ledger import RunLedger
from ccr.workflow.run import RefactorRunConfig, run_refactor
from ccr.workflow.state import RunState

GILDED_ROSE_PYTHON = Path(
    "/home/vvlcek/Code/CodeReferences/TestTargets/Python/GildedRose-Refactoring-Kata/python"
)


pytestmark = pytest.mark.skipif(
    not GILDED_ROSE_PYTHON.exists(),
    reason="Gilded Rose kata is not cloned into CodeReferences.",
)


def test_heuristic_run_refactors_cloned_gilded_rose_without_touching_original(
    tmp_path: Path,
) -> None:
    original_source = (GILDED_ROSE_PYTHON / "gilded_rose.py").read_text(encoding="utf-8")

    summary = run_refactor(
        RefactorRunConfig(
            project=GILDED_ROSE_PYTHON,
            provider="heuristic",
            run_root=tmp_path / "runs",
            characterization_commands=[f"{sys.executable} texttest_fixture.py 30"],
            judge=True,
        )
    )

    run_dir = Path(summary.copied_workspace).parent
    state = RunState.load(run_dir)
    entries = RunLedger(run_dir / "ledger.jsonl").read()
    copied_workspace = Path(summary.copied_workspace)
    copied_source = (copied_workspace / "gilded_rose.py").read_text(encoding="utf-8")
    dashboard = (run_dir / "dashboard.html").read_text(encoding="utf-8")
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")

    assert state.status == "complete"
    assert state.baseline_commit
    assert [entry.outcome for entry in entries] == ["tests_added", "accepted", "accepted"]
    assert "CCR Run Dashboard" in dashboard
    assert "test_audit_completed" in events
    assert "retrieval_completed" in events
    assert (copied_workspace / "tests" / "test_gilded_rose_behavior.py").exists()
    assert "def _update_backstage_pass_quality" in copied_source
    assert (GILDED_ROSE_PYTHON / "gilded_rose.py").read_text(encoding="utf-8") == original_source
    assert summary.apply_command.startswith(f"ccr apply {GILDED_ROSE_PYTHON}")
    assert "Applying these edits modifies your original project" in summary.warning
