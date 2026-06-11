from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class RunState(BaseModel):
    run_id: str
    original_path: str
    copied_workspace: str
    language: str
    provider: str
    model: str | None = None
    reasoning_effort: str | None = None
    baseline_commit: str
    status: str = "running"
    units_total: int = 0
    units_done: int = 0
    current_head: str | None = None
    current_unit_id: str | None = None
    current_stage: str | None = None
    error: str | None = None
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, run_dir: Path) -> RunState:
        return cls.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))

    def save(self, run_dir: Path) -> None:
        (run_dir / "state.json").write_text(
            json.dumps(self.model_dump(), indent=2) + "\n",
            encoding="utf-8",
        )
