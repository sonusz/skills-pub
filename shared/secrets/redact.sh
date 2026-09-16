#!/usr/bin/env bash
# redact.sh -- stdin -> stdout with well-known secret shapes replaced by
# `<redacted>` markers.
#
# Same contract as the original skills/auto-fix/scripts/redact-secrets.sh
# (which now delegates here): text in, text out, line by line; perl required
# (exit 1 with a message if it is missing). The denylist lives in patterns.pl
# next to this script -- see that file and README.md for the pattern list.
#
# This is a denylist of high-confidence patterns, not a guarantee of
# completeness. A novel secret format passes through; callers must still avoid
# quoting raw blocks of logs or fetched files verbatim when the surrounding
# context suggests sensitive content. Defense in depth, not silver bullet.
#
# Usage:
#   echo "$evidence" | shared/secrets/redact.sh
#   shared/secrets/redact.sh < /path/to/build.log

set -euo pipefail

SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

if ! command -v perl >/dev/null 2>&1; then
  echo "redact.sh: perl is required" >&2
  exit 1
fi

exec perl -e '
  use strict;
  use warnings;

  my $pfile = shift @ARGV;
  my $patterns = do $pfile;
  die "redact.sh: cannot load $pfile: " . ($@ || $! || "not an array ref") . "\n"
    unless ref $patterns eq "ARRAY";

  my $block_end;     # set while inside a block pattern (e.g. a PEM private key)
  my $body_seen = 0; # whether the single <redacted> body line was already printed

  while (my $line = <STDIN>) {
    if ($block_end) {
      if ($line =~ $block_end) {
        print "<redacted>\n" unless $body_seen;
        print $line;
        undef $block_end;
      } else {
        print "<redacted>\n" unless $body_seen++;
      }
      next;
    }

    for my $p (@$patterns) {
      my ($name, $re, $rep, $opt) = @$p;
      if ($opt && $opt->{block_end} && $line =~ $re) {
        $block_end = $opt->{block_end};
        $body_seen = 0;
      }
      $line =~ s/$re/ ref $rep eq "CODE" ? $rep->() : $rep /ge;
    }
    print $line;
  }
' "$SECRETS_DIR/patterns.pl"
