"""Markdown artifact helpers — HTML-comment hash provenance."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from auto_dev.state.atomic import atomic_write
from auto_dev.state.hashing import parse_markdown_source_hash


def markdown_header(source: str, source_hash: str, written: str | None = None) -> str:
    written = written or date.today().isoformat()
    return (
        f"<!-- source: {source} -->\n"
        f"<!-- source_hash: {source_hash} -->\n"
        f"<!-- written: {written} -->\n\n"
    )


def write_markdown_with_hash(
    path: Path,
    body: str,
    *,
    source: str,
    source_hash: str,
    written: str | None = None,
) -> None:
    content = markdown_header(source, source_hash, written) + body
    atomic_write(path, content)


def read_markdown_with_hash(path: Path) -> tuple[str, str | None]:
    text = Path(path).read_text(encoding="utf-8")
    return text, parse_markdown_source_hash(Path(path))
