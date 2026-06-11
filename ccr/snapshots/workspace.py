from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from ccr.extraction.units import DEFAULT_EXCLUDED_DIRS

WORKSPACE_WARNING = (
    "Inspect and test the copied workspace carefully before applying changes.\n"
    "Applying these edits modifies your original project. If the copied project was not verified "
    "carefully, the changes may introduce bugs, break integrations, overwrite local work, or cause "
    "data loss."
)


def create_workspace_copy(
    original_project: Path,
    *,
    run_root: Path,
    run_id: str | None = None,
) -> tuple[str, Path]:
    original_project = original_project.resolve()
    if not original_project.exists():
        msg = f"Project does not exist: {original_project}"
        raise FileNotFoundError(msg)
    if not original_project.is_dir():
        msg = f"Project must be a directory: {original_project}"
        raise NotADirectoryError(msg)

    run_id = run_id or _new_run_id()
    run_dir = (run_root / run_id).resolve()
    workspace = run_dir / "workspace"
    if workspace.exists():
        msg = f"Run workspace already exists: {workspace}"
        raise FileExistsError(msg)

    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(original_project, workspace, ignore=_copy_ignore)
    return run_id, workspace


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = set(DEFAULT_EXCLUDED_DIRS).intersection(names)
    ignored.update(name for name in names if name.endswith((".pyc", ".pyo")))
    return ignored


def _new_run_id() -> str:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}"
