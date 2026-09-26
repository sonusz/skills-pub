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
        "4809539e8d6876916ecebd0e3ec14e33a09abd88bd8527e78a87782d2826a1a1",
    "review-trace-review.md":
        "4306f587f1d5d5e630bec001013e06f2613772c0115f4893c1368528bb75f834",
    "review-close-approval.md":
        "2722a014d3249fed1849a0aad1e297a58a3314d98e2547d2c3af3ac3c15db5f1",
    "synthesize.md":
        "b42562768ac11807fdefef764ff7599111419bf2b4e9f4a32c034545969d6d12",
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
