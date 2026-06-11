from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

GENERATED_DIR_NAMES = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "htmlcov",
}
GENERATED_FILE_NAMES = {
    ".coverage",
    "coverage.xml",
}
GENERATED_SUFFIXES = {
    ".pyc",
    ".pyo",
}
GENERATED_EXCLUDE_PATHS = [
    *[
        pathspec
        for name in sorted(GENERATED_DIR_NAMES)
        for pathspec in (
            f":(exclude){name}",
            f":(exclude){name}/**",
            f":(exclude)**/{name}",
            f":(exclude)**/{name}/**",
        )
    ],
    *[
        pathspec
        for name in sorted(GENERATED_FILE_NAMES)
        for pathspec in (f":(exclude){name}", f":(exclude)**/{name}")
    ],
    *[
        pathspec
        for suffix in sorted(GENERATED_SUFFIXES)
        for pathspec in (f":(exclude)*{suffix}", f":(exclude)**/*{suffix}")
    ],
]


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            check=check,
            text=True,
            capture_output=True,
        )

    def init(self) -> None:
        self.run("init")
        self.run("config", "user.email", "ccr@example.local")
        self.run("config", "user.name", "Clean Code Refactor")
        self.install_excludes()

    def install_excludes(self) -> None:
        info = self.path / ".git" / "info"
        if not info.exists():
            return
        exclude = info / "exclude"
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        additions = [
            "# CCR generated artifacts",
            *[
                pattern
                for name in sorted(GENERATED_DIR_NAMES)
                for pattern in (f"{name}/", f"**/{name}/")
            ],
            *[
                pattern
                for name in sorted(GENERATED_FILE_NAMES)
                for pattern in (name, f"**/{name}")
            ],
            *[
                pattern
                for suffix in sorted(GENERATED_SUFFIXES)
                for pattern in (f"*{suffix}", f"**/*{suffix}")
            ],
        ]
        missing = [line for line in additions if line not in existing.splitlines()]
        if missing:
            with exclude.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write("\n".join(missing) + "\n")

    def is_repo(self) -> bool:
        result = self.run("rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def has_commits(self) -> bool:
        return self.run("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    def add_all(self, *, force: bool = False) -> None:
        self.remove_generated_artifacts()
        args = ["add", "-A"]
        if force:
            args.append("-f")
        args.extend(["--", ".", *GENERATED_EXCLUDE_PATHS])
        self.run(*args)

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        args = ["commit", "-m", message]
        if allow_empty:
            args.insert(1, "--allow-empty")
        result = self.run(*args)
        return result.stdout.strip()

    def ensure_baseline(self) -> str:
        if not self.is_repo():
            self.init()
        else:
            self.install_excludes()
        self.add_all(force=True)
        if self.has_commits():
            if self.status_short().strip():
                self.commit("ccr baseline")
            return self.head()
        status = self.status_short()
        self.commit("ccr baseline", allow_empty=not bool(status))
        return self.head()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()

    def status_short(self) -> str:
        return self.run("status", "--short").stdout

    def changed_files(self) -> list[str]:
        result = self.run("diff", "--name-only")
        staged = self.run("diff", "--cached", "--name-only")
        names = {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and not _is_generated_path(line.strip())
        }
        names.update(
            line.strip()
            for line in staged.stdout.splitlines()
            if line.strip() and not _is_generated_path(line.strip())
        )
        for line in self.status_short().splitlines():
            if line.startswith("?? "):
                names.update(self._expand_untracked(line[3:].strip()))
        return sorted(names)

    def diff(self, *revisions: str) -> str:
        args = ["diff", "--binary", *revisions, "--", ".", *GENERATED_EXCLUDE_PATHS]
        return self.run(*args).stdout

    def show_stat(self, *revisions: str) -> str:
        args = ["diff", "--stat", *revisions, "--", ".", *GENERATED_EXCLUDE_PATHS]
        return self.run(*args).stdout

    def reset_hard(self, revision: str = "HEAD") -> None:
        self.run("reset", "--hard", revision)

    def clean_untracked(self) -> None:
        self.run("clean", "-fd")
        self.run("clean", "-fdX")
        self.remove_generated_artifacts()

    def apply_patch(self, patch_file: Path) -> None:
        self.run("apply", "--binary", str(patch_file))

    def remove_generated_artifacts(self) -> None:
        paths = sorted(
            self.path.rglob("*"),
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        )
        for path in paths:
            if not path.exists() or not _is_generated_path(str(path.relative_to(self.path))):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    def _expand_untracked(self, name: str) -> list[str]:
        path = self.path / name
        if path.is_dir():
            return [
                str(child.relative_to(self.path))
                for child in sorted(path.rglob("*"))
                if child.is_file() and not _is_generated_path(str(child.relative_to(self.path)))
            ]
        return [] if _is_generated_path(name) else [name]


def _is_generated_path(name: str) -> bool:
    path = Path(name)
    return (
        bool(GENERATED_DIR_NAMES.intersection(path.parts))
        or path.name in GENERATED_FILE_NAMES
        or path.suffix in GENERATED_SUFFIXES
    )
