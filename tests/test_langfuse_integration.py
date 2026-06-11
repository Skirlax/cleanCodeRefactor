from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ccr.langfuse_related.prompts import (
    LangfusePromptStore,
    PromptName,
    check_required_prompts,
)
from ccr.langfuse_related.sync import (
    SCHEMA_MODELS,
    build_schema_bundle,
    sync_schemas_to_langfuse,
)


class FakePrompt:
    def __init__(self, value: str) -> None:
        self.value = value

    def compile(self) -> str:
        return self.value


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.created_prompts: list[dict[str, object]] = []

    def get_prompt(self, name: str, **kwargs: str) -> FakePrompt:
        self.requests.append((name, kwargs))
        return FakePrompt(f"{name} from langfuse")

    def create_prompt(self, **kwargs: object) -> None:
        self.created_prompts.append(kwargs)


def test_prompt_store_fetches_prompt_from_langfuse_client() -> None:
    client = FakeLangfuseClient()
    store = LangfusePromptStore(client=client)

    assert store.get_text(PromptName.RETRIEVAL) == "ccr-retrieval from langfuse"
    assert client.requests == [("ccr-retrieval", {"type": "text", "label": "production"})]


def test_required_prompt_check_uses_langfuse_prompt_store() -> None:
    statuses = check_required_prompts(LangfusePromptStore(client=FakeLangfuseClient()))

    assert statuses == {
        "ccr-retrieval": "ok",
        "ccr-refactor": "ok",
        "ccr-judge": "ok",
        "ccr-summarize": "ok",
        "ccr-test-audit": "ok",
        "ccr-test-write": "ok",
    }


def test_schema_bundle_does_not_include_prompts() -> None:
    bundle = build_schema_bundle()

    assert "schemas" in bundle
    assert "prompts" not in bundle
    assert not (Path(__file__).resolve().parents[1] / "ccr" / "prompts").exists()


def test_schema_sync_creates_langfuse_inspection_artifacts() -> None:
    client = FakeLangfuseClient()

    artifacts = sync_schemas_to_langfuse(client=client)

    assert "ccr-json-schema-bundle" in artifacts
    assert len(client.created_prompts) == len(SCHEMA_MODELS) + 1
    assert all(prompt["type"] == "text" for prompt in client.created_prompts)
    assert all(prompt["labels"] == ["schema-inspection"] for prompt in client.created_prompts)
    assert all(prompt["config"]["source_of_truth"] == "code" for prompt in client.created_prompts)


def test_internal_langfuse_related_package_does_not_shadow_langfuse_sdk() -> None:
    repo = Path(__file__).resolve().parents[1]
    script = f"""
import sys
sys.path.insert(0, {str(repo / "ccr")!r})
import langfuse
from langfuse import get_client
assert "site-packages/langfuse" in langfuse.__file__, langfuse.__file__
print(get_client.__name__)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "get_client"
