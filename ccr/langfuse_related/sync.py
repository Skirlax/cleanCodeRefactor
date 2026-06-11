from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

from ccr.schemas.judge import JudgeResult
from ccr.schemas.refactor import RefactorResult
from ccr.schemas.retrieval import RetrievalResult
from ccr.schemas.summary import CumulativeSummary, RunSummary
from ccr.schemas.tests import TestAssessment, TestWriteResult
from ccr.schemas.unit import CodeUnit

SCHEMA_MODELS = {
    "unit": CodeUnit,
    "retrieval": RetrievalResult,
    "refactor": RefactorResult,
    "summary": CumulativeSummary,
    "run_summary": RunSummary,
    "judge": JudgeResult,
    "test_assessment": TestAssessment,
    "test_write": TestWriteResult,
}

SCHEMA_ARTIFACT_PREFIX = "ccr-json-schema-"
SCHEMA_BUNDLE_ARTIFACT_NAME = "ccr-json-schema-bundle"
SCHEMA_ARTIFACT_LABELS = ["schema-inspection"]


def build_schema_bundle() -> dict[str, object]:
    return {
        "schemas": {name: model.model_json_schema() for name, model in SCHEMA_MODELS.items()},
        "note": (
            "JSON schemas are synced to Langfuse-facing inspection artifacts only. The source of "
            "truth for schemas is the Pydantic models in code. Prompts are not stored in code; "
            "they are fetched from Langfuse at runtime."
        ),
    }


def write_schema_bundle(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_schema_bundle(), indent=2) + "\n", encoding="utf-8")
    return output


def sync_schemas_to_langfuse(client: Any | None = None) -> list[str]:
    langfuse = client or _get_langfuse_client()
    synced: list[str] = []
    for name, model in SCHEMA_MODELS.items():
        artifact_name = f"{SCHEMA_ARTIFACT_PREFIX}{name}"
        langfuse.create_prompt(
            name=artifact_name,
            type="text",
            prompt=json.dumps(model.model_json_schema(), indent=2),
            labels=SCHEMA_ARTIFACT_LABELS,
            config={
                "source_of_truth": "code",
                "schema_model": model.__name__,
                "runtime_prompt": False,
            },
            commit_message="Sync JSON schema inspection artifact from code",
        )
        synced.append(artifact_name)

    langfuse.create_prompt(
        name=SCHEMA_BUNDLE_ARTIFACT_NAME,
        type="text",
        prompt=json.dumps(build_schema_bundle(), indent=2),
        labels=SCHEMA_ARTIFACT_LABELS,
        config={
            "source_of_truth": "code",
            "runtime_prompt": False,
        },
        commit_message="Sync JSON schema inspection bundle from code",
    )
    synced.append(SCHEMA_BUNDLE_ARTIFACT_NAME)
    return synced


def diff_schema_bundle(snapshot: Path) -> str:
    current = (json.dumps(build_schema_bundle(), indent=2) + "\n").splitlines(keepends=True)
    if snapshot.exists():
        previous = snapshot.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        previous = []
    return "".join(
        difflib.unified_diff(
            previous,
            current,
            fromfile=str(snapshot),
            tofile="current schema bundle",
        )
    )


def _get_langfuse_client() -> Any:
    try:
        from langfuse import get_client
    except ImportError as exc:  # pragma: no cover - dependency check
        msg = (
            "The langfuse package is required for schema inspection sync. "
            f"Current interpreter: {sys.executable}. "
            "Use the project .venv interpreter or install dependencies with "
            "`.venv/bin/python -m pip install -e '.[dev]'`."
        )
        raise RuntimeError(msg) from exc
    return get_client()
