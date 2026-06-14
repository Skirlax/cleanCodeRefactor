from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pytest

from ccr.extraction import clusters as cluster_module
from ccr.extraction.token_budget import (
    SourceBudget,
    SourceTokenCount,
    TokenCounter,
    context_window_for_model,
    count_source_tokens,
    extract_model_limits_from_document,
    max_output_tokens_for_model,
    source_budget_for_model,
)
from ccr.extraction.units import extract_units

GILDED_ROSE_PYTHON = Path(
    "/home/vvlcek/Code/CodeReferences/TestTargets/Python/GildedRose-Refactoring-Kata/python"
)


def _file_profile(path: str, *, token_count: int = 10) -> cluster_module.FileProfile:
    return cluster_module.FileProfile(
        profile_id=path,
        path=path,
        module=Path(path).with_suffix("").as_posix().replace("/", "."),
        text=f"# {path}\n",
        start_line=1,
        end_line=1,
        token_count=token_count,
        line_count=1,
        imported_modules=frozenset(),
        imported_names=frozenset(),
        defined_symbols=frozenset(),
        referenced_names=frozenset(),
        identifier_tokens=frozenset(),
    )


@pytest.mark.skipif(
    not GILDED_ROSE_PYTHON.exists(),
    reason="Gilded Rose kata is not cloned into CodeReferences.",
)
def test_extracts_preferred_gilded_rose_units() -> None:
    units = extract_units(GILDED_ROSE_PYTHON)

    assert [unit.qualified_name for unit in units] == ["GildedRose", "Item"]
    assert [unit.kind for unit in units] == ["class", "class"]
    assert units[0].path == "gilded_rose.py"


@pytest.mark.skipif(
    not GILDED_ROSE_PYTHON.exists(),
    reason="Gilded Rose kata is not cloned into CodeReferences.",
)
def test_can_include_methods_for_inspection() -> None:
    units = extract_units(GILDED_ROSE_PYTHON, include_methods=True)
    names = {unit.qualified_name for unit in units}

    assert "GildedRose" in names
    assert "GildedRose.update_quality" in names
    assert "Item.__repr__" in names


def test_extracts_file_units_when_requested(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="file")

    assert [unit.unit_id for unit in units] == ["pkg/module.py::<file>"]
    assert units[0].kind == "file"
    assert units[0].qualified_name == "pkg.module"


def test_extracts_package_units_and_falls_back_to_files(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    child_package = package / "child"
    loose = tmp_path / "scripts"
    child_package.mkdir(parents=True)
    loose.mkdir()
    (package / "__init__.py").write_text("PACKAGE = True\n", encoding="utf-8")
    (package / "module.py").write_text("class PackageThing:\n    pass\n", encoding="utf-8")
    (child_package / "__init__.py").write_text("", encoding="utf-8")
    (child_package / "feature.py").write_text(
        "def child_feature():\n    return 1\n",
        encoding="utf-8",
    )
    (loose / "tool.py").write_text("def run():\n    return 2\n", encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="package")

    assert [unit.unit_id for unit in units] == [
        "pkg::package",
        "pkg/child::package",
        "scripts/tool.py::<file>",
    ]
    assert [unit.kind for unit in units] == ["package", "package", "file"]
    assert "# File: pkg/module.py" in units[0].text
    assert units[2].qualified_name == "scripts.tool"


def test_package_mode_falls_back_to_files_without_packages(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="package")

    assert [unit.unit_id for unit in units] == ["module.py::<file>"]
    assert units[0].kind == "file"


def test_extracts_related_cluster_units_near_target_count(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    other = tmp_path / "other"
    package.mkdir()
    other.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(
        "class UserRecord:\n"
        "    def normalized_name(self):\n"
        "        return 'user'\n",
        encoding="utf-8",
    )
    (package / "service.py").write_text(
        "from pkg.models import UserRecord\n\n"
        "def load_user():\n"
        "    return UserRecord().normalized_name()\n",
        encoding="utf-8",
    )
    (other / "report.py").write_text(
        "def render_report():\n"
        "    return 'report'\n",
        encoding="utf-8",
    )

    units = extract_units(tmp_path, unit_mode="cluster", target_unit_count=2, model="gpt-5.5")

    assert [unit.kind for unit in units] == ["cluster", "cluster", "cluster"]
    owned_path_groups = [set(unit.owned_paths) for unit in units]
    assert {"pkg/models.py"} in owned_path_groups
    assert {"pkg/service.py"} in owned_path_groups
    assert {"other/report.py"} in owned_path_groups
    related_unit = next(unit for unit in units if "pkg/service.py" in unit.owned_paths)
    assert related_unit.estimated_tokens is not None
    assert related_unit.source_token_budget is not None
    assert "# Owned files:" in related_unit.text


def test_cluster_mode_packs_small_outliers_without_spending_target_units(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for index in range(8):
        if index == 0:
            source = "def value_0():\n    return 0\n"
        else:
            source = (
                f"from pkg.module_{index - 1} import value_{index - 1}\n\n"
                f"def value_{index}():\n"
                f"    return value_{index - 1}() + 1\n"
            )
        (package / f"module_{index}.py").write_text(source, encoding="utf-8")

    outlier_sources = {
        "tools/report_alpha.py": "def report_alpha():\n    return 'alpha'\n",
        "jobs/publish_beta.py": "def publish_beta():\n    return 'beta'\n",
        "scripts/clean_gamma.py": "def clean_gamma():\n    return 'gamma'\n",
        "extras/export_delta.py": "def export_delta():\n    return 'delta'\n",
    }
    for relative_path, source in outlier_sources.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    units = extract_units(tmp_path, unit_mode="cluster", target_unit_count=5, model="gpt-5.5")

    main_units = [
        unit
        for unit in units
        if any(path.startswith("pkg/") for path in unit.owned_paths)
    ]
    outlier_units = [
        unit
        for unit in units
        if not any(path.startswith("pkg/") for path in unit.owned_paths)
    ]

    assert len(units) == 6
    assert len(main_units) == 5
    assert len(outlier_units) == 1
    assert set(outlier_units[0].owned_paths) == set(outlier_sources)


def test_cluster_merge_balances_dense_core_instead_of_leaving_satellites_singleton() -> None:
    budget = SourceBudget(
        model="tiny-test",
        context_window_tokens=10_000,
        source_tokens=9_000,
        prompt_overhead_tokens=100,
        response_reserve_tokens=100,
        safety_reserve_tokens=100,
        tokenizer="test",
    )

    class WordCounter:
        def count(self, text: str) -> int:
            return len(text.split())

    core_profiles = [
        _file_profile(f"pkg/core_{index}.py", token_count=100)
        for index in range(12)
    ]
    satellite_profiles = [
        _file_profile(f"pkg/satellite_{index}.py", token_count=10)
        for index in range(4)
    ]
    groups = [
        cluster_module.ClusterGroup([profile])
        for profile in [*core_profiles, *satellite_profiles]
    ]
    edges: dict[tuple[str, str], float] = {}
    for left, right in combinations(core_profiles, 2):
        edges[cluster_module._edge_key(left.profile_id, right.profile_id)] = 200.0
    for profile in satellite_profiles:
        edges[cluster_module._edge_key(core_profiles[0].profile_id, profile.profile_id)] = 45.0

    merged = cluster_module._merge_groups(
        groups,
        edges,
        target=5,
        counter=WordCounter(),
        budget=budget,
    )

    assert len(merged) == 5
    assert max(len(group.profiles) for group in merged) <= 5


def test_cluster_refinement_moves_profile_to_clearer_affinity_group() -> None:
    budget = SourceBudget(
        model="tiny-test",
        context_window_tokens=10_000,
        source_tokens=9_000,
        prompt_overhead_tokens=100,
        response_reserve_tokens=100,
        safety_reserve_tokens=100,
        tokenizer="test",
    )

    class WordCounter:
        def count(self, text: str) -> int:
            return len(text.split())

    misplaced = _file_profile("pkg/misplaced.py", token_count=10)
    weak_neighbor = _file_profile("pkg/weak_neighbor.py", token_count=10)
    weak_peer = _file_profile("pkg/weak_peer.py", token_count=10)
    strong_neighbor = _file_profile("pkg/strong_neighbor.py", token_count=10)
    strong_peer = _file_profile("pkg/strong_peer.py", token_count=10)
    groups = [
        cluster_module.ClusterGroup([misplaced, weak_neighbor, weak_peer]),
        cluster_module.ClusterGroup([strong_neighbor, strong_peer]),
    ]
    edges = {
        cluster_module._edge_key(misplaced.profile_id, weak_neighbor.profile_id): 60.0,
        cluster_module._edge_key(misplaced.profile_id, weak_peer.profile_id): 55.0,
        cluster_module._edge_key(misplaced.profile_id, strong_neighbor.profile_id): 130.0,
        cluster_module._edge_key(misplaced.profile_id, strong_peer.profile_id): 120.0,
    }

    refined = cluster_module._refine_group_ownership(
        groups,
        edges,
        counter=WordCounter(),
        budget=budget,
        balance_limits=(3, 100),
    )

    assert [set(group.profile_ids) for group in refined] == [
        {"pkg/weak_neighbor.py", "pkg/weak_peer.py"},
        {"pkg/misplaced.py", "pkg/strong_neighbor.py", "pkg/strong_peer.py"},
    ]


def test_cluster_refinement_does_not_hollow_source_group_to_singleton() -> None:
    budget = SourceBudget(
        model="tiny-test",
        context_window_tokens=10_000,
        source_tokens=9_000,
        prompt_overhead_tokens=100,
        response_reserve_tokens=100,
        safety_reserve_tokens=100,
        tokenizer="test",
    )

    class WordCounter:
        def count(self, text: str) -> int:
            return len(text.split())

    movable = _file_profile("pkg/movable.py", token_count=10)
    weak_neighbor = _file_profile("pkg/weak_neighbor.py", token_count=10)
    strong_neighbor = _file_profile("pkg/strong_neighbor.py", token_count=10)
    strong_peer = _file_profile("pkg/strong_peer.py", token_count=10)
    groups = [
        cluster_module.ClusterGroup([movable, weak_neighbor]),
        cluster_module.ClusterGroup([strong_neighbor, strong_peer]),
    ]
    edges = {
        cluster_module._edge_key(movable.profile_id, weak_neighbor.profile_id): 60.0,
        cluster_module._edge_key(movable.profile_id, strong_neighbor.profile_id): 130.0,
        cluster_module._edge_key(movable.profile_id, strong_peer.profile_id): 120.0,
    }

    refined = cluster_module._refine_group_ownership(
        groups,
        edges,
        counter=WordCounter(),
        budget=budget,
        balance_limits=(3, 100),
    )

    assert [set(group.profile_ids) for group in refined] == [
        {"pkg/movable.py", "pkg/weak_neighbor.py"},
        {"pkg/strong_neighbor.py", "pkg/strong_peer.py"},
    ]


def test_cluster_mode_respects_model_source_budget(monkeypatch, tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "alpha.py").write_text(
        "def alpha():\n"
        "    return '" + ("alpha " * 30) + "'\n",
        encoding="utf-8",
    )
    (package / "beta.py").write_text(
        "from pkg.alpha import alpha\n\n"
        "def beta():\n"
        "    return alpha() + '" + ("beta " * 30) + "'\n",
        encoding="utf-8",
    )

    budget = SourceBudget(
        model="tiny-test",
        context_window_tokens=200,
        source_tokens=100,
        prompt_overhead_tokens=10,
        response_reserve_tokens=10,
        safety_reserve_tokens=10,
        tokenizer="test",
    )

    class WordCounter:
        name = "test"
        approximate = False

        @classmethod
        def for_model(cls, model: str | None) -> WordCounter:
            return cls()

        def count(self, text: str) -> int:
            return len(text.split())

    monkeypatch.setattr("ccr.extraction.clusters.source_budget_for_model", lambda model: budget)
    monkeypatch.setattr("ccr.extraction.clusters.TokenCounter", WordCounter)

    units = extract_units(tmp_path, unit_mode="cluster", target_unit_count=1, model="tiny-test")

    assert len(units) == 2
    assert all(unit.estimated_tokens <= unit.source_token_budget for unit in units)


def test_cluster_mode_splits_group_when_api_validation_exceeds_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (package / "beta.py").write_text(
        "from pkg.alpha import alpha\n\n"
        "def beta():\n"
        "    return alpha()\n",
        encoding="utf-8",
    )

    budget = SourceBudget(
        model="tiny-test",
        context_window_tokens=2_000,
        source_tokens=1_000,
        prompt_overhead_tokens=100,
        response_reserve_tokens=100,
        safety_reserve_tokens=100,
        tokenizer="test",
        api_validation_model="tiny-test",
        api_validation_threshold_tokens=1,
    )

    class WordCounter:
        name = "test"
        approximate = False

        @classmethod
        def for_model(cls, model: str | None) -> WordCounter:
            return cls()

        def count(self, text: str) -> int:
            return len(text.split())

    def fake_count_source_tokens(
        text: str,
        *,
        counter: WordCounter,
        budget: SourceBudget,
        api_client: object | None = None,
    ) -> SourceTokenCount:
        owned_file_count = text.count("# Owned file:")
        tokens = budget.source_tokens + 1 if owned_file_count > 1 else counter.count(text)
        return SourceTokenCount(tokens=tokens, local_tokens=counter.count(text), used_api=True)

    monkeypatch.setattr("ccr.extraction.clusters.source_budget_for_model", lambda model: budget)
    monkeypatch.setattr("ccr.extraction.clusters.TokenCounter", WordCounter)
    monkeypatch.setattr("ccr.extraction.clusters.count_source_tokens", fake_count_source_tokens)

    units = extract_units(tmp_path, unit_mode="cluster", target_unit_count=1, model="tiny-test")

    assert len(units) == 2
    assert all(len(unit.owned_paths) == 1 for unit in units)
    assert all(unit.estimated_tokens <= unit.source_token_budget for unit in units)


def test_cluster_mode_splits_instead_of_dropping_most_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    budget = SourceBudget(
        model="tiny-test",
        context_window_tokens=2_000,
        source_tokens=1_000,
        prompt_overhead_tokens=100,
        response_reserve_tokens=100,
        safety_reserve_tokens=100,
        tokenizer="test",
        api_validation_model="tiny-test",
        api_validation_threshold_tokens=1,
    )

    class WordCounter:
        name = "test"
        approximate = False

        def count(self, text: str) -> int:
            return len(text.split())

    def fake_count_source_tokens(
        text: str,
        *,
        counter: WordCounter,
        budget: SourceBudget,
        api_client: object | None = None,
    ) -> SourceTokenCount:
        would_drop_context_from_merged_owned_cluster = (
            "# Context file:" in text and text.count("# Owned file:") > 1
        )
        tokens = (
            budget.source_tokens + 1
            if would_drop_context_from_merged_owned_cluster
            else counter.count(text)
        )
        return SourceTokenCount(tokens=tokens, local_tokens=counter.count(text), used_api=True)

    monkeypatch.setattr("ccr.extraction.clusters.count_source_tokens", fake_count_source_tokens)

    alpha = _file_profile("pkg/alpha.py", token_count=100)
    beta = _file_profile("pkg/beta.py", token_count=100)
    context = _file_profile("pkg/context.py", token_count=500)
    group = cluster_module.ClusterGroup([alpha, beta], context_profiles=[context])

    fitted_groups, split_occurred = cluster_module._fit_group_to_source_budget(
        tmp_path,
        group,
        counter=WordCounter(),
        budget=budget,
    )

    assert split_occurred
    assert [fitted.profile_ids for fitted in fitted_groups] == [
        {"pkg/alpha.py"},
        {"pkg/beta.py"},
    ]


def test_context_window_accepts_gpt_model_alias_without_hyphen() -> None:
    assert context_window_for_model("gpt5.5") == (1_050_000, False)
    assert max_output_tokens_for_model("gpt5.5") == (128_000, False)


def test_gpt55_source_budget_reserves_full_max_output_tokens() -> None:
    budget = source_budget_for_model("gpt-5.5")

    assert budget.context_window_tokens == 1_050_000
    assert budget.max_output_tokens == 128_000
    assert budget.prompt_overhead_tokens == 64_000
    assert budget.response_reserve_tokens == 128_000
    assert budget.safety_reserve_tokens == 84_000
    assert budget.source_tokens == 774_000
    assert budget.api_validation_threshold_tokens == 696_600


def test_extracts_model_limits_from_openai_model_document() -> None:
    limits = extract_model_limits_from_document(
        model="gpt-5.5",
        source_url="https://developers.openai.com/api/docs/models/gpt-5.5",
        document="""
        <h1>GPT-5.5</h1>
        <p>1,050,000 context window</p>
        <p>128,000 max output tokens</p>
        """,
    )

    assert limits.context_window_tokens == 1_050_000
    assert limits.max_output_tokens == 128_000
    assert not limits.approximate


def test_openai_token_count_api_is_only_used_near_source_budget() -> None:
    budget = SourceBudget(
        model="gpt-5.5",
        context_window_tokens=1_000,
        source_tokens=100,
        prompt_overhead_tokens=10,
        response_reserve_tokens=10,
        safety_reserve_tokens=10,
        tokenizer="test",
        max_output_tokens=100,
        api_validation_model="gpt-5.5",
        api_validation_threshold_tokens=90,
    )

    class FixedCounter(TokenCounter):
        def __init__(self, tokens: int) -> None:
            self.tokens = tokens
            self.name = "test"
            self.approximate = False

        def count(self, text: str) -> int:
            return self.tokens

    class FakeApiClient:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        def count_input_tokens(self, *, model: str, text: str) -> int:
            self.calls += 1
            assert model == "gpt-5.5"
            assert text == "unit text"
            return 95

    api_client = FakeApiClient()

    far = count_source_tokens(
        "unit text",
        counter=FixedCounter(80),
        budget=budget,
        api_client=api_client,
    )
    near = count_source_tokens(
        "unit text",
        counter=FixedCounter(90),
        budget=budget,
        api_client=api_client,
    )

    assert far.tokens == 80
    assert not far.used_api
    assert near.tokens == 95
    assert near.used_api
    assert api_client.calls == 1
