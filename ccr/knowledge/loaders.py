from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ccr.extraction.units import DEFAULT_EXCLUDED_DIRS
from ccr.knowledge.references import DEFAULT_REFERENCES_ROOT


@dataclass(frozen=True)
class ReferenceDocument:
    path: str
    text: str
    purpose: str


@dataclass(frozen=True)
class ReferenceContext:
    guides: list[ReferenceDocument]
    examples: list[ReferenceDocument]


def load_reference_context(
    *,
    language: str,
    references_root: Path = DEFAULT_REFERENCES_ROOT,
    max_guide_chars: int = 12_000,
    max_example_chars: int = 24_000,
) -> ReferenceContext:
    language = language.lower()
    if language != "python":
        return ReferenceContext(guides=[], examples=[])

    language_root = references_root / "Python"
    guides = _load_guides(language_root, max_chars=max_guide_chars)
    examples = _load_python_examples(language_root, max_chars=max_example_chars)
    return ReferenceContext(guides=guides, examples=examples)


def _load_guides(language_root: Path, *, max_chars: int) -> list[ReferenceDocument]:
    guide_path = language_root / "clean-code-python.md"
    if not guide_path.exists():
        return []
    text = guide_path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    return [ReferenceDocument(path=str(guide_path), text=text, purpose="clean-code guide")]


def _load_python_examples(language_root: Path, *, max_chars: int) -> list[ReferenceDocument]:
    example_roots = [language_root / "pydantic-ai"]
    documents: list[ReferenceDocument] = []
    remaining = max_chars
    for root in example_roots:
        if remaining <= 0 or not root.exists():
            break
        for path in sorted(root.rglob("*.py")):
            if remaining <= 0:
                break
            if any(part in DEFAULT_EXCLUDED_DIRS for part in path.parts):
                continue
            if "tests" in path.parts or "docs" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            snippet = text[: min(len(text), 4_000, remaining)]
            remaining -= len(snippet)
            documents.append(
                ReferenceDocument(
                    path=str(path),
                    text=snippet,
                    purpose="example codebase",
                )
            )
    return documents
