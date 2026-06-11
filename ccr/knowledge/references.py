from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

DEFAULT_REFERENCES_ROOT = Path("/home/vvlcek/Code/CodeReferences")


class ReferenceRecord(BaseModel):
    name: str
    source_url: str
    commit_hash: str | None = None
    license: str | None = None
    language: str
    purpose: str
    local_path: str


@dataclass(frozen=True)
class ReferenceSpec:
    name: str
    source_url: str
    relative_path: str
    language: str
    purpose: str
    clone: bool = True


REFERENCE_SPECS = [
    ReferenceSpec(
        name="clean-code-python",
        source_url="local:/home/vvlcek/Code/CodeReferences/Python/clean-code-python.md",
        relative_path="Python/clean-code-python.md",
        language="python",
        purpose="clean-code guide",
        clone=False,
    ),
    ReferenceSpec(
        name="pydantic-ai",
        source_url="https://github.com/pydantic/pydantic-ai.git",
        relative_path="Python/pydantic-ai",
        language="python",
        purpose="modern Python example codebase",
    ),
]

TEST_TARGET_SPECS = [
    ReferenceSpec(
        name="GildedRose-Refactoring-Kata",
        source_url="https://github.com/emilybache/GildedRose-Refactoring-Kata.git",
        relative_path="TestTargets/Python/GildedRose-Refactoring-Kata",
        language="python",
        purpose="dirty refactoring kata test target",
    )
]


def sync_references(
    *,
    languages: list[str],
    references_root: Path = DEFAULT_REFERENCES_ROOT,
    include_test_targets: bool = True,
) -> list[ReferenceRecord]:
    languages = [language.lower() for language in languages]
    specs = [spec for spec in REFERENCE_SPECS if spec.language in languages]
    if include_test_targets:
        specs.extend(spec for spec in TEST_TARGET_SPECS if spec.language in languages)

    records: list[ReferenceRecord] = []
    for spec in specs:
        path = references_root / spec.relative_path
        if spec.clone and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", spec.source_url, str(path)],
                check=True,
                text=True,
            )
        records.append(_record_for_spec(spec, path))

    manifest_path = references_root / "references.manifest.json"
    manifest_path.write_text(
        json.dumps([record.model_dump() for record in records], indent=2) + "\n",
        encoding="utf-8",
    )
    return records


def _record_for_spec(spec: ReferenceSpec, path: Path) -> ReferenceRecord:
    commit_hash = _git_output(path, "rev-parse", "HEAD") if path.is_dir() else None
    license_text = _detect_license(path)
    return ReferenceRecord(
        name=spec.name,
        source_url=spec.source_url,
        commit_hash=commit_hash,
        license=license_text,
        language=spec.language,
        purpose=spec.purpose,
        local_path=str(path),
    )


def _git_output(path: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _detect_license(path: Path) -> str | None:
    if path.is_file():
        return None
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        license_path = path / name
        if license_path.exists():
            first_line = license_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            return first_line[0][:120] if first_line else name
    return None
