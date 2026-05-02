# PRD: hash-file-cli

## 1. Problem

Users auditing auto-dev artifact hashes need a one-shot tool that
computes the exact on-disk SHA-256 of a file and prints it in the
`sha256:<lowercase-64-hex>` form that auto-dev embeds in artifact
headers. Shell pipelines like `sha256sum | awk` format differently and,
when used with `echo` (which appends a newline), hash a surprising set
of bytes — the resulting confusion is what this CLI removes. The tool
is also the reference feature for `auto-dev implement` end-to-end
validation (see §2, ad-17).

**Definition of "auto-dev hashing invariant"** (anchored here so the
PRD is self-contained): the hash is computed over the byte stream
`open(path, "rb").read()` yields, read in chunks until EOF, with no
byte normalization (no EOL conversion, no BOM stripping, no
whitespace trimming). Changing a single byte — including adding or
removing a trailing newline — changes the hash. The target inputs
are **regular files and EOF-terminating stream specials** (e.g.
`/dev/null`, empty FIFOs with a writer that closed); unbounded
streams like `/dev/zero` or a live FIFO are explicitly **out of
scope** and behavior on them is undefined. This matches
`autodev.state.hashing.hash_file` in the parent repo and
`sha256sum <file>` on a regular file, but not
`echo text | sha256sum` (which hashes `text\n`).

## 2. Users

- Anyone auditing auto-dev artifacts manually.
- CI / scripts wanting a one-liner hash check on POSIX-like systems.
- The auto-dev-sdk acceptance test (ad-17) — this PRD is the canonical
  reference feature that proves `auto-dev implement` works end-to-end.

## 3. Requirements

- **R1** Read a file path from `argv[1]`; print
  `sha256:<64-lowercase-hex>` to stdout followed by a single `\n`.
  Exactly one line. Nothing else on stdout on success. Extra
  positional arguments beyond `argv[1]` are silently ignored; this
  is a required behavior (not just a default), so an implementation
  that rejects extra args with a usage error does NOT satisfy R1.
- **R2** Exit code + stderr message for each error branch,
  specified as *observable behavior* rather than Python exception
  class. In every error case, stdout MUST be empty (no partial
  hash). Exception classes named below are hints for
  Python implementers, not part of the contract:
  - **Missing argument** (no `argv[1]`): exit 2; stderr contains
    `usage`.
  - **Path does not resolve to an existing filesystem entry**
    (`FileNotFoundError`-category): exit 1; stderr contains the
    path and the substring `does not exist`.
  - **Path resolves to a directory** (`IsADirectoryError`-category;
    detected via `os.path.isdir(path)` before `open`, or equivalent):
    exit 1; stderr contains the path and the substring
    `is a directory`.
  - **Path cannot be opened or read** (all other `OSError`
    categories raised by `open()` OR during `read()` in the chunked
    loop — including `PermissionError`, transient I/O errors,
    loop-aborting errors on special files): exit 1; stderr contains
    the path and a non-empty reason string (the exception's
    `strerror` is the recommended reason). The same exit
    code + message shape covers open-time and mid-stream failures;
    tests are only mandated at open-time (see R5 mandatory cases),
    but mid-stream behavior is specified here so it is not undefined.
- **R3** No normalization of bytes. Digest = SHA-256 of the exact
  file contents as stored (see §1 for the canonical definition).
- **R4** No third-party dependencies in the shipped script. Python
  stdlib only. The script may use any stdlib module (hashlib, sys,
  pathlib, argparse, os, etc.). `pytest` is used only as the test
  runner and is explicitly a test-time-only dependency (not a
  runtime dep; not installed by the script itself).
- **R5** Ship a unit test file. The mandatory test cases are
  enumerated below; they cover R1's success path, the four open-time
  branches of R2, and the R3 trailing-newline invariant. Mid-stream
  read failures (per R2's last bullet) are specified in R2 but NOT
  required as a test case here — exercising them portably is
  awkward, and the open-time `PermissionError` case validates the
  same exit-code + stderr-shape contract. Expected digests are
  embedded so the test is self-contained:
  - Empty file → `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
  - File containing exactly `hello` (5 bytes) →
    `sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824`
  - File containing exactly `hello\n` (6 bytes) →
    `sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03`
    (MUST differ from the 5-byte case — this is R3 in test form)
  - Missing argument → exit 2, stderr contains `usage`
  - Nonexistent path → exit 1, stderr contains `does not exist`
  - Directory path → exit 1, stderr contains `is a directory`
  - **Unreadable file** (create a file, `os.chmod` it to `0o000`,
    skip the test if running as root since root bypasses POSIX
    perms): exit 1, stderr contains the path and a non-empty
    strerror fragment (`permission` or `denied` or similar — exact
    wording may vary by libc, but the message MUST be non-empty).
- **R6** Single-file implementation (`hash_file_cli.py`) + single
  test file (`test_hash_file_cli.py`). No package structure
  (`__init__.py`), no `pyproject.toml`, no `setup.py`.

## 4. Constraints

- **Runtime**: Python 3.11+ (matches auto-dev-sdk project floor).
- **Dependencies at runtime**: stdlib only.
- **Dependencies at test time**: `pytest` only (the outer project
  already requires it).
- **Streaming**: read the file in 64 KB chunks (`hashlib.sha256`
  `update` calls per chunk). The script MUST work on files larger
  than RAM.
- **Output format**: `sha256:` + 64 lowercase hex chars; no space;
  no uppercase; terminated by a single `\n`.
- **Platform**: POSIX-like filesystems (Linux, macOS). **Windows is
  explicitly out of scope** for both the runtime and the test suite.
  R5's unreadable-file test uses `os.chmod(..., 0o000)` which relies
  on POSIX permission semantics; SC-A references `/dev/null`. Neither
  is expected to work on Windows and neither should be ported. A
  Windows port would be a separate future PRD.

## 5. Success criteria

All four must hold:

- **SC-A** (acceptance smoke, Unix): `python3 hash_file_cli.py /dev/null`
  prints exactly `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n`
  and exits 0.
- **SC-B** (unit tests): `python3 -m pytest test_hash_file_cli.py`
  passes all R5 cases.
- **SC-C** (error surface): `python3 hash_file_cli.py /does/not/exist`
  exits 1 with stderr including `does not exist`;
  `python3 hash_file_cli.py` (no args) exits 2 with stderr including
  `usage`; `python3 hash_file_cli.py /` exits 1 with stderr
  including `is a directory`.
- **SC-D** (R3 invariant): a file containing `hello` and a file
  containing `hello\n` produce different digests — this is
  exercised by R5 and is the load-bearing invariant that justifies
  the tool existing.

## 6. Out of scope

- Directory recursion.
- Alternative digest algorithms (MD5, SHA-1, SHA-512, BLAKE2, etc.).
- Stdin as input source.
- Colored output, progress bars, TTY detection.
- `setup.py` / `pyproject.toml` / pip-installable entry points.
- Checksum file generation, manifest verification, or `sha256sum -c`
  compatibility.
- Windows-specific path handling or acceptance smoke tests.
- Parallel / multi-file hashing.
- Hashing unbounded byte streams (`/dev/zero`, live FIFOs, sockets,
  etc.) — only regular files and EOF-terminating stream specials are
  supported.

## 7. Default assumptions

1. Produced files live directly under `examples/hash-file-cli/` —
   `hash_file_cli.py` and `test_hash_file_cli.py` at that root.
2. The CLI is invoked as `python3 hash_file_cli.py <path>`; no
   console-script entry point needed.
3. The test suite runs under pytest with no `conftest.py` or
   custom fixtures beyond `tmp_path`.
4. R1 mandates silent ignore of extra positional args — this
   assumption restates where to implement it: plain `sys.argv[1]`
   access satisfies R1 for free. Implementers using `argparse`
   must disable its default "error on extra positionals" behavior
   (e.g. `parser.parse_args(sys.argv[1:2])`).
