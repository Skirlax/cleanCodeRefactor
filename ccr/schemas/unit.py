from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class UnitKind(StrEnum):
    CLASS = "class"
    CLUSTER = "cluster"
    FILE = "file"
    FUNCTION = "function"
    METHOD = "method"
    PACKAGE = "package"


class CodeUnit(BaseModel):
    unit_id: str
    language: str = "python"
    kind: UnitKind
    name: str
    qualified_name: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    text: str
    sha256: str
    member_paths: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)
    context_paths: list[str] = Field(default_factory=list)
    estimated_tokens: int | None = None
    source_token_budget: int | None = None
    model_context_window_tokens: int | None = None
    model_max_output_tokens: int | None = None
    response_reserve_tokens: int | None = None
    budget_notes: list[str] = Field(default_factory=list)

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"
