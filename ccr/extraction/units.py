from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from ccr.schemas.unit import CodeUnit, UnitKind

DEFAULT_EXCLUDED_DIRS = {
    ".ccr",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
    "vendor",
    "venv",
}

DEFAULT_EXCLUDED_REFACTOR_DIRS = {
    "tests",
    "test",
}


def is_excluded(path: Path, root: Path, excluded_dirs: set[str] | None = None) -> bool:
    excluded = excluded_dirs or DEFAULT_EXCLUDED_DIRS
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    return any(part in excluded for part in relative_parts)


def iter_python_files(root: Path, excluded_dirs: set[str] | None = None) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if not is_excluded(path, root, excluded_dirs) and not is_test_or_fixture_path(path, root):
            yield path


def is_test_or_fixture_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    if any(part in DEFAULT_EXCLUDED_REFACTOR_DIRS for part in relative.parts):
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name.endswith("_fixture.py")


def extract_units(
    project_root: Path,
    *,
    language: str = "python",
    include_methods: bool = False,
    excluded_dirs: set[str] | None = None,
    unit_mode: str = "code",
    model: str | None = None,
    target_unit_count: int = 5,
) -> list[CodeUnit]:
    if language.lower() != "python":
        msg = f"Only Python extraction is implemented for the MVP, got {language!r}."
        raise ValueError(msg)
    normalized_mode = unit_mode.lower()
    if normalized_mode not in {"code", "file", "package", "cluster"}:
        msg = f"Unknown unit mode {unit_mode!r}. Expected code, file, package, or cluster."
        raise ValueError(msg)

    if normalized_mode == "file":
        return _extract_file_units(project_root, excluded_dirs=excluded_dirs)
    if normalized_mode == "package":
        return _extract_package_or_file_units(project_root, excluded_dirs=excluded_dirs)
    if normalized_mode == "cluster":
        from ccr.extraction.clusters import extract_cluster_units

        return extract_cluster_units(
            project_root,
            model=model,
            target_unit_count=target_unit_count,
            excluded_dirs=excluded_dirs,
        )

    from ccr.extraction.tree_sitter_index import PythonTreeSitterIndex

    index = PythonTreeSitterIndex(project_root, excluded_dirs=excluded_dirs)
    return index.extract(include_methods=include_methods)


def _extract_file_units(
    project_root: Path,
    *,
    excluded_dirs: set[str] | None = None,
) -> list[CodeUnit]:
    return [
        _file_unit(project_root, path)
        for path in iter_python_files(project_root, excluded_dirs)
        if path.read_text(encoding="utf-8", errors="ignore").strip()
    ]


def _extract_package_or_file_units(
    project_root: Path,
    *,
    excluded_dirs: set[str] | None = None,
) -> list[CodeUnit]:
    python_files = list(iter_python_files(project_root, excluded_dirs))
    package_dirs = {path.parent for path in python_files if (path.parent / "__init__.py").is_file()}
    if not package_dirs:
        return _extract_file_units(project_root, excluded_dirs=excluded_dirs)

    grouped_package_files: dict[Path, list[Path]] = {directory: [] for directory in package_dirs}
    standalone_files: list[Path] = []
    for path in python_files:
        if path.parent in grouped_package_files:
            grouped_package_files[path.parent].append(path)
        else:
            standalone_files.append(path)

    units: list[CodeUnit] = []
    for package_dir in sorted(grouped_package_files):
        files = [
            path
            for path in sorted(grouped_package_files[package_dir])
            if path.read_text(encoding="utf-8", errors="ignore").strip()
        ]
        if files:
            units.append(_package_unit(project_root, package_dir, files))
    units.extend(
        _file_unit(project_root, path)
        for path in sorted(standalone_files)
        if path.read_text(encoding="utf-8", errors="ignore").strip()
    )
    return sorted(units, key=lambda unit: (unit.path, unit.kind.value, unit.qualified_name))


def _file_unit(project_root: Path, path: Path) -> CodeUnit:
    text = path.read_text(encoding="utf-8", errors="ignore")
    relative_path = path.relative_to(project_root).as_posix()
    qualified_name = Path(relative_path).with_suffix("").as_posix().replace("/", ".")
    line_count = max(1, text.count("\n") + 1)
    return CodeUnit(
        unit_id=f"{relative_path}::<file>",
        kind=UnitKind.FILE,
        name=Path(relative_path).name,
        qualified_name=qualified_name,
        path=relative_path,
        start_line=1,
        end_line=line_count,
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        member_paths=[relative_path],
        owned_paths=[relative_path],
    )


def _package_unit(project_root: Path, package_dir: Path, files: list[Path]) -> CodeUnit:
    relative = package_dir.relative_to(project_root)
    relative_path = "." if str(relative) == "." else relative.as_posix()
    qualified_name = project_root.name if relative_path == "." else relative_path.replace("/", ".")
    text = "\n\n".join(
        f"# File: {path.relative_to(project_root).as_posix()}\n"
        f"{path.read_text(encoding='utf-8', errors='ignore')}"
        for path in files
    )
    line_count = max(1, text.count("\n") + 1)
    return CodeUnit(
        unit_id=f"{relative_path}::package",
        kind=UnitKind.PACKAGE,
        name=package_dir.name,
        qualified_name=qualified_name,
        path=relative_path,
        start_line=1,
        end_line=line_count,
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        member_paths=[path.relative_to(project_root).as_posix() for path in files],
        owned_paths=[path.relative_to(project_root).as_posix() for path in files],
    )
