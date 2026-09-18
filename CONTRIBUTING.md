# Contributing

This repository is a **read-only mirror**. Its `main` branch is regenerated
from a private upstream repository by an automated job on every upstream
change, and a ruleset blocks direct pushes, including from the maintainer.

## Pull requests

PRs are welcome. Because `main` is regenerated, a PR is never merged here
with the merge button. Instead the maintainer applies it upstream as a patch:

```bash
curl -sL https://github.com/sonusz/skills-pub/pull/N.patch | git am -3
```

Your authorship (name, email, date) is preserved in the resulting commit. The
PR is then closed with a note pointing at the commit that landed. Expect the
landed commit's hash to differ from your branch.

Keep PRs to the paths that exist here: the six `skills/*/` directories,
`shared/`, and the root files. Changes elsewhere
cannot be mirrored.

For `auto-dev-sdk`, run the suite before opening a PR:

```bash
cd skills/auto-dev-sdk && python3 -m pytest -q
```

## Issues

Bug reports and questions go in Issues. Include the OS, Python version, and
the vendor CLIs involved.
