"""Vendor adapter layer.

Unified call signature:

    run_subagent(model, system, inputs, *, response_schema=None, tools=None, max_tokens=...)
        -> SubagentResponse

All three adapters (Anthropic / OpenAI / Google) implement this contract.
Content / reasoning style differences are intentionally preserved — the
adapter enforces only the JSON *shape*, not the language inside.
"""

from auto_dev.vendors.base import SubagentResponse, VendorAdapter
from auto_dev.vendors.config import StageSpec, VendorsConfig, load_vendors_config
from auto_dev.vendors.registry import get_adapter, register_adapter

__all__ = [
    "SubagentResponse",
    "VendorAdapter",
    "StageSpec",
    "VendorsConfig",
    "load_vendors_config",
    "get_adapter",
    "register_adapter",
]
