from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_VARIABLE = "CCR_ENV_FILE"
DEFAULT_ENV_FILES = (".env", ".env.local")


def load_environment_files(
    *,
    cwd: Path | None = None,
    extra_files: tuple[Path, ...] = (),
) -> list[Path]:
    """Load local environment files without overriding already-exported variables."""
    loaded: list[Path] = []
    for path in _candidate_env_files(cwd or Path.cwd(), extra_files):
        if not path.exists() or not path.is_file():
            continue
        _load_env_file(path)
        _normalize_langfuse_environment()
        loaded.append(path)
    return loaded


def _candidate_env_files(cwd: Path, extra_files: tuple[Path, ...]) -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get(ENV_FILE_VARIABLE)
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(path.expanduser() for path in extra_files)

    project_root = Path(__file__).resolve().parents[1]
    for root in (cwd.resolve(), project_root):
        for filename in DEFAULT_ENV_FILES:
            path = root / filename
            if path not in candidates:
                candidates.append(path)
    return candidates


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _strip_env_value(value.strip()))


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _normalize_langfuse_environment() -> None:
    if "LANGFUSE_BASE_URL" not in os.environ and "NEXTAUTH_URL" in os.environ:
        os.environ["LANGFUSE_BASE_URL"] = os.environ["NEXTAUTH_URL"]
    if "LANGFUSE_PUBLIC_KEY" not in os.environ and "LANGFUSE_INIT_PROJECT_PUBLIC_KEY" in os.environ:
        os.environ["LANGFUSE_PUBLIC_KEY"] = os.environ["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
    if "LANGFUSE_SECRET_KEY" not in os.environ and "LANGFUSE_INIT_PROJECT_SECRET_KEY" in os.environ:
        os.environ["LANGFUSE_SECRET_KEY"] = os.environ["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
