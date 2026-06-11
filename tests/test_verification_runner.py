from __future__ import annotations

import sys
from pathlib import Path

from ccr.verification.commands import parse_command
from ccr.verification.runner import run_commands


def test_run_commands_supports_leading_environment_assignments(tmp_path: Path) -> None:
    command = parse_command(
        f"CCR_TEST_ENV=ok {sys.executable} -c "
        "'import os; raise SystemExit(0 if os.environ[\"CCR_TEST_ENV\"] == \"ok\" else 1)'"
    )

    report = run_commands([command], cwd=tmp_path)

    assert report.ok
    assert report.results[0].command == command
