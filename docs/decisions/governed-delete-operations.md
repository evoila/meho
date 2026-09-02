# Delete-shaped operations get a governed path behind a dedicated destructive safety tier (decision)

**Status:** decided — operator determination of record; **reverses** the standing
"delete-shaped operations are deliberately ungoverned" posture
**Date:** 2026-08-29
**Goal:** management-plane lockdown (evoila-bosnia/meho-internal#234) — the driver;
its enforcement stages cannot deny operator-direct reach to appliances until
delete-shaped work is *governed* rather than *impossible*
**Issue:** [#3183](https://github.com/evoila/meho/issues/3183) — `policy`-labelled
design home (design review → this decision → implementation Tasks)
**Composes with:** the satellite write-path decision
([#2901](https://github.com/evoila/meho/issues/2901), landing as
`docs/decisions/satellite-write-path.md` in #3187) — delete-shaped operations are
**never minted to a satellite**, the two decisions agree at that boundary

## The determination

> Delete-shaped operations (destroy VM, delete datastore, remove object) gain a
> **governed execution path** through the backplane, gated **strictly harder than
> any existing write tier**. Four requirements hold together, and the security is
> the composition — no single one is sufficient:
>
> 1. **Mandatory human approval, always.** No standing grant applies, no
>    self-approval, no autonomous-session approval. This reuses the approvals
>    plane's existing human-only line (v0.1-spec §7); it does not invent a second
>    approval mechanism.
> 2. **Mandatory preview.** Dispatch is refused unless a `preview_operation` of the
>    same `(connector_id, op_id, target, params)` was executed and **its result
>    hash** is presented with the approval — the approver sees precisely what dies.
> 3. **Blast-radius statement in the approval payload** — object identity, child
>    objects, irreversibility class — carried on the parked row's `proposed_effect`
>    envelope so the reviewer reads it in the queue, not only post-approval.
> 4. **A dedicated safety tier above every current write level**, excluded by
>    default from every policy/filter — including the satellite runner's safety
>    gate. Deletes are never minted to a satellite (see the compose note above).

This reverses a standing design decision. Today the backplane refuses the delete
class by construction and operators drop to local tools (vendor UIs, `govc`) when
something must be destroyed — a posture that was correct while the backplane's job
was making the *constructive* surface safe.

## Why now — the management-plane-lockdown driver

The operator direction is now stated: **management-plane lockdown**
(evoila-bosnia/meho-internal#234) makes the backplane (central instance + satellite
runners) the sole management-plane ingress, with operator-direct network reach to
appliances eliminated in steady state. In that end-state "drop to a local tool"
ceases to exist as a path. Delete-shaped work is then either **governed or
impossible** — and "impossible" is not an operational answer: decommissions,
failed-deploy cleanup, and lab teardown are routine work. This decision chooses
*governed*, behind the hardest gate MEHO has.

## Why this is not a loosening

The four requirements each *tighten* an existing seam; none relaxes one:

- The human-only line already refuses model-session decisions. The three decision
  verbs (`meho_approvals_approve`, `meho_approvals_reject`,
  `meho_agents_grant_elevate`) have **no MCP path under any claim set** — pinned in
  `backend/src/meho_backplane/mcp/human_only.py:53` and enforced *before* registry
  lookup in `backend/src/meho_backplane/mcp/handlers.py:299`, both citing v0.1-spec
  §7. The self-approval guard (`operations/approval_queue.py:1093`,
  `_check_self_approval`) already refuses `operator.sub == request.principal_sub`
  unless the audited emergency break-glass switch is set. The destructive tier
  **removes even that escape**: for this tier self-approval is refused with no
  break-glass, and no standing grant can pre-clear it.
- Standing grants already exclude deletes. `ServicePrincipalGrantService.create`
  refuses delete-shaped op-ids at creation
  (`operations/service_grants.py:136` `_delete_shaped_reason_by_pattern` +
  `:155` `_delete_shaped_reason_by_descriptor`, gate in `create` at `:234`) — a
  grant is "the
  floor of what runs unattended, not a bypass of a destructive gate." Today that
  classifier exists **only as a refusal**; this decision promotes it into a
  positive safety tier so the same shape drives execution, not just grant denial.
- Agent verdicts already cap destructive ops. `AgentPermission` caps a
  `caution`/`dangerous` grant at `needs-approval` — "a destructive op is never
  auto-executed" (`db/models.py:4185-4190`). The new tier is stricter still:
  DENY for agent principals, non-grantable, human-approval-always.

## The gate, grounded in real seams

### Requirement 4 — the dedicated tier (the load-bearing addition)

`safety_level` is a closed three-value enum today — the canonical alias
`SafetyLevel = Literal["safe", "caution", "dangerous"]`
(`backend/src/meho_backplane/operations/ingest/schemas.py:24`) — declared on the
descriptor (`db/models.py:1540`), pinned by the DB CHECK constraint
`ck_endpoint_descriptor_safety_level`
(`db/models.py:1631`: `safety_level IN ('safe', 'caution', 'dangerous')`),
validated in `operations/typed_register.py:496`
(`_VALID_SAFETY_LEVELS` frozenset, enforced by `_validate_safety_level` `:758`),
and repeated as a `Literal` union on every connector op contract.

The tier adds a **fourth, most-restrictive value** — proposed token `destructive`
(final string resolved in the first implementation Task) — above `dangerous`.
Ordering becomes `safe < caution < dangerous < destructive`, so `_more_restrictive`
(`operations/_validate.py`) treats it as the ceiling. Because `safety_level` is
CHECK-constrained in the DB, adding the value is an **Alembic migration** widening
`ck_endpoint_descriptor_safety_level`, plus the `SafetyLevel` alias, the
`_VALID_SAFETY_LEVELS` frozenset, and the two policy maps below.

**Excluded by default everywhere** — the tier is a deny/park floor at each gate,
not an opt-in:

- **Agent policy gate.** The agent-principal resolver `resolve_verdict`
  (`auth/permissions.py:273`, called from `operations/_validate.py:396`) maps a
  level to a default verdict via `_SAFETY_DEFAULT`
  (`permissions.py:130`: `{safe→AUTO_EXECUTE, caution→NEEDS_APPROVAL,
  dangerous→DENY}`) capped by `_SAFETY_CEILING` (`permissions.py:145`). The tier
  adds `destructive→DENY` with a `needs-approval` ceiling that no `AgentPermission`
  grant can lift — and note the fail-closed default already in place: an unmodeled
  level resolves to DENY at `permissions.py:359`, so the gate is safe even before
  the map is updated.
- **Service-principal gate** (`_service_safety_gate_reason`,
  `operations/_validate.py:164`; consulted in `_non_agent_verdict` `:235`): today
  a `dangerous` op parks always and a mutating `caution` op parks; the tier adds
  `destructive` → parks always and is never satisfiable by a standing
  `ServicePrincipalGrant` (deletes are already un-grantable, above).
- **Satellite mint safe-only wall** (`operations/gateway_commands.py:370`, in
  `mint_gateway_command` `:273`): the wall already refuses any
  `descriptor.safety_level != 'safe'` op with `MintRefusalCode.OP_NOT_SAFE`
  *before* the policy gate (ladder Step 3, docstring `:296-299`), so the
  destructive tier is transitively excluded from every satellite the day it
  exists. The
  satellite-write-path decision records the same boundary explicitly (deletes never
  minted to a satellite); this tier is the classification that keeps that true even
  if the satellite wall ever tiers up from binary safe-only.

### Requirement 1 — mandatory human approval

Reuses the approvals substrate unchanged in mechanism: the dispatcher parks a
`NEEDS_APPROVAL` verdict via `_handle_needs_approval`
(`operations/dispatcher.py:2010` → `create_pending_request`,
`operations/approval_queue.py`), and a human clears it on a REST/CLI/console
surface. The destructive tier's specialisation: the verdict is **always**
`NEEDS_APPROVAL` for a human and **DENY** for an agent (no `requires_approval=False`
fast path, no grant pre-clear, no self-approval even under break-glass).

### Requirement 2 — mandatory preview + result-hash binding

`preview_operation` already exists as the read-only sibling of `call_operation`
(`operations/meta_tools.py::preview_operation` / `PreviewOperationBody`;
orchestration in `operations/_request_preview.py::preview_dispatch`,
docs `docs/codebase/dispatch-request-preview.md`). It returns
`{status, method, resolved_path, query, redacted_body}` and — **today — computes no
hash** and writes nothing (it deliberately skips the policy gate and audit,
`_request_preview.py`). The parked row carries a `params_hash`
(SHA-256 over canonical JSON via `compute_params_hash`, `operations/_validate.py:81`;
stored `db/models.py:5007`), but that hashes the *params*, not the *preview
result*, and it is stamped only on the dispatch/mint paths — never on preview.

So the binding is **new work**: for a `destructive` op the dispatcher refuses to
park (and the approver refuses to clear) unless a preview of the identical
`(connector_id, op_id, target, params)` was executed and its result hash is
recorded on the request and re-verified at approve time (extending the existing
`params_hash` verification in `approve_request`). This gives the approver a
tamper-evident "this is exactly what will be destroyed" artefact, not a
best-effort echo.

### Requirement 3 — blast-radius statement on the payload

The reviewer-facing `proposed_effect` envelope is built at park time by
`dispatcher._build_proposed_effect` (`operations/dispatcher.py:2061`) from the
per-op preview builders (`operations/_preview.py`) and already stamps
`safety_level`, `op_class`, and a preview/params-echo body
(docs `docs/codebase/approvals.md`, "`proposed_effect` builder hook"). The
destructive tier makes a **blast-radius block mandatory** on that envelope —
object identity, enumerated child objects, and an irreversibility class — so a
`destructive` op cannot park with only an identifier-only default. The console
modal already renders `proposed_effect` structurally
(`ui/templates/approvals/_modal.html`, #2447), so the block surfaces without a new
render path.

## Scope / non-goals

- **This decision records the design, not the code.** The implementation seams are
  filed as sibling Tasks under #3183, unimplemented. This issue is the design home.
- **No new approval mechanism.** The destructive tier rides the existing approvals
  plane (queue, park, decide, resume, audit, broadcast) — it tightens the gate, it
  does not fork it.
- **No satellite path for deletes.** Consistent with the satellite write-path
  decision (#2901 / #3187): governed *remote* deletes, if ever needed, are a
  separate future decision built on this tier — explicitly not in scope here.
- **The first governed delete op family is illustrative, not exhaustive.** This
  decision creates the tier and the gate; which vendor delete operations adopt it,
  and in what order, is per-connector work under the follow-up Tasks. No connector
  is required to expose a delete until it is modeled into this tier.
- **The break-glass `APPROVAL_ALLOW_SELF_APPROVAL` switch does not reach this
  tier.** A single-operator tenant that must run a governed delete uses the
  agent-requester pattern (`docs/codebase/approvals.md`), never self-approval.

## Implementation shape (for the follow-up Tasks)

1. **The tier + exclusions** — add the `destructive` `safety_level` value
   (migration widening `ck_endpoint_descriptor_safety_level`, `_VALID_SAFETY_LEVELS`,
   the `Literal` unions, `_more_restrictive` ordering); wire the deny/park floor at
   the agent gate, the service-principal gate, and confirm the satellite safe-only
   wall transitively excludes it. Fold the existing delete-shaped classifier
   (`service_grants.py`) into the tier so `DELETE:` verbs and `destructive`-tagged
   typed ops resolve to it.
2. **Preview-hash binding + blast-radius payload** — have `preview_operation` emit a
   stable result hash; persist it on the request; require + re-verify it in
   `approve_request` for the destructive tier; make the blast-radius block mandatory
   on `proposed_effect` and refuse the park without it.
3. **The first governed delete op family + conformance tests** — model one real
   delete family into the tier end-to-end (preview → blast-radius → human approval →
   audited resume) with conformance tests proving no agent path, no grant path, no
   self-approval, and no satellite mint.

## References

- Approvals plane: `docs/codebase/approvals.md`;
  `backend/src/meho_backplane/operations/approval_queue.py`,
  `operations/_validate.py`, `operations/dispatcher.py`,
  `mcp/human_only.py`, `mcp/handlers.py`.
- Preview: `docs/codebase/dispatch-request-preview.md`;
  `operations/meta_tools.py`, `operations/_request_preview.py`,
  `operations/_preview.py`.
- Safety-level model + gate: canonical alias `operations/ingest/schemas.py:24`;
  descriptor column + CHECK `db/models.py:1540` / `:1631`;
  `operations/typed_register.py:496`; agent default/ceiling maps
  `auth/permissions.py:130` / `:145` / `:359` via `resolve_verdict` `:273`;
  service gate `operations/_validate.py:164`; satellite safe-only wall
  `operations/gateway_commands.py:370`.
- Delete-shaped classifier reused: `operations/service_grants.py:136` / `:155` /
  `:234`; `settings.py:1260` (`service_grant_delete_shaped_patterns`).
- Standing posture reversed: #3183 (this issue), #2907 (backplane-first coverage —
  the one op class it deliberately excludes today).
- Compose boundary: the satellite write-path decision (#2901 / #3187, file
  `docs/decisions/satellite-write-path.md`); driver Goal
  evoila-bosnia/meho-internal#234.
- v0.1-spec §7 (approval is a human decision) — the human-only line this tier
  reuses.

## Amendment (2026-08-31) — dispatch flight recorder cross-reference

Added once the dispatch flight recorder's fail-closed redaction engine landed
([docs/decisions/dispatch-flight-recorder.md](dispatch-flight-recorder.md),
Initiative [#3207](https://github.com/evoila/meho/issues/3207); redaction #3213,
capture #3214). It discharges the reciprocal cross-reference that decision
flagged as a deliberate follow-up ("Interactions with sibling decisions" →
"Governed delete-shaped operations"). It records only what the merged code does
today.

**The destructive / delete-shaped family is a hard body-exclusion in the flight
recorder, single-sourced with this decision's classifier.** The recorder's
redaction engine classifies every captured span for body exclusion in
`redaction/flight_recorder/families.py` (`classify_body_exclusion`). It does
**not** re-declare the delete-shaped patterns: it reads them from the **same
single source** the grant guard uses — `Settings.service_grant_delete_shaped_patterns`,
the `_delete_shaped_reason_by_pattern` seam at `operations/service_grants.py`
this decision reuses (References → "Delete-shaped classifier reused") — and
applies the same descriptor signals: an HTTP `DELETE` method and the
`destructive` tag the safety tier (#3196) promotes. An op the classifier places
into the `destructive` family has its request **and** response bodies **never
recorded** (`redact_span` sets `body_recorded=False` and emits the omission
marker, `redaction/flight_recorder/span.py`). Because both classifiers draw on
one source, the flight recorder's exclusion set and this decision's `destructive`
tier cannot drift.

**A destroy dispatch's trace carries metadata spans, never the excluded bodies.**
For a body-excluded destructive op the span still records method, URL, status,
duration, and allowlisted (non-secret) headers — only the bodies are dropped. The
exclusion is a **deliberate, certain** omission (`BodyExclusion.uncertain=False`
for a placed family), not a redaction-uncertainty, so a destroy trace is **not**
forced operator-only on that account; it stays agent-readable through the
narrow-waist result-handle idiom (F5), subject to the per-tenant gate, exactly as
any other trace. The operator-only degrade is reserved for the *unplaceable* op
(a missing / blank op id), not for a cleanly-excluded destructive one. Each span
is classified against the op that *made* the call, so within a delete composite
only the destructive / delete-shaped spans are body-excluded; sibling non-
destructive sub-steps follow ordinary per-connector redaction.

**The flight recorder records execution, not the approval gate.** A `destructive`
op reaches the central dispatch path — where the capture scope is opened
(`operations/dispatcher.py:706`) — only after this decision's mandatory human
approval clears it (requirement 1), bound to its preview-result hash and
blast-radius statement (requirements 2–3). Those gates live on the approvals
plane and the audit row, not in the trace; the flight recorder neither duplicates
nor bypasses them — it captures the post-approval vendor traffic of the executed
destroy, with the destructive / delete-shaped spans' bodies excluded as above.

## Amendment (2026-09-02) — preview parity + a builder-coverage CI sweep (#3312)

Two coupled follow-ups from the 2026-09-02 through-backplane canary of the
governed `vault.kv.delete` chain (lab evoila-bosnia/claude-rdc-hetzner-dc#2814):
the park → human-approve → execute chain held, but `preview_operation` answered
`preview_unavailable` for the op even though the park-time apparatus built a rich
`proposed_effect`. Both extend — do not revise — requirements 2 and 3 above.

**1. Preview parity for approval-requiring typed ops.** Requirement 2's
previewability gate (`_resolve_previewable_descriptor` → `_is_previewable`)
admitted the `destructive` tier only. It now also admits any **non-credential-class**
`requires_approval` op (the `dangerous`-tier typed ops, canonical case
`vault.kv.delete`), and the synthetic preview (now
`operations/_composite_preview.py`) layers the **reused** park-time
`proposed_effect` onto the params-bound projection — `build_proposed_effect` run
with **no connector instance**, so it stays egress-free (a pure builder like
`vault.kv.delete`'s populates; a live-read blast-radius builder declines). This
closes the asymmetry where the approver saw the effect block but the calling
agent could not preview it. A **credential-class** op (`vault.kv.put` /
`k8s.secret.create`) is deliberately excluded — its secret rides in the request
params, and the synthetic preview's `redacted_body` slot uses only
connector-boundary value-shape redaction, which cannot be trusted to scrub a
structured secret; the exclusion mirrors `build_proposed_effect`'s own
credential-class suppression. The preview-hash binding is untouched: the
`proposed_effect` key is outside the hashed projection, and — per this decision's
non-goals — #3197's preview-*hash* binding is **not** extended to the `dangerous`
tier (typed synthetic previews are params-derived, so the existing `params_hash`
on dangerous parks already pins the same content).

**2. A registry-driven CI sweep for destructive builder coverage.** Requirement 3
makes a destructive op un-parkable without a blast-radius builder — fail-closed,
but **runtime-only**: a posture sweep (#3247 / #3288 / #3305 show these recur)
that promotes an op to `destructive` without adding its builder passes CI and
ships an un-parkable op, silently broken until first use. The conformance tests
this decision's requirement 3 named (§"conformance tests proving no agent path,
no grant path, …") scoped to **access-control invariants** only. #3312 adds a
new, orthogonal conformance sweep
(`tests/test_destructive_builder_conformance.py`): it enumerates every registered
`destructive` descriptor from the real typed-op registrars (every connector, both
source kinds) and fails the build if any resolves no registered proposed-effect
builder. A promotion without a builder now fails CI, not first use.
