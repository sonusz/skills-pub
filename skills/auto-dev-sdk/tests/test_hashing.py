"""ad-2: hashing + markdown header parsing."""
from __future__ import annotations

import re

from auto_dev.state.hashing import (
    HASH_PREFIX,
    hash_bytes,
    hash_file,
    parse_markdown_source_hash,
)


def test_hash_bytes_known_value():
    # sha256 of empty string.
    expected = HASH_PREFIX + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert hash_bytes(b"") == expected


def test_hash_format_prefix():
    assert re.match(r"^sha256:[0-9a-f]{64}$", hash_bytes(b"hi"))


def test_hash_file_vs_bytes(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"hello world")
    assert hash_file(p) == hash_bytes(b"hello world")


def test_hash_includes_trailing_newline(tmp_path):
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    p1.write_bytes(b"x")
    p2.write_bytes(b"x\n")
    assert hash_file(p1) != hash_file(p2)


def test_parse_markdown_source_hash(tmp_path):
    p = tmp_path / "m.md"
    expected = "sha256:" + "a" * 64
    p.write_text(
        f"<!-- source: foo.json -->\n<!-- source_hash: {expected} -->\n<!-- written: 2026-04-19 -->\n\nbody\n"
    )
    assert parse_markdown_source_hash(p) == expected


def test_parse_markdown_source_hash_missing(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("no header here\n")
    assert parse_markdown_source_hash(p) is None
