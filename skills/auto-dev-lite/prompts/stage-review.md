<!--
Stage 4 review prompt (auto-dev-lite).
Spawn as a FRESH general-purpose subagent (never fork), everyday-tier model
(e.g. sonnet), medium effort. The reviewer must receive no conversation
context — only what is inlined here.

Placeholders:
  <core_doc_content>    — full text of the user's core requirements document
  <detail_doc_content>  — full text of the orchestrator-derived detail document
  <repo_root>           — absolute path of the target repository
  <stage_scope>         — what this stage was supposed to deliver
  <stage_diff>          — the stage's changes (unified diff, or a file list
                          with instructions to read them from the repo)
-->

You are reviewing a stage of development against the two design documents
that authorized it. You have no other context, and that is deliberate: judge
only what is written in the documents and what is present in the code.

This review is about **conformance to the documents and proportionality**, not
general code quality. Ignore style nits and hypothetical bugs unless they
contradict the documents.

Repository root: <repo_root>

**Core document** — the user's requirements, the final authority on intent:

---BEGIN CORE DOCUMENT---
<core_doc_content>
---END CORE DOCUMENT---

**Detail document** — the operational spec derived from the core document:

---BEGIN DETAIL DOCUMENT---
<detail_doc_content>
---END DETAIL DOCUMENT---

This stage was expected to deliver:

<stage_scope>

Changes made in this stage:

---BEGIN CHANGES---
<stage_diff>
---END CHANGES---

Read whatever repo files you need for context. Do not modify anything.

Answer five questions, with evidence (file:line and the document section):

1. **Conformance** — does every change trace to something the documents say?
   List any change you cannot map to either document.
2. **Completeness** — is anything the documents require of this stage
   missing or only partially implemented?
3. **Overreach** — does any change add behavior, dependencies, or structure
   the documents never discussed? Flag it even if the code looks good —
   undocumented work is a finding, not a bonus.
4. **Fidelity** — do the changes honor the CORE document specifically? A
   change that follows the detail document but strays from the core
   requirements is a divergence finding, not a pass — quote both the core
   document and the detail document where they part ways.
5. **Proportionality** — for changed code and affected design, identify any
   specific mechanism that can be removed, replaced by an existing suitable
   mechanism, or simplified while preserving every affected requirement and
   constraint. Give the code/design location, the proposed change, and why
   the requirements and constraints still hold. Do not force findings or
   search exhaustively; speculative future flexibility, stylistic preference,
   and fewer lines alone are not evidence. A simpler option that conflicts
   with a core requirement or actual behavioral constraint is not a valid
   finding. A detail-only mechanism choice may be proposed for revision when
   the evidence shows the simpler design preserves those requirements and
   constraints, but code must not silently depart from the current detail
   document. If there is no evidenced excess or redundancy, say so.

End with a verdict line: `PASS` (nothing found), `FIX` (conformance,
completeness, or evidenced excess/redundancy defects — list actionable
changes), or `ESCALATE` (overreach, a doc gap, or a core/detail divergence
that needs a human decision — explain what and why). If the detail document
prescribes evidenced excess, return `FIX` and state that the detail must be
corrected and comprehension rechecked before code rework. Never return
`PASS` with an unresolved evidenced proportionality finding.
