from __future__ import annotations

from pydantic import BaseModel, Field


class JudgeResult(BaseModel):
    unit_id: str
    accepted: bool
    issues: list[str] = Field(default_factory=list)
    summary: str
