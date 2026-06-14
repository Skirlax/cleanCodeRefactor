from __future__ import annotations

from pydantic import BaseModel, Field

from ccr.schemas.refactor import IntegrationUpdate, MovedLogicRecord, RenameRecord


class AcceptedRefactor(BaseModel):
    unit: str
    files_changed: list[str] = Field(default_factory=list)
    renames: list[RenameRecord] = Field(default_factory=list)
    signature_changes: list[str] = Field(default_factory=list)
    moved_logic: list[MovedLogicRecord] = Field(default_factory=list)
    integration_points: list[str] = Field(default_factory=list)
    integration_points_updated: list[IntegrationUpdate] = Field(default_factory=list)
    constraints_to_preserve: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    behavior_changes: list[str] = Field(default_factory=list)


class CumulativeSummary(BaseModel):
    accepted_refactors: list[AcceptedRefactor] = Field(default_factory=list)


class RunSummary(BaseModel):
    run_id: str
    original_path: str
    copied_workspace: str
    applied_changes: list[str] = Field(default_factory=list)
    skipped_changes: list[str] = Field(default_factory=list)
    examples_used: list[str] = Field(default_factory=list)
    verification_results: list[str] = Field(default_factory=list)
    apply_command: str
    warning: str
