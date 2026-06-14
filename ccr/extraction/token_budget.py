from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
DEFAULT_MODEL_LIMITS_PATH = Path(__file__).with_name("model_limits.json")
MODEL_LIMITS_FILE_ENV = "CCR_MODEL_LIMITS_FILE"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_BASE_URL_ENV = "OPENAI_BASE_URL"
OPENAI_TOKEN_COUNT_TIMEOUT_SECONDS = 20
API_VALIDATION_THRESHOLD_FRACTION = 0.9
OPENAI_DOCS_MODEL_URL = "https://developers.openai.com/api/docs/models/{model}"
DEFAULT_REFRESH_MODELS = ("gpt-5.5",)

_PLACEHOLDER_API_KEYS = {
    "",
    "replace-me",
    "replace-with-your-openai-api-key",
    "your-openai-api-key",
    "sk-...",
}

_FALLBACK_MODEL_LIMITS = {
    "gpt-5.5": (1_050_000, 128_000),
    "gpt-5": (400_000, 128_000),
    "gpt-4.1": (1_000_000, 32_768),
    "gpt-4.1-mini": (1_000_000, 32_768),
    "gpt-4.1-nano": (1_000_000, 32_768),
    "gpt-4o": (128_000, 16_384),
    "gpt-4o-mini": (128_000, 16_384),
    "o3": (200_000, 100_000),
    "o3-mini": (200_000, 100_000),
    "o4-mini": (200_000, 100_000),
}


@dataclass(frozen=True)
class ModelLimits:
    model: str
    context_window_tokens: int
    max_output_tokens: int
    source: str
    verified_at: str | None = None
    approximate: bool = False


@dataclass(frozen=True)
class SourceBudget:
    model: str | None
    context_window_tokens: int
    source_tokens: int
    prompt_overhead_tokens: int
    response_reserve_tokens: int
    safety_reserve_tokens: int
    tokenizer: str
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    model_limits_source: str = "fallback"
    model_limits_verified_at: str | None = None
    approximate_tokenizer: bool = False
    approximate_context_window: bool = False
    approximate_max_output: bool = False
    api_validation_model: str | None = None
    api_validation_threshold_tokens: int | None = None

    @property
    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.approximate_context_window:
            notes.append(
                "Model context window is not verified in the bundled OpenAI model limits; "
                "using a conservative fallback."
            )
        if self.approximate_max_output:
            notes.append(
                "Model max output tokens are not verified in the bundled OpenAI model limits; "
                "using a fallback reserve."
            )
        if self.approximate_tokenizer:
            notes.append("tiktoken was unavailable; using an approximate token estimator.")
        return notes


@dataclass(frozen=True)
class SourceTokenCount:
    tokens: int
    local_tokens: int
    used_api: bool = False
    notes: tuple[str, ...] = ()


class TokenCounter:
    def __init__(self, *, model: str | None, encoding: object | None, name: str) -> None:
        self.model = model
        self._encoding = encoding
        self.name = name
        self.approximate = encoding is None

    @classmethod
    @lru_cache(maxsize=32)
    def for_model(cls, model: str | None) -> TokenCounter:
        try:
            import tiktoken
        except ImportError:
            return cls(model=model, encoding=None, name="approximate")

        normalized = _normalized_model_name(model)
        if normalized:
            try:
                encoding = tiktoken.encoding_for_model(normalized)
            except KeyError:
                try:
                    encoding = tiktoken.get_encoding(_fallback_encoding_name(normalized))
                except Exception:
                    return cls(model=model, encoding=None, name="approximate")
            except Exception:
                return cls(model=model, encoding=None, name="approximate")
        else:
            try:
                encoding = tiktoken.get_encoding("o200k_base")
            except Exception:
                return cls(model=model, encoding=None, name="approximate")
        return cls(model=model, encoding=encoding, name=getattr(encoding, "name", "tiktoken"))

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is None:
            return max(1, (len(text.encode("utf-8")) + 3) // 4)
        return len(self._encoding.encode(text))


class OpenAIInputTokenCounter:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = OPENAI_TOKEN_COUNT_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(OPENAI_API_KEY_ENV, "")
        self.base_url = (base_url or os.environ.get(OPENAI_BASE_URL_ENV) or "https://api.openai.com/v1").rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return _usable_openai_api_key(self.api_key)

    def count_input_tokens(self, *, model: str, text: str) -> int:
        if not self.available:
            raise RuntimeError("OPENAI_API_KEY is not configured for token-count validation.")
        payload = json.dumps({"model": model, "input": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses/input_tokens",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            msg = f"OpenAI input-token API returned HTTP {exc.code}: {detail}"
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI input-token API request failed: {exc.reason}") from exc

        parsed = json.loads(body)
        input_tokens = parsed.get("input_tokens")
        if not isinstance(input_tokens, int):
            raise RuntimeError("OpenAI input-token API response did not include input_tokens.")
        return input_tokens


def source_budget_for_model(model: str | None) -> SourceBudget:
    counter = TokenCounter.for_model(model)
    limits, approximate_limits = model_limits_for_model(model)
    prompt_overhead = min(max(int(limits.context_window_tokens * 0.08), 12_000), 64_000)
    response_reserve = _response_reserve_for_limits(limits)
    safety_reserve = min(max(int(limits.context_window_tokens * 0.08), 8_000), 96_000)
    source_tokens = max(
        8_000,
        limits.context_window_tokens - prompt_overhead - response_reserve - safety_reserve,
    )
    api_validation_model = None if approximate_limits or not model else limits.model
    return SourceBudget(
        model=model,
        context_window_tokens=limits.context_window_tokens,
        source_tokens=source_tokens,
        prompt_overhead_tokens=prompt_overhead,
        response_reserve_tokens=response_reserve,
        safety_reserve_tokens=safety_reserve,
        tokenizer=counter.name,
        max_output_tokens=limits.max_output_tokens,
        model_limits_source=limits.source,
        model_limits_verified_at=limits.verified_at,
        approximate_tokenizer=counter.approximate,
        approximate_context_window=approximate_limits,
        approximate_max_output=approximate_limits,
        api_validation_model=api_validation_model,
        api_validation_threshold_tokens=max(
            1,
            int(source_tokens * API_VALIDATION_THRESHOLD_FRACTION),
        ),
    )


def count_source_tokens(
    text: str,
    *,
    counter: TokenCounter,
    budget: SourceBudget,
    api_client: OpenAIInputTokenCounter | None = None,
) -> SourceTokenCount:
    local_tokens = counter.count(text)
    threshold = budget.api_validation_threshold_tokens
    if (
        not budget.api_validation_model
        or threshold is None
        or local_tokens < threshold
    ):
        return SourceTokenCount(tokens=local_tokens, local_tokens=local_tokens)

    client = api_client or OpenAIInputTokenCounter()
    if not client.available:
        return SourceTokenCount(tokens=local_tokens, local_tokens=local_tokens)

    try:
        api_tokens = client.count_input_tokens(model=budget.api_validation_model, text=text)
    except RuntimeError as exc:
        return SourceTokenCount(
            tokens=local_tokens,
            local_tokens=local_tokens,
            notes=(f"OpenAI input-token validation failed; local estimate used: {exc}",),
        )
    return SourceTokenCount(
        tokens=api_tokens,
        local_tokens=local_tokens,
        used_api=True,
        notes=("Validated with OpenAI input-token counting API.",),
    )


def context_window_for_model(model: str | None) -> tuple[int, bool]:
    limits, approximate = model_limits_for_model(model)
    return limits.context_window_tokens, approximate


def max_output_tokens_for_model(model: str | None) -> tuple[int, bool]:
    limits, approximate = model_limits_for_model(model)
    return limits.max_output_tokens, approximate


def model_limits_for_model(model: str | None) -> tuple[ModelLimits, bool]:
    normalized = _normalized_model_name(model)
    if not normalized:
        return _fallback_limits(None), True

    cached = _load_model_limits().get(normalized)
    if cached is not None:
        return cached, cached.approximate

    for prefix, limits in sorted(
        _load_model_limits().items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if normalized.startswith(prefix):
            return limits, limits.approximate

    fallback = _fallback_limits(normalized)
    return fallback, True


def refresh_model_limits_from_openai_docs(
    *,
    models: list[str] | tuple[str, ...] = DEFAULT_REFRESH_MODELS,
    output: Path = DEFAULT_MODEL_LIMITS_PATH,
) -> Path:
    refreshed = _load_model_limits_from_path(output)
    for model in models:
        normalized = _normalized_model_name(model)
        if not normalized:
            continue
        source_url = OPENAI_DOCS_MODEL_URL.format(model=normalized)
        document = _fetch_text(source_url)
        refreshed[normalized] = extract_model_limits_from_document(
            model=normalized,
            document=document,
            source_url=source_url,
        )

    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "models": {
            model: {
                "context_window_tokens": limits.context_window_tokens,
                "max_output_tokens": limits.max_output_tokens,
                "source": limits.source,
                "verified_at": limits.verified_at,
            }
            for model, limits in sorted(refreshed.items())
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _load_model_limits.cache_clear()
    return output


def extract_model_limits_from_document(
    *,
    model: str,
    document: str,
    source_url: str,
) -> ModelLimits:
    plain = _plain_text(document)
    context_match = re.search(r"([\d,.]+[KkMm]?)\s+context window", plain)
    max_output_match = re.search(r"([\d,.]+[KkMm]?)\s+max output tokens", plain)
    if context_match is None or max_output_match is None:
        raise RuntimeError(f"Could not extract model limits for {model} from {source_url}.")
    return ModelLimits(
        model=_normalized_model_name(model),
        context_window_tokens=_parse_token_count(context_match.group(1)),
        max_output_tokens=_parse_token_count(max_output_match.group(1)),
        source=source_url,
        verified_at=datetime.now(UTC).isoformat(),
        approximate=False,
    )


@lru_cache(maxsize=1)
def _load_model_limits() -> dict[str, ModelLimits]:
    path = Path(os.environ.get(MODEL_LIMITS_FILE_ENV, DEFAULT_MODEL_LIMITS_PATH)).expanduser()
    return _load_model_limits_from_path(path)


def _load_model_limits_from_path(path: Path) -> dict[str, ModelLimits]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    models = payload.get("models", {})
    if not isinstance(models, dict):
        return {}

    limits: dict[str, ModelLimits] = {}
    for model, data in models.items():
        if not isinstance(data, dict):
            continue
        normalized = _normalized_model_name(model)
        try:
            context_window = int(data["context_window_tokens"])
            max_output = int(data["max_output_tokens"])
        except (KeyError, TypeError, ValueError):
            continue
        limits[normalized] = ModelLimits(
            model=normalized,
            context_window_tokens=context_window,
            max_output_tokens=max_output,
            source=str(data.get("source") or path),
            verified_at=(
                data.get("verified_at") if isinstance(data.get("verified_at"), str) else None
            ),
            approximate=bool(data.get("approximate", False)),
        )
    return limits


def _fallback_limits(model: str | None) -> ModelLimits:
    normalized = _normalized_model_name(model)
    if normalized in _FALLBACK_MODEL_LIMITS:
        context_window, max_output = _FALLBACK_MODEL_LIMITS[normalized]
    else:
        matched = None
        for prefix, values in sorted(
            _FALLBACK_MODEL_LIMITS.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if normalized.startswith(prefix):
                matched = values
                break
        context_window, max_output = matched or (
            DEFAULT_CONTEXT_WINDOW_TOKENS,
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
    return ModelLimits(
        model=normalized or "unknown",
        context_window_tokens=context_window,
        max_output_tokens=max_output,
        source="fallback",
        approximate=True,
    )


def _response_reserve_for_limits(limits: ModelLimits) -> int:
    return limits.max_output_tokens


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ccr-model-limits-refresh"})
    with urllib.request.urlopen(request, timeout=OPENAI_TOKEN_COUNT_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def _plain_text(document: str) -> str:
    text = re.sub(r"<[^>]+>", " ", document)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text)


def _parse_token_count(value: str) -> int:
    normalized = value.strip().replace(",", "")
    multiplier = 1
    if normalized[-1:].lower() == "k":
        multiplier = 1_000
        normalized = normalized[:-1]
    elif normalized[-1:].lower() == "m":
        multiplier = 1_000_000
        normalized = normalized[:-1]
    return int(float(normalized) * multiplier)


def _usable_openai_api_key(api_key: str | None) -> bool:
    normalized = (api_key or "").strip()
    return normalized.lower() not in _PLACEHOLDER_API_KEYS


def _fallback_encoding_name(model: str) -> str:
    normalized = _normalized_model_name(model)
    if normalized.startswith(("gpt-4", "gpt-5", "o1", "o3", "o4")):
        return "o200k_base"
    return "cl100k_base"


def _normalized_model_name(model: str | None) -> str:
    normalized = (model or "").strip().lower().replace("_", "-")
    if normalized.startswith("gpt") and not normalized.startswith("gpt-"):
        normalized = f"gpt-{normalized[3:]}"
    return normalized
