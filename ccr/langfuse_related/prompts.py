from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PromptName(StrEnum):
    RETRIEVAL = "ccr-retrieval"
    REFACTOR = "ccr-refactor"
    JUDGE = "ccr-judge"
    SUMMARIZE = "ccr-summarize"
    TEST_AUDIT = "ccr-test-audit"
    TEST_WRITE = "ccr-test-write"


REQUIRED_PROMPTS = tuple(PromptName)


class LangfusePromptError(RuntimeError):
    pass


class PromptSeedEntry(BaseModel):
    name: str
    prompt: str | list[dict[str, str]]
    type: str = "text"
    labels: list[str] = Field(default_factory=lambda: ["production"])


class LangfusePromptStore:
    def __init__(
        self,
        *,
        client: Any | None = None,
        label: str = "production",
        prompt_type: str = "text",
    ) -> None:
        self._client = client
        self.label = label
        self.prompt_type = prompt_type

    def get_text(self, name: PromptName | str) -> str:
        prompt_name = name.value if isinstance(name, PromptName) else name
        client = self._client or _get_langfuse_client()
        prompt = self._get_prompt(client, prompt_name)
        compiled = prompt.compile()
        if not isinstance(compiled, str):
            msg = (
                f"Langfuse prompt {prompt_name!r} compiled to {type(compiled).__name__}; "
                "CCR expects a text prompt."
            )
            raise LangfusePromptError(msg)
        return compiled

    def _get_prompt(self, client: Any, prompt_name: str) -> Any:
        kwargs: dict[str, str] = {"type": self.prompt_type}
        if self.label:
            kwargs["label"] = self.label
        try:
            return client.get_prompt(prompt_name, **kwargs)
        except TypeError:
            kwargs.pop("label", None)
            return client.get_prompt(prompt_name, **kwargs)
        except Exception as exc:
            msg = (
                f"Could not fetch Langfuse prompt {prompt_name!r}. Create it in Langfuse and "
                "label the intended version as production, or configure LANGFUSE_* credentials "
                "for the right project."
            )
            raise LangfusePromptError(msg) from exc


def seed_prompts_from_file(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload["prompts"] if isinstance(payload, dict) else payload
    client = _get_langfuse_client()
    seeded: list[str] = []
    for raw_entry in entries:
        entry = PromptSeedEntry.model_validate(raw_entry)
        client.create_prompt(
            name=entry.name,
            type=entry.type,
            prompt=entry.prompt,
            labels=entry.labels,
        )
        seeded.append(entry.name)
    return seeded


def check_required_prompts(store: LangfusePromptStore | None = None) -> dict[str, str]:
    store = store or LangfusePromptStore()
    statuses: dict[str, str] = {}
    for prompt_name in REQUIRED_PROMPTS:
        try:
            store.get_text(prompt_name)
        except Exception as exc:
            statuses[prompt_name.value] = f"missing: {exc}"
        else:
            statuses[prompt_name.value] = "ok"
    return statuses


def _get_langfuse_client() -> Any:
    try:
        from langfuse import get_client
    except ImportError as exc:
        msg = (
            "The langfuse package is required for prompt management. "
            f"Current interpreter: {sys.executable}. "
            "Use the project .venv interpreter or install dependencies with "
            "`.venv/bin/python -m pip install -e '.[dev]'`."
        )
        raise LangfusePromptError(msg) from exc
    return get_client()
