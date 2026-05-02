"""ad-4: vendor adapter JSON extraction."""
from __future__ import annotations

import pytest

from auto_dev.errors import VendorProtocolError
from auto_dev.vendors.base import VendorAdapter


class DummyAdapter(VendorAdapter):
    name = "dummy"

    def run_subagent(self, **kw):  # pragma: no cover
        raise NotImplementedError


def test_extract_whole_json():
    a = DummyAdapter()
    assert a._extract_json('{"x": 1}') == {"x": 1}


def test_extract_fenced():
    a = DummyAdapter()
    text = 'here is the answer:\n```json\n{"y": 2}\n```\nend'
    assert a._extract_json(text) == {"y": 2}


def test_extract_balanced_braces():
    a = DummyAdapter()
    text = 'prose {"z": 3} more'
    assert a._extract_json(text) == {"z": 3}


def test_no_json_raises():
    a = DummyAdapter()
    with pytest.raises(VendorProtocolError):
        a._extract_json("no json here")


def test_empty_raises():
    a = DummyAdapter()
    with pytest.raises(VendorProtocolError):
        a._extract_json("")
