from __future__ import annotations

import json
from pathlib import Path

from ccr import cli
from ccr.extraction.token_budget import ModelLimits
from ccr.schemas.refactor import RefactorIntensity
from ccr.schemas.unit import CodeUnit, UnitKind
from ccr.workflow.run import RefactorRunConfig


def test_refactor_cli_passes_model_and_reasoning_effort(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(
        [
            "refactor",
            str(tmp_path),
            "--model",
            "gpt-5.5",
            "--reasoning-effort",
            "medium",
        ]
    )

    config = captured["config"]
    assert exit_code == 0
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "medium"
    assert config.unit_mode == "code"
    assert config.unit_sort == "value"
    assert not config.fast_mode
    assert config.test_generation_enabled
    assert not config.staged_verification
    assert config.judge_retries == 1
    assert config.refactor_intensity == RefactorIntensity.CONSERVATIVE
    assert config.instructions is None


def test_refactor_cli_fast_mode_sets_optional_speed_preset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(["refactor", str(tmp_path), "--fast"])

    config = captured["config"]
    assert exit_code == 0
    assert config.fast_mode
    assert config.unit_mode == "package"
    assert not config.test_generation_enabled
    assert config.staged_verification
    assert config.refactor_intensity == RefactorIntensity.CONSERVATIVE


def test_refactor_cli_explicit_unit_mode_overrides_fast_preset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(["refactor", str(tmp_path), "--fast", "--unit-mode", "file"])

    assert exit_code == 0
    assert captured["config"].unit_mode == "file"


def test_refactor_cli_structural_mode_uses_cluster_units_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(["refactor", str(tmp_path), "--refactor-intensity", "structural"])

    config = captured["config"]
    assert exit_code == 0
    assert config.refactor_intensity == RefactorIntensity.STRUCTURAL
    assert config.unit_mode == "cluster"
    assert config.target_unit_count == 5


def test_refactor_cli_explicit_unit_mode_overrides_structural_preset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(
        ["refactor", str(tmp_path), "--refactor-intensity", "structural", "--unit-mode", "file"]
    )

    assert exit_code == 0
    assert captured["config"].unit_mode == "file"


def test_refactor_cli_can_preserve_source_order(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(["refactor", str(tmp_path), "--unit-sort", "source"])

    assert exit_code == 0
    assert captured["config"].unit_sort == "source"


def test_refactor_cli_passes_judge_retry_count(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_run_refactor(config: RefactorRunConfig) -> Summary:
        captured["config"] = config
        return Summary()

    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(
        ["refactor", str(tmp_path), "--judge", "--judge-retries", "2"]
    )

    assert exit_code == 0
    assert captured["config"].judge
    assert captured["config"].judge_retries == 2


def test_refactor_cli_print_units_uses_real_refactor_selection_without_running(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, RefactorRunConfig] = {}

    def fake_preview_refactor_units(config: RefactorRunConfig) -> list[CodeUnit]:
        captured["config"] = config
        return [
            CodeUnit(
                unit_id="cluster/01-alpha::cluster",
                kind=UnitKind.CLUSTER,
                name="alpha",
                qualified_name="cluster.alpha",
                path="cluster/01-alpha",
                start_line=1,
                end_line=20,
                start_byte=0,
                end_byte=100,
                text="def alpha():\n    return 1\n",
                sha256="abc",
                owned_paths=["pkg/alpha.py"],
                context_paths=["pkg/context.py"],
                member_paths=["pkg/alpha.py"],
                estimated_tokens=42,
                source_token_budget=1_000,
            )
        ]

    def fake_run_refactor(config: RefactorRunConfig) -> object:
        raise AssertionError("run_refactor should not be called for --print-units")

    monkeypatch.setattr(cli, "preview_refactor_units", fake_preview_refactor_units)
    monkeypatch.setattr(cli, "run_refactor", fake_run_refactor)

    exit_code = cli.main(
        [
            "refactor",
            str(tmp_path),
            "--refactor-intensity",
            "structural",
            "--max-units",
            "1",
            "--print-units",
        ]
    )

    assert exit_code == 0
    assert captured["config"].unit_mode == "cluster"
    assert captured["config"].max_units == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["unit_mode"] == "cluster"
    assert payload["units_total"] == 1
    assert payload["units"][0]["owned_paths"] == ["pkg/alpha.py"]


def test_models_limits_cli_prints_cached_model_limits(monkeypatch, capsys) -> None:
    def fake_model_limits_for_model(model: str) -> tuple[ModelLimits, bool]:
        return (
            ModelLimits(
                model=model,
                context_window_tokens=1_050_000,
                max_output_tokens=128_000,
                source="test-source",
                verified_at="2026-06-14T00:00:00+00:00",
            ),
            False,
        )

    monkeypatch.setattr(cli, "model_limits_for_model", fake_model_limits_for_model)

    exit_code = cli.main(["models", "limits", "--model", "gpt-5.5"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"context_window_tokens": 1050000' in output
    assert '"max_output_tokens": 128000' in output


def test_models_refresh_limits_cli_runs_manual_docs_refresh(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_refresh_model_limits_from_openai_docs(*, models: list[str], output: Path) -> Path:
        captured["models"] = models
        captured["output"] = output
        return output

    monkeypatch.setattr(
        cli,
        "refresh_model_limits_from_openai_docs",
        fake_refresh_model_limits_from_openai_docs,
    )

    output = tmp_path / "limits.json"
    exit_code = cli.main(
        ["models", "refresh-limits", "--model", "gpt-5.5", "--output", str(output)]
    )

    assert exit_code == 0
    assert captured == {"models": ["gpt-5.5"], "output": output}


def test_resume_cli_passes_run_directory(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Path] = {}

    class Summary:
        def model_dump_json(self, *, indent: int) -> str:
            return "{}"

    def fake_resume_refactor(run_dir: Path) -> Summary:
        captured["run_dir"] = run_dir
        return Summary()

    monkeypatch.setattr(cli, "resume_refactor", fake_resume_refactor)

    exit_code = cli.main(["resume", "--run", str(tmp_path / "run")])

    assert exit_code == 0
    assert captured["run_dir"] == tmp_path / "run"
