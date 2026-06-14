from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ccr.knowledge.loaders import ReferenceContext
from ccr.langfuse_related.prompts import LangfusePromptStore, PromptName
from ccr.schemas.judge import JudgeResult
from ccr.schemas.refactor import RefactorIntensity, RefactorResult
from ccr.schemas.retrieval import RetrievalResult
from ccr.schemas.summary import CumulativeSummary
from ccr.schemas.tests import TestAssessment, TestWriteResult
from ccr.schemas.unit import CodeUnit

SchemaModel = TypeVar("SchemaModel", bound=BaseModel)
_LANGFUSE_WARNING_PRINTED = False


class CodexCliProvider:
    name = "codex"

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        codex_binary: str = "codex",
        run_dir: Path | None = None,
        prompt_store: LangfusePromptStore | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.codex_binary = _resolve_executable(codex_binary)
        self.run_dir = run_dir
        self.prompt_store = prompt_store or LangfusePromptStore()

    def retrieve(
        self, *, unit: CodeUnit, references: ReferenceContext, workspace: Path
    ) -> RetrievalResult:
        prompt = "\n\n".join(
            [
                self.prompt_store.get_text(PromptName.RETRIEVAL),
                "## Current Unit",
                _unit_json_for_prompt(unit),
                "## Clean-Code Guides",
                _documents_json(references.guides),
                "## Example Codebase Files",
                _documents_json(references.examples),
            ]
        )
        return self._run_codex(
            name="retrieval",
            prompt=prompt,
            workspace=workspace,
            schema_model=RetrievalResult,
            sandbox="read-only",
        )

    def refactor(
        self,
        *,
        unit: CodeUnit,
        retrieval: RetrievalResult,
        summary: CumulativeSummary,
        workspace: Path,
        instructions: str | None,
        refactor_intensity: RefactorIntensity,
    ) -> RefactorResult:
        effective_instructions = instructions or self.prompt_store.get_text(
            _refactor_instruction_prompt_name(refactor_intensity)
        )
        prompt = "\n\n".join(
            [
                self.prompt_store.get_text(PromptName.REFACTOR),
                "## Current Unit",
                _unit_json_for_prompt(unit),
                "## Retrieval JSON",
                retrieval.model_dump_json(indent=2),
                "## Cumulative Refactor Summary",
                summary.model_dump_json(indent=2),
                "## Refactor Intensity",
                refactor_intensity.value,
                "## Refactor Instructions",
                effective_instructions,
            ]
        )
        return self._run_codex(
            name="refactor",
            prompt=prompt,
            workspace=workspace,
            schema_model=RefactorResult,
            sandbox="workspace-write",
        )

    def judge(
        self,
        *,
        unit: CodeUnit,
        diff: str,
        refactor_result: RefactorResult,
        summary: CumulativeSummary,
        workspace: Path,
        refactor_intensity: RefactorIntensity,
    ) -> JudgeResult:
        prompt = "\n\n".join(
            [
                self.prompt_store.get_text(PromptName.JUDGE),
                "## Unit",
                _unit_json_for_prompt(unit),
                "## Refactor Intensity",
                refactor_intensity.value,
                "## Refactor Result",
                refactor_result.model_dump_json(indent=2),
                "## Diff",
                diff,
                "## Cumulative Refactor Summary",
                summary.model_dump_json(indent=2),
            ]
        )
        return self._run_codex(
            name="judge",
            prompt=prompt,
            workspace=workspace,
            schema_model=JudgeResult,
            sandbox="read-only",
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
        prompt = "\n\n".join(
            [
                self.prompt_store.get_text(PromptName.TEST_AUDIT),
                "## Current Unit",
                _unit_json_for_prompt(unit),
                "## Cumulative Refactor Summary",
                summary.model_dump_json(indent=2),
                "## Verification Commands",
                json.dumps(verification_commands, indent=2),
                "## Characterization Commands",
                json.dumps(characterization_commands, indent=2),
                "## Existing Test Files",
                _test_files_json(workspace),
            ]
        )
        return self._run_codex(
            name="test_audit",
            prompt=prompt,
            workspace=workspace,
            schema_model=TestAssessment,
            sandbox="read-only",
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
        prompt = "\n\n".join(
            [
                self.prompt_store.get_text(PromptName.TEST_WRITE),
                "## Current Unit",
                _unit_json_for_prompt(unit),
                "## Test Assessment",
                assessment.model_dump_json(indent=2),
                "## Cumulative Refactor Summary",
                summary.model_dump_json(indent=2),
                "## Verification Commands",
                json.dumps(verification_commands, indent=2),
                "## Characterization Commands",
                json.dumps(characterization_commands, indent=2),
                "## Existing Test Files",
                _test_files_json(workspace),
            ]
        )
        return self._run_codex(
            name="test_write",
            prompt=prompt,
            workspace=workspace,
            schema_model=TestWriteResult,
            sandbox="workspace-write",
        )

    def _run_codex(
        self,
        *,
        name: str,
        prompt: str,
        workspace: Path,
        schema_model: type[SchemaModel],
        sandbox: str,
    ) -> SchemaModel:
        started_at = _utc_now()
        monotonic_start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="ccr-codex-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            output_path = tmp_path / "last-message.json"
            output_schema = _codex_output_schema(schema_model)
            schema_path.write_text(
                json.dumps(output_schema, indent=2),
                encoding="utf-8",
            )
            command = [self.codex_binary, "exec"]
            if self.model:
                command.extend(["--model", self.model])
            if self.reasoning_effort:
                command.extend(
                    [
                        "-c",
                        f"model_reasoning_effort={json.dumps(self.reasoning_effort)}",
                    ]
                )
            command.extend(
                [
                    "-c",
                    "approval_policy=never",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--sandbox",
                    sandbox,
                    "--cd",
                    str(workspace),
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
            )
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                self._record_codex_call(
                    name=name,
                    started_at=started_at,
                    duration_seconds=time.monotonic() - monotonic_start,
                    workspace=workspace,
                    schema_model=schema_model,
                    sandbox=sandbox,
                    command=command,
                    prompt=prompt,
                    output_schema=output_schema,
                    completed=completed,
                    raw_output=None,
                    parsed_output=None,
                    error="Codex CLI failed.",
                )
                msg = (
                    f"Codex CLI failed with exit code {completed.returncode}.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )
                raise RuntimeError(msg)
            raw_output = output_path.read_text(encoding="utf-8")
        try:
            parsed = schema_model.model_validate_json(raw_output)
        except Exception as exc:
            self._record_codex_call(
                name=name,
                started_at=started_at,
                duration_seconds=time.monotonic() - monotonic_start,
                workspace=workspace,
                schema_model=schema_model,
                sandbox=sandbox,
                command=command,
                prompt=prompt,
                output_schema=output_schema,
                completed=completed,
                raw_output=raw_output,
                parsed_output=None,
                error=str(exc),
            )
            raise
        self._record_codex_call(
            name=name,
            started_at=started_at,
            duration_seconds=time.monotonic() - monotonic_start,
            workspace=workspace,
            schema_model=schema_model,
            sandbox=sandbox,
            command=command,
            prompt=prompt,
            output_schema=output_schema,
            completed=completed,
            raw_output=raw_output,
            parsed_output=parsed.model_dump(mode="json"),
            error=None,
        )
        return parsed

    def _record_codex_call(
        self,
        *,
        name: str,
        started_at: str,
        duration_seconds: float,
        workspace: Path,
        schema_model: type[BaseModel],
        sandbox: str,
        command: list[str],
        prompt: str,
        output_schema: dict[str, object],
        completed: subprocess.CompletedProcess[str],
        raw_output: str | None,
        parsed_output: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        record: dict[str, Any] = {
            "name": name,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "duration_seconds": round(duration_seconds, 3),
            "provider": self.name,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "workspace": str(workspace),
            "schema_model": schema_model.__name__,
            "sandbox": sandbox,
            "command": command,
            "prompt": prompt,
            "output_schema": output_schema,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "raw_output": raw_output,
            "parsed_output": parsed_output,
            "error": error,
        }
        _write_langfuse_codex_call(record, self.run_dir)
        _write_local_codex_call(self.run_dir, record)


def _refactor_instruction_prompt_name(refactor_intensity: RefactorIntensity) -> PromptName:
    if refactor_intensity == RefactorIntensity.STRUCTURAL:
        return PromptName.REFACTOR_INSTRUCTIONS_STRUCTURAL
    return PromptName.REFACTOR_INSTRUCTIONS_CONSERVATIVE


def _unit_json_for_prompt(unit: CodeUnit) -> str:
    payload = unit.model_dump(mode="json")
    member_paths = payload.get("member_paths") or []
    owned_paths = payload.get("owned_paths") or []
    if not member_paths or member_paths == owned_paths:
        payload.pop("member_paths", None)
        return json.dumps(payload, indent=2)

    return "\n".join(
        [
            (
                "Member paths identify the exact source files or source regions that define "
                "this unit; they may be more granular than the editable file paths."
            ),
            json.dumps(payload, indent=2),
        ]
    )


def _documents_json(documents: list[object]) -> str:
    return json.dumps(
        [
            {
                "path": document.path,
                "purpose": document.purpose,
                "text": document.text,
            }
            for document in documents
        ],
        indent=2,
    )


def _resolve_executable(executable: str) -> str:
    path = Path(executable)
    if path.parent != Path("."):
        return executable
    return shutil.which(executable) or executable


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _codex_output_schema(schema_model: type[BaseModel]) -> dict[str, object]:
    schema = schema_model.model_json_schema()
    _make_openai_schema_strict(schema)
    return schema


def _write_local_codex_call(run_dir: Path | None, record: dict[str, Any]) -> None:
    if run_dir is None:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "codex-calls.jsonl"
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_langfuse_codex_call(record: dict[str, Any], run_dir: Path | None) -> None:
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        _warn_langfuse_unavailable(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not both set.",
            run_dir,
        )
        return
    try:
        from langfuse import get_client

        langfuse = get_client()
        generation = langfuse.start_observation(
            name=f"ccr-codex-{record['name']}",
            as_type="generation",
            input=record["prompt"],
            output=record["parsed_output"] or record["raw_output"],
            model=record["model"] or "codex-cli-config-default",
            metadata={
                "provider": record["provider"],
                "reasoning_effort": record["reasoning_effort"],
                "workspace": record["workspace"],
                "schema_model": record["schema_model"],
                "sandbox": record["sandbox"],
                "command": record["command"],
                "returncode": record["returncode"],
                "stdout": record["stdout"],
                "stderr": record["stderr"],
                "output_schema": record["output_schema"],
                "duration_seconds": record["duration_seconds"],
            },
            level="ERROR" if record["error"] else "DEFAULT",
            status_message=record["error"],
        )
        generation.update_trace(
            name="ccr codex run",
            session_id=_run_session_id(record["workspace"]),
            tags=["ccr", "codex-cli"],
        )
        record["langfuse_trace_id"] = generation.trace_id
        record["langfuse_observation_id"] = generation.id
        generation.end()
        langfuse.flush()
    except Exception as exc:
        record["langfuse_error"] = str(exc)
        _warn_langfuse_unavailable(str(exc), run_dir)


def _warn_langfuse_unavailable(reason: str, run_dir: Path | None) -> None:
    global _LANGFUSE_WARNING_PRINTED
    if _LANGFUSE_WARNING_PRINTED:
        return
    _LANGFUSE_WARNING_PRINTED = True
    local_log = (
        f" Codex calls are still logged locally to {run_dir / 'codex-calls.jsonl'}."
        if run_dir is not None
        else " Local Codex call logging is disabled because no run directory was provided."
    )
    print(f"ccr: warning: Langfuse tracing unavailable: {reason}{local_log}", file=sys.stderr)


def _run_session_id(workspace: str) -> str:
    path = Path(workspace)
    return path.parent.name if path.name == "workspace" else path.name


def _make_openai_schema_strict(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _make_openai_schema_strict(item)
        return

    if not isinstance(value, dict):
        return

    for keyword in (
        "default",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "title",
        "uniqueItems",
    ):
        value.pop(keyword, None)

    properties = value.get("properties")
    if isinstance(properties, dict):
        value["additionalProperties"] = False
        value["required"] = list(properties)

    for item in value.values():
        _make_openai_schema_strict(item)


def _test_files_json(workspace: Path) -> str:
    test_files: list[str] = []
    for pattern in ("tests/**/*.py", "test_*.py", "**/test_*.py", "**/*_test.py"):
        test_files.extend(
            str(path.relative_to(workspace))
            for path in workspace.glob(pattern)
            if path.is_file() and ".venv" not in path.parts
        )
    return json.dumps(sorted(set(test_files)), indent=2)
