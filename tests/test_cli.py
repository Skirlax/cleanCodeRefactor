from __future__ import annotations

from pathlib import Path

from ccr import cli
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
