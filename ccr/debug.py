from __future__ import annotations

import json
from pathlib import Path

from ccr.env import load_environment_files
from ccr.knowledge.references import DEFAULT_REFERENCES_ROOT
from ccr.schemas.refactor import RefactorIntensity
from ccr.schemas.summary import RunSummary
from ccr.workflow.run import RefactorRunConfig, run_refactor


def build_debug_config() -> RefactorRunConfig:
    project = Path("/home/vvlcek/Code/PycharmProjects/MuAlphaZeroBuild")
    language = "python"
    provider = "codex"
    model = "gpt-5.5"
    reasoning_effort = "xhigh"
    run_root = Path("/home/vvlcek/Documents/ccr/debug-runs")
    references_root = DEFAULT_REFERENCES_ROOT
    max_units = None
    include_methods = False
    verification_commands: list[str] = []
    characterization_commands = []
    test_generation_enabled = True
    judge = True
    refactor_intensity = RefactorIntensity.STRUCTURAL

    return RefactorRunConfig(
        project=project,
        language=language,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        run_root=run_root,
        references_root=references_root,
        max_units=max_units,
        include_methods=include_methods,
        verification_commands=verification_commands,
        characterization_commands=characterization_commands,
        test_generation_enabled=test_generation_enabled,
        judge=judge,
        refactor_intensity=refactor_intensity,
    )


def main() -> RunSummary:
    langfuse_env_file = Path("/tmp/ccr-langfuse-selfhost/.env.ccr-local")
    load_langfuse_env_file = True

    if load_langfuse_env_file:
        load_environment_files(extra_files=(langfuse_env_file,))

    config = build_debug_config()
    if not config.project.exists():
        msg = (
            f"Debug target does not exist: {config.project}. "
            "Run `ccr references sync --languages python` first or edit `project` in "
            "`build_debug_config()`."
        )
        raise FileNotFoundError(msg)

    summary = run_refactor(config)
    run_dir = Path(summary.copied_workspace).parent
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "dashboard": str(run_dir / "dashboard.html"),
                "copied_workspace": summary.copied_workspace,
                "apply_command": summary.apply_command,
                "provider": config.provider,
                "model": config.model,
                "reasoning_effort": config.reasoning_effort,
                "test_generation_enabled": config.test_generation_enabled,
            },
            indent=2,
        )
    )
    return summary


if __name__ == "__main__":
    main()
