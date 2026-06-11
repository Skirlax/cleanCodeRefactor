from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class UnitKind(StrEnum):
    CLASS = "class"
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

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"
