from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ccr.langfuse_related.sync import SCHEMA_MODELS


class LangChainUtilityError(RuntimeError):
    pass


def parser_diagnostics(schema_name: str) -> dict[str, object]:
    if schema_name not in SCHEMA_MODELS:
        choices = ", ".join(sorted(SCHEMA_MODELS))
        msg = f"Unknown schema {schema_name!r}. Expected one of: {choices}."
        raise ValueError(msg)

    model = SCHEMA_MODELS[schema_name]
    parser_class = _load_json_output_parser()
    parser = parser_class(pydantic_object=model)
    format_instructions = parser.get_format_instructions()
    valid_sample = _sample_for_model(model)
    valid_json = json.dumps(valid_sample, sort_keys=True)

    return {
        "schema": schema_name,
        "model": model.__name__,
        "parser": parser.__class__.__name__,
        "required_fields": _required_fields(model),
        "fields": _field_diagnostics(model),
        "nested_models": sorted(model.model_json_schema().get("$defs", {})),
        "enum_values": _enum_values(model.model_json_schema()),
        "format_instruction_chars": len(format_instructions),
        "format_instructions": format_instructions,
        "valid_sample": valid_sample,
        "valid_parse": _valid_parse_status(parser, model, valid_json),
        "invalid_json_parse": _invalid_json_parse_status(parser),
        "empty_object_validation": _empty_object_validation_status(model),
    }


def schema_names() -> list[str]:
    return sorted(SCHEMA_MODELS)


def _load_json_output_parser() -> type[Any]:
    try:
        from langchain_core.output_parsers import JsonOutputParser
    except ImportError as exc:
        msg = (
            "LangChain parser diagnostics require the optional LangChain utilities dependency. "
            "Install it with `.venv/bin/python -m pip install -e '.[langchain]'`."
        )
        raise LangChainUtilityError(msg) from exc
    return JsonOutputParser


def _valid_parse_status(parser: Any, model: type[BaseModel], valid_json: str) -> dict[str, object]:
    try:
        parsed = parser.parse(valid_json)
        payload = parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
        model.model_validate(payload)
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "parsed_type": type(parsed).__name__}


def _invalid_json_parse_status(parser: Any) -> dict[str, object]:
    try:
        parser.parse("not-json")
    except Exception as exc:
        return {"status": "failed_as_expected", "error": str(exc)}
    return {"status": "unexpected_ok"}


def _empty_object_validation_status(model: type[BaseModel]) -> dict[str, object]:
    try:
        model.model_validate({})
    except Exception as exc:
        return {"status": "failed_as_expected", "error": str(exc)}
    return {"status": "accepted", "note": "This schema has no required fields."}


def _required_fields(model: type[BaseModel]) -> list[str]:
    return [name for name, field in model.model_fields.items() if field.is_required()]


def _field_diagnostics(model: type[BaseModel]) -> dict[str, dict[str, object]]:
    fields: dict[str, dict[str, object]] = {}
    for name, field in model.model_fields.items():
        details: dict[str, object] = {
            "required": field.is_required(),
            "annotation": _annotation_label(field.annotation),
        }
        if not field.is_required() and field.default is not PydanticUndefined:
            details["default"] = _jsonable(field.default)
        elif not field.is_required() and field.default_factory is not None:
            details["default_factory"] = getattr(
                field.default_factory,
                "__name__",
                field.default_factory.__class__.__name__,
            )
        fields[name] = details
    return fields


def _enum_values(schema: dict[str, object]) -> dict[str, list[object]]:
    values: dict[str, list[object]] = {}
    for name, definition in (schema.get("$defs") or {}).items():
        if isinstance(definition, dict) and isinstance(definition.get("enum"), list):
            values[str(name)] = list(definition["enum"])
    if isinstance(schema.get("enum"), list):
        values[str(schema.get("title") or "root")] = list(schema["enum"])
    return values


def _sample_for_model(model: type[BaseModel]) -> dict[str, object]:
    schema = model.model_json_schema()
    sample = _sample_for_schema(schema, root_schema=schema, seen_refs=set())
    if not isinstance(sample, dict):
        msg = f"Could not generate object sample for schema model {model.__name__}."
        raise LangChainUtilityError(msg)
    return sample


def _sample_for_schema(
    schema: dict[str, object],
    *,
    root_schema: dict[str, object],
    seen_refs: set[str],
) -> object:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in seen_refs:
            return {}
        target = _resolve_ref(ref, root_schema)
        return _sample_for_schema(target, root_schema=root_schema, seen_refs={*seen_refs, ref})

    if "default" in schema:
        return _jsonable(schema["default"])

    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return _jsonable(schema["enum"][0])

    for union_key in ("anyOf", "oneOf"):
        options = schema.get(union_key)
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and option.get("type") != "null":
                    return _sample_for_schema(
                        option,
                        root_schema=root_schema,
                        seen_refs=seen_refs,
                    )
            return None

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            return {}
        return {
            name: _sample_for_schema(
                property_schema,
                root_schema=root_schema,
                seen_refs=seen_refs,
            )
            for name, property_schema in properties.items()
            if isinstance(property_schema, dict)
        }
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return [_sample_for_schema(items, root_schema=root_schema, seen_refs=seen_refs)]
        return []
    if schema_type == "boolean":
        return True
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "null":
        return None
    return "example"


def _resolve_ref(ref: str, root_schema: dict[str, object]) -> dict[str, object]:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        msg = f"Unsupported JSON schema reference: {ref}"
        raise LangChainUtilityError(msg)
    definitions = root_schema.get("$defs")
    if not isinstance(definitions, dict):
        msg = f"JSON schema reference {ref} has no $defs section to resolve."
        raise LangChainUtilityError(msg)
    target = definitions.get(ref.removeprefix(prefix))
    if not isinstance(target, dict):
        msg = f"JSON schema reference target not found: {ref}"
        raise LangChainUtilityError(msg)
    return target


def _annotation_label(annotation: object) -> str:
    if isinstance(annotation, type):
        return annotation.__name__
    if isinstance(annotation, Enum):
        return annotation.value
    return str(annotation).replace("typing.", "")


def _jsonable(value: object) -> Any:
    return json.loads(json.dumps(value, default=str))
