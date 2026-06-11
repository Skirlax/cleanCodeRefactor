from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field


class CommandResult(BaseModel):
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def describe(self) -> str:
        status = "ok" if self.ok else f"failed:{self.returncode}"
        return f"{' '.join(self.command)} ({status})"


class VerificationReport(BaseModel):
    results: list[CommandResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def descriptions(self) -> list[str]:
        return [result.describe() for result in self.results]


def run_commands(
    commands: list[list[str]],
    *,
    cwd: Path,
    timeout_seconds: int = 120,
) -> VerificationReport:
    results: list[CommandResult] = []
    for command in commands:
        environment, executable_command = _split_leading_env_assignments(command)
        try:
            completed = subprocess.run(
                executable_command,
                cwd=cwd,
                env=os.environ | environment,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            results.append(
                CommandResult(
                    command=command,
                    returncode=127,
                    stderr=str(exc),
                )
            )
            break
        results.append(
            CommandResult(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
        if completed.returncode != 0:
            break
    return VerificationReport(results=results)


def _split_leading_env_assignments(command: list[str]) -> tuple[dict[str, str], list[str]]:
    environment: dict[str, str] = {}
    index = 0
    while index < len(command) and _ENV_ASSIGNMENT.match(command[index]):
        key, value = command[index].split("=", 1)
        environment[key] = value
        index += 1
    return environment, command[index:]


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
