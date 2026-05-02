"""Artifact readers/writers with schema validation."""

from autodev.artifacts.scope import Scope, ScopeItem, ExcludedItem, load_scope, write_scope
from autodev.artifacts.build import BuildReport, load_build, write_build
from autodev.artifacts.common import read_markdown_with_hash, write_markdown_with_hash
from autodev.artifacts.design_packet import (
    accepted_design_fresh,
    build_design_packet,
    design_packet_fresh,
    write_accepted_design,
    write_design_packet,
)
from autodev.artifacts.design_rework_memory import (
    load_design_rework_memory,
    record_design_review_memory,
)
from autodev.artifacts.workflow_state import (
    bootstrap_workflow_state,
    discover_root_context_paths,
    ensure_workflow_state,
    load_workflow_state,
    workflow_state_path,
    write_workflow_state,
)
from autodev.artifacts.implementation_index import (
    ImplementationIndex,
    load_implementation_index,
    write_implementation_index,
)
from autodev.artifacts.prd_checklist import (
    PrdChecklist,
    PrdRequirement,
    load_prd_checklist,
    write_prd_checklist,
)
from autodev.artifacts.verdict import (
    PanelVerdict,
    PanelFinding,
    load_verdict,
    write_verdict,
    InvariantViolation,
)
from autodev.artifacts.failure import FailureReport, load_failure, write_failure
from autodev.artifacts.overrides import Overrides, OverrideRecord, load_overrides, write_overrides

__all__ = [
    "Scope", "ScopeItem", "ExcludedItem", "load_scope", "write_scope",
    "BuildReport", "load_build", "write_build",
    "read_markdown_with_hash", "write_markdown_with_hash",
    "accepted_design_fresh", "build_design_packet", "design_packet_fresh",
    "write_accepted_design", "write_design_packet",
    "load_design_rework_memory", "record_design_review_memory",
    "bootstrap_workflow_state", "discover_root_context_paths",
    "ensure_workflow_state", "load_workflow_state",
    "workflow_state_path", "write_workflow_state",
    "ImplementationIndex", "load_implementation_index", "write_implementation_index",
    "PrdChecklist", "PrdRequirement", "load_prd_checklist", "write_prd_checklist",
    "PanelVerdict", "PanelFinding", "load_verdict", "write_verdict", "InvariantViolation",
    "FailureReport", "load_failure", "write_failure",
    "Overrides", "OverrideRecord", "load_overrides", "write_overrides",
]
