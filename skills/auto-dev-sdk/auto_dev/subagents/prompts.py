"""Load prompt templates bundled with the package."""
from __future__ import annotations

from importlib import resources
from pathlib import Path


def load(name: str) -> str:
    """Return prompt text for `name` (e.g., 'plan', 'implement')."""
    # Support both installed-package and in-tree layouts.
    pkg_dir = Path(__file__).resolve().parent.parent / "prompts"
    p = pkg_dir / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    # Package-resource fallback for installed wheel.
    try:
        return resources.files("auto_dev.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as e:
        raise FileNotFoundError(f"prompt template not found: {name}") from e
