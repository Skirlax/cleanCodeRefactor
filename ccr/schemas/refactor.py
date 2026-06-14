from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RefactorIntensity(StrEnum):
    CONSERVATIVE = "conservative"
    STRUCTURAL = "structural"


class RefactorOutcome(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


class RenameRecord(BaseModel):
    old_name: str
    new_name: str
    kind: str
    location: str = ""


class MovedLogicRecord(BaseModel):
    from_symbol: str
    to_symbol: str
    reason: str


class IntegrationUpdate(BaseModel):
    path: str
    description: str


class RefactorResult(BaseModel):
    unit_id: str
    outcome: RefactorOutcome
    changed_files: list[str] = Field(default_factory=list)
    message: str
    assumptions: list[str] = Field(default_factory=list)
    renames: list[RenameRecord] = Field(default_factory=list)
    signature_changes: list[str] = Field(default_factory=list)
    moved_logic: list[MovedLogicRecord] = Field(default_factory=list)
    integration_points_updated: list[IntegrationUpdate] = Field(default_factory=list)
    behavior_changes: list[str] = Field(default_factory=list)
