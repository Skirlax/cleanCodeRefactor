from __future__ import annotations

import subprocess
from pathlib import Path

from ccr.verification.runner import CommandResult, VerificationReport
from ccr.workflow import run as workflow_run


def test_format_workspace_formats_only_changed_python_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "changed.py").write_text("VALUE=1\n", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(workflow_run.shutil, "which", lambda executable: executable)
    monkeypatch.setattr(workflow_run.subprocess, "run", fake_run)

    workflow_run._format_workspace(
        tmp_path,
        [
            "pkg/changed.py",
            "pkg/missing.py",
            "pkg/notes.md",
            "pkg/__pycache__/changed.cpython-312.pyc",
        ],
    )

    command = captured["command"]
    assert command[-1] == "pkg/changed.py"
    assert "." not in command


def test_verification_failure_message_prefers_stdout_for_wrapper_errors() -> None:
    report = VerificationReport(
        results=[
            CommandResult(
                command=["conda", "run", "python", "-m", "pytest", "tests"],
                returncode=1,
                stdout="FAILED tests/test_network.py::test_loss\nRuntimeError: device mismatch",
                stderr=(
                    "ERROR conda.cli.main_run:execute(124): "
                    "`conda run python -m pytest tests` failed. (See above for error)\n"
                ),
            )
        ]
    )

    assert workflow_run._verification_failure_message(report) == (
        "FAILED tests/test_network.py::test_loss\nRuntimeError: device mismatch"
    )
