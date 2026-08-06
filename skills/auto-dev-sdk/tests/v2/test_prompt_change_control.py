"""Prompt change-control regression.

Post-G15, the panel prompts live under autodev/panel/prompts/ — three
per-gate reviewer prompts plus one synthesizer prompt. SC3 pins all four
hashes. Changes require intentional hash bumps.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "autodev" / "panel" / "prompts"
)

REVIEWER_PROMPTS = (
    "review-design-review.md",
    "review-trace-review.md",
    "review-close-approval.md",
)
SYNTHESIZER_PROMPT = "synthesize.md"

# Directed-phrase blacklist. Hitting any of these in a reviewer prompt is
# a bug — panels must be open-ended (R4b / §5 SC3). Synthesizer is
# exempt: it is a mechanical translator, not a judgment-maker.
BLACKLIST = (
    r"\bare these deviations ok\b",
    r"\bdoes this address\b",
    r"\bis this acceptable\b",
    r"\bdo you agree\b",
    r"\bjust confirm\b",
    r"\bdeviations are ok\b",
    r"\bplease confirm\b",
)


def _hash_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_all_panel_prompts_exist():
    for name in REVIEWER_PROMPTS + (SYNTHESIZER_PROMPT,):
        assert (PROMPTS_DIR / name).exists(), f"missing prompt: {name}"


def test_reviewer_prompts_absent_from_blacklist():
    """SC3 (b): no directed wording in any reviewer prompt."""
    for name in REVIEWER_PROMPTS:
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8").lower()
        for pat in BLACKLIST:
            assert not re.search(pat, text), (
                f"directed-phrase pattern {pat!r} found in {name}"
            )


# Fixture-pinned hashes. Bumping these requires intentional prompt change.
EXPECTED_HASHES = {
    "review-design-review.md":
        "5945ca93f649833fe4332337cd69bddf351689b5393eb3a593dfbbfb224fc19f",
    "review-trace-review.md":
        "53553bc2ee4a6f600e4d29289a44eaef4d8831a0fff8694199b13f972dff3dae",
    "review-close-approval.md":
        "b48947cd53be245f8b938ec9fd952413b6fc8467dc5b6594b8f8c0026d2e192d",
    "synthesize.md":
        "4685aef10285ef8bb6d605e5fa6a4a7a2b3350371cf0e8e8d7aa78f8569a727f",
}


def test_panel_prompts_hash_change_control():
    """SC3 (a): hash stability. Update EXPECTED_HASHES on intentional change."""
    for name in REVIEWER_PROMPTS + (SYNTHESIZER_PROMPT,):
        actual = _hash_file(PROMPTS_DIR / name)
        expected = EXPECTED_HASHES.get(name)
        assert expected is not None, f"no pinned hash for {name}"
        assert actual == expected, (
            f"{name} hash changed: {actual} != pinned {expected}. "
            f"If intentional, update EXPECTED_HASHES."
        )
