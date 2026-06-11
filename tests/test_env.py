from __future__ import annotations

import os
from pathlib import Path

from ccr.env import load_environment_files


def test_load_environment_files_reads_local_env_and_maps_langfuse_aliases(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "NEXTAUTH_URL=http://localhost:3000",
                "LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-local",
                "LANGFUSE_INIT_PROJECT_SECRET_KEY='sk-local'",
                "IGNORED_LINE",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_environment_files()

    assert loaded[0] == tmp_path / ".env"
    assert os.environ["LANGFUSE_BASE_URL"] == "http://localhost:3000"
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-local"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-local"


def test_load_environment_files_uses_explicit_file_and_preserves_existing_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-existing")
    env_file = tmp_path / ".env.ccr-local"
    env_file.write_text(
        "\n".join(
            [
                "LANGFUSE_BASE_URL=https://langfuse.example",
                "LANGFUSE_PUBLIC_KEY=pk-file",
                "LANGFUSE_SECRET_KEY=sk-file",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_environment_files(cwd=tmp_path, extra_files=(env_file,))

    assert env_file in loaded
    assert os.environ["LANGFUSE_BASE_URL"] == "https://langfuse.example"
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-existing"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-file"


def _clear_langfuse_env(monkeypatch) -> None:
    for key in [
        "CCR_ENV_FILE",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "NEXTAUTH_URL",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
