"""vendors.yml loader + validator (v2 schema)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from autodev.errors import ConfigError

# Coding stages — vendors.yml must cover all.
STAGES = ("design", "build", "spec", "review")

# Vendors allowed for coding (R3: gemini excluded; panel-review handles gemini separately).
# `openai` and `codex` both route through the Codex CLI in shared/vendors.
ALLOWED_VENDORS = {"claude", "codex", "openai"}

# Panel review vendors (gemini welcomed as a reviewer).
PANEL_REVIEWER_VENDORS = {"claude", "codex", "openai", "gemini"}
# Synthesizer requires native JSON-schema output (currently only claude).
PANEL_SYNTHESIZER_VENDORS = {"claude"}
# The idle probe is a short, read-only LLM call. Keep it on coding-capable
# CLIs that accept prompt-on-stdin in headless mode.
PROBE_VENDORS = {"claude", "codex", "openai"}

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


@dataclass(frozen=True)
class PanelReviewerSpec:
    vendor: str
    model: str
    effort: str = ""


@dataclass(frozen=True)
class PanelSynthesizerSpec:
    vendor: str
    model: str
    effort: str = ""


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
                f"(gemini is panel-review-only in v2)"
            )
        if not isinstance(entry["model"], str) or not entry["model"].strip():
            raise ConfigError(f"{path}: stages.{s}.model must be non-empty string")
        if "probe_interval_sec" in entry and not isinstance(entry["probe_interval_sec"], int):
            raise ConfigError(f"{path}: stages.{s}.probe_interval_sec must be int")
        if "timeout_sec" in entry:
            raise ConfigError(
                f"{path}: stages.{s}.timeout_sec is deprecated; rename to "
                f"`probe_interval_sec` (idle threshold for the probe). The "
                f"hard wall-clock cap is derived in subprocess_runner."
            )
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
    seen: set[str] = set()
    for i, entry in enumerate(reviewers_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: panel.reviewers[{i}] must be a mapping")
        for k in ("vendor", "model"):
            if k not in entry:
                raise ConfigError(f"{path}: panel.reviewers[{i}] missing {k!r}")
        v = _normalize_vendor_value(
            entry["vendor"], path=path, field=f"panel.reviewers[{i}].vendor"
        )
        if v not in PANEL_REVIEWER_VENDORS:
            raise ConfigError(
                f"{path}: panel.reviewers[{i}].vendor {v!r} not in {PANEL_REVIEWER_VENDORS}"
            )
        if v in seen:
            raise ConfigError(f"{path}: panel.reviewers vendor {v!r} listed twice")
        seen.add(v)
        model = entry["model"]
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"{path}: panel.reviewers[{i}].model must be non-empty string")
        effort = _normalize_effort_value(
            entry.get("effort", ""), path=path, field=f"panel.reviewers[{i}].effort"
        )
        rs.append(PanelReviewerSpec(vendor=v, model=model, effort=effort))
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
    synthesizer = PanelSynthesizerSpec(
        vendor=synth_vendor, model=synth_model, effort=synth_effort
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
            raise ConfigError(
                f"{path}: panel.{old} is deprecated; rename to "
                f"`{new}` (idle threshold for the probe). The hard "
                f"wall-clock cap is derived in panel/runner."
            )

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
    return ProbeConfig(
        vendor=vendor,
        model=model,
        timeout_sec=timeout_sec,
        effort=effort,
        flags=tuple(flags),
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
        )

    panel = _parse_panel(raw.get("panel") if isinstance(raw, dict) else None, p)
    probe = _parse_probe(raw.get("probe") if isinstance(raw, dict) else None, p)
    validate_flags(vendor=probe.vendor, stage="probe", flags=probe.flags)
    return VendorsConfig(path=p, stages=stages, panel=panel, probe=probe)
