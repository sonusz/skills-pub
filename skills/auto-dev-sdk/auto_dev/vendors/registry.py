"""Adapter registry — name → class, pluggable so tests can register FakeVendor."""
from __future__ import annotations

from typing import Dict, Type

from auto_dev.errors import ConfigError
from auto_dev.vendors.base import VendorAdapter


_REGISTRY: Dict[str, Type[VendorAdapter]] = {}


def register_adapter(name: str, cls: Type[VendorAdapter]) -> None:
    _REGISTRY[name] = cls


def get_adapter(name: str) -> VendorAdapter:
    if name not in _REGISTRY:
        _load_builtins()
    if name not in _REGISTRY:
        raise ConfigError(f"unknown vendor: {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def _load_builtins() -> None:
    """Lazy-register built-in adapters so SDK import doesn't require vendor deps."""
    from auto_dev.vendors.anthropic import AnthropicAdapter
    from auto_dev.vendors.openai import OpenAIAdapter
    from auto_dev.vendors.google import GoogleAdapter

    _REGISTRY.setdefault("anthropic", AnthropicAdapter)
    _REGISTRY.setdefault("openai", OpenAIAdapter)
    _REGISTRY.setdefault("google", GoogleAdapter)
