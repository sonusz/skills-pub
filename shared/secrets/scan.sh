#!/usr/bin/env bash
# scan.sh -- report where the secret denylist (patterns.pl) matches, without
# ever printing the matched text.
#
# Usage:
#   scan.sh [FILE...]              scan files (no args or `-` = stdin)
#   scan.sh --diff [FILE...]       scan unified diff(s): only ADDED lines are
#                                  checked; the path comes from the `+++ b/`
#                                  header and the line number is the new-file
#                                  line number
#   scan.sh -h | --help
#
# Output: one `path:line:pattern-name` per hit on stdout (path is `-` for
# stdin; a line hit by several patterns is reported once per pattern). The
# matched secret is never printed, and a path that is itself secret-shaped
# (a file named after a token) is printed through the same denylist.
# Diagnostics go to stderr.
#
# Exit codes:
#   0  clean
#   1  at least one hit
#   2  usage error, perl missing, or an input file could not be read
#
# Binary files (git's heuristic: NUL byte / mostly non-text in the first
# block) are skipped with a note on stderr. In --diff mode git's "Binary files
# differ" stanzas contain no added lines and are skipped naturally.
#
# The denylist is high-confidence, not complete: a clean scan means "nothing
# recognisable", not "no secrets". See patterns.pl and README.md.

set -uo pipefail

SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

MODE=plain
FILES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --diff) MODE=diff; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; while [ $# -gt 0 ]; do FILES+=("$1"); shift; done ;;
    -) FILES+=("-"); shift ;;
    -*) echo "scan.sh: unknown option: $1" >&2; usage; exit 2 ;;
    *) FILES+=("$1"); shift ;;
  esac
done
[ "${#FILES[@]}" -eq 0 ] && FILES=("-")

if ! command -v perl >/dev/null 2>&1; then
  echo "scan.sh: perl is required" >&2
  exit 2
fi

PROG='
  use strict;
  use warnings;

  my ($pfile, $mode, @files) = @ARGV;
  my $patterns = do $pfile;
  die "scan.sh: cannot load $pfile: " . ($@ || $! || "not an array ref") . "\n"
    unless ref $patterns eq "ARRAY";

  my $hits = 0;
  my $errors = 0;

  # A path can itself be secret-shaped (a file named after a token), so every
  # printed path goes through the same denylist as the content.
  sub redact_path {
    my ($path) = @_;
    for my $p (@$patterns) {
      my ($name, $re, $rep) = @$p;
      $path =~ s/$re/ ref $rep eq "CODE" ? $rep->() : $rep /ge;
    }
    return $path;
  }

  sub scan_line {
    my ($path, $n, $line) = @_;
    for my $p (@$patterns) {
      my ($name, $re) = @$p;
      if ($line =~ $re) {
        print "$path:$n:$name\n";
        $hits++;
      }
    }
  }

  # Plain text: every line. Stops at the first NUL byte (binary content).
  sub scan_plain {
    my ($fh, $path) = @_;
    my $n = 0;
    while (my $line = <$fh>) {
      $n++;
      if (index($line, "\0") >= 0) {
        print STDERR "scan.sh: skipping binary input: $path\n";
        last;
      }
      chomp $line;
      scan_line($path, $n, $line);
    }
  }

  # Unified diff: added lines only, addressed by new-file path and line.
  # Hunk extents come from the @@ header so `+++`/`---` inside a hunk (an
  # added line whose content starts with `++`) are never mistaken for headers.
  sub scan_diff {
    my ($fh, $fallback) = @_;
    my $path = $fallback;
    my ($in_hunk, $old_left, $new_left, $new_line) = (0, 0, 0, 0);
    while (my $line = <$fh>) {
      chomp $line;
      $line =~ s/\r$//;
      if ($in_hunk) {
        if ($line =~ /^\+/) {
          scan_line($path, $new_line, substr($line, 1));
          $new_line++; $new_left--;
        } elsif ($line =~ /^-/) {
          $old_left--;
        } elsif ($line =~ /^\\/) {
          # "\ No newline at end of file" -- not a content line
        } else {
          $new_line++; $new_left--; $old_left--;
        }
        $in_hunk = 0 if $old_left <= 0 && $new_left <= 0;
        next;
      }
      if ($line =~ /^\+\+\+ (.*)$/) {
        my $p = $1;
        $p =~ s/\t.*$//;
        $p =~ s/^"(.*)"$/$1/;
        $p =~ s{^b/}{};
        $path = $p eq "/dev/null" ? $fallback : redact_path($p);
      } elsif ($line =~ /^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/) {
        $old_left = defined $1 ? $1 : 1;
        $new_line = $2;
        $new_left = defined $3 ? $3 : 1;
        $in_hunk = ($old_left + $new_left) > 0;
      }
    }
  }

  for my $f (@files) {
    my $fh;
    my $path = $f;
    if ($f eq "-") {
      $fh = \*STDIN;
    } else {
      if (!-e $f) {
        print STDERR "scan.sh: no such file: $f\n"; $errors++; next;
      }
      if (-d $f) {
        print STDERR "scan.sh: is a directory (pass files, not directories): $f\n"; $errors++; next;
      }
      if (-s $f && -B $f) {
        print STDERR "scan.sh: skipping binary file: $f\n"; next;
      }
      unless (open $fh, "<", $f) {
        print STDERR "scan.sh: cannot read $f: $!\n"; $errors++; next;
      }
    }
    my $shown = redact_path($path);
    if ($mode eq "diff") { scan_diff($fh, $shown) } else { scan_plain($fh, $shown) }
    close $fh unless $f eq "-";
  }

  exit 2 if $errors;
  exit($hits ? 1 : 0);
'

exec perl -e "$PROG" "$SECRETS_DIR/patterns.pl" "$MODE" "${FILES[@]}"
