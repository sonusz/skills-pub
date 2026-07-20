"""vendors.yml loader + validator (v2 schema)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from autodev.errors import ConfigError


def _deprecation_warn(path: Path, old: str, new: str) -> None:
    """Emit a one-line deprecation warning to stderr. The harness auto-migrates
    the value to the new key in-memory so the run continues — the user can
    rename the key in vendors.yml at their convenience."""
    print(
        f"[autodev] WARNING: {path}: {old!r} is deprecated; auto-migrating "
        f"to {new!r}. Rename the key in your vendors.yml to silence this.",
        file=sys.stderr,
    )

# Coding stages — vendors.yml must cover all.
STAGES = ("design", "build", "spec", "review")

# Vendors allowed for coding (R3: agy excluded; panel-review handles agy separately).
# `openai` and `codex` both route through the Codex CLI in shared/vendors.
# `cursor` (cursor-agent) is allowed for stages too — it pins a model SKU with
# effort encoded in the model id (e.g. `gpt-5.5-high`); the `--effort` flag has
# no cursor analog (see shared/vendors/vendors.conf).
ALLOWED_VENDORS = {"claude", "codex", "openai", "cursor"}

# Panel review vendors. `cursor` is allowed as a reviewer (it proxies a
# backing provider — e.g. a Gemini or Claude model — under cursor's own auth);
# diversity is enforced on the inferred underlying provider, not the literal
# `cursor` label (see _infer_cursor_underlying_vendor).
PANEL_REVIEWER_VENDORS = {"claude", "codex", "openai", "agy", "cursor"}
# Synthesizer requires native JSON-schema output (claude via --json-schema,
# codex/openai via --output-schema; agy and cursor lack native enforcement).
PANEL_SYNTHESIZER_VENDORS = {"claude", "codex", "openai"}
# The idle probe is a short, read-only LLM call. Keep it on coding-capable
# CLIs that accept prompt-on-stdin in headless mode.
PROBE_VENDORS = {"claude", "codex", "openai"}

# Cursor proxies many backends. For panel diversity we care about the underlying
# LLM provider, not the `cursor` proxy. These substrings are matched against the
# lowercased model id; first match wins. Unknown ids fall back to a per-model
# sentinel so two distinct unknown models still count as distinct, while two
# entries naming the exact same unknown model collide.
_CURSOR_MODEL_VENDOR_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("claude", "claude"),
    ("sonnet", "claude"),
    ("opus", "claude"),
    ("haiku", "claude"),
    ("anthropic", "claude"),
    ("gpt", "openai"),
    ("codex", "openai"),
    ("chatgpt", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("openai", "openai"),
    ("gemini", "gemini"),
    ("composer", "cursor"),
)


def _infer_cursor_underlying_vendor(model: str) -> str:
    """Best-effort map cursor model id → underlying provider for diversity checks."""
    m = model.strip().lower()
    for kw, ven in _CURSOR_MODEL_VENDOR_KEYWORDS:
        if kw in m:
            return ven
    return f"cursor:{m}"

DEFAULT_TIMEOUT_SEC = 1800
DEFAULT_PANEL_REVIEWER_TIMEOUT_SEC = 600
DEFAULT_PANEL_SYNTHESIZER_TIMEOUT_SEC = 300
DEFAULT_PROBE_TIMEOUT_SEC = 60

EFFORT_ORDER = ("min", "low", "medium", "high", "xhigh", "max")
EFFORT_VALUES = set(EFFORT_ORDER)
EFFORT_ALIASES = {
    "min": "min",
    "minimal": "min",
    "minimum": "min",
    "low": "low",
    "med": "medium",
    "mid": "medium",
    "medium": "medium",
    "hi": "high",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extrahigh": "xhigh",
    "max": "max",
    "maximum": "max",
}


def _normalize_vendor_value(value: Any, *, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {field} must be non-empty string")
    return value.strip().lower()


def _normalize_model_value(
    value: Any, *, vendor: str, path: Path, field: str
) -> str:
    """Normalize a model, allowing Agy to use its configured CLI default."""
    if value in (None, "") and vendor == "agy":
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {field} must be non-empty string")
    return value.strip()


def _normalize_effort_value(value: Any, *, path: Path, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {field} must be string")
    raw = value.strip().lower().replace("_", "-").replace(" ", "-")
    normalized = EFFORT_ALIASES.get(raw)
    if normalized is None:
        allowed = ", ".join(EFFORT_ORDER)
        raise ConfigError(f"{path}: {field} must be one of {allowed}")
    return normalized


def _flags_define_effort(flags: tuple[str, ...] | list[str]) -> bool:
    return any(f == "--effort" or f.startswith("--effort=") for f in flags)


def _normalize_min_quota(value: Any, *, path: Path, field: str) -> float | None:
    """A minimum *remaining* quota percent (0-100) gate, or None for ungated."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: {field} must be a number 0-100")
    v = float(value)
    if not (0.0 <= v <= 100.0):
        raise ConfigError(f"{path}: {field} must be between 0 and 100")
    return v


@dataclass(frozen=True)
class FallbackSpec:
    """An alternate LLM to use when the primary (or a higher-priority fallback)
    is below its remaining-quota floor. A runnable LLM in its own right."""
    vendor: str
    model: str
    effort: str = ""
    min_quota_pct: float | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageSpec:
    stage: str
    vendor: str
    model: str
    # Probe-check interval: how long the stream output file may be
    # silent before the harness consults the idle probe. NOT a hard
    # wall-clock cap on the stage; the bash watchdog and Python wait
    # deadline are derived from this as a generous multiple in
    # subprocess_runner.
    probe_interval_sec: int = DEFAULT_TIMEOUT_SEC
    effort: str = ""
    flags: tuple[str, ...] = ()
    # Minimum remaining-quota % to run on this vendor (None = ungated), and
    # ordered fallbacks tried when below it. See autodev.vendors.fallback.
    min_quota_pct: float | None = None
    fallbacks: tuple[FallbackSpec, ...] = ()


@dataclass(frozen=True)
class PanelReviewerSpec:
    vendor: str
    model: str
    effort: str = ""
    min_quota_pct: float | None = None
    fallbacks: tuple[FallbackSpec, ...] = ()


@dataclass(frozen=True)
class PanelSynthesizerSpec:
    vendor: str
    model: str
    effort: str = ""
    min_quota_pct: float | None = None
    fallbacks: tuple[FallbackSpec, ...] = ()


@dataclass(frozen=True)
class PanelConfig:
    reviewers: tuple[PanelReviewerSpec, ...]
    synthesizer: PanelSynthesizerSpec
    # Probe-check intervals: how long the panel reviewer / synthesizer
    # subprocess may be silent on its stream output before the harness
    # consults the idle probe. Hard wall-clock backstop is derived as a
    # generous multiple in panel/runner.
    reviewer_probe_interval_sec: int = DEFAULT_PANEL_REVIEWER_TIMEOUT_SEC
    synthesizer_probe_interval_sec: int = DEFAULT_PANEL_SYNTHESIZER_TIMEOUT_SEC


@dataclass(frozen=True)
class ProbeConfig:
    vendor: str
    model: str
    timeout_sec: int = DEFAULT_PROBE_TIMEOUT_SEC
    effort: str = ""
    flags: tuple[str, ...] = ()
    min_quota_pct: float | None = None
    fallbacks: tuple[FallbackSpec, ...] = ()


@dataclass
class VendorsConfig:
    path: Path
    stages: dict[str, StageSpec]
    panel: PanelConfig
    probe: ProbeConfig

    def resolve(self, stage: str) -> StageSpec:
        if stage not in self.stages:
            raise ConfigError(f"vendors.yml: missing stage {stage!r}")
        return self.stages[stage]


def _parse_fallbacks(
    raw: Any,
    *,
    path: Path,
    field: str,
    allowed_vendors: set[str],
    flags_label: str,
    forbid_provider: str | None = None,
    allow_flags: bool = True,
) -> tuple[FallbackSpec, ...]:
    """Validate + build the ordered fallback list for any role.

    ``allowed_vendors`` restricts the fallback vendor to that role's allowed set.
    ``forbid_provider`` (reviewers) rejects a fallback that resolves to the same
    underlying provider as its own reviewer primary (a same-provider fallback is
    pointless and would not preserve panel diversity if selected). ``allow_flags``
    is False for roles whose specs carry no native flags (panel reviewers /
    synthesizer), matching their primaries."""
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: {field} must be a list")
    from autodev.vendors.allowlist import validate_flags  # lazy: avoid import cycle

    out: list[FallbackSpec] = []
    for i, entry in enumerate(raw):
        fld = f"{field}[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: {fld} must be a mapping")
        for k in ("vendor",):
            if k not in entry:
                raise ConfigError(f"{path}: {fld} missing {k!r}")
        vendor = _normalize_vendor_value(entry["vendor"], path=path, field=f"{fld}.vendor")
        if vendor not in allowed_vendors:
            raise ConfigError(f"{path}: {fld}.vendor {vendor!r} not in {allowed_vendors}")
        model = _normalize_model_value(
            entry.get("model"), vendor=vendor, path=path, field=f"{fld}.model"
        )
        effort = _normalize_effort_value(entry.get("effort", ""), path=path, field=f"{fld}.effort")
        min_q = _normalize_min_quota(entry.get("min_quota_pct"), path=path, field=f"{fld}.min_quota_pct")
        flags_raw = entry.get("flags", [])
        if not isinstance(flags_raw, list):
            raise ConfigError(f"{path}: {fld}.flags must be a list")
        if flags_raw and not allow_flags:
            raise ConfigError(f"{path}: {fld} does not support `flags` for this role")
        for f in flags_raw:
            if not isinstance(f, str):
                raise ConfigError(f"{path}: {fld}.flags entries must be strings")
        if effort and _flags_define_effort(flags_raw):
            raise ConfigError(f"{path}: {fld} cannot set both `effort` and flags `--effort`")
        flags_tuple = tuple(flags_raw)
        if allow_flags:
            validate_flags(vendor=vendor, stage=flags_label, flags=flags_tuple)
        if forbid_provider is not None:
            eff = _infer_cursor_underlying_vendor(model) if vendor == "cursor" else vendor
            if eff == forbid_provider:
                raise ConfigError(
                    f"{path}: {fld} resolves to provider {eff!r}, same as its reviewer "
                    f"primary; a reviewer fallback must use a different provider"
                )
        out.append(
            FallbackSpec(
                vendor=vendor, model=model, effort=effort, min_quota_pct=min_q, flags=flags_tuple
            )
        )
    return out


def _validate_raw(raw: Any, path: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")
    stages = raw.get("stages")
    if not isinstance(stages, dict):
        raise ConfigError(f"{path}: missing/malformed `stages` top-level key")
    missing = [s for s in STAGES if s not in stages]
    if missing:
        raise ConfigError(f"{path}: `stages` missing {missing}")
    for s in STAGES:
        entry = stages[s]
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: stages.{s} must be a mapping")
        for k in ("vendor", "model"):
            if k not in entry:
                raise ConfigError(f"{path}: stages.{s} missing `{k}`")
        entry["vendor"] = _normalize_vendor_value(
            entry["vendor"], path=path, field=f"stages.{s}.vendor"
        )
        if entry["vendor"] not in ALLOWED_VENDORS:
            raise ConfigError(
                f"{path}: stages.{s}.vendor = {entry['vendor']!r} not in {ALLOWED_VENDORS} "
                f"(agy is panel-review-only in v2)"
            )
        if not isinstance(entry["model"], str) or not entry["model"].strip():
            raise ConfigError(f"{path}: stages.{s}.model must be non-empty string")
        if "timeout_sec" in entry:
            # Auto-migrate legacy key: move timeout_sec → probe_interval_sec
            # if the new key isn't already set. Emit a deprecation warning.
            if "probe_interval_sec" not in entry:
                entry["probe_interval_sec"] = entry["timeout_sec"]
            _deprecation_warn(path, f"stages.{s}.timeout_sec", f"stages.{s}.probe_interval_sec")
            del entry["timeout_sec"]
        if "probe_interval_sec" in entry and not isinstance(entry["probe_interval_sec"], int):
            raise ConfigError(f"{path}: stages.{s}.probe_interval_sec must be int")
        if "effort" in entry:
            entry["effort"] = _normalize_effort_value(
                entry["effort"], path=path, field=f"stages.{s}.effort"
            )
        if "flags" in entry:
            if not isinstance(entry["flags"], list):
                raise ConfigError(f"{path}: stages.{s}.flags must be a list")
            for f in entry["flags"]:
                if not isinstance(f, str):
                    raise ConfigError(f"{path}: stages.{s}.flags entries must be strings")
        if entry.get("effort") and _flags_define_effort(entry.get("flags", [])):
            raise ConfigError(
                f"{path}: stages.{s} cannot set both `effort` and flags `--effort`"
            )
    return stages


def _parse_panel(raw: Any, path: Path) -> PanelConfig:
    """Validate required `panel:` top-level block in vendors.yml."""
    if raw is None:
        raise ConfigError(f"{path}: missing required top-level `panel` block")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: `panel` must be a mapping")

    reviewers_raw = raw.get("reviewers")
    if not isinstance(reviewers_raw, list) or not reviewers_raw:
        raise ConfigError(f"{path}: panel.reviewers must be a non-empty list")
    rs: list[PanelReviewerSpec] = []
    # Diversity is enforced on the *effective* provider. For non-cursor vendors
    # that's the vendor name; for cursor it's the underlying provider inferred
    # from the model id (so two cursor entries proxying different backends are
    # allowed, and a cursor entry proxying e.g. claude collides with a native
    # claude entry).
    seen_providers: dict[str, tuple[str, str]] = {}
    for i, entry in enumerate(reviewers_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: panel.reviewers[{i}] must be a mapping")
        for k in ("vendor",):
            if k not in entry:
                raise ConfigError(f"{path}: panel.reviewers[{i}] missing {k!r}")
        v = _normalize_vendor_value(
            entry["vendor"], path=path, field=f"panel.reviewers[{i}].vendor"
        )
        if v not in PANEL_REVIEWER_VENDORS:
            raise ConfigError(
                f"{path}: panel.reviewers[{i}].vendor {v!r} not in {PANEL_REVIEWER_VENDORS}"
            )
        model = _normalize_model_value(
            entry.get("model"),
            vendor=v,
            path=path,
            field=f"panel.reviewers[{i}].model",
        )
        effective_provider = (
            _infer_cursor_underlying_vendor(model) if v == "cursor" else v
        )
        if effective_provider in seen_providers:
            prev_vendor, prev_model = seen_providers[effective_provider]
            raise ConfigError(
                f"{path}: panel.reviewers entry vendor={v!r} model={model!r} "
                f"collides with earlier entry vendor={prev_vendor!r} model={prev_model!r} "
                f"(both resolve to provider {effective_provider!r}; "
                f"panel reviewers must use distinct underlying providers)"
            )
        seen_providers[effective_provider] = (v, model)
        effort = _normalize_effort_value(
            entry.get("effort", ""), path=path, field=f"panel.reviewers[{i}].effort"
        )
        min_q = _normalize_min_quota(
            entry.get("min_quota_pct"), path=path, field=f"panel.reviewers[{i}].min_quota_pct"
        )
        fbs = _parse_fallbacks(
            entry.get("fallbacks"),
            path=path,
            field=f"panel.reviewers[{i}].fallbacks",
            allowed_vendors=PANEL_REVIEWER_VENDORS,
            flags_label=f"reviewer:{v}",
            forbid_provider=effective_provider,
            allow_flags=False,
        )
        rs.append(
            PanelReviewerSpec(
                vendor=v, model=model, effort=effort, min_quota_pct=min_q, fallbacks=fbs
            )
        )
    reviewers = tuple(rs)

    synth_raw = raw.get("synthesizer")
    if not isinstance(synth_raw, dict):
        raise ConfigError(f"{path}: panel.synthesizer must be a mapping")
    for k in ("vendor", "model"):
        if k not in synth_raw:
            raise ConfigError(f"{path}: panel.synthesizer missing {k!r}")
    synth_vendor = _normalize_vendor_value(
        synth_raw["vendor"], path=path, field="panel.synthesizer.vendor"
    )
    if synth_vendor not in PANEL_SYNTHESIZER_VENDORS:
        raise ConfigError(
            f"{path}: panel.synthesizer.vendor {synth_raw['vendor']!r} "
            f"not in {PANEL_SYNTHESIZER_VENDORS} "
            f"(native JSON-schema output required)"
        )
    synth_model = synth_raw["model"]
    if not isinstance(synth_model, str) or not synth_model.strip():
        raise ConfigError(f"{path}: panel.synthesizer.model must be non-empty string")
    synth_effort = _normalize_effort_value(
        synth_raw.get("effort", ""), path=path, field="panel.synthesizer.effort"
    )
    synth_min_q = _normalize_min_quota(
        synth_raw.get("min_quota_pct"), path=path, field="panel.synthesizer.min_quota_pct"
    )
    synth_fbs = _parse_fallbacks(
        synth_raw.get("fallbacks"),
        path=path,
        field="panel.synthesizer.fallbacks",
        allowed_vendors=PANEL_SYNTHESIZER_VENDORS,
        flags_label="synthesizer",
        allow_flags=False,
    )
    synthesizer = PanelSynthesizerSpec(
        vendor=synth_vendor,
        model=synth_model,
        effort=synth_effort,
        min_quota_pct=synth_min_q,
        fallbacks=synth_fbs,
    )

    def _int_field(key: str, default: int) -> int:
        val = raw.get(key, default)
        if not isinstance(val, int):
            raise ConfigError(f"{path}: panel.{key} must be int")
        return val

    for old, new in (
        ("reviewer_timeout_sec", "reviewer_probe_interval_sec"),
        ("synthesizer_timeout_sec", "synthesizer_probe_interval_sec"),
    ):
        if old in raw:
            if new not in raw:
                raw[new] = raw[old]
            _deprecation_warn(path, f"panel.{old}", f"panel.{new}")
            del raw[old]

    return PanelConfig(
        reviewers=reviewers,
        synthesizer=synthesizer,
        reviewer_probe_interval_sec=_int_field(
            "reviewer_probe_interval_sec", DEFAULT_PANEL_REVIEWER_TIMEOUT_SEC
        ),
        synthesizer_probe_interval_sec=_int_field(
            "synthesizer_probe_interval_sec", DEFAULT_PANEL_SYNTHESIZER_TIMEOUT_SEC
        ),
    )


def _parse_probe(raw: Any, path: Path) -> ProbeConfig:
    """Validate required `probe:` top-level block in vendors.yml."""
    if raw is None:
        raise ConfigError(f"{path}: missing required top-level `probe` block")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: `probe` must be a mapping")
    for k in ("vendor", "model"):
        if k not in raw:
            raise ConfigError(f"{path}: probe missing {k!r}")
    vendor = _normalize_vendor_value(raw["vendor"], path=path, field="probe.vendor")
    if vendor not in PROBE_VENDORS:
        raise ConfigError(f"{path}: probe.vendor {vendor!r} not in {PROBE_VENDORS}")
    model = raw["model"]
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{path}: probe.model must be non-empty string")
    timeout_sec = raw.get("timeout_sec", DEFAULT_PROBE_TIMEOUT_SEC)
    if not isinstance(timeout_sec, int):
        raise ConfigError(f"{path}: probe.timeout_sec must be int")
    effort = _normalize_effort_value(raw.get("effort", ""), path=path, field="probe.effort")
    flags_raw = raw.get("flags", [])
    if not isinstance(flags_raw, list):
        raise ConfigError(f"{path}: probe.flags must be a list")
    flags: list[str] = []
    for f in flags_raw:
        if not isinstance(f, str):
            raise ConfigError(f"{path}: probe.flags entries must be strings")
        flags.append(f)
    if effort and _flags_define_effort(flags):
        raise ConfigError(f"{path}: probe cannot set both `effort` and flags `--effort`")
    min_q = _normalize_min_quota(raw.get("min_quota_pct"), path=path, field="probe.min_quota_pct")
    fbs = _parse_fallbacks(
        raw.get("fallbacks"),
        path=path,
        field="probe.fallbacks",
        allowed_vendors=PROBE_VENDORS,
        flags_label="probe",
    )
    return ProbeConfig(
        vendor=vendor,
        model=model,
        timeout_sec=timeout_sec,
        effort=effort,
        flags=tuple(flags),
        min_quota_pct=min_q,
        fallbacks=fbs,
    )


def load_vendors_config(path: Path) -> VendorsConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"vendors.yml not found at {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    stages_raw = _validate_raw(raw, p)

    # Lazy import to avoid circular: allowlist also references vendors.config
    from autodev.vendors.allowlist import validate_flags

    stages: dict[str, StageSpec] = {}
    for stage_name, entry in stages_raw.items():
        flags_tuple = tuple(entry.get("flags", []))
        # Validate per-vendor allowlist at load time (R3 / Codex R4 blocker #1 fix).
        validate_flags(vendor=entry["vendor"], stage=stage_name, flags=flags_tuple)
        stages[stage_name] = StageSpec(
            stage=stage_name,
            vendor=entry["vendor"],
            model=entry["model"],
            probe_interval_sec=entry.get("probe_interval_sec", DEFAULT_TIMEOUT_SEC),
            effort=entry.get("effort", ""),
            flags=flags_tuple,
            min_quota_pct=_normalize_min_quota(
                entry.get("min_quota_pct"), path=p, field=f"stages.{stage_name}.min_quota_pct"
            ),
            fallbacks=_parse_fallbacks(
                entry.get("fallbacks"),
                path=p,
                field=f"stages.{stage_name}.fallbacks",
                allowed_vendors=ALLOWED_VENDORS,
                flags_label=stage_name,
            ),
        )

    panel = _parse_panel(raw.get("panel") if isinstance(raw, dict) else None, p)
    probe = _parse_probe(raw.get("probe") if isinstance(raw, dict) else None, p)
    validate_flags(vendor=probe.vendor, stage="probe", flags=probe.flags)
    return VendorsConfig(path=p, stages=stages, panel=panel, probe=probe)
