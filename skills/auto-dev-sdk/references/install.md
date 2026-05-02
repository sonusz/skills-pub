# Installing the auto-dev SDK CLI

Use this only when the skill needs to call `autodev` and `autodev --help` fails or `command -v autodev` finds nothing.

## Principles

- Install the Python package from the skill root, i.e. the directory containing the active `SKILL.md`.
- Do not hardcode machine-specific absolute paths.
- Prefer user-scoped install locations derived from XDG or Python user-base.
- Do not install into a project virtualenv unless the user explicitly asks.
- Respect an active/user-chosen Python: `PYTHON` wins, then `python3`, then `python`; use the same interpreter for every install step.
- Do not create or guess a target repo's `vendors.yml`; copying/editing `vendors.yml.example` is a separate user decision.

## Choose Python

```bash
PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "Python not found: set PYTHON=/path/to/python" >&2
  exit 1
fi
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(
        f"auto-dev-sdk requires Python 3.11+; got "
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
PY
```

This preserves venv compatibility: if the user exports `PYTHON=/path/to/venv/bin/python`, every subsequent command uses that interpreter.

## Locate the SDK root

Use the loaded skill directory as `SDK_DIR`. If the runtime does not expose it,
prefer an explicit `AUTODEV_SDK_DIR`, then the current checkout. If neither is
available, ask the user for the SDK path rather than guessing a home-directory
skill location.

```bash
SDK_DIR=$("$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

env = os.environ.get("AUTODEV_SDK_DIR")
if env:
    p = Path(env).expanduser()
    if p.exists():
        print(p.resolve())
        raise SystemExit(0)

p = Path.cwd()
if (p/"SKILL.md").exists() and (p/"pyproject.toml").exists():
    print(p.resolve())
    raise SystemExit(0)

raise SystemExit("auto-dev skill root not found; ask the user for the SDK path")
PY
)
```

`SDK_DIR` must contain `SKILL.md`, `pyproject.toml`, and
`shared/vendors/scripts/call.sh`. If `shared/vendors` is a symlink, keep it
valid or copy the shared vendors module into that path as part of packaging.

## Choose install paths

```bash
USER_BASE=$("$PYTHON_BIN" -m site --user-base)
DATA_HOME=${XDG_DATA_HOME:-"$USER_BASE/share"}
BIN_HOME=${XDG_BIN_HOME:-"$USER_BASE/bin"}
VENV_DIR=${AUTODEV_SDK_VENV:-"$DATA_HOME/auto-dev-sdk/venv"}
AUTODEV_BIN="$BIN_HOME/autodev"
```

Notes:

- `AUTODEV_SDK_VENV` can override the venv path for unusual environments.
- `BIN_HOME` may not be on `PATH`; if not, call `$AUTODEV_BIN` directly for this session and tell the user what path was used.

## Install or refresh

```bash
test -f "$SDK_DIR/pyproject.toml"
test -x "$SDK_DIR/shared/vendors/scripts/call.sh"
mkdir -p "$(dirname "$VENV_DIR")" "$BIN_HOME"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -U pip
"$VENV_DIR/bin/python" -m pip install -e "$SDK_DIR"
ln -sf "$VENV_DIR/bin/autodev" "$AUTODEV_BIN"
"$AUTODEV_BIN" --help
```

After this, use `autodev` if `command -v autodev` finds it; otherwise use `$AUTODEV_BIN` directly.

## Failure handling

- If `"$PYTHON_BIN" -m venv` is missing, report the missing Python venv support package; do not fall back to global `pip`.
- If symlink creation fails, use `$VENV_DIR/bin/autodev` directly and report the failure.
- If `pip install -e "$SDK_DIR"` fails, surface stderr and stop; do not retry with sudo.
