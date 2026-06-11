from __future__ import annotations

from pathlib import Path

import pytest

from ccr.extraction.units import extract_units

GILDED_ROSE_PYTHON = Path(
    "/home/vvlcek/Code/CodeReferences/TestTargets/Python/GildedRose-Refactoring-Kata/python"
)


@pytest.mark.skipif(
    not GILDED_ROSE_PYTHON.exists(),
    reason="Gilded Rose kata is not cloned into CodeReferences.",
)
def test_extracts_preferred_gilded_rose_units() -> None:
    units = extract_units(GILDED_ROSE_PYTHON)

    assert [unit.qualified_name for unit in units] == ["GildedRose", "Item"]
    assert [unit.kind for unit in units] == ["class", "class"]
    assert units[0].path == "gilded_rose.py"


@pytest.mark.skipif(
    not GILDED_ROSE_PYTHON.exists(),
    reason="Gilded Rose kata is not cloned into CodeReferences.",
)
def test_can_include_methods_for_inspection() -> None:
    units = extract_units(GILDED_ROSE_PYTHON, include_methods=True)
    names = {unit.qualified_name for unit in units}

    assert "GildedRose" in names
    assert "GildedRose.update_quality" in names
    assert "Item.__repr__" in names


def test_extracts_file_units_when_requested(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="file")

    assert [unit.unit_id for unit in units] == ["pkg/module.py::<file>"]
    assert units[0].kind == "file"
    assert units[0].qualified_name == "pkg.module"


def test_extracts_package_units_and_falls_back_to_files(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    child_package = package / "child"
    loose = tmp_path / "scripts"
    child_package.mkdir(parents=True)
    loose.mkdir()
    (package / "__init__.py").write_text("PACKAGE = True\n", encoding="utf-8")
    (package / "module.py").write_text("class PackageThing:\n    pass\n", encoding="utf-8")
    (child_package / "__init__.py").write_text("", encoding="utf-8")
    (child_package / "feature.py").write_text(
        "def child_feature():\n    return 1\n",
        encoding="utf-8",
    )
    (loose / "tool.py").write_text("def run():\n    return 2\n", encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="package")

    assert [unit.unit_id for unit in units] == [
        "pkg::package",
        "pkg/child::package",
        "scripts/tool.py::<file>",
    ]
    assert [unit.kind for unit in units] == ["package", "package", "file"]
    assert "# File: pkg/module.py" in units[0].text
    assert units[2].qualified_name == "scripts.tool"


def test_package_mode_falls_back_to_files_without_packages(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="package")

    assert [unit.unit_id for unit in units] == ["module.py::<file>"]
    assert units[0].kind == "file"
