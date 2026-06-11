from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

from ccr.knowledge.loaders import ReferenceContext
from ccr.knowledge.retrieval import choose_retrieval_ideas
from ccr.schemas.judge import JudgeResult
from ccr.schemas.refactor import RefactorOutcome, RefactorResult
from ccr.schemas.retrieval import RetrievalResult
from ccr.schemas.summary import CumulativeSummary
from ccr.schemas.tests import TestAssessment, TestRecommendation, TestWriteResult
from ccr.schemas.unit import CodeUnit


class ModelProvider(Protocol):
    name: str

    def retrieve(
        self, *, unit: CodeUnit, references: ReferenceContext, workspace: Path
    ) -> RetrievalResult: ...

    def refactor(
        self,
        *,
        unit: CodeUnit,
        retrieval: RetrievalResult,
        summary: CumulativeSummary,
        workspace: Path,
        instructions: str,
    ) -> RefactorResult: ...

    def judge(
        self,
        *,
        unit: CodeUnit,
        diff: str,
        summary: CumulativeSummary,
        workspace: Path,
    ) -> JudgeResult: ...

    def assess_tests(
        self,
        *,
        unit: CodeUnit,
        summary: CumulativeSummary,
        workspace: Path,
        verification_commands: list[str],
        characterization_commands: list[str],
    ) -> TestAssessment: ...

    def write_tests(
        self,
        *,
        unit: CodeUnit,
        assessment: TestAssessment,
        summary: CumulativeSummary,
        workspace: Path,
        verification_commands: list[str],
        characterization_commands: list[str],
    ) -> TestWriteResult: ...


def build_provider(
    name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    run_dir: Path | None = None,
) -> ModelProvider:
    normalized = name.lower()
    if normalized == "codex":
        from ccr.codex.session import CodexCliProvider

        return CodexCliProvider(
            model=model,
            reasoning_effort=reasoning_effort,
            run_dir=run_dir,
        )
    if normalized == "heuristic":
        return HeuristicProvider()
    if normalized == "openai":
        raise NotImplementedError(
            "OpenAI API-key support is reserved behind the provider interface but is not part of "
            "this MVP implementation yet. Use provider='codex' or provider='heuristic'."
        )
    msg = f"Unknown provider {name!r}. Expected codex, heuristic, or openai."
    raise ValueError(msg)


class HeuristicProvider:
    name = "heuristic"

    def retrieve(
        self, *, unit: CodeUnit, references: ReferenceContext, workspace: Path
    ) -> RetrievalResult:
        return choose_retrieval_ideas(unit, references)

    def refactor(
        self,
        *,
        unit: CodeUnit,
        retrieval: RetrievalResult,
        summary: CumulativeSummary,
        workspace: Path,
        instructions: str,
    ) -> RefactorResult:
        replacement = _known_python_replacement(unit)
        if replacement is None:
            return RefactorResult(
                unit_id=unit.unit_id,
                outcome=RefactorOutcome.UNCHANGED,
                changed_files=[],
                message="No deterministic heuristic refactor is known for this unit.",
            )

        file_path = workspace / unit.path
        source = file_path.read_text(encoding="utf-8")
        updated = _replace_unit(source, unit, replacement)
        if updated == source:
            return RefactorResult(
                unit_id=unit.unit_id,
                outcome=RefactorOutcome.UNCHANGED,
                changed_files=[],
                message="Known heuristic replacement matched the current source exactly.",
            )
        file_path.write_text(updated, encoding="utf-8")
        return RefactorResult(
            unit_id=unit.unit_id,
            outcome=RefactorOutcome.CHANGED,
            changed_files=[unit.path],
            message="Applied deterministic clean-code refactor for known kata unit.",
        )

    def judge(
        self,
        *,
        unit: CodeUnit,
        diff: str,
        summary: CumulativeSummary,
        workspace: Path,
    ) -> JudgeResult:
        return JudgeResult(
            unit_id=unit.unit_id,
            accepted=bool(diff.strip()),
            issues=[] if diff.strip() else ["No diff was produced for the unit."],
            summary="Heuristic judge accepts non-empty diffs after verification passes.",
        )

    def assess_tests(
        self,
        *,
        unit: CodeUnit,
        summary: CumulativeSummary,
        workspace: Path,
        verification_commands: list[str],
        characterization_commands: list[str],
    ) -> TestAssessment:
        if (
            unit.language == "python"
            and unit.name in {"GildedRose", "Item"}
            and (workspace / "tests" / "test_gilded_rose_behavior.py").exists()
        ):
            return TestAssessment(
                unit_id=unit.unit_id,
                adequate=True,
                reason="The copied workspace contains targeted Gilded Rose baseline tests.",
            )
        if unit.language == "python" and unit.name in {"GildedRose", "Item"}:
            return TestAssessment(
                unit_id=unit.unit_id,
                adequate=False,
                reason="The kata has no pytest coverage for item behavior before refactoring.",
                recommendations=[
                    TestRecommendation(
                        name="test_gilded_rose_baseline_behavior",
                        behavior=(
                            "Normal items degrade, aged brie improves, backstage passes change "
                            "by sell-in window, sulfuras remains unchanged, and quality remains "
                            "bounded."
                        ),
                        suggested_location="tests/test_gilded_rose_behavior.py",
                        reason="These are the core observable Gilded Rose rules.",
                    )
                ],
            )
        return TestAssessment(
            unit_id=unit.unit_id,
            adequate=bool(characterization_commands),
            reason=(
                "Existing characterization commands provide behavior coverage."
                if characterization_commands
                else "No deterministic heuristic test generation is known for this unit."
            ),
        )

    def write_tests(
        self,
        *,
        unit: CodeUnit,
        assessment: TestAssessment,
        summary: CumulativeSummary,
        workspace: Path,
        verification_commands: list[str],
        characterization_commands: list[str],
    ) -> TestWriteResult:
        if unit.language != "python" or unit.name != "GildedRose":
            return TestWriteResult(
                unit_id=unit.unit_id,
                changed_files=[],
                test_commands=[],
                message="No deterministic heuristic tests are known for this unit.",
            )

        test_path = workspace / "tests" / "test_gilded_rose_behavior.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(_GILDED_ROSE_BEHAVIOR_TESTS, encoding="utf-8")
        return TestWriteResult(
            unit_id=unit.unit_id,
            changed_files=[str(test_path.relative_to(workspace))],
            test_commands=[f"{sys.executable} -m pytest tests/test_gilded_rose_behavior.py"],
            message="Added deterministic baseline pytest coverage for Gilded Rose behavior.",
        )


def _replace_unit(source: str, unit: CodeUnit, replacement: str) -> str:
    source_bytes = source.encode("utf-8")
    replacement_bytes = replacement.encode("utf-8")
    updated = source_bytes[: unit.start_byte] + replacement_bytes + source_bytes[unit.end_byte :]
    return updated.decode("utf-8")


def _known_python_replacement(unit: CodeUnit) -> str | None:
    if unit.language != "python":
        return None
    if unit.name == "GildedRose" and "Backstage passes to a TAFKAL80ETC concert" in unit.text:
        return _GILDED_ROSE_CLASS
    if unit.name == "Item" and '"%s, %s, %s"' in unit.text:
        return _ITEM_CLASS
    return None


_GILDED_ROSE_CLASS = """class GildedRose(object):
    AGED_BRIE = "Aged Brie"
    BACKSTAGE_PASSES = "Backstage passes to a TAFKAL80ETC concert"
    SULFURAS = "Sulfuras, Hand of Ragnaros"
    MAX_QUALITY = 50
    MIN_QUALITY = 0

    def __init__(self, items):
        self.items = items

    def update_quality(self):
        for item in self.items:
            if item.name == self.SULFURAS:
                continue

            self._update_item_quality(item)
            item.sell_in -= 1
            if item.sell_in < 0:
                self._update_expired_item_quality(item)

    def _update_item_quality(self, item):
        if item.name == self.AGED_BRIE:
            self._increase_quality(item)
        elif item.name == self.BACKSTAGE_PASSES:
            self._update_backstage_pass_quality(item)
        else:
            self._decrease_quality(item)

    def _update_expired_item_quality(self, item):
        if item.name == self.AGED_BRIE:
            self._increase_quality(item)
        elif item.name == self.BACKSTAGE_PASSES:
            item.quality = self.MIN_QUALITY
        else:
            self._decrease_quality(item)

    def _update_backstage_pass_quality(self, item):
        self._increase_quality(item)
        if item.sell_in < 11:
            self._increase_quality(item)
        if item.sell_in < 6:
            self._increase_quality(item)

    def _increase_quality(self, item):
        if item.quality < self.MAX_QUALITY:
            item.quality += 1

    def _decrease_quality(self, item):
        if item.quality > self.MIN_QUALITY:
            item.quality -= 1"""


_ITEM_CLASS = '''class Item:
    def __init__(self, name, sell_in, quality):
        self.name = name
        self.sell_in = sell_in
        self.quality = quality

    def __repr__(self):
        return f"{self.name}, {self.sell_in}, {self.quality}"'''


_GILDED_ROSE_BEHAVIOR_TESTS = """from gilded_rose import GildedRose, Item


def update_item(name, sell_in, quality):
    item = Item(name, sell_in, quality)
    GildedRose([item]).update_quality()
    return item


def test_normal_item_degrades_before_sell_date():
    item = update_item("Dexterity Vest", 10, 20)

    assert item.sell_in == 9
    assert item.quality == 19


def test_normal_item_degrades_twice_as_fast_after_sell_date():
    item = update_item("Dexterity Vest", 0, 20)

    assert item.sell_in == -1
    assert item.quality == 18


def test_quality_never_goes_negative():
    item = update_item("Dexterity Vest", 10, 0)

    assert item.quality == 0


def test_aged_brie_increases_in_quality():
    item = update_item("Aged Brie", 2, 0)

    assert item.sell_in == 1
    assert item.quality == 1


def test_quality_never_exceeds_fifty():
    item = update_item("Aged Brie", 2, 50)

    assert item.quality == 50


def test_backstage_passes_increase_by_sell_in_window():
    assert update_item("Backstage passes to a TAFKAL80ETC concert", 15, 20).quality == 21
    assert update_item("Backstage passes to a TAFKAL80ETC concert", 10, 20).quality == 22
    assert update_item("Backstage passes to a TAFKAL80ETC concert", 5, 20).quality == 23


def test_backstage_passes_drop_to_zero_after_concert():
    item = update_item("Backstage passes to a TAFKAL80ETC concert", 0, 20)

    assert item.sell_in == -1
    assert item.quality == 0


def test_sulfuras_never_changes():
    item = update_item("Sulfuras, Hand of Ragnaros", 0, 80)

    assert item.sell_in == 0
    assert item.quality == 80
"""
