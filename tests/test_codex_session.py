from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

import ccr.codex.session as session_module
from ccr.codex.session import CodexCliProvider
from ccr.schemas.tests import TestAssessment as AssessmentSchema


class CodexResult(BaseModel):
    value: str


def test_run_codex_uses_current_exec_arguments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = _capture_codex_run(monkeypatch)

    result = CodexCliProvider(codex_binary="codex-test")._run_codex(
        name="unit-test",
        prompt="hello",
        workspace=tmp_path,
        schema_model=CodexResult,
        sandbox="read-only",
    )

    command = captured["command"]
    assert result == CodexResult(value="ok")
    assert command[:4] == [
        "codex-test",
        "exec",
        "-c",
        "approval_policy=never",
    ]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--cd") + 1] == str(tmp_path)
    assert command[-1] == "-"
    assert captured["input"] == "hello"


def test_default_codex_binary_resolves_to_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = _capture_codex_run(monkeypatch)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda executable: "/usr/local/bin/codex" if executable == "codex" else None,
    )

    CodexCliProvider()._run_codex(
        name="unit-test",
        prompt="hello",
        workspace=tmp_path,
        schema_model=CodexResult,
        sandbox="read-only",
    )

    command = captured["command"]
    assert command[0] == "/usr/local/bin/codex"
    assert command[command.index("-c") + 1] == "approval_policy=never"


def test_run_codex_applies_model_and_reasoning_effort_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = _capture_codex_run(monkeypatch)

    CodexCliProvider(
        model="gpt-5.5",
        reasoning_effort="medium",
        codex_binary="codex-test",
    )._run_codex(
        name="unit-test",
        prompt="hello",
        workspace=tmp_path,
        schema_model=CodexResult,
        sandbox="read-only",
    )

    command = captured["command"]
    assert command[:4] == ["codex-test", "exec", "--model", "gpt-5.5"]
    assert "-c" in command
    assert "model_reasoning_effort=\"medium\"" in command
    assert "approval_policy=never" in command


def test_run_codex_writes_openai_strict_output_schema(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = _capture_codex_run(
        monkeypatch,
        output=(
            '{"unit_id": "unit", "adequate": false, '
            '"reason": "missing tests", "recommendations": []}'
        ),
    )

    result = CodexCliProvider(codex_binary="codex-test")._run_codex(
        name="test_audit",
        prompt="hello",
        workspace=tmp_path,
        schema_model=AssessmentSchema,
        sandbox="read-only",
    )

    schema = captured["schema"]
    recommendation = schema["$defs"]["TestRecommendation"]
    assert result.recommendations == []
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["unit_id", "adequate", "reason", "recommendations"]
    assert recommendation["additionalProperties"] is False
    assert recommendation["required"] == ["name", "behavior", "suggested_location", "reason"]
    assert "minLength" not in schema["properties"]["reason"]
    assert "title" not in schema


def test_run_codex_logs_call_locally_and_warns_when_langfuse_is_unavailable(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    _disable_langfuse(monkeypatch)
    captured = _capture_codex_run(monkeypatch, stub_langfuse=False)
    run_dir = tmp_path / "run"
    workspace = tmp_path / "workspace"

    result = CodexCliProvider(codex_binary="codex-test", run_dir=run_dir)._run_codex(
        name="test_audit",
        prompt="hello",
        workspace=workspace,
        schema_model=CodexResult,
        sandbox="read-only",
    )

    log_path = run_dir / "codex-calls.jsonl"
    record = json.loads(log_path.read_text(encoding="utf-8"))
    stderr = capsys.readouterr().err
    assert result == CodexResult(value="ok")
    assert record["name"] == "test_audit"
    assert record["prompt"] == "hello"
    assert record["parsed_output"] == {"value": "ok"}
    assert record["command"] == captured["command"]
    assert "Langfuse tracing unavailable" in stderr
    assert str(log_path) in stderr


def _capture_codex_run(
    monkeypatch,
    *,
    output: str = '{"value": "ok"}',
    stub_langfuse: bool = True,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = input
        captured["text"] = text
        captured["capture_output"] = capture_output
        captured["check"] = check
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(output, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if stub_langfuse:
        monkeypatch.setattr(
            session_module,
            "_write_langfuse_codex_call",
            lambda record, run_dir: None,
        )
    return captured


def _disable_langfuse(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(session_module, "_LANGFUSE_WARNING_PRINTED", False)
