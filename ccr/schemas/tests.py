from __future__ import annotations

from pydantic import BaseModel, Field


class TestRecommendation(BaseModel):
    name: str = Field(min_length=1)
    behavior: str = Field(min_length=1)
    suggested_location: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class TestAssessment(BaseModel):
    unit_id: str
    adequate: bool
    reason: str = Field(min_length=1)
    recommendations: list[TestRecommendation] = Field(default_factory=list)


class TestWriteResult(BaseModel):
    unit_id: str
    changed_files: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
