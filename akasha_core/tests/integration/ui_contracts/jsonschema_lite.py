"""jsonschema_lite — the smallest validator that is HONEST about the contract.

The vendored `contracts/ui.schema.json` uses a deliberately small JSON Schema
subset. Rather than skipping validation when a library is unavailable (which
would silently make UX-013 a no-op), this module implements exactly that subset
and refuses any keyword it does not implement — an unknown keyword is a test
failure, not a silent pass.

Supported: $ref, type, properties, required, additionalProperties, items,
minItems, maxItems, enum, const, minimum, maximum, pattern, format(date-time),
oneOf, allOf, if/then.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "enum",
        "const",
        "minimum",
        "maximum",
        "pattern",
        "format",
        "oneOf",
        "allOf",
        "if",
        "then",
    }
)

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "null": type(None),
}


class SchemaError(AssertionError):
    pass


def collect_keywords(schema: Any, *, out: set[str] | None = None) -> set[str]:
    """Every keyword appearing anywhere in the document (for coverage checks)."""
    found = set() if out is None else out
    if isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            if key in ("properties", "$defs"):
                for sub in value.values():
                    collect_keywords(sub, out=found)
            else:
                collect_keywords(value, out=found)
    elif isinstance(schema, list):
        for item in schema:
            collect_keywords(item, out=found)
    return found


def assert_supported(schema: dict) -> None:
    unknown = collect_keywords(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaError(
            f"ui.schema.json uses keywords this validator does not implement: {sorted(unknown)}. "
            "Extend the validator instead of weakening the check."
        )


def _is_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    python_type = _TYPE_MAP.get(expected)
    if python_type is None:
        raise SchemaError(f"unsupported type keyword: {expected!r}")
    return isinstance(value, python_type)


def _resolve(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise SchemaError(f"only local $ref is supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(instance: Any, schema: dict, *, root: dict | None = None, path: str = "$") -> None:
    """Raise SchemaError on the first violation."""
    root = root if root is not None else schema

    if "$ref" in schema:
        validate(instance, _resolve(root, schema["$ref"]), root=root, path=path)
        return

    if "const" in schema and instance != schema["const"]:
        raise SchemaError(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} not in enum {schema['enum']!r}")

    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(instance, option) for option in options):
            raise SchemaError(f"{path}: expected type {expected!r}, got {type(instance).__name__}")

    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                validate(instance, candidate, root=root, path=path)
            except SchemaError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaError(f"{path}: oneOf matched {matches} branches (expected exactly 1)")

    if "allOf" in schema:
        for candidate in schema["allOf"]:
            validate(instance, candidate, root=root, path=path)

    if "if" in schema:
        try:
            validate(instance, schema["if"], root=root, path=path)
            condition_holds = True
        except SchemaError:
            condition_holds = False
        if condition_holds and "then" in schema:
            validate(instance, schema["then"], root=root, path=path)

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise SchemaError(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                validate(value, properties[key], root=root, path=f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise SchemaError(f"{path}: unexpected property {key!r}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(f"{path}: expected at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(instance):
                validate(item, schema["items"], root=root, path=f"{path}[{index}]")

    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise SchemaError(f"{path}: {instance!r} does not match {schema['pattern']!r}")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SchemaError(f"{path}: {instance!r} is not an ISO date-time") from exc

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(f"{path}: {instance} > maximum {schema['maximum']}")


__all__ = [
    "SUPPORTED_KEYWORDS",
    "SchemaError",
    "assert_supported",
    "collect_keywords",
    "validate",
]
