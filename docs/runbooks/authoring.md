<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Authoring runbooks

> The human-facing companion to the agent-facing `meho.runbook.*`
> template tool descriptions in
> [`backend/src/meho_backplane/mcp/tools/runbooks.py`](../../backend/src/meho_backplane/mcp/tools/runbooks.py).
> The tools teach the agent how to *call* the surface; this doc teaches a
> senior how to *author* a runbook with that agent across one or several
> sessions.
>
> Covers the template lifecycle landed under
> [Initiative #1197 (G12.2)](https://github.com/evoila/meho/issues/1197).
> The run-execution side (how an operator runs a published template) is
> [Initiative #1198 (G12.3)](https://github.com/evoila/meho/issues/1198).

## Why runbooks exist

A runbook is a procedure that used to live in one engineer's head — cert
rotation, host onboarding, vault unseal-after-restart — captured as a
governance-graded artifact MEHO can hand to a less experienced operator and
gate step by step. The point is to move tribal know-how out of Slack threads
and wiki pages and into a versioned, auditable template that records who
authored it, who edited it, and which version each run was pinned to. See
[Goal #1195](https://github.com/evoila/meho/issues/1195) for the framing.

Concrete example: rotating a vCenter certificate is a sequence a senior does
twice a year and a junior has never done. Written down as free-form text it
goes stale and nobody runs it the same way twice. Written as a runbook
template it becomes a published `vcenter-9.0-cert-rotation` that gates each
step on an explicit verification before the next one is shown.

## The senior + Claude + junior split

Three roles show up across a runbook's life. They do not all show up at the
same time.

- **Senior** — owns the procedure. Walks Claude through it during authoring,
  reviews the draft, and signs off on publish. The senior is the only role
  that drafts, edits, publishes, or deprecates a template (those tools are
  `TENANT_ADMIN`-only).
- **Claude** — the authoring agent. During a drafting session it captures
  what the senior demonstrates (bash, SSH, the verification at each step)
  into the template body and persists it through the `meho.runbook.*` template
  tools. It does not invent steps; it transcribes and structures.
- **Junior** — the operator who later *runs* a published template. The junior
  never sees the authoring surface; at run time they see one step at a time
  (the opacity floor lives on the run surface, G12.3 — see
  [#1198](https://github.com/evoila/meho/issues/1198)). Authoring and running
  are different audiences, which is why this doc is separate from the run docs.

## The multi-session drafting pattern

Authoring a real procedure is rarely one sitting. A senior walks Claude
through cert rotation across a few afternoons, and the draft has to survive
the gaps between sessions. It does, because draft state lives server-side —
each `meho.runbook.edit_template` call persists the whole body, and a new
session re-reads it with `meho.runbook.show_template`.

> **Naming note (#1612).** The dotted `meho.runbook.<verb>` names are
> canonical; the original flat `runbook_*` names were kept as deprecated
> aliases for one release and removed in v0.15.0 (#1625; the deadline was
> deferred once from the original v0.14.0 window by #1702). The template
> id field is `template_slug` everywhere — the deprecated `slug` input
> alias on the template verbs was removed alongside the flat names,
> though template-verb responses still carry both `template_slug` and the
> model's `slug` key.

The load-bearing property: **drafts are mutable, and editing a draft does not
bump the version.** A published version is pinned and immutable; a draft is a
scratchpad you keep appending to until the senior signs off.

### Step by step

1. **Start a draft.** `meho.runbook.draft_template(template_slug, body)` creates version 1
   with `status=draft`. The `body` can be minimal — a title, a description,
   and a couple of placeholder steps. There is exactly one draft per slug at a
   time; a second `meho.runbook.draft_template` for the same slug is refused
   (JSON-RPC `-32602`).

2. **Walk the procedure and capture as you go.** As the senior demonstrates,
   Claude calls `meho.runbook.edit_template(template_slug, body)` periodically with the
   updated body. Each call mutates the draft **in place** — the version stays
   the same, only `edited_by` / `edited_at` advance. There is no "save" versus
   "checkpoint" distinction; every edit is the new draft.

3. **Close the session mid-draft.** Nothing special to do. The last
   `meho.runbook.edit_template` call already persisted the state server-side. Close
   Claude; the draft is safe.

4. **Resume in a new session.** Open Claude and ask it to continue drafting
   runbook `<slug>`. Claude calls `meho.runbook.show_template(template_slug)` to read the
   current draft back (the `version` argument is optional — omit it to get the
   latest version, which is the draft), then resumes appending via further
   `meho.runbook.edit_template(template_slug, body)` calls.

5. **Publish on sign-off.** Once the senior is happy,
   `meho.runbook.publish_template(template_slug, version)` flips the draft to `published`.
   From then on it is the latest start target for runs, and it is immutable —
   the next edit forks (see below).

The status state machine is `draft -> published -> deprecated`. Publish is
idempotent (re-publishing an already-published version is a no-op);
deprecate is idempotent the same way.

## Fork-on-edit semantics

Once a template is published it is immutable. Calling
`meho.runbook.edit_template(template_slug, body)` when the slug has **no draft** — only
published or deprecated versions — does not error and does not mutate the
published row. Instead it **forks a new draft** at `max(version) + 1`.

The edit response then carries a `forked_from` block:

```json
{
  "slug": "vcenter-9.0-cert-rotation",
  "version": 2,
  "status": "draft",
  "forked_from": {
    "slug": "vcenter-9.0-cert-rotation",
    "version": 1,
    "in_flight_run_count": 3
  }
}
```

`forked_from.in_flight_run_count` is the number of runs still **in progress**
against the version you forked from. It is the senior's decision input: it
says how many operators are mid-procedure on the old version right now.

What the fork does and does not change:

- **In-flight runs keep advancing on their pinned version.** A run is pinned
  to the `(slug, version)` it started on; forking the template does not move a
  run to the new draft. Operators mid-procedure finish on the version they
  started.
- **New starts pick up the latest published version.** Until you publish the
  fork, new runs still start against the previous published version. Once you
  `meho.runbook.publish_template(template_slug, 2)`, new starts pick up version 2.
- **Editing the in-flight procedure is not what fork-on-edit does.** If you
  need to change what an operator who is *currently mid-run* does, the fork
  does not reach them. The tool guidance is to abort the run and start over
  against the new version rather than expect the edit to retroactively apply.

When the slug already has a draft, `meho.runbook.edit_template` takes the in-place
path instead (`forked_from` is `null`) — you can only fork from a published
version, and only when no draft is open.

## What NOT to put in a runbook

The template body is deliberately thin. The substrate rejects anything richer
than the shapes below — determinism over expressivity, the same minimalism
call as [#1177](https://github.com/evoila/meho/issues/1177). Authoring against
the grain wastes a publish attempt; here is what is out of bounds.

- **Conditional logic.** There is no `if X then Y else Z`. A step is either an
  `operation_call` step (the agent dispatches it via an `op_id`) or a `manual`
  step (the operator performs it off-MEHO). If a procedure genuinely branches,
  that is two templates, not one with a conditional.

- **Parallel steps.** Steps are an ordered list and execute single-threaded.
  Each step's `verify` gate has to pass before the next step is shown — that
  gating does not compose with parallelism, so there is no way to express
  "do these two at once".

- **Template expressions beyond the allowlist.** The only substitutions
  accepted in the body are `${run.target}` and `${run.params.X}`
  (where `X` is one flat parameter name matching `[a-z_][a-z0-9_]*` — nested
  paths like `${run.params.x.y}` are rejected). Every other `${...}` pattern
  is refused when the template is validated. The check walks the whole body
  recursively, including dict keys, so there is nowhere to smuggle one in.
  The one field that takes *no* substitution at all is `op_id`: it names the
  operation a step or verify dispatches, which is operation identity and must
  be fixed at publish time. Any `${...}` in an `op_id` — even an allowlisted
  one — is rejected, so a run parameter can never redirect a published step to
  a different operation.

- **Verify DSLs.** A step's `verify` is one of exactly two shapes:
  - `confirm` — MEHO shows a `prompt` to the operator and only an affirmative
    answer advances. The human is the oracle.
  - `operation_call` — MEHO dispatches a call (`op_id` + `params`) and matches
    the result against `expect` by **structural equality and presence only**:
    every key/value in `expect` must be present and equal in the result. There
    are no comparison operators, no JSONPath, and no boolean composition.

  That is the entire verify surface. If a check needs more than equality, it
  is a `confirm` step where a human makes the judgement.

## Worked example: a cert-rotation runbook across three sessions

The slug is `vcenter-9.0-cert-rotation`. Each session below is a short
transcript: the senior's instruction, Claude's action, the tool call, and the
result.

### Session 1 — skeleton draft

```text
Senior: We're going to write a runbook for rotating the vCenter 9.0 cert.
        Start a draft, we'll fill it in as we go.

Claude: Creating the draft now.

  -> meho.runbook.draft_template(
       template_slug="vcenter-9.0-cert-rotation",
       body={
         "title": "vCenter 9.0 certificate rotation",
         "description": "Rotate the machine SSL cert on a vCenter 9.0 appliance.",
         "target_kind": "vcenter",
         "steps": [
           {
             "id": "placeholder",
             "title": "TODO: fill in during walkthrough",
             "body": "Placeholder step.",
             "type": "manual",
             "verify": {"type": "confirm", "prompt": "Continue?"}
           }
         ]
       }
     )

  <- { "slug": "vcenter-9.0-cert-rotation", "version": 1, "status": "draft" }

Claude: Draft created at version 1. Ready when you are.
```

### Session 2 — capturing the revoke / issue / install steps

```text
Senior: Continue drafting the cert-rotation runbook. First we revoke the old
        cert, then issue the new one, then install it on the appliance.

Claude: Let me re-read the current draft first.

  -> meho.runbook.show_template(template_slug="vcenter-9.0-cert-rotation")
  <- { "slug": "...", "version": 1, "status": "draft",
       "steps": [ { "id": "placeholder", ... } ], ... }

Claude: Got it. I'll replace the placeholder with the three steps you
        described and persist after each one.

  -> meho.runbook.edit_template(template_slug="vcenter-9.0-cert-rotation", body={ ...revoke step... })
  <- { "slug": "...", "version": 1, "status": "draft", "forked_from": null }

  -> meho.runbook.edit_template(template_slug="vcenter-9.0-cert-rotation", body={ ...revoke + issue... })
  <- { "slug": "...", "version": 1, "status": "draft", "forked_from": null }

  -> meho.runbook.edit_template(template_slug="vcenter-9.0-cert-rotation", body={ ...revoke + issue + install... })
  <- { "slug": "...", "version": 1, "status": "draft", "forked_from": null }

Claude: Three steps captured, still version 1 (drafts edit in place). We can
        stop here and pick up the verify steps next time.
```

Note `forked_from` is `null` on every edit — the draft is mutated in place
and the version never moves.

### Session 3 — verify steps, review, publish

```text
Senior: Continue the cert-rotation runbook. Add a verify on the install step
        that checks the new cert thumbprint, then we'll publish.

Claude: Re-reading the draft.

  -> meho.runbook.show_template(template_slug="vcenter-9.0-cert-rotation")
  <- { "version": 1, "status": "draft", "steps": [revoke, issue, install], ... }

Claude: Adding an operation_call verify to the install step that matches the
        live thumbprint against the expected value.

  -> meho.runbook.edit_template(
       template_slug="vcenter-9.0-cert-rotation",
       body={ ...install step now has verify:
         { "type": "operation_call",
           "op_id": "vmware.cert.get_thumbprint",
           "params": {"target": "${run.target}"},
           "expect": {"thumbprint": "${run.params.new_thumbprint}"} } ... }
     )
  <- { "version": 1, "status": "draft", "forked_from": null }

Senior: Looks right. Publish it.

Claude: Publishing version 1.

  -> meho.runbook.publish_template(template_slug="vcenter-9.0-cert-rotation", version=1)
  <- { "template_slug": "vcenter-9.0-cert-rotation", "slug": "vcenter-9.0-cert-rotation", "version": 1, "status": "published" }

Claude: Published. New runs will start against version 1. Editing it now would
        fork a new draft at version 2.
```

The `verify` above uses both allowlisted substitutions: `${run.target}` for
the appliance the run is pointed at, and `${run.params.new_thumbprint}` for a
run parameter the operator supplies at start. Anything outside that allowlist
would be rejected at publish.

## Cross-references

- [Goal #1195](https://github.com/evoila/meho/issues/1195) — G12 Runbooks
  (the overall outcome).
- [Initiative #1197](https://github.com/evoila/meho/issues/1197) — G12.2
  template lifecycle (draft / edit / publish / deprecate / list / show + this
  drafting pattern + fork-visibility-on-edit).
- [Initiative #1198](https://github.com/evoila/meho/issues/1198) — G12.3 run
  lifecycle (the execution-side counterpart: start / next / abort / reassign).
- [#1191](https://github.com/evoila/meho/issues/1191) — design source for the
  runbook surface.
- [#1177](https://github.com/evoila/meho/issues/1177) — the substrate-minimalism
  rationale the `verify` surface and the substitution allowlist follow.
- Agent-facing tool descriptions:
  [`backend/src/meho_backplane/mcp/tools/runbooks.py`](../../backend/src/meho_backplane/mcp/tools/runbooks.py).
- Shape contract (step / verify / substitution rules):
  [`backend/src/meho_backplane/runbooks/schemas.py`](../../backend/src/meho_backplane/runbooks/schemas.py).
- Versioning algebra (in-place edit vs fork-from-published):
  [`backend/src/meho_backplane/runbooks/service.py`](../../backend/src/meho_backplane/runbooks/service.py).
- The architecture doc (`docs/architecture/runbooks.md`, storage shape +
  dispatcher correlation + opacity contract) and the CLI usage doc
  (`docs/cli/runbook.md`, G12.5) land separately.
