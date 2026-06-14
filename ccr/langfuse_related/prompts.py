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
    REFACTOR_INSTRUCTIONS_CONSERVATIVE = "ccr-refactor-instructions-conservative"
    REFACTOR_INSTRUCTIONS_STRUCTURAL = "ccr-refactor-instructions-structural"
    JUDGE = "ccr-judge"
    SUMMARIZE = "ccr-summarize"
    TEST_AUDIT = "ccr-test-audit"
    TEST_WRITE = "ccr-test-write"


REQUIRED_PROMPTS = tuple(PromptName)
DEFAULT_PROMPT_BACKUP_FILE = Path(__file__).with_name("prompt_backups.yaml")


class LangfusePromptError(RuntimeError):
    pass


class PromptSeedEntry(BaseModel):
    name: str
    prompt: str | list[dict[str, str]]
    type: str = "text"
    labels: list[str] = Field(default_factory=lambda: ["production"])
    config: dict[str, object] = Field(default_factory=dict)
    commit_message: str | None = None


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


def seed_prompts_from_file(path: Path, *, client: Any | None = None) -> list[str]:
    payload = _load_prompt_seed_payload(path)
    entries = payload["prompts"] if isinstance(payload, dict) else payload
    langfuse = client or _get_langfuse_client()
    seeded: list[str] = []
    for raw_entry in entries:
        entry = PromptSeedEntry.model_validate(raw_entry)
        kwargs: dict[str, object] = {
            "name": entry.name,
            "type": entry.type,
            "prompt": entry.prompt,
            "labels": entry.labels,
        }
        if entry.config:
            kwargs["config"] = entry.config
        if entry.commit_message:
            kwargs["commit_message"] = entry.commit_message
        langfuse.create_prompt(**kwargs)
        seeded.append(entry.name)
    return seeded


def sync_prompt_backups_to_langfuse(
    path: Path = DEFAULT_PROMPT_BACKUP_FILE,
    *,
    client: Any | None = None,
) -> list[str]:
    return seed_prompts_from_file(path, client=client)


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


def _load_prompt_seed_payload(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        try:
            return _load_prompt_backup_yaml_without_pyyaml(text)
        except Exception as fallback_exc:
            msg = (
                "PyYAML is required to read this prompt backup YAML file. "
                f"Current interpreter: {sys.executable}."
            )
            raise LangfusePromptError(msg) from fallback_exc
    payload = yaml.safe_load(text)
    if payload is None:
        msg = f"Prompt backup file is empty: {path}"
        raise LangfusePromptError(msg)
    return payload


def _load_prompt_backup_yaml_without_pyyaml(text: str) -> dict[str, list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("  - name: "):
            index += 1
            continue

        entry: dict[str, object] = {"name": _scalar_value(line.removeprefix("  - name: "))}
        index += 1
        while index < len(lines) and not lines[index].startswith("  - name: "):
            current = lines[index]
            if not current.strip():
                index += 1
                continue
            if current.startswith("    type: "):
                entry["type"] = _scalar_value(current.removeprefix("    type: "))
                index += 1
                continue
            if current.startswith("    commit_message: "):
                entry["commit_message"] = _scalar_value(
                    current.removeprefix("    commit_message: ")
                )
                index += 1
                continue
            if current == "    labels:":
                values, index = _read_simple_yaml_list(lines, index + 1)
                entry["labels"] = values
                continue
            if current == "    config:":
                config, index = _read_simple_yaml_mapping(lines, index + 1)
                entry["config"] = config
                continue
            if current == "    prompt: |":
                prompt, index = _read_yaml_block(lines, index + 1)
                entry["prompt"] = prompt
                continue
            index += 1
        entries.append(entry)

    if not entries:
        msg = "Prompt backup YAML did not contain any prompts."
        raise ValueError(msg)
    return {"prompts": entries}


def _read_simple_yaml_list(lines: list[str], index: int) -> tuple[list[str], int]:
    values: list[str] = []
    while index < len(lines) and lines[index].startswith("      - "):
        values.append(_scalar_value(lines[index].removeprefix("      - ")))
        index += 1
    return values, index


def _read_simple_yaml_mapping(lines: list[str], index: int) -> tuple[dict[str, object], int]:
    values: dict[str, object] = {}
    while index < len(lines) and lines[index].startswith("      "):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            break
        values[key] = _scalar_value(value.strip())
        index += 1
    return values, index


def _read_yaml_block(lines: list[str], index: int) -> tuple[str, int]:
    block: list[str] = []
    while index < len(lines) and not lines[index].startswith("  - name: "):
        line = lines[index]
        block.append(line[6:] if line.startswith("      ") else line)
        index += 1
    return "\n".join(block).rstrip() + "\n", index


def _scalar_value(value: str) -> object:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


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
