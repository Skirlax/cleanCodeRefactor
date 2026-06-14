from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from ccr.schemas.refactor import IntegrationUpdate, MovedLogicRecord, RenameRecord


class LedgerEntry(BaseModel):
    unit_id: str
    outcome: str
    changed_files: list[str] = Field(default_factory=list)
    member_paths: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)
    context_paths: list[str] = Field(default_factory=list)
    examples_used: list[str] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    renames: list[RenameRecord] = Field(default_factory=list)
    signature_changes: list[str] = Field(default_factory=list)
    moved_logic: list[MovedLogicRecord] = Field(default_factory=list)
    integration_points_updated: list[IntegrationUpdate] = Field(default_factory=list)
    behavior_changes: list[str] = Field(default_factory=list)
    commit: str | None = None
    message: str = ""


class RunLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: LedgerEntry) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.model_dump(), sort_keys=True) + "\n")

    def read(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        return [
            LedgerEntry.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
