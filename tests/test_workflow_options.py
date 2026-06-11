from __future__ import annotations

import sys
from pathlib import Path

from ccr.workflow import run as workflow_run


def test_analyze_project_applies_optional_unit_filters(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "class Tiny:",
                "    pass",
                "",
                "",
                "class Useful:",
                "    def calculate(self):",
                "        first = 1",
                "        second = 2",
                "        return first + second",
                "",
            ]
        ),
        encoding="utf-8",
    )

    units = workflow_run.analyze_project(
        tmp_path,
        min_unit_lines=4,
        exclude_units=["*Tiny*"],
        skip_low_value_units=True,
    )

    assert [unit.qualified_name for unit in units] == ["Useful"]


def test_analyze_project_sorts_by_value_by_default(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "class Simple:",
                "    pass",
                "",
                "",
                "class Complex:",
                "    def calculate(self, items):",
                "        total = 0",
                "        for item in items:",
                "            if item:",
                "                if item > 10:",
                "                    total += item",
                "                else:",
                "                    total -= item",
                "        return total",
                "",
            ]
        ),
        encoding="utf-8",
    )

    units = workflow_run.analyze_project(tmp_path)

    assert [unit.qualified_name for unit in units] == ["Complex", "Simple"]


def test_analyze_project_can_preserve_source_order(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "class Simple:",
                "    pass",
                "",
                "",
                "class Complex:",
                "    def calculate(self, items):",
                "        for item in items:",
                "            if item:",
                "                return item",
                "        return 0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    units = workflow_run.analyze_project(tmp_path, unit_sort="source")

    assert [unit.qualified_name for unit in units] == ["Simple", "Complex"]


def test_staged_verification_runs_changed_file_syntax_only(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    failing_full_command = f"{sys.executable} -c 'raise SystemExit(3)'"

    report = workflow_run._verify_workspace(
        tmp_path,
        [failing_full_command],
        [],
        staged=True,
        changed_files=["sample.py"],
    )

    assert report.ok
    assert report.results[0].command == [sys.executable, "-m", "py_compile", "sample.py"]


def test_full_verification_still_runs_configured_commands(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    failing_full_command = f"{sys.executable} -c 'raise SystemExit(3)'"

    report = workflow_run._verify_workspace(
        tmp_path,
        [failing_full_command],
        [],
    )

    assert not report.ok
    assert report.results[-1].returncode == 3
