"""Synthesizer JSON schema (G15).

The synthesizer is a PURE text-to-JSON extractor. It reads N reviewer
markdown outputs and emits per-reviewer structured entries. It does NOT
merge findings across reviewers, does NOT assign severity, and does NOT
derive the overall verdict — those are either reviewer judgments
(preserved verbatim) or harness-side deterministic computations.

Schema: one entry per responding reviewer with that reviewer's verdict
and findings as-they-stated-them.
"""
from __future__ import annotations

import json


def _require_every_object_property(schema: object) -> None:
    """Normalize a schema for OpenAI strict structured outputs.

    OpenAI requires every key declared in an object's properties mapping to
    also appear in that object's required list. Empty arrays/strings retain
    the optional semantic at the payload level.
    """
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            schema["required"] = list(properties)
        for value in schema.values():
            _require_every_object_property(value)
    elif isinstance(schema, list):
        for value in schema:
            _require_every_object_property(value)


def synthesizer_output_schema(*, openai_compatible: bool = False) -> dict:
    """JSON Schema the synthesizer must conform to.

    Pure per-reviewer extraction. Each responding reviewer gets one
    entry listing their own verdict + their own findings. No cross-
    reviewer merging — semantic overlap is preserved as-is; the stage
    agent re-running against an amended PRD is what resolves divergence.
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["per_reviewer"],
        "properties": {
            "per_reviewer": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["vendor", "verdict", "findings"],
                    "properties": {
                        "vendor": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 32,
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["pass", "needs_revision", "fail"],
                        },
                        "findings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["severity", "summary"],
                                "properties": {
                                    "severity": {
                                        "type": "string",
                                        "enum": [
                                            "invariant_violation",
                                            "risk",
                                            "opinion",
                                        ],
                                    },
                                    "summary": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 1000,
                                    },
                                    "targets": {
                                        "type": "array",
                                        "items": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 200,
                                        },
                                    },
                                },
                            },
                        },
                        "coverage": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["req_id", "status", "evidence"],
                                "properties": {
                                    "req_id": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 64,
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": [
                                            "satisfied",
                                            "partial",
                                            "missing",
                                            "deviated",
                                            "ambiguous",
                                        ],
                                    },
                                    "evidence": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 1000,
                                    },
                                    "notes": {
                                        "type": "string",
                                        "maxLength": 1000,
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "decision": {
                "type": "object",
                "additionalProperties": False,
                "required": ["node", "outcome", "blocking", "severity", "summary"],
                "properties": {
                    "node": {
                        "type": "string",
                        "enum": ["design_review"],
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["pass", "retry_design", "halt_for_human"],
                    },
                    "blocking": {
                        "type": "boolean",
                    },
                    "severity": {
                        "type": "string",
                        "enum": [
                            "invariant_violation",
                            "risk",
                            "opinion",
                        ],
                    },
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "prd_targeted": {
                        "type": "boolean",
                    },
                },
            },
        },
    }
    if not openai_compatible:
        return schema

    # Non-design groups do not have a canonical design decision. Strict
    # schemas still require the top-level key, so represent its absence as
    # JSON null while preserving the object form for design-review.
    decision = schema["properties"]["decision"]
    schema["properties"]["decision"] = {
        "anyOf": [decision, {"type": "null"}],
    }
    _require_every_object_property(schema)
    return schema


def synthesizer_output_schema_json(*, openai_compatible: bool = False) -> str:
    """Schema serialized for CLI `--json-schema` argument."""
    return json.dumps(
        synthesizer_output_schema(openai_compatible=openai_compatible),
        separators=(",", ":"),
    )
