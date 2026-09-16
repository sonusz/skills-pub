# OS Module

This directory is a reusable module for other skills, not a standalone skill.
It holds the **one definition of host-OS detection** so the mapping from the
running system to `linux` / `darwin` / other is not re-typed per script.

- `host-os.sh` — sourced shell library defining `host_os` → `darwin`, `linux`,
  or `other` (from `uname -s`). Defines the function only: no `set` options,
  no variables, safe to source into hooks and interactive shells.
- `hostos.py` — `host_os()` → `"linux"`, `"darwin"`, or the raw
  `sys.platform` for anything else. Load it by path with `importlib`
  (`python3 shared/os/hostos.py` prints the value).

## The rule

**Detect the OS, then run the OS-specific command.** Never trial-and-error:
do not launch one platform's form of a command and sniff its error to try the
other, and do not probe for `/proc` to guess the platform. Branch on the value
returned here, run that platform's command, and give `other` a deliberate
branch (usually the pre-detection behavior) so unknown platforms are no worse
off.

Typical shell use:

```bash
. "$SKILL_DIR/shared/os/host-os.sh"
case "$(host_os)" in
  darwin) date -r "$epoch" "$fmt" ;;     # BSD userland
  linux)  date -d "@$epoch" "$fmt" ;;    # GNU userland
  *)      date -r "$epoch" "$fmt" 2>/dev/null || date -d "@$epoch" "$fmt" ;;
esac
```

## Consumers and the inline fallback

Skills reach this module through a link, like `shared/vendors`:
`skills/<skill>/shared/os -> ../../../shared/os`. Each consumer sources or
imports the shared definition when that link resolves and otherwise falls
back to an inline copy of the same lines, so a materialized install shipped
without `shared/os` still works. Consumers that can be reached through a
symlink (frugal installs its hooks as symlinks into the checkout) walk the
symlink chain to their physical directory before looking for `../shared/os`
(bash 3.2, no `readlink -f` on macOS).

| Consumer | Function |
|----------|----------|
| `shared/vendors/scripts/vendor-launch.sh` | `vendors_host_os` (delegates to `host_os`) |
| `shared/vendors/scripts/session-state.py` | `_host_os` |
| `skills/auto-dev-sdk/autodev/state/hostos.py` | `_host_os` (via `skills/auto-dev-sdk/shared/os`) |
| `skills/frugal/scripts/frugal-lib.sh` | `host_os` (via `skills/frugal/shared/os`) |
| `skills/aws-poc-deploy/scripts/ssmrun.sh` | `host_os` (via `skills/aws-poc-deploy/shared/os`) |

When installing a skill by copy, materialize `shared/os` the same way as
`shared/vendors` (see the repo README, "Installing a skill"); a symlink install
resolves inside the checkout and needs nothing extra.
