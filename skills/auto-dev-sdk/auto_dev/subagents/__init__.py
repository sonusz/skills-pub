"""Subagent runners (plan / implement / spec / review).

Each runner:
  * Loads the prompt template via `prompts`.
  * Validates expected source hashes against current files (TOCTOU guard).
  * Calls the vendor adapter `run_subagent(...)`.
  * Validates the returned JSON shape.
  * Writes artifacts atomically with hash provenance.
"""

from auto_dev.subagents.runner import SubagentRunner, SubagentError
from auto_dev.subagents.plan import run_plan
from auto_dev.subagents.implement import run_implement
from auto_dev.subagents.review import run_review
from auto_dev.subagents.spec import run_spec
from auto_dev.subagents.prd_review import run_prd_review

__all__ = [
    "SubagentRunner",
    "SubagentError",
    "run_plan",
    "run_implement",
    "run_review",
    "run_spec",
    "run_prd_review",
]
