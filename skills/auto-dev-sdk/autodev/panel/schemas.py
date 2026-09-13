"""Synthesizer JSON schema (G15).

The synthesizer preserves every reviewer finding as its own structured
record, then adds a semantic grouping layer. The harness validates cluster
membership and derives release behavior mechanically; the synthesizer never
drops or rewrites raw findings to create the grouping.
"""
from __future__ import annotations

import json


def synthesizer_output_schema() -> dict:
    """JSON Schema the synthesizer must conform to.

    Each responding reviewer gets one entry listing their own verdict +
    findings. ``issue_clusters`` references those findings by ID so semantic
    duplicates count as one ticket without destroying the raw audit trail.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        # OpenAI strict structured outputs require every declared object
        # property to appear in ``required``.  Fields that are semantically
        # optional therefore use explicit neutral values ([], "", or null)
        # instead of being omitted.
        "required": ["per_reviewer", "issue_clusters", "decision"],
        "properties": {
            "per_reviewer": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["vendor", "verdict", "findings", "coverage"],
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
                                "required": [
                                    "severity",
                                    "priority",
                                    "finding_id",
                                    "summary",
                                    "targets",
                                    "category",
                                    "evidence_refs",
                                    "failure_class",
                                    "missized_direction",
                                ],
                                "properties": {
                                    "severity": {
                                        "type": "string",
                                        "enum": [
                                            "invariant_violation",
                                            "risk",
                                            "opinion",
                                        ],
                                    },
                                    "priority": {
                                        "type": "string",
                                        "enum": ["P0", "P1", "P2"],
                                    },
                                    "finding_id": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 100,
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
                                    # Rigor-tier structured fields
                                    # (docs/proposals/rigor-tier.md).
                                    # Extracted verbatim from labeled
                                    # reviewer finding fields; the
                                    # synthesizer never infers them.
                                    "category": {
                                        "type": ["string", "null"],
                                        "enum": [
                                            "missing",
                                            "invented",
                                            "ambiguous",
                                            "undelivered",
                                            "redundant",
                                            "missized",
                                            "untestable",
                                            "underspecified-contract",
                                            "other",
                                            None,
                                        ],
                                    },
                                    "evidence_refs": {
                                        "type": "array",
                                        "items": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 200,
                                        },
                                    },
                                    "failure_class": {
                                        "type": ["string", "null"],
                                        "enum": ["mainline", "edge", None],
                                    },
                                    "missized_direction": {
                                        "type": ["string", "null"],
                                        "enum": ["coarse", "fine", None],
                                    },
                                },
                            },
                        },
                        "coverage": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["req_id", "status", "evidence", "notes"],
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
            "issue_clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prior_cluster_id", "finding_ids", "summary",
                    ],
                    "properties": {
                        # Reuse only an ID supplied in the prior-cluster
                        # catalog. null means this is a new issue.
                        "prior_cluster_id": {
                            "type": ["string", "null"],
                        },
                        "finding_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 100,
                            },
                        },
                        "summary": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                        },
                    },
                },
            },
            "decision": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "node",
                            "outcome",
                            "blocking",
                            "severity",
                            "summary",
                        ],
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
                        },
                    },
                    {"type": "null"},
                ],
            },
        },
    }


def synthesizer_output_schema_json() -> str:
    """Schema serialized for CLI `--json-schema` argument."""
    return json.dumps(synthesizer_output_schema(), separators=(",", ":"))
