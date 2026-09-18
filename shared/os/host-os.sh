#!/usr/bin/env bash
# host-os.sh — the one definition of host-OS detection for shell. SOURCED, not
# executed. It only defines `host_os` (no `set` options, no variables), so it is
# safe to source into hooks and interactive shells.
#
# host_os -> darwin | linux | other. Detect the OS, then run that OS's command;
# never launch one platform's form and sniff its error to try the other, and
# never probe for /proc. `other` branches keep whatever the caller did before
# detection so unknown platforms are no worse off.
#
# Consumers locate this file through their skill's `shared/os` link
# (skills/<skill>/shared/os -> ../../../shared/os) and keep an identical inline
# copy as the fallback for a materialized install shipped without shared/os:
#   shared/vendors/scripts/vendor-launch.sh   (vendors_host_os)
# The Python twin is hostos.py in this directory.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  printf "host-os.sh is a library; source it.\n" >&2
  exit 2
fi

host_os() {
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf 'darwin\n' ;;
    Linux)  printf 'linux\n' ;;
    *)      printf 'other\n' ;;
  esac
}
