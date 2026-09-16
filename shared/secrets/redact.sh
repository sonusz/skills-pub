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

  # Block patterns (a PEM private key). Multi-line block mode is entered only
  # for a real header: the BEGIN marker at the end of its line, allowing
  # trailing whitespace, quotes, or a literal \n / \r escape
  # (`key: "-----BEGIN ...-----`, `"-----BEGIN ...-----\n`). Any other text
  # after the marker is same-line content and never opens a block.
  my $HEADER_TAIL = qr/^(?:\s|["\x27`]|\\[nr])*$/;
  # A block collapses at most this many body lines. A stray header in a log
  # (an ssh error quoting the marker at end of line) must never swallow the
  # rest of the stream when no END line comes.
  my $MAX_BLOCK_LINES = 128;

  my $block_end;    # END regex while inside a block; undef in normal mode
  my $block_rep;    # replacement text for the block body
  my $block_lines;  # body lines collapsed so far

  while (my $line = <STDIN>) {
    if ($block_end) {
      if ($line =~ $block_end) {
        print "$block_rep\n" unless $block_lines;
        $line =~ s/^.*?(?=$block_end)//;  # key bytes may precede the END marker
        undef $block_end;                 # the rest of the line is normal text
      } elsif (++$block_lines > $MAX_BLOCK_LINES) {
        undef $block_end;                 # cap reached: resume normal mode here
      } else {
        print "$block_rep\n" if $block_lines == 1;
        next;
      }
    }

    for my $p (@$patterns) {
      my ($name, $re, $rep, $opt) = @$p;
      my $end = $opt ? $opt->{block_end} : undef;
      unless ($end) {
        $line =~ s/$re/ ref $rep eq "CODE" ? $rep->() : $rep /ge;
        next;
      }

      my $r = ref $rep eq "CODE" ? $rep->() : $rep;
      # BEGIN ... END on one line (GCP JSON, a .env one-liner with \n-escaped
      # PEM): redact the span between the markers; stay in normal mode.
      $line =~ s/(?<open>$re)(?<body>.*?)(?<end>$end)/$+{open} . $r . $+{end}/ge;
      # Only an opener with no END after it on this line is left to handle;
      # the pairs above keep their BEGIN marker, so look past those.
      next unless $line =~ /(?<open>$re)(?<tail>(?:(?!$end).)*)$/;
      if ($+{tail} =~ $HEADER_TAIL) {
        # A real PEM header: the following body lines collapse to one line.
        $block_end = $end;
        $block_rep = $r;
        $block_lines = 0;
      } else {
        # Marker followed by other text and no END on this line (a log line
        # quoting the header, a key dumped on one line): redact to end of
        # line only; no block state.
        $line =~ s/(?<open>$re)(?:(?!$end).)*$/$+{open} . $r/e;
      }
    }
    print $line;
  }
' "$SECRETS_DIR/patterns.pl"
