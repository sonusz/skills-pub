"""Quota-aware candidate resolution for a vendor role.

Given a primary LLM spec plus ordered fallbacks (each carrying its own minimum
remaining-quota %), pick the first candidate whose vendor has enough quota left.
Policy is FAIL-CLOSED: a candidate whose quota cannot be read (no creds, endpoint
error) is treated as insufficient and skipped. If no candidate qualifies, raise
:class:`QuotaHalt` carrying the earliest expected recovery time so the caller can
pause and schedule a conditional resume.

A candidate with ``min_quota_pct is None`` is *ungated* — it is selected
unconditionally (use it as an always-available last-resort fallback, or to opt a
role out of the quota gate entirely by leaving the primary ungated).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from autodev.errors import QuotaHalt
from autodev.vendors.quota import get_remaining


@dataclass(frozen=True)
class Candidate:
    vendor: str
    model: str
    effort: str = ""
    min_quota_pct: float | None = None
    flags: tuple[str, ...] = ()


def build_candidates(spec) -> list[Candidate]:
    """Primary first, then each fallback, from a *Spec (StageSpec / reviewer /
    synthesizer / probe). Tolerates specs that predate the quota fields."""
    primary = Candidate(
        vendor=spec.vendor,
        model=spec.model,
        effort=getattr(spec, "effort", "") or "",
        min_quota_pct=getattr(spec, "min_quota_pct", None),
        flags=tuple(getattr(spec, "flags", ()) or ()),
    )
    cands = [primary]
    for fb in getattr(spec, "fallbacks", ()) or ():
        cands.append(
            Candidate(
                vendor=fb.vendor,
                model=fb.model,
                effort=getattr(fb, "effort", "") or "",
                min_quota_pct=getattr(fb, "min_quota_pct", None),
                flags=tuple(getattr(fb, "flags", ()) or ()),
            )
        )
    return cands


def resolve_candidate(
    candidates: list[Candidate],
    *,
    role: str,
    logger: Callable[[str], None] | None = None,
    force: bool = False,
) -> Candidate:
    """Return the first candidate that meets its quota floor.

    ``force`` bypasses the quota TTL cache (used by quota-resume re-checks).
    Raises :class:`QuotaHalt` when every gated candidate is insufficient/unknown.
    """
    diagnostics: list[dict] = []
    earliest = None
    for cand in candidates:
        if cand.min_quota_pct is None:
            if logger:
                logger(f"[quota] {role}: using ungated {cand.vendor}/{cand.model}")
            return cand
        q = get_remaining(cand.vendor, cand.model, force=force)
        diagnostics.append(
            {
                "vendor": cand.vendor,
                "model": cand.model,
                "min_quota_pct": cand.min_quota_pct,
                "remaining_pct": q.remaining_pct,
                "resets_at": q.resets_at.isoformat() if q.resets_at else None,
                "error": q.error,
            }
        )
        if q.remaining_pct is not None and q.remaining_pct >= cand.min_quota_pct:
            if logger:
                logger(
                    f"[quota] {role}: {cand.vendor}/{cand.model} "
                    f"{q.remaining_pct:.0f}% >= {cand.min_quota_pct}% — selected"
                )
            return cand
        if logger:
            seen = "unknown" if q.remaining_pct is None else f"{q.remaining_pct:.0f}%"
            logger(
                f"[quota] {role}: {cand.vendor}/{cand.model} {seen} "
                f"< {cand.min_quota_pct}% — skipping"
            )
        if q.resets_at is not None and (earliest is None or q.resets_at < earliest):
            earliest = q.resets_at
    raise QuotaHalt(role=role, diagnostics=diagnostics, resume_at=earliest)
