from __future__ import annotations

from pathlib import Path

from ccr.debug import build_debug_config


def test_debug_config_keeps_arguments_in_code() -> None:
    config = build_debug_config()

    assert isinstance(config.project, Path)
    assert config.provider == "codex"
    assert config.max_units is None
    assert config.run_root == Path("/home/vvlcek/Documents/ccr/debug-runs")
    assert config.test_generation_enabled is True
    assert config.judge is True
