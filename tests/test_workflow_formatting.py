from __future__ import annotations

import subprocess
from pathlib import Path

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
