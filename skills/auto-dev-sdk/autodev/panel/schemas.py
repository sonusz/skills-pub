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


def synthesizer_output_schema() -> dict:
    """JSON Schema the synthesizer must conform to.

    Pure per-reviewer extraction. Each responding reviewer gets one
    entry listing their own verdict + their own findings. No cross-
    reviewer merging — semantic overlap is preserved as-is; the stage
    agent re-running against an amended PRD is what resolves divergence.
    """
    return {
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


def synthesizer_output_schema_json() -> str:
    """Schema serialized for CLI `--json-schema` argument."""
    return json.dumps(synthesizer_output_schema(), separators=(",", ":"))
