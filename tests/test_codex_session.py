from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

import ccr.codex.session as session_module
from ccr.codex.session import CodexCliProvider, _unit_json_for_prompt
from ccr.langfuse_related.prompts import PromptName
from ccr.schemas.judge import JudgeResult
from ccr.schemas.refactor import RefactorIntensity, RefactorOutcome, RefactorResult
from ccr.schemas.retrieval import RetrievalResult
from ccr.schemas.summary import CumulativeSummary
from ccr.schemas.tests import TestAssessment as AssessmentSchema
from ccr.schemas.unit import CodeUnit, UnitKind


class CodexResult(BaseModel):
    value: str


def _unit(
    *,
    member_paths: list[str] | None = None,
    owned_paths: list[str] | None = None,
) -> CodeUnit:
    return CodeUnit(
        unit_id="sample.py::Widget",
        kind=UnitKind.CLASS,
        name="Widget",
        qualified_name="Widget",
        path="sample.py",
        start_line=1,
        end_line=2,
        start_byte=0,
        end_byte=20,
        text="class Widget:\n    pass\n",
        sha256="abc",
        member_paths=member_paths or [],
        owned_paths=owned_paths or [],
    )


def test_unit_prompt_json_omits_duplicate_member_paths() -> None:
    unit = _unit(
        member_paths=["pkg/service.py"],
        owned_paths=["pkg/service.py"],
    )

    prompt_json = _unit_json_for_prompt(unit)
    payload = json.loads(prompt_json)

    assert "member_paths" not in payload
    assert payload["owned_paths"] == ["pkg/service.py"]
    assert "Member paths identify" not in prompt_json


def test_unit_prompt_json_explains_distinct_member_paths() -> None:
    unit = _unit(
        member_paths=["pkg/large.py::Service", "pkg/large.py::helper"],
        owned_paths=["pkg/large.py"],
    )

    prompt_json = _unit_json_for_prompt(unit)
    _, json_text = prompt_json.split("\n", 1)
    payload = json.loads(json_text)

    assert "Member paths identify the exact source files or source regions" in prompt_json
    assert payload["member_paths"] == ["pkg/large.py::Service", "pkg/large.py::helper"]
    assert payload["owned_paths"] == ["pkg/large.py"]


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


def test_refactor_fetches_langfuse_instructions_for_selected_intensity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    prompt_requests: list[PromptName] = []

    class FakePromptStore:
        def get_text(self, name: PromptName) -> str:
            prompt_requests.append(name)
            return f"{name.value} text"

    def fake_run_codex(self, *, name, prompt, workspace, schema_model, sandbox):
        captured["prompt"] = prompt
        captured["schema_model"] = schema_model
        captured["sandbox"] = sandbox
        return schema_model(
            unit_id="sample.py::Widget",
            outcome=RefactorOutcome.CHANGED,
            changed_files=["sample.py"],
            message="changed",
        )

    monkeypatch.setattr(CodexCliProvider, "_run_codex", fake_run_codex)

    result = CodexCliProvider(prompt_store=FakePromptStore()).refactor(
        unit=CodeUnit(
            unit_id="sample.py::Widget",
            kind=UnitKind.CLASS,
            name="Widget",
            qualified_name="Widget",
            path="sample.py",
            start_line=1,
            end_line=2,
            start_byte=0,
            end_byte=20,
            text="class Widget:\n    pass\n",
            sha256="abc",
        ),
        retrieval=RetrievalResult(unit_id="sample.py::Widget"),
        summary=CumulativeSummary(),
        workspace=tmp_path,
        instructions=None,
        refactor_intensity=RefactorIntensity.STRUCTURAL,
    )

    assert result.outcome == RefactorOutcome.CHANGED
    assert prompt_requests == [
        PromptName.REFACTOR_INSTRUCTIONS_STRUCTURAL,
        PromptName.REFACTOR,
    ]
    assert captured["sandbox"] == "workspace-write"
    assert "## Refactor Intensity\n\nstructural" in str(captured["prompt"])
    assert "ccr-refactor-instructions-structural text" in str(captured["prompt"])


def test_judge_prompt_includes_declared_behavior_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakePromptStore:
        def get_text(self, name: PromptName) -> str:
            assert name == PromptName.JUDGE
            return "judge policy text"

    def fake_run_codex(self, *, name, prompt, workspace, schema_model, sandbox):
        captured["name"] = name
        captured["prompt"] = prompt
        captured["schema_model"] = schema_model
        captured["sandbox"] = sandbox
        return JudgeResult(unit_id="sample.py::Widget", accepted=True, summary="ok")

    monkeypatch.setattr(CodexCliProvider, "_run_codex", fake_run_codex)

    result = CodexCliProvider(prompt_store=FakePromptStore()).judge(
        unit=CodeUnit(
            unit_id="sample.py::Widget",
            kind=UnitKind.CLASS,
            name="Widget",
            qualified_name="Widget",
            path="sample.py",
            start_line=1,
            end_line=2,
            start_byte=0,
            end_byte=20,
            text="class Widget:\n    pass\n",
            sha256="abc",
        ),
        diff="diff --git a/sample.py b/sample.py",
        refactor_result=RefactorResult(
            unit_id="sample.py::Widget",
            outcome=RefactorOutcome.CHANGED,
            changed_files=["sample.py"],
            message="changed",
            behavior_changes=["Fixes legacy empty-input handling."],
        ),
        summary=CumulativeSummary(),
        workspace=tmp_path,
        refactor_intensity=RefactorIntensity.STRUCTURAL,
    )

    assert result.accepted
    assert captured["name"] == "judge"
    assert captured["schema_model"] is JudgeResult
    assert captured["sandbox"] == "read-only"
    prompt = str(captured["prompt"])
    assert "## Refactor Intensity\n\nstructural" in prompt
    assert "## Refactor Result" in prompt
    assert "Fixes legacy empty-input handling." in prompt


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
