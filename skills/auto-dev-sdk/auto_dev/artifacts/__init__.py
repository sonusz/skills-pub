"""Artifact readers/writers with schema validation."""

from auto_dev.artifacts.scope import Scope, ScopeItem, load_scope, write_scope
from auto_dev.artifacts.build import BuildReport, load_build, write_build
from auto_dev.artifacts.common import read_markdown_with_hash, write_markdown_with_hash

__all__ = [
    "Scope",
    "ScopeItem",
    "load_scope",
    "write_scope",
    "BuildReport",
    "load_build",
    "write_build",
    "read_markdown_with_hash",
    "write_markdown_with_hash",
]
