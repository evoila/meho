# Satellite write path — threat model + scoped-hybrid design (Initiative #2901)

Design artifact for Initiative
[#2901](https://github.com/evoila/meho/issues/2901) — *security-review-first
design for governed remote mutations*. This document is the threat model and
the ratified design; the operator determination it records lives in
[docs/decisions/satellite-write-path.md](../decisions/satellite-write-path.md).

Answers one question: **how does the satellite runner execute _write_
operations against estates the central backplane cannot dial, without
weakening the push-only/poll boundary ([#2877](https://github.com/evoila/meho/issues/2877))
or the defence-in-depth safe-only walls the read path relies on?**

The write path is the reachability prerequisite of the management-plane-lockdown
Goal (evoila-bosnia/meho-internal#234): its enforcement stages cannot deny
operator-direct reach anywhere the central instance does not dial directly until
governed remote mutations exist. This design also composes with
[#3183](https://github.com/evoila/meho/issues/3183) (delete-shaped operations
carry a mandatory-human-approval + preview-hash + blast-radius + dedicated
safety tier).

Three sections:

1. **§1 What exists today** — the machinery a write path must ride or replace,
   grounded `file:line` in the current tree.
2. **§2 Threat model** — per-asset, concrete, read/write asymmetry as the
   organising fact.
3. **§3 Chosen design** — the scoped hybrid: four composed mechanisms + staged
   rollout, with the residual risks named.

---

## §1 — What exists today (the machinery a write path must ride or replace)

The write path is an *extension* of a working push-only read path, not a
greenfield build. Every claim below is grounded in the current tree.

### 1.1 Two fail-closed walls, both safe-only

The current design refuses non-`safe` work at **three independent layers**:

- **Central mint wall.** `mint_gateway_command`
  (`backend/src/meho_backplane/operations/gateway_commands.py:370-383`) refuses
  `descriptor.safety_level != "safe"` with `MintRefusalCode.OP_NOT_SAFE`
  **before** the policy gate, and mints **only** on an explicit
  `PermissionVerdict.AUTO_EXECUTE` (`:389`). A refusal writes **no**
  `gateway_command` row and **no** `approval_request` row — a non-`safe` op is
  never even parked (`:299`). This is the v1 read-only guarantee: a non-`safe`
  op can never reach a runner because it is never minted.
- **Edge executor wall.** `_screen_item` (`runner/executor.py:76-80`) refuses
  any item whose `safety_level != "safe"` and any `handler_ref` outside
  `meho_backplane.connectors.*`, checked **before** import (imports have
  module-load side effects) and re-checked on the resolved callable's
  `__module__` after import (`executor.py:174-188`). The runner doc calls this
  "defence in depth — central mint is the real authorization boundary"
  (`docs/codebase/satellite-runner.md`).
- **Assignment materialiser** applies the same wall a third time
  (`gateway/assignment_service.py:181,255,279` — `_is_runnable_safe`).

**Consequence for the write path:** a write path is precisely the act of
punching through all three safe-only walls. Whatever replaces them must be at
least as strong, per-layer, or the initiative's own defence-in-depth premise is
lost.

### 1.2 The capability command (single-use, expiring, args-bound)

`gateway_command` rows are already the pre-authorized envelope the "signed
single-use expiring work items" mechanism reaches for:

- **Token = opaque UUID row PK, not a JWT** (deliberate
  [#2500](https://github.com/evoila/meho/issues/2500) decision,
  `gateway_commands.py:36-43`). Verification is a DB lookup, revocation is a row
  update, replay refusal is a conditional-`UPDATE` latch. *Possession is not
  authorization* — a command is delivered only over the runner's authenticated,
  `runner_id`-scoped channel.
- **Args-hash binding.** `params_hash = compute_params_hash(params)` stamped at
  mint (`gateway_commands.py:334`); delivery re-hashes stored params against it
  (post-mint mutation defence), mirroring the approval queue's swap defence.
- **Bounded expiry.** Every command carries a NOT-NULL `expires_at`, a caller
  deadline bounded down to a default TTL ceiling (`gateway_commands.py:326-327`).
- **At-most-once *acceptance*.** `consume_command`
  (`gateway_commands.py:450-506`) is a one-way conditional
  `UPDATE ... SET consumed_at = now WHERE consumed_at IS NULL AND status =
  'delivered'`. The winner accepts the result; a replay is refused
  (`GatewayCommandAlreadyConsumedError`).
- **Runner-side dedup.** `execute_command_once` (`executor.py:103-148`) records
  `command_id` **before** dispatch (record-before-execute); a redelivery
  re-submits the spooled result, never re-executes.

**The asymmetry hidden in the lifecycle.** `GatewayCommandStatus`
(`db/models.py:5242-5263`) is a four-state lifecycle whose `DELIVERED` docstring
says it plainly: *"A row that is claimed but never reported stays here (lost, not
redelivered — the v1 at-most-once failure mode)."* For a **read**, a
claimed-but-never-reported command is benign — nothing changed in the estate.
For a **write**, that same DELIVERED-forever row **is the un-audited mutation
window**: the effect already happened at the edge, and the centre has a mint
audit row but no effect audit row. §2 (T4) and mechanism 4 turn on this.

### 1.3 Audit lineage (synchronous at authorization, deferred at effect)

- **Mint** writes a synchronous audit row in the mint transaction —
  `method='GATEWAY'`, `path='gateway.command.mint'`, `params_hash` stamped
  (`gateway_commands.py:111-114,421-437`).
- **Result** is audited only when the centre accepts the runner's posted result:
  `accept_command_result` wins the consume latch, records the outcome, then
  writes `path='gateway.command.result'` with `parent_audit_id = mint_audit_id`
  (`gateway_commands.py:509-563`) — one audit subtree per remote execution.

So remote execution already runs a *split* audit: synchronous at
**authorization time**, deferred to **result-acceptance time** for the effect.
[v0.1-spec §6](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/v0.1-spec.md)
("an operation does not return success unless the audit row commits") holds for
the *mint*; it structurally **cannot** hold for the *effect* of a remote write,
because the executing side has no DB and the mutation is inherently off-net. This
is not a bug to fix — it is the fact the write-path audit design has to legislate
around.

### 1.4 Identity, the route cage, and the credential seam

- **Runner principal identity.** `principal_kind=runner` + unforgeable
  `runner_id` claim (`auth/runner_guard.py`), registered per runner through a
  REST lifecycle (`auth/runner_principals.py`).
- **Two gates.** `require_runner` (kind gate) + `assert_runner_scope`
  (`runner_guard.py:75-207`): resolves the tenant-scoped `runner_principal` row
  by **name** and requires `row.id == operator.runner_id`. **No existence
  oracle** (unknown-runner and not-my-runner both 403,
  `runner_guard.py:151-157`). Heartbeat is a side effect of this gate
  (`:198-206`).
- **Negative route cage.** `RUNNER_ALLOWED_PATH_PREFIXES =
  ("/api/v1/gateway/", "/api/v1/checks/")` (`middleware.py:127-130`);
  `_reject_runner_outside_gateway` (`middleware.py:415-440`) fail-closed 403s a
  runner token on **every** other authenticated route, keyed on the unforgeable
  `principal_kind` discriminator.
- **Kill switch is coarse.** `assert_runner_scope` deliberately does **not**
  consult `revoked` (`runner_guard.py:40-47`): revocation is Keycloak
  `enabled=false` (blocks new token grants) + a short access-token TTL + the
  [#2501](https://github.com/evoila/meho/issues/2501) dead-man switch — **not** a
  per-request DB check. Revocation latency ≈ token TTL. Fine for read-only; a
  review item for writes (§3, recommendation 3).
- **Dead-man switch.** `gateway/deadman.py` — a central-clock interval sweeper
  (advisory-lock-elected, conditional-`UPDATE` idempotent) flips
  `runner_assignments.stale_at` when `last_seen_at` falls behind the cutoff, one
  audit row per flip. This is a **liveness** monitor, not a **security** monitor
  — mechanism 4 reuses its exact mould for a security alarm.
- **Credential seam + reference-not-value discipline.** `secret_ref` travels to
  the runner as a **reference, never a value** (`runner/wire.py:76-80`,
  `gateway/assignment_service.py:74-99`): "no credential value is ever embedded
  (the runner spools assignments on disk, so a value would durably persist)."
  The backend-agnostic resolution seam is
  `connectors/_shared/credential_backend.py` +
  `connectors/_shared/vault_creds.py`.

**Load-bearing gap found in this review.** The wire docstring says the runner
"resolves [`secret_ref`] outbound under its own read-only scope," but the
resolution path `vault_client_for_operator` authenticates via
`operator.raw_jwt` (`auth/vault.py:226,266`), and the runner reconstructs its
acting operator with `raw_jwt=""` (`runner/executor.py:214-228`,
`wire.py:109-124`). **A credentialed target op cannot currently resolve its
secret on the runner** — so the v1 runner workload is targetless `net.*` `safe`
probes only. Edge credential resolution for *any* credentialed op — read or
write — is therefore an **open design point**, not a solved one. Mechanism 3 is
where the write path answers it (this is the empty-JWT gap).

### 1.5 The approvals plane (v0.1-spec §7 as implemented)

`operations/approval_queue.py`: a `NEEDS_APPROVAL` verdict parks the dispatch as
a durable `ApprovalRequest` row instead of executing; two REST endpoints
(`/api/v1/approvals/{id}/approve|reject`) let authorised humans decide; approval
re-dispatches the original call with the gate bypassed (`dispatch(...,
_approved=True)`) because the committed decision *is* the authorization. Key
properties the write path reuses:

- **Params swap defence** — approve re-hashes caller params against the stored
  `params_hash`, `ParamsMismatchError` on mismatch.
- **Synchronous-audit invariant** — an approval isn't granted until its decision
  row commits (same transaction as the status update).
- **Human-only decision.** `meho_approvals_approve/reject` have **no MCP path
  under any claim set** (CLAUDE.md agent-surface). Approval is a human decision.
- **At-most-once resume** — `claim_resume` conditional-`UPDATE` latch (the same
  mould `consume_command` copied).

Today the gateway mint **deliberately does not park to the approval queue**
(`gateway_commands.py:299`). The write path is exactly where that decision is
revisited.

---

## §2 — Threat model

Per-asset, concrete, with the existing mitigation named from code. The write path
changes the consequence class of every one of these, because the distinguishing
fact is **read/write asymmetry**:

> A read executed at a compromised edge that is never reported changes nothing in
> the estate — the worst case is exfiltration of what the runner's scoped
> credential could already read. A write executed at a compromised edge mutates
> the estate the instant the handler runs, **before** any result is posted, in a
> network the centre cannot dial to verify or roll back. The single-use consume
> latch (`gateway_commands.py:450-506`) protects against double **acceptance** of
> a result; it does **not** protect against execution-without-report, because the
> latch is won on the *centre's* result-ingest, which a silent edge never
> triggers.

| # | Asset / threat | Read consequence (today) | Write consequence | Existing mitigation | Gap for writes |
|---|---|---|---|---|---|
| T1 | **Remote-site / runner compromise** — attacker controls the runner process | Exfiltrate reads within the runner's tenant + `secret_ref` scope | Attacker executes any write it can get **minted** for, and can execute-then-not-report (T4) | Central mint is the only authorization; edge executor is a *dumb* bounded executor; route cage + `runner_id`-scoped channel (`middleware.py:415-440`) | Blast radius becomes "everything mintable to this runner." Must be bounded by a **minimal per-runner allowlist** (mech 2) + **short-lived per-target creds** (mech 3). The allowlist *is* the blast radius. |
| T2 | **Work-item tampering in transit** — item altered between mint and edge execution | Wrong read; low impact | Wrong **mutation** — different target/params than authorised | `params_hash` re-hash at delivery (`gateway_commands.py:334`); TLS on the poll channel; opaque-UUID token delivered only over the scoped channel | The hash defends the *stored* params against DB mutation; it does **not** give the *edge* an independent, offline way to verify the item is genuine + fresh + in-scope before mutating → **signed work item** (mech 1). |
| T3 | **Credential exposure at the edge** — vendor creds resolvable in a fenced, less-trusted network | Read-scoped creds exposed | **Write-scoped** creds exposed; a captured standing credential grants broad estate mutation for its whole lifetime | `secret_ref` is reference-not-value; never spooled to disk (`wire.py:76-80`) | **No edge credential path exists yet** (§1.4 gap). Must not hand a standing broad write credential to a fenced host → **per-work-item short-lived, response-wrapped creds** (mech 3). |
| T4 | **Execute-but-don't-report** (the write-specific omission threat) | Benign — nothing changed | Mutation happens; centre holds a `mint` audit row but no `result` audit row; the row sits `DELIVERED` forever (`db/models.py:5254-5259`) | Mint audited synchronously (`gateway_commands.py:421`); dead-man switch flags *liveness* only | The un-audited-mutation window. Needs a **security** alarm on minted-write-capabilities unreported past `expires_at` (mech 4), distinct from the liveness dead-man. |
| T5 | **Replay of a captured capability** | Bounded: single-use consume latch + expiry | Honest-but-partitioned runner protected by record-before-execute + expiry; a *compromised* runner ignores its own dedup store | Consume latch + runner-side dedup + bounded expiry | Against a compromised edge the latch guarantees at-most-once *acceptance*, not *execution*. Compensate with **short expiry + minimal allowlist**, not stronger replay logic. |
| T6 | **Rogue-mint / authorization bypass** — attacker gets a write capability minted it should not have | N/A (only `safe` mints) | Directly grants estate mutation | Mint ladder is fail-closed: descriptor lookup → param validation → safe wall → policy gate, mint only on `AUTO_EXECUTE` (`gateway_commands.py:337-406`) | A write tier must add **allowlist check + approval binding + write-tier policy** to the ladder *without* widening what `safe` means. |
| T7 | **Programmatic enrollment abuse** (#2901 scope update §4) — a blueprint/adoption workflow that auto-provisions a scoped execution identity | Auto-provisions a *read* identity | Auto-provisions a **write-capable** identity — "the most attractive attack surface in the design" | Manual CLI enrollment lifecycle exists (`runner_principals.py`); the runner principal is tenant-scoped | Bootstrap credential shape, scope binding at issuance, revocation are review items. A write-capable allowlist provisioned *automatically at birth* is the sharpest edge (§3, recommendation 2). |
| T8 | **Blast radius of a single rogue runner** | Its tenant's readable targets | Its tenant's targets **within its write allowlist**, for the credential TTL | Tenant scoping (`assert_runner_scope`); route cage | Radius is a *product* of (allowlist breadth) × (credential lifetime) × (revocation latency). All three are design knobs in §3. |

**Threat-model conclusion.** For reads, the trust model is "a compromised edge
can only read what its scoped credential permits." For writes, that framing
collapses: a compromised edge with a write capability *will* perform the
authorised mutation. Therefore the write path cannot be secured by making the
edge more trustworthy — it must be secured by **minimising what any edge can
ever be authorised to do** (allowlist), **minimising how long a captured
credential is useful** (short-lived per-work-item creds), and **guaranteeing the
centre always knows a write capability was granted and can detect if it was not
reported** (synchronous mint audit + un-reported-mint alarm). The four
mechanisms below map one-to-one onto these levers.

---

## §3 — Chosen design: the scoped hybrid

**Ratified path: the scoped hybrid — satellite writes for an enumerated set of
low-risk op-classes, gated by a composition of four mechanisms, rolled out in
stages — NOT a per-customer full-backplane install.** (Operator determination:
[docs/decisions/satellite-write-path.md](../decisions/satellite-write-path.md).)

The evidence points away from per-customer install: #2901's scope update
committed to the **execution-point model** where "environments own their
execution point" and a satellite is the *second kind* of execution point
resolved per-environment; a per-customer full backplane duplicates the entire
control plane (Postgres, policy, approvals, audit, Keycloak) per customer and
contradicts "no hand-managed satellite inventory at fleet scale." The
management-plane-lockdown Goal (evoila-bosnia/meho-internal#234) names the
satellite write path — not a per-customer install — as its keystone.
Install-per-customer remains the *fallback* only if a given customer's risk
posture judges the composed control set below insufficient; it is not the
default.

### The four composed mechanisms

Extend the mint ladder from a binary safe-wall into a **tiered wall**, and gate
the new tier with all four:

**Mechanism 1 — Signed, single-use, expiring work items with centrally-parked
approval.** Rides the whole `gateway_command` envelope (opaque-UUID PK,
`params_hash` binding, bounded `expires_at`, one-way `consume_command` latch) and
the approval queue (durable park, human-only decision, `_approved=True`
re-dispatch as the authorization). Two new pieces:

1. **Approval-bound minting.** For the write tier, `mint_gateway_command` mints
   **only** after a committed `ApprovalRequest` for the same `(op, target,
   params_hash)` — the mint's authorization becomes the committed approval
   decision, exactly as `approve_request` re-dispatches with the gate bypassed.
   (Contrast today: mint requires a *live* `AUTO_EXECUTE`,
   `gateway_commands.py:389`.)
2. **A real signature over the canonical work item.** The centre signs the
   canonical serialisation; the runner verifies **signature + freshness + target
   scope** in `_screen_item` *before* executing. This **reverses the #2500
   decision** ("token is a bare DB-row PK, not a JWT") **for the write tier
   only** — defensible *because the consequence changed*: for a `safe` read, an
   edge-verifiable signature bought nothing over the DB latch; for a write, it
   gives the edge an offline integrity + freshness + scope check against T2 and a
   non-repudiation anchor for the effect audit (mech 4). Keep the DB consume latch
   for at-most-once — a signature does not replace central state (the #2500
   rationale still holds *for that property*).

**Mechanism 2 — Per-runner capability allowlists provisioned at enrollment.**
A per-runner-principal **allowlist** artifact (op-pattern + target-scope caps,
tenant-scoped), provisioned at enrollment and **checked twice**: at **mint** (a
capability for an op-class outside the runner's allowlist is never minted —
extends the mint ladder alongside the safe wall) *and* at the **edge**
(re-checked in `_screen_item`, defence-in-depth exactly like the safe wall
today). The allowlist **is** the definition of a runner's blast radius (T1, T8),
so it must be **minimal per environment** and, in staged rollout, start at a
single enumerated op-class. Its residual risk is exactly "the allowlist you
granted." The **enrollment path is the attack surface** (T7): the allowlist must
be bound at issuance and not self-widenable by the runner.

**Mechanism 3 — Per-work-item short-lived, response-wrapped credentials.** Rides
the backend-agnostic credential seam (`credential_backend.py`, `vault_creds.py`)
and must **close the §1.4 empty-JWT gap**. Standing broad runner creds are
**rejected for the write tier** — they are exactly the T3 worst case (a
compromised fenced host holds standing broad write power for the credential's
whole life). Instead the centre mints/brokers a short-lived,
**single-target-scoped** vendor credential bound to *this* work item, credential
TTL ≤ capability `expires_at`. To honour the no-durable-artifact rule
(`wire.py:76-80`), the disk-spooled item holds only a **single-use unwrap token**
(Vault response-wrapping), not the credential; the runner unwraps just-in-time at
execution. The unwrap fetch is the runner dialling **outbound** — push-only
preserved, no inbound listener. The runner presents *its own* runner principal
(not the acting operator) to obtain the wrapped cred, which resolves the
empty-JWT identity question. Residual: a compromised edge within the TTL can
mutate the one bound target — bounded to **one target, minutes**, versus standing
broad creds.

**Mechanism 4 — Audit parity via store-and-forward + un-reported-mint alarm.**
True synchronous-at-centre audit of the **effect** is **structurally impossible**
for a satellite write — the executing side has no DB and the mutation is off-net
(§1.3). So v0.1-spec §6 cannot hold for the remote effect; the design legislates a
**conscious exception** with compensating controls, not pretends to satisfy §6.
Two parts:

1. **Tamper-evident store-and-forward effect audit.** The runner writes a local,
   **hash-chained** (sequence-numbered, per-runner) audit record at execution
   time — one *before* it attempts the mutation, one *after* with the outcome —
   spools it, and forwards it. The centre ingests it into `audit_log` with a
   `store-and-forward` provenance marker and **verifies the chain**; a **sequence
   gap is detectable** (a dropped/suppressed record breaks the chain). The signed
   work item (mech 1) provides the non-repudiation anchor the effect record
   references.
2. **Un-reported-mint security alarm.** A central sweeper (`deadman.py` mould)
   over **minted write capabilities** past `expires_at` with `consumed_at IS
   NULL` (still `DELIVERED`/`PENDING`, `db/models.py:5254-5259`) raises a
   **security** event — distinct from the liveness `stale_at` flip. Because the
   mint is audited **synchronously at authorization**, the centre always knows a
   write capability was granted, and can detect within the expiry window that its
   effect was never reported. This is the compensating control for T4.

Residual on mechanism 4: a compromised runner controls its own signing key at the
edge, so it *can* fabricate a plausible effect record. Tamper evidence detects
**transit tampering** and **dropped records** (chain gaps), **not** a lying edge.
The residual is bounded only by the *composition*: the centre always knows a
capability was granted, knows if it went unreported, and the damage ceiling is
(allowlist, mech 2) × (credential TTL, mech 3). Audit alone cannot make an
off-net edge trustworthy — it makes the edge's silence and tampering *observable*.

### Tiered mint authorization

In `mint_gateway_command`:

- `safe` → mint on `AUTO_EXECUTE` (today, unchanged).
- a new **`remote-write` tier** (enumerated low-risk write op-classes) → mint
  **only** when (a) the op-class is on the runner's enrollment **allowlist**
  (mech 2), **and** (b) either policy `AUTO_EXECUTE` (for the idempotent subset,
  later stages) or a **committed `ApprovalRequest`** (mech 1) for the caution
  subset. Mint a **signed** work item (mech 1).
- `dangerous` / delete-shaped → **never minted to a satellite** in the staged
  rollout. This is where the design **composes with #3183**: #3183's dedicated
  destructive tier is excluded by the satellite gate by default *everywhere*, so
  delete-shaped work stays central-or-break-glass and never rides a runner. If a
  customer estate ever needs governed remote deletes, that is a separate, later
  decision built on #3183's approval + preview-hash + blast-radius model —
  explicitly out of this rollout.

### Staged rollout

A domain/estate moves one stage at a time, mirroring #234's ratchet:

- **Stage 0 (today):** `safe` reads only. No change.
- **Stage 1 (prove it):** exactly **one** enumerated, idempotent, reversible
  write op-class, on **one** fenced environment, with the **full** control stack,
  and **per-work-item human approval mandatory for every write** (even where
  policy would `AUTO_EXECUTE`), un-reported-mint alarm live. Success =
  end-to-end on one environment with zero VPN-sourced writes (the #234 DoD shape).
- **Stage 2 (widen):** extend to the enumerated low-risk class list; allow
  `AUTO_EXECUTE` (no per-write human approval) for the **idempotent** subset; the
  **caution** subset keeps mandatory approval.
- **Stage 3 (steady state):** standing capability for the enumerated classes
  within-allowlist; `dangerous`/delete-shaped stays central-or-break-glass
  **permanently** (per #3183).

### Recommendations carried to the decision record

Three sub-points are recorded in the decision record as operator-vetoable
recommendations rather than settled here:

1. **Stage-1 op-class** — recommend **one** idempotent, reversible op-class
   (e.g. a tag/annotation set) as the single Stage-1 class; the operator picks
   the exact class.
2. **Programmatic-enrollment write scope** (T7) — recommend that programmatic
   enrollment **can never grant write capability at birth**; a write allowlist
   requires a separate human step after enrollment.
3. **Revocation latency** (T8) — recommend **TTL + dead-man initially**, with a
   **per-mint revocation check** listed as the **Stage-3 gate** (so a revoked
   runner cannot execute an already-minted write once the tier reaches steady
   state).

---

## §4 — Implementation seams (filed as Tasks under #2901)

The task breakdown for the chosen path is filed on the board as sibling Tasks of
#2901, unimplemented — this initiative is the design/decision home and does not
ship code. The seams:

- **`remote-write` safety tier + tiered mint gate** — extend the safe-wall in
  `mint_gateway_command` into a tier ladder; compose the `dangerous`/delete
  exclusion with #3183; mirror the wall in `executor._screen_item` and
  `assignment_service._is_runnable_safe`.
- **Per-runner capability allowlist** — model + migration on the runner
  principal, enrollment-time provisioning, mint-time check, edge-time re-check.
- **Work-item signing + edge verification** — centre signs the canonical work
  item; runner verifies signature + freshness + target-scope in `_screen_item`
  before execute; key provisioned at enrollment.
- **Approval-bound minting for the caution sub-tier** — bind mint to a committed
  `ApprovalRequest`, mould `approve_request`'s `_approved` re-dispatch; reuse the
  human-only decision endpoints (no MCP path).
- **Per-work-item short-lived wrapped-credential brokering** — extend the
  credential-backend seam + a centre broker; runner just-in-time unwrap; resolve
  the runner's own identity for the fetch; close the §1.4 empty-JWT gap.
- **Store-and-forward tamper-evident effect audit** — runner-side hash-chained
  local audit (pre/post execution) + centre ingest with provenance + chain
  verification + gap detection.
- **Un-reported-mint security alarm** — `deadman.py`-mould sweeper over minted
  write capabilities past `expires_at` with `consumed_at IS NULL`; raises a
  security event distinct from the liveness flip.
- **Revocation hardening for write-capable runners** — per-mint revocation check
  or capability CRL / shorter TTL (the Stage-3 gate above).

---

## Appendix — key file references

- Mint gate + safe wall + audit lineage: `backend/src/meho_backplane/operations/gateway_commands.py`
- Edge executor walls + at-most-once dedup: `backend/src/meho_backplane/runner/executor.py`
- Wire models + reference-not-value credential discipline: `backend/src/meho_backplane/runner/wire.py`
- Command lifecycle (the DELIVERED-forever gap): `backend/src/meho_backplane/db/models.py:5242-5263`
- Identity gates + coarse kill switch: `backend/src/meho_backplane/auth/runner_guard.py`
- Route cage: `backend/src/meho_backplane/middleware.py:127-130,415-440`
- Credential seam + the empty-JWT gap: `backend/src/meho_backplane/connectors/_shared/credential_backend.py`, `connectors/_shared/vault_creds.py`, `auth/vault.py:226,266`
- Dead-man sweeper mould: `backend/src/meho_backplane/gateway/deadman.py`
- Approvals plane (v0.1-spec §7): `backend/src/meho_backplane/operations/approval_queue.py`
- Runner architecture: [`docs/codebase/satellite-runner.md`](../codebase/satellite-runner.md)
