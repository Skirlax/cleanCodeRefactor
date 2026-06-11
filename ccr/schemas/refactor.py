from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RefactorOutcome(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


class RefactorResult(BaseModel):
    unit_id: str
    outcome: RefactorOutcome
    changed_files: list[str] = Field(default_factory=list)
    message: str
    assumptions: list[str] = Field(default_factory=list)
