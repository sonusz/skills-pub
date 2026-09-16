# patterns.pl -- the secret denylist shared by redact.sh and scan.sh.
#
# Evaluates to an array ref of [ name, qr/regex/, replacement, (options) ]:
#
#   name         short kebab-case id; scan.sh prints it, never the match
#   regex        applied per line (no ^/$ anchors needed; do not rely on
#                multi-line matching -- both scripts process one line at a time)
#   replacement  a literal string, or a code ref evaluated inside s///e that may
#                use $1.. captures (e.g. sub { "$1<redacted>" }); redact.sh uses
#                it, scan.sh ignores it
#   options      optional hash ref. block_end => qr/.../ marks a block pattern
#                (a PEM private key): regex matches the BEGIN marker, block_end
#                the END marker, and replacement is the text that stands in for
#                the key material between them (a string, or a code ref called
#                without captures). redact.sh keeps both markers: a BEGIN...END
#                pair on one line is redacted between the markers; a BEGIN
#                marker at the end of its line (trailing quotes / a literal \n
#                allowed -- a real PEM header) opens a multi-line block whose
#                body collapses to one replacement line until the END line
#                (printed from the END marker onward) or for at most 128 body
#                lines; a BEGIN marker followed by other text and no END is
#                redacted to the end of that line only. scan.sh reports only
#                the BEGIN line.
#
# This is a DENYLIST OF HIGH-CONFIDENCE SHAPES, NOT A GUARANTEE. A secret in a
# novel format, a base64 blob, a password that looks like a word, or a token
# split across lines passes through untouched. Callers still own judgement
# about what they paste into prompts, tool results and logs.
#
# Order matters for redact.sh: specific token shapes first, the generic
# assignment pattern last so it sees already-redacted values only.
#
# Load with:   my $patterns = do "/path/to/patterns.pl";
# Check with:  perl -c patterns.pl

use strict;
use warnings;

my $R = '<redacted>';

my @patterns = (

  # ---- carried over verbatim from skills/auto-fix/scripts/redact-secrets.sh ----

  # AWS access key id / session-token key id
  [ 'aws-access-key',   qr/\bAKIA[0-9A-Z]{16}\b/, "AKIA$R" ],
  [ 'aws-session-key',  qr/\bASIA[0-9A-Z]{16}\b/, "ASIA$R" ],

  # GitHub PATs (ghp_, gho_, ghu_, ghs_, ghr_)
  [ 'github-pat',       qr/\bgh[pousr]_[A-Za-z0-9]{16,}\b/, "gh$R" ],

  # JWTs (header.payload.signature, all base64url)
  [ 'jwt',              qr/\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b/, "eyJ$R.$R.$R" ],

  # Slack tokens (xoxb-/xoxp-/xoxa-/xoxr-/xoxs-)
  [ 'slack-token',      qr/\bxox[baprs]-[A-Za-z0-9-]{10,}\b/, "xox$R" ],

  # Authorization headers (full `Authorization: Scheme value`)
  [ 'authorization-header', qr/(Authorization:\s*)\S+\s+\S+/i, sub { "$1$R" } ],

  # Bearer/Basic tokens not preceded by an Authorization header that already matched
  [ 'bearer-basic-token', qr/\b(Bearer|Basic)\s+[A-Za-z0-9._\-=+\/]{16,}/i, sub { "$1 $R" } ],

  # URIs with embedded credentials: scheme://user:pass@host -> scheme://user:<redacted>@host
  [ 'uri-credentials',  qr|(://[^:/\s@]+):[^@\s]+@|, sub { "$1:$R@" } ],

  # ---- additions ----

  # PEM private key blocks (RSA, EC, OPENSSH, DSA, PKCS#8, encrypted PKCS#8, PGP).
  # Block pattern (see the options note above): both markers are kept, the key
  # material between them becomes <redacted>.
  [ 'private-key-block',
    qr/-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----/,
    $R,
    { block_end => qr/-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----/ } ],

  # OpenAI / Anthropic style keys: sk-..., sk-proj-..., sk-ant-... (prefix kept)
  [ 'openai-anthropic-key', qr/\bsk-((?:ant-|proj-)?)[A-Za-z0-9_\-]{20,}/, sub { "sk-$1$R" } ],

  # Google API key
  [ 'google-api-key',   qr/\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])/, "AIza$R" ],

  # xAI API key
  [ 'xai-key',          qr/\bxai-[A-Za-z0-9]{20,}/, "xai-$R" ],

  # GitLab personal access token
  [ 'gitlab-pat',       qr/\bglpat-[A-Za-z0-9_\-]{20,}/, "glpat-$R" ],

  # Generic `<name> = <value>` / `<name>: <value>` assignments where <name> is
  # api_key / api-key / apikey / secret / secret_key / access_key / token /
  # password / passwd (any case, any prefix such as client_secret or
  # "access_token"). The name, separator and opening quote are kept; only the
  # value is replaced. To stay high-confidence the value must be either
  #   * quoted: 8+ non-quote, non-space chars, or
  #   * bare:   8+ chars of [A-Za-z0-9_/+=-] forming the whole token
  # and must not start with $ or < (env references such as ${TOKEN}, and
  # placeholders such as <your-token> / <redacted>). Dotted or bracketed
  # expressions (config.token, os.environ["TOKEN"], get_token()) do not match.
  [ 'secret-assignment',
    qr/((?:api[_-]?key|(?:secret|access)[_-]?key|secret|token|password|passwd)['"]?\s*[:=]\s*)(?:(['"])(?![\$<])[^'"\s]{8,}\2|(?![\$<])[A-Za-z0-9_\/+=\-]{8,}(?=[\s,;)\]}'"]|$))/i,
    sub { defined $2 ? "$1$2$R$2" : "$1$R" } ],
);

return \@patterns;
