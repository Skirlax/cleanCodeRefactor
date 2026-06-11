from __future__ import annotations

from pydantic import BaseModel, Field


class RetrievalIdea(BaseModel):
    code_example: str = Field(min_length=1)
    why: str = Field(min_length=1)
    how: str = Field(min_length=1)


class RetrievalResult(BaseModel):
    unit_id: str
    ideas: list[RetrievalIdea] = Field(default_factory=list)
