from __future__ import annotations

import sys
from pathlib import Path


def detect_verification_commands(project_root: Path) -> list[list[str]]:
    commands: list[list[str]] = [[sys.executable, "-m", "compileall", "-q", "."]]
    if (project_root / "tests").exists() or any(project_root.glob("test_*.py")):
        commands.append([sys.executable, "-m", "pytest"])
    return commands


def parse_command(command: str) -> list[str]:
    import shlex

    return shlex.split(command)
