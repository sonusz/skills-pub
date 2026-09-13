<!--
Stage 2 comprehension-gate prompt (auto-dev-lite).
Spawn as a FRESH general-purpose subagent (never fork), everyday-tier model
(e.g. sonnet), medium effort.

Placeholders:
  <core_doc_content>    — full text of the user's core requirements document
  <detail_doc_content>  — full text of the orchestrator-derived detail document
  <repo_root>           — absolute path of the target repository
-->

You are given two design documents and a repository. Your job is to show how
you would execute them — WITHOUT executing any of it — and to check the two
documents against each other.

This is a dry run. Do not edit, write, or create any file. Do not run any
build, test, or state-changing command. Reading files and searching the
repository is allowed and encouraged for grounding.

Repository root: <repo_root>

**Core document** — the user's requirements, the authority on intent:

---BEGIN CORE DOCUMENT---
<core_doc_content>
---END CORE DOCUMENT---

**Detail document** — a working spec derived from the core document. It is
supposed to elaborate the core document faithfully: everything in it should
be derivable from the core document, adding specifics but never new scope.

---BEGIN DETAIL DOCUMENT---
<detail_doc_content>
---END DETAIL DOCUMENT---

These two documents are your only statement of intent — do not assume any
requirement not written in them.

Reply with:

1. **Execution plan** — the concrete steps you would take, in order: which
   files you would create or modify, what behavior you would implement in
   each, how you would verify it. Be specific enough that someone could
   judge whether your plan matches their intent.
2. **Assumptions** — every point where the documents were silent or
   ambiguous and you had to guess. State the guess you made. If you made no
   guesses, say so.
3. **Divergence** — every place the detail document drifts from the core
   document: contradicts it, silently adds scope it never asked for, drops
   a requirement it states, or reinterprets it. Quote both sides. If the
   detail document is a faithful elaboration, say so.
4. **Proportionality** — identify any specific design mechanism that could be
   removed, replaced by an existing suitable mechanism, or simplified while
   preserving every affected core requirement and constraint. Name the
   design location, the removal or alternative, and why the requirements and
   constraints still hold. A mechanism may be redundant even when it traces
   to a requirement. Do not force a finding or search exhaustively; future
   flexibility, stylistic preference, and fewer lines alone are not evidence.
   If no such mechanism is supported by concrete evidence, say so.
5. **Out of scope** — what you would deliberately NOT do, per your reading
   of the documents.

Work at a straightforward, practical level — a clear plan, not an exhaustive
design study.
