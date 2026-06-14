from __future__ import annotations

import re

from ccr.knowledge.loaders import ReferenceContext, ReferenceDocument
from ccr.schemas.retrieval import RetrievalIdea, RetrievalResult
from ccr.schemas.unit import CodeUnit


def choose_retrieval_ideas(
    unit: CodeUnit, context: ReferenceContext, *, limit: int = 3
) -> RetrievalResult:
    candidates = [*context.guides, *context.examples]
    selected = _rank_documents(unit, candidates)[:limit]
    ideas = [
        RetrievalIdea(
            code_example=_short_example(document.text),
            why=_why_for_unit(unit),
            how=_how_for_unit(unit, document),
        )
        for document in selected
    ]
    if not ideas:
        ideas.append(
            RetrievalIdea(
                code_example=unit.text.splitlines()[0] if unit.text.strip() else unit.name,
                why=_why_for_unit(unit),
                how="Use small, intention-revealing helpers and keep the public behavior stable.",
            )
        )
    return RetrievalResult(unit_id=unit.unit_id, ideas=ideas)


def _rank_documents(unit: CodeUnit, documents: list[ReferenceDocument]) -> list[ReferenceDocument]:
    unit_words = set(_words(unit.name)) | set(_words(unit.text))

    def score(document: ReferenceDocument) -> tuple[int, int]:
        doc_words = set(_words(document.text))
        overlap = len(unit_words & doc_words)
        guide_bonus = 2 if document.purpose == "clean-code guide" else 0
        return overlap + guide_bonus, -len(document.text)

    return sorted(documents, key=score, reverse=True)


def _short_example(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:24])[:2_000]


def _why_for_unit(unit: CodeUnit) -> str:
    if unit.kind == "class":
        return (
            f"`{unit.qualified_name}` is a coherent class-sized unit; clearer helper methods and "
            "named domain branches can make the behavior easier to audit."
        )
    return (
        f"`{unit.qualified_name}` should express one level of abstraction at a time so callers can "
        "understand the behavior without reading every branch."
    )


def _how_for_unit(unit: CodeUnit, document: ReferenceDocument) -> str:
    return (
        f"Use the {document.purpose} at {document.path} as a style reference. Extract named "
        "helpers, prefer direct conditionals over nested branching, and update repository-local "
        "integration points together when cleaner names or structure require it."
    )


def _words(text: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", text)]
