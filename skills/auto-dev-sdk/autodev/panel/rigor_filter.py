"""Rigor severity filter — mechanism 1 of docs/proposals/rigor-tier.md.

Runs inside verdict assembly (runner.py), after the anchor filter and
before verdict derivation. Decides, per finding, whether the reviewer's
severity *blocks* given the per-requirement rigor levels in the PRD's
`## Assurance` section.

Severity-slot semantics: the filter rewrites each downgraded finding's
legacy ``severity`` field to the effective value (``opinion``) and
preserves the reviewer's original in ``severity_reported``. Every
existing reader (``effectively_blocks``, ``_blocking_findings``,
dispatch) then sees effective severity with no code change.

Processing matrix (rigor levels: strict > core > loose):

    | finding                                  | strict | core | loose |
    |------------------------------------------|--------|------|-------|
    | IV, category=undelivered (contradiction) | block  | block| block |
    | IV, category=missing (semantic gap)      | block  | block| DOWN  |
    | IV, any other/unknown category           | block  | block| block |
    | risk, category=invented (over-design)    | block  | block| block |
    | risk, category=missized, dir=coarse/none | block  | block| block |
    | risk, category=missized, dir=fine        | block  | DOWN | DOWN  |
    | risk, other category, class=mainline/none| block  | block| DOWN  |
    | risk, other category, class=edge         | block  | DOWN | DOWN  |
    | opinion                                  | pass   | pass | pass  |

Rule A (cross-R blast radius): a finding's rigor is the max level
across every R it resolves to — reviewer-cited ``prd:R<n>`` evidence
tokens combined with Rs reached via ``scope:<id>`` → scope.prd_ref.
Reviewer citation can only raise effective rigor.

Rule B (fail closed): missing ``failure_class`` → mainline; missing
``missized_direction`` → coarse; unresolvable refs → strict. Every
fail-closed event is returned for loud logging so a silently-inert
filter is observable.

Counterfactual mode (mechanism 2's stall classifier): pass
``override_level`` to re-run the filter with selected Rs lowered; the
caller supplies copies of the findings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from autodev.artifacts.scope import Scope
from autodev.artifacts.verdict import PanelFinding
from autodev.assurance import AssuranceMap, Level, max_level

_PRD_R_REF = re.compile(r"^prd:(R\d+)$")
_SCOPE_REF = re.compile(r"^scope:(.+)$")
_TRACE_REF = re.compile(r"^trace:(.+?)\.r\d+$")
_TEST_PLAN_REF = re.compile(r"^test-plan:(.+?)\.t\d+$")
_R_TOKEN = re.compile(r"^R\d+$")

_BLOCKING = ("invariant_violation", "risk")


def one_level_down(level: Level) -> Level:
    return {"strict": "core", "core": "loose", "loose": "loose"}[level]


@dataclass
class RigorEvent:
    """One filter action or fail-closed occurrence, for log.jsonl."""
    kind: str            # "downgrade" | "fail-closed"
    reason: str          # matrix rule or fail-closed cause
    vendor: str
    severity_reported: str
    rigor: str           # resolved rigor level ("" when unresolved)
    summary: str         # truncated finding summary


@dataclass
class RigorFilterResult:
    examined: int = 0
    downgraded: int = 0
    events: list[RigorEvent] = field(default_factory=list)

    @property
    def fail_closed(self) -> int:
        return sum(1 for e in self.events if e.kind == "fail-closed")


def _scope_prd_rs(scope: Scope | None, scope_id: str) -> list[str]:
    if scope is None:
        return []
    for item in scope.in_scope:
        if item.id == scope_id:
            return [t for t in item.prd_ref if _R_TOKEN.match(t)]
    return []


def resolve_rs(
    finding: PanelFinding, scope: Scope | None,
    known_rs: set[str] | None = None,
) -> list[str]:
    """Every R the finding's evidence resolves to (Rule A source set).

    ``known_rs`` (the PRD's real `### R<n>:` markers) rejects
    reviewer-cited R tokens that do not exist — otherwise a
    hallucinated `prd:R99` resolves to the Assurance `Default:` level
    and can silently downgrade a blocking finding (Rule B failing
    open). None = unknown, no rejection (direct-constructed maps).
    """
    rs: set[str] = set()
    for ref in finding.evidence_refs:
        token = ref.strip()
        m = _PRD_R_REF.match(token)
        if m:
            r = m.group(1)
            if known_rs is None or r in known_rs:
                rs.add(r)
            continue
        for pat in (_SCOPE_REF, _TRACE_REF, _TEST_PLAN_REF):
            m = pat.match(token)
            if m:
                rs.update(_scope_prd_rs(scope, m.group(1)))
                break
    return sorted(rs, key=lambda r: int(r[1:]))


def _matrix_downgrades(
    finding: PanelFinding, rigor: Level,
) -> tuple[bool, str, list[str]]:
    """Return (downgrade?, rule, fail_closed_reasons) for one blocking
    finding at the resolved ``rigor`` level."""
    fail_closed: list[str] = []
    category = finding.category or "other"
    if finding.category is None:
        # Not an error by itself (trace-review taxonomy differs); it only
        # matters for the category-keyed rows, which then fail closed.
        pass

    if rigor == "strict":
        return False, "strict", fail_closed

    if finding.severity == "invariant_violation":
        if category == "missing" and rigor == "loose":
            return True, "row2:iv-missing-on-loose", fail_closed
        return False, "row1:iv-blocks", fail_closed

    # severity == "risk"
    if category == "invented":
        return False, "row5:invented-blocks", fail_closed
    if category == "underspecified-contract":
        # Mechanism 4: an unusable boundary poisons build like a
        # contradiction; `defer` waives the interior, never the
        # boundary. Blocks at every rigor level.
        return False, "row5:underspecified-contract-blocks", fail_closed
    if category == "undelivered":
        return False, "row1:contradiction-blocks", fail_closed
    if category == "missized":
        direction = finding.missized_direction
        if direction is None:
            fail_closed.append("missized-direction-absent->coarse")
            direction = "coarse"
        if direction == "fine":
            return True, "row6b:missized-fine", fail_closed
        return False, "row6a:missized-coarse-blocks", fail_closed

    # Remaining categories (missing / ambiguous / untestable / other):
    # failure_class rules, rows 3/4.
    failure_class = finding.failure_class
    if failure_class is None:
        fail_closed.append("failure-class-absent->mainline")
        failure_class = "mainline"
    if failure_class == "edge":
        return True, "row4:edge-risk", fail_closed
    if rigor == "loose":
        return True, "row3:mainline-risk-on-loose", fail_closed
    return False, "row3:mainline-risk-blocks", fail_closed


def apply_rigor_filter(
    findings: list[PanelFinding],
    assurance: AssuranceMap,
    scope: Scope | None = None,
    override_level: dict[str, Level] | None = None,
) -> RigorFilterResult:
    """Rewrite ``severity`` to the effective value in place.

    ``override_level`` (counterfactual mode) wins over the Assurance
    map for the Rs it names. Returns the filter result for logging;
    callers running counterfactuals must pass copies of the findings.
    """
    result = RigorFilterResult()
    if not assurance.present and not override_level:
        # All-strict default — byte-identical to pre-Assurance behavior.
        return result

    def level_for(r: str) -> Level:
        if override_level and r in override_level:
            return override_level[r]
        return assurance.level_for(r)

    for f in findings:
        if f.severity not in _BLOCKING:
            continue
        result.examined += 1
        rs = resolve_rs(f, scope, assurance.known_rs)
        if not rs:
            # Rule B: nothing resolvable → strict treatment.
            result.events.append(RigorEvent(
                kind="fail-closed", reason="unresolvable-refs->strict",
                vendor=f.vendor, severity_reported=f.severity, rigor="",
                summary=f.summary[:120],
            ))
            continue
        rigor = max_level([level_for(r) for r in rs])
        downgrade, rule, fail_closed_reasons = _matrix_downgrades(f, rigor)
        for reason in fail_closed_reasons:
            result.events.append(RigorEvent(
                kind="fail-closed", reason=reason, vendor=f.vendor,
                severity_reported=f.severity, rigor=rigor,
                summary=f.summary[:120],
            ))
        if downgrade:
            result.downgraded += 1
            result.events.append(RigorEvent(
                kind="downgrade", reason=rule, vendor=f.vendor,
                severity_reported=f.severity, rigor=rigor,
                summary=f.summary[:120],
            ))
            f.severity_reported = f.severity
            f.severity = "opinion"
    return result


def has_effective_blocking(
    findings: list[PanelFinding], release_threshold: str = "P1",
) -> bool:
    """Post-filter + release-policy blocking predicate.

    ``P1`` is the backward-compatible default. Priority remains independent
    from severity: a finding must have blocking severity *and* meet the
    configured release threshold.
    """
    rank = {"P0": 0, "P1": 1, "P2": 2}
    threshold = rank.get(release_threshold, rank["P1"])
    return any(
        f.severity in _BLOCKING
        and rank[f.effective_priority()] <= threshold
        for f in findings
    )
