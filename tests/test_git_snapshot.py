from __future__ import annotations

from pathlib import Path

from ccr.snapshots.git import GitRepo


def test_baseline_tracks_ignored_source_and_filters_generated_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("pkg/game.py\n", encoding="utf-8")
    package = tmp_path / "pkg"
    package.mkdir()
    source = package / "game.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    repo = GitRepo(tmp_path)
    repo.ensure_baseline()

    tracked = repo.run("ls-files").stdout.splitlines()
    assert "pkg/game.py" in tracked

    source.write_text("VALUE = 2\n", encoding="utf-8")
    pycache = package / "__pycache__"
    pycache.mkdir()
    (pycache / "game.cpython-312.pyc").write_bytes(b"compiled")
    ruff_cache = tmp_path / ".ruff_cache" / "0.15.16"
    ruff_cache.mkdir(parents=True)
    (ruff_cache / "16881651732669577055").write_bytes(b"cache")

    assert repo.changed_files() == ["pkg/game.py"]
    diff = repo.diff()
    assert "pkg/game.py" in diff
    assert "__pycache__" not in diff
    assert ".ruff_cache" not in diff

    repo.add_all()
    repo.commit("change source")

    tracked_after_commit = repo.run("ls-files").stdout.splitlines()
    assert "pkg/game.py" in tracked_after_commit
    assert "pkg/__pycache__/game.cpython-312.pyc" not in tracked_after_commit
    assert ".ruff_cache/0.15.16/16881651732669577055" not in tracked_after_commit
