from __future__ import annotations

from typing import Any


def _object_schema(
    properties: dict[str, dict[str, Any]],
    *,
    required: tuple[str, ...] = (),
    alternatives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    if alternatives:
        schema["oneOf"] = alternatives
    return schema


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_TOP_N = {"type": "integer", "minimum": 1, "maximum": 1000}
_AS_OF = _INTEGER

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "module_profile": {
        "name": "module_profile",
        "description": "Summarize a module's recent PR activity, owners, contributors, and decisions.",
        "inputSchema": _object_schema(
            {"module_key": _STRING, "as_of_ts": _AS_OF}, required=("module_key",)
        ),
    },
    "ownership_drift": {
        "name": "ownership_drift",
        "description": "Compare declared module owners with recent human activity.",
        "inputSchema": _object_schema(
            {"module_key": _STRING, "top_n": _TOP_N, "as_of_ts": _AS_OF},
            required=("module_key",),
        ),
    },
    "bus_factor": {
        "name": "bus_factor",
        "description": "Rank modules by human-review concentration and configured bus-factor risk.",
        "inputSchema": _object_schema(
            {"module_key": _STRING, "top_n": _TOP_N, "as_of_ts": _AS_OF}
        ),
    },
    "blast_radius": {
        "name": "blast_radius",
        "description": "Measure recent activity among reverse dependents up to two dependency hops away.",
        "inputSchema": _object_schema(
            {
                "module_key": _STRING,
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 2},
                "as_of_ts": _AS_OF,
            },
            required=("module_key",),
        ),
    },
    "criticality": {
        "name": "criticality",
        "description": "Rank integration and core modules by PageRank over dependency edges.",
        "inputSchema": _object_schema({"top_n": _TOP_N, "as_of_ts": _AS_OF}),
    },
    "suggest_reviewers": {
        "name": "suggest_reviewers",
        "description": "Rank human reviewers with G3 expertise and recent-review evidence.",
        "inputSchema": _object_schema(
            {
                "modules": {"type": "array", "items": _STRING, "minItems": 1},
                "pr_number": {"type": "integer", "minimum": 1},
                "author_login": _STRING,
                "top_n": _TOP_N,
                "as_of_ts": _AS_OF,
            },
            alternatives=[
                {"required": ["modules"]},
                {"required": ["pr_number"]},
            ],
        ),
    },
    "maintainer_loss": {
        "name": "maintainer_loss",
        "description": "Show modules newly uncovered or over the review-share threshold if a person is removed.",
        "inputSchema": _object_schema(
            {"person_login": _STRING, "as_of_ts": _AS_OF},
            required=("person_login",),
        ),
    },
    "decision_trace": {
        "name": "decision_trace",
        "description": "Trace referenced and breaking decisions, their sources, and superseded ADRs.",
        "inputSchema": _object_schema(
            {
                "module_key": _STRING,
                "pr_number": {"type": "integer", "minimum": 1},
                "as_of_ts": _AS_OF,
            },
            alternatives=[
                {"required": ["module_key"]},
                {"required": ["pr_number"]},
            ],
        ),
    },
}


def openai_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
        }
        for tool in TOOL_REGISTRY.values()
    ]


def mcp_tool_definitions() -> list[dict[str, Any]]:
    return [dict(tool) for tool in TOOL_REGISTRY.values()]