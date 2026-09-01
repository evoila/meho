# Satellite runner — headless push-only deploy mode (Initiative #2415)

## Overview

The **satellite runner** is a second deploy mode of the one backplane
codebase. The central instance runs the FastAPI app
(`uvicorn meho_backplane.main:app`); a runner runs
`python -m meho_backplane.runner` — the third execution mode of the
shared container image, alongside Serve and Migrate
(`backend/Dockerfile`).

A runner exists to give the backplane reach into networks its central pod
cannot dial: targets behind NAT, private control planes, ClusterIP-only
services. The path is one-directional — a runner inside the isolated
network dials the central instance outbound, never the reverse — so the
runner is **push-only**: it initiates every connection; the center is
passive.

A runner is a *dumb executor of centrally-authorized work*. It has **no**
local Postgres, Valkey, UI, MCP, or inbound listener. Each tick it polls
central for its assignment, executes the read-only
(`safety_level == "safe"`) operations locally against the same connector
surface the central instance uses, and reports results back. All
authorization, approval, and audit stay central; the runner never
self-authorizes.

This package (`backend/src/meho_backplane/runner/`, #2497) is the runner
**chassis**: entrypoint, settings, tick loop, poll/report client,
on-disk retry spool, and work-item executor. The central endpoints the
client polls land in #2499; the long-poll command plane in #2498.

## Key types

- **`runner.wire`** — the versioned pydantic models shared verbatim with
  the central endpoints (#2499 imports these; it may widen them here, and
  must not fork a parallel copy — one codebase, one schema):
  - `RunnerAssignment` — `assignment_version` (an opaque content digest
    the runner uses only as a cache key; the digest contract is #2499's)
    plus `items`.
  - `RunnerWorkItem` — one authorized op: `check_ref`, `op_id`,
    `(product, version, impl_id)`, `handler_ref`, `params`,
    `safety_level`, a `RunnerPrincipal`, and an optional
    `ResolvedTargetDescriptor`.
  - `ResolvedTargetDescriptor` — the centrally-resolved target
    attributes a connector handler duck-reads (the runner has no local
    target table). v1 carries `name` / `product` / `version` /
    `fingerprint` / `extras` / `preferred_impl_id`; #2499 widens it with
    the connection-routing set (host/port/secret_ref/TLS).
  - `RunnerResult` / `RunnerResultBatch` — each result carries a
    runner-generated `result_uid` (uuid4) so central ingest can
    deduplicate spool re-posts idempotently. `status` is a runner-level
    tri-state: `ok` (handler ran, returned a payload), `refused` (runner
    declined), `error` (handler raised).
- **`RunnerSettings`** (`runner.settings`) — the `MEHO_RUNNER_*` config,
  a separate model from the chassis `Settings` (which hard-requires
  Keycloak + `DATABASE_URL` env a runner does not have). Resolved once
  via `get_runner_settings()`; a missing/malformed required var raises
  `RunnerConfigError` naming the variable.
- **`RunnerClient`** (`runner.client`) — an `httpx.AsyncClient` wrapper
  for the two calls (`fetch_assignment`, `post_results`). Both raise a
  single `RunnerClientError` on any transport or non-success status; a
  `304` fetch returns the `ASSIGNMENT_UNCHANGED` sentinel.
- **`ResultSpool`** (`runner.spool`) — a directory of un-posted result
  batches, one atomic JSON file per batch, drained oldest-first, bounded
  by `spool_max_files`.
- **`execute_work_item`** (`runner.executor`) — resolves and invokes one
  work item's handler locally.

## Control flow

`python -m meho_backplane.runner` → `runner/__main__.py::main()`:

1. `run_runner()` calls `configure_logging()`, then
   `get_runner_settings()` (a `RunnerConfigError` here propagates to
   `main`, which prints it to stderr and exits 1), then
   `_eager_import_connectors()` (DB-free — imports every connector
   subpackage so registrations land in the in-memory registry), then
   `asyncio.run(_async_main(settings))`.
2. `_async_main` starts the tick loop as a task and wires SIGTERM/SIGINT
   to cancel it. On signal, the task is cancelled, the loop unwinds
   (closing the httpx client via its async context manager), and the
   process exits 0.
3. The tick loop (`_run_loop`) is **sweep-then-sleep** — moulded on the
   in-process interval-tick sweepers (`topology/scheduler.py`,
   `memory/expiry.py`), **not** the DB-session-bound scheduler trigger
   loop. A fresh runner sweeps immediately rather than sleeping a full
   cadence first. Each tick's body is fully guarded: an unexpected error
   logs and waits for the next cadence; `CancelledError` propagates.

Each tick (`run_one_tick`):

1. **Drain the spool** oldest-first, stopping at the first re-post
   failure (a still-down uplink must not spin).
2. **Fetch the assignment**, echoing the cached `assignment_version` as
   `known_version`. A `304` or a fetch failure keeps the cached
   assignment — the runner keeps executing the last assignment while the
   uplink is down.
3. **Execute** each work item through `execute_work_item`.
4. **Post** the result batch; on POST failure, write it to the spool.

`execute_work_item` is fail-closed defence in depth (the real
authorization boundary is central minting, #2500):

1. Classify the item's `safety_level` against the shared satellite-mint
   tier ladder (see below) and refuse anything but a `SAFE`-tier item —
   an `EXCLUDED` (`dangerous`/`destructive`) item outright, a `remote-write`
   (`caution`) item through the fail-closed edge re-check.
2. Refuse any `handler_ref` not lexically under
   `meho_backplane.connectors.` — checked **before** import (import has
   module-load side effects) and re-checked on the resolved callable's
   `__module__`.
3. Resolve the handler via `import_handler` (dotted-path import + getattr
   walk, no DB). Rebind a bound-method handler against its connector
   instance via `is_unbound_method` + `get_or_create_connector_instance`
   (the dispatcher's own rebinding steps, minus the DB descriptor lookup;
   the connector class comes from the in-memory registry keyed on the
   payload's `(product, version, impl_id)`). Module-level handlers such
   as `net.*` need no rebinding.
4. Reconstruct the acting `Operator` from the principal context with an
   empty `raw_jwt` (no bearer token for the acting principal exists on
   the runner; the op was authorized centrally). Build the duck-typed
   target from the descriptor (`None` for targetless ops).
5. Invoke `handler(operator, target, params)`. A handler exception
   becomes a structured `error` result — a failed check is a result,
   never a crashed tick.

## Satellite-mint tier ladder — the write path (#3188)

The read path refuses any non-`safe` op at three independent layers. The
write path (Initiative #2901, decision
`docs/decisions/satellite-write-path.md`) generalises that binary safe-wall
into a **tier ladder** without widening what `safe` means. The ladder is a
runtime classification of the existing `safe < caution < dangerous <
destructive` `safety_level` enum (#3196) — **not** a new `safety_level`
value, so it needs no migration:

| `safety_level` | Satellite tier | Mint outcome |
|---|---|---|
| `safe` | `SAFE` | mints on `AUTO_EXECUTE` — the read path, **unchanged** |
| `caution` | `REMOTE_WRITE` | mints **only** through the composed write-path gate; fail-closed today |
| `dangerous`, `destructive` (or unknown) | `EXCLUDED` | **never** minted to a satellite |

The single source of truth is `runner/satellite_tier.py`
(`classify_satellite_tier` + the `evaluate_remote_write_gate` seam). It
lives beside `runner/wire.py` — the other central+edge shared contract — and
imports only the standard library, so classifying a tier on the **DB-free**
runner never pulls the central stack.

**The three-layer mirror.** All three fail-closed layers classify against
that one ladder (defence in depth — no single layer alone can punch a write
through):

- **Central mint** — `mint_gateway_command`
  (`operations/gateway_commands.py`): `EXCLUDED` → `MintRefusalCode.OP_NOT_SAFE`
  (the code #3225's conformance test asserts for `vmware.composite.vm.destroy`);
  `REMOTE_WRITE` → the composed gate, refused with
  `MintRefusalCode.REMOTE_WRITE_GATE_UNSATISFIED` until it is provisioned;
  `SAFE` → the unchanged policy gate. All checked **before** the policy gate,
  so a refused op is never parked.
- **Assignment materialiser** — `_is_runnable_safe` /
  `_validate_authored_item` (`gateway/assignment_service.py`): only `SAFE`-tier
  ops are authorable into (and materialised from) the recurring assignment;
  `remote-write` work rides the one-shot capability-mint path, not this
  document.
- **Edge executor** — `_screen_item` (`runner/executor.py`): re-screens the
  delivered item independently, refusing `EXCLUDED` outright, verifying a
  `REMOTE_WRITE` item's signature (mechanism 1, below) and re-checking it
  through the per-runner allowlist gate against the runner's **own**
  provisioning config (mechanism 2's "checked at mint *and* re-checked at the
  edge", below).

### Mechanism 1 — signed work items + approval-bound minting (#3189)

The caution (`remote-write`) tier is authorised by a **composition** of
mechanisms; #3189 lands the first: a real Ed25519 signature over the canonical
work item, plus approval-bound minting.

- **Approval-bound minting.** `mint_gateway_command` routes a `REMOTE_WRITE`
  op to `_mint_remote_write`, which mints **only** against a committed,
  un-consumed `ApprovalRequest` for the identical `(op, target, params_hash)`
  (`find_remote_write_approval` / `consume_remote_write_approval` in
  `operations/approval_queue.py`). The human approval decision **is** the
  authorization, so the live policy gate is bypassed for this tier — the exact
  mould of `approve_request`'s `_approved=True` re-dispatch. The binding is
  **param-bound** (the `params_hash` predicate is the #1503 / #3197 swap
  defence) and **single-use** (the approval's one-way `resumed_at` latch,
  claimed in the mint session so it is consumed iff the mint commits). No
  approval → `MintRefusalCode.REMOTE_WRITE_GATE_UNSATISFIED`.
- **Signed capability.** On a bound mint the centre signs the canonical
  payload (`op_id` + `params_hash` + `target_scope` + `expires_at`) with its
  Ed25519 **signing (private) key** and stamps the base64 signature on the
  `gateway_command` row (`signature` column, migration `0092`). This is the
  deliberate, **write-tier-only** reversal of #2500: for a `safe` read an
  edge-verifiable signature bought nothing, but for a write it is the offline
  integrity + freshness + target-scope check against transit tampering (T2)
  and the non-repudiation anchor the effect audit references. Asymmetric on
  purpose (`runner/work_item_signing.py`, stdlib + `cryptography` only, no DB
  import): the runner holds only the **verification (public) key**, provisioned
  at enrollment, so a compromised runner cannot forge a capability. No signing
  key → `MintRefusalCode.REMOTE_WRITE_SIGNING_UNAVAILABLE` (fail-closed). The
  DB consume latch (`consume_command`) is **retained unchanged** for
  at-most-once acceptance — the signature does not replace central state.
- **Edge verification.** `_verify_remote_write_signature` (`runner/executor.py`)
  reconstructs the canonical payload from the delivered item, verifies the
  signature with the provisioned public key (integrity + target-scope), then
  refuses a stale item on the separate `expires_at` freshness check. An
  unsigned, tampered, out-of-scope, expired, or unverifiable-key item all fail
  closed **before** any handler import.

### Mechanism 2 — per-runner capability allowlist (#3190)

The allowlist **is** the definition of a runner's write blast radius (design
§3, threats T1/T8): a `remote-write` op mints (and executes) **only** when its
op-class + target is on the runner's allowlist. Composed with #3189 so the tier
is satisfiable only when **both** halves pass — the approval binding **and** the
allowlist — and fail-closed when either is absent.

- **Storage — `runner_write_allowlist` (migration `0095`).** One row per
  granted `(op_pattern, target_scope)` capability, hung off the runner principal
  (`runner_principal_id` FK). `op_pattern` is an `fnmatch` glob over `op_id`
  (an exact op-class for a minimal Stage-1 allowlist, or a `*` prefix);
  `target_scope` is a cap — `*` (any target in the tenant) or a concrete
  `str(target.id)`. `created_by_sub` records the granting human.
- **Not at birth (T7).** Enrollment (`RunnerPrincipalService.register`) writes
  **no** rows here: programmatic enrollment can never grant write capability at
  birth. A capability requires the **separate human step**
  `RunnerWriteAllowlistService.grant`, reachable only over the operator-gated
  route `POST /api/v1/runner-principals/{name}/write-allowlist` (`tenant_admin`).
  A runner's own read-only, route-caged token cannot reach that path, so a
  runner cannot self-widen its allowlist. Read back with the `GET` sibling.
- **Shared matcher (single source of truth).**
  `evaluate_remote_write_gate(op_id, allowlist, target_scope)`
  (`runner/satellite_tier.py`, stdlib-only, DB-free) is the one matcher both
  layers run against a `RemoteWriteAllowEntry` sequence — the same
  defence-in-depth mould `classify_satellite_tier` uses.
- **At the mint** — `_mint_remote_write` reads the runner's rows
  (`load_runner_allowlist`) and ANDs the gate with the approval binding; an
  op-class/target off the allowlist (or no allowlist at all) →
  `MintRefusalCode.REMOTE_WRITE_NOT_ALLOWLISTED`, before the single-use latch
  (a refusal writes no rows / consumes no approval).
- **At the edge** — `_screen_item` re-runs the same matcher against the
  runner's **own** provisioning-config mirror (`satellite_write_allowlist`,
  parsed by `parse_runner_allowlist`), never the mint's and never a field on
  the work item. So an item allowlisted centrally is still refused if the edge
  config disagrees — defence in depth, fail-closed when unprovisioned.

The **assignment materialiser** needs no third mirror here: only `SAFE`-tier ops
are ever materialised into recurring assignments (`_is_runnable_safe`), and
`remote-write` work rides the one-shot capability-mint path exclusively, so it
never reaches that layer.

Credential brokering (#3191) and revocation hardening (#3192) are the remaining
sibling mechanisms.

**Composition with #3183.** The destructive tier is `EXCLUDED` by this ladder
everywhere, so delete-shaped work stays central-or-break-glass and never
rides a runner — the satellite write-path decision's "delete-shaped
operations never minted to a satellite," dovetailing with #3196's default
gate exclusions.

### Revocation hardening for write-capable runners (#3192, the Stage-3 gate)

The read path's revocation is deliberately **coarse**: `assert_runner_scope`
does **not** consult `revoked` (`auth/runner_guard.py`); a revoked runner is
stopped by Keycloak `enabled=false` (blocks new token grants) + a short
access-token TTL + the #2501 dead-man switch, so revocation latency ≈ token
TTL. Fine for a read (a stale read at a silent edge changes nothing). For an
already-minted **write**, that latency is the T8 gap: a revoked-but-not-yet-
expired runner could still execute the mutation (blast radius = allowlist ×
credential lifetime × **revocation latency**). The decision
(`docs/decisions/satellite-write-path.md`, recommendation 3) answers this
with **TTL + dead-man initially, and a per-mint revocation check as the
Stage-3 gate** — so the write tier does not reach steady state without it.

The revocation check is a **separate central gate** from the composed
`evaluate_remote_write_gate` (which composes the four mechanisms; #3189 wires
it): revocation is its own Stage-3 term, enforced live off the runner
principal's `revoked` column, **scoped to the `remote-write` tier only** so
the read path's coarse kill switch is untouched.

- **No new mint to a revoked runner** — `mint_gateway_command`
  (`operations/gateway_commands.py`): in the `REMOTE_WRITE` branch, a DB read
  of `runner_principal.revoked` (`_runner_is_revoked`) refuses with
  `MintRefusalCode.RUNNER_REVOKED` **before** the composed gate, so a revoked
  runner reads the specific refusal and no command row is written. A `safe`
  mint never reaches this branch.
- **No delivery of an already-minted write** (the materialisation half) —
  `claim_next_command` (`gateway/queue.py`): the poll route reads the live
  `revoked` flag off the scope-gate row (once per poll) and threads
  `runner_revoked` into the claim, which narrows to `safety_level NOT IN
  REMOTE_WRITE_SAFETY_LEVELS`. So a `remote-write` command minted *before*
  revocation is never handed to the runner post-revocation (it stays
  `pending` and expires under its capability TTL), while a queued `safe`
  command still delivers. This needs the op's `safety_level` denormalised
  onto the command row at mint (migration `0091`) — the same "bind at mint,
  check at delivery without a re-lookup" discipline as `params_hash` /
  `expires_at`.
- **At the edge** — `_screen_item` (`runner/executor.py`): the DB-free edge
  cannot itself read `revoked`, but it already fail-closes on **every**
  `remote-write` item through the unprovisioned composed gate
  (`test_remote_write_op_is_refused_without_invocation`), so an
  already-delivered write in a revoked runner's spool still refuses at
  execution today. The residual in-flight window composes with the #3191
  credential seam (`screen_remote_write_credential` +
  `WrappedCredentialBackend`): the wrapped vendor credential is brokered only
  at the authorised mint — which this task blocks for a revoked runner — and
  is **single-use and TTL-bounded ≤ the capability expiry**, so a
  delivered-but-not-yet-executed write carries a short-lived one-shot secret,
  never a standing credential. Tying the wrapped-credential unwrap directly
  to live revocation state, if ever needed, is #3191's concern; this task
  owns only the mint + delivery gate and the audit trail.
- **Observable / auditable** (mechanism 4) — `RunnerPrincipalService.revoke`
  (`auth/runner_principals.py`) writes an internal audit row
  (`method='INTERNAL'`, `path='runner.principal.revoked'`) on the central
  clock, the tamper-evident trail that a runner's write capability was
  withdrawn — distinct from the liveness `gateway.runner.stale` dead-man flip.

**Residual (bounded, not eliminated).** A command already flipped
`delivered` before revocation is off-net and cannot be recalled centrally;
that window is bounded by the capability TTL (default 5 min) plus the #3191
credential seam and the un-reported-mint security alarm (a sibling seam).

**Stage-3 promotion criterion.** A `remote-write` domain does **not** advance
to Stage 3 (standing capability within-allowlist) until this per-mint +
per-delivery revocation check is in force — the write tier does not reach
steady state on TTL + dead-man alone.

## Per-work-item wrapped-credential brokering — the write path (#3191)

A `remote-write` op mutates a vendor estate, so it needs a **vendor
credential** at the edge — but the read path's credential resolution stumbles
on the runner: `load_basic_credentials` resolves a target's `secret_ref`
through `vault_client_for_operator`, which needs `operator.raw_jwt`, and the
runner reconstructs its acting operator with an **empty** `raw_jwt` (the
empty-JWT gap, decision §1.4). Handing the runner a **standing broad** vendor
credential instead is the write path's worst case (threat T3): a compromised
fenced host would hold broad estate-mutation power for the credential's whole
life.

Mechanism 3 (`connectors/_shared/wrapped_creds.py`) resolves both without a
standing credential and without the operator JWT:

- **Centre broker** (`broker_wrapped_credential`) — at the authorised
  remote-write mint (where the operator *is* present with a real JWT), the
  centre reads **one** target's secret and Vault-**response-wraps** that single
  payload with a TTL bounded so the credential never outlives the capability
  (`credential TTL ≤ expires_at`). It returns only a `wrapped:<token>`
  reference; the credential value never leaves the centre. The brokered
  reference is set as the runner-bound target descriptor's `secret_ref` at
  mint (the approval-bound mint seam, #3189, calls the broker — this function
  *is* the seam).
- **Runner unwrap backend** (`WrappedCredentialBackend`, registered under kind
  `wrapped` on the shared credential seam) — the disk-spooled item carries only
  the single-use token as `secret_ref = wrapped:<token>`, honouring the
  no-durable-artifact rule (`wire.py`). At execution a connector handler's
  unchanged `load_basic_credentials` call dispatches to this backend, which
  dials Vault **outbound** (push-only preserved, #2877) and presents the
  **wrapping token itself** to the unwrap endpoint — *not* the acting operator
  (its empty `raw_jwt` is ignored). Vault consumes the token on the first
  unwrap, so single-use is enforced by Vault: a replay or spool redelivery
  unwraps a second time and Vault refuses.
- **Seam, not a fork.** `wrapped_creds` registers alongside `vault` / `gsm`
  (`_shared/__init__.py`, mould: `gsm_creds`) and reuses `load_vault_secret_data`
  for the centre-side read. Resolving an explicit-scheme ref (the `wrapped:`
  token) no longer consults the chassis `Settings` (`credential_backend.py::parse_credential_scheme`
  + the lazy default in `vault_creds._resolve_and_load`), so the **DB-free
  runner** — which cannot build `Settings` — resolves a wrapped credential.

**Fail-closed at the edge.** `screen_remote_write_credential` (composed after
the remote-write gate in `executor._screen_item`) refuses a `remote-write`
item whose target carries a standing/broad `secret_ref` (schemeless, `vault:`,
`gsm:`, …) or no target at all — only a single-use `wrapped:` credential may
ride the write tier, so a config that would grant a standing runner credential
fails closed. An expired or already-consumed token fails closed at unwrap.

**Runner env for the outbound unwrap.** `MEHO_RUNNER_VAULT_ADDR` (required for
the write tier — the runner needs an outbound Vault to unwrap against; unset
fails closed), plus optional `MEHO_RUNNER_VAULT_NAMESPACE` (Enterprise) and
`MEHO_RUNNER_VAULT_TIMEOUT_SECONDS` (default 10). Wrapped brokering is a Vault
feature; a Vault-free (`gsm`) deployment has no wrapped-brokering path yet.

## Store-and-forward effect audit + un-reported-mint alarm — mechanism 4 (#3193)

Mechanism 4 of the composed write-tier gate (decision
`docs/decisions/satellite-write-path.md`, design
`docs/research/2901-satellite-write-path.md` §3, threat T4) is the two
compensating controls for a **consciously-recorded exception to v0.1-spec §6**.

**Relationship to the §6 invariant (stated explicitly).** v0.1-spec §6 requires
that an operation does not return success unless its audit row commits
synchronously at the centre. For a satellite **write** that invariant *cannot*
hold for the **effect**: the executing side (a runner) has no Postgres and the
mutation is off-net, so there is no central transaction to commit the effect row
in before the mutation happens. §6 **still holds for the mint** — the capability
is minted only inside a synchronous central audit transaction
(`gateway.command.mint`, `operations/gateway_commands.py`). What §6 cannot cover
— the remote effect — is replaced by a *store-and-forward* record plus a
*detector for its absence*, not by pretending the effect was synchronously
audited. This is the recorded exception; the two mechanisms below make the edge's
silence and tampering **observable**, they do not make a lying edge honest (that
residual is bounded by the composition: allowlist × credential TTL).

**Store-and-forward tamper-evident effect audit** (`runner/effect_audit.py`,
DB-free — stdlib + pydantic, imported verbatim by the centre). For a
`remote-write` item the runner appends a **hash-chained** record **before** the
mutation (`intent`) and **after** it (`outcome`), keyed by a strictly-monotonic
per-runner `seq`; each record's `record_hash = sha256(prev_hash + canonical_body)`
folds in its predecessor's hash, and every record references the signed work
item's Ed25519 `signature` (#3189) as the non-repudiation anchor. The chain head
(`last_seq` / `last_hash`) persists on disk (`ResultSpool` mould) so a runner
restart continues the chain rather than rewinding `seq` — a rewind would read as
a gap. The executor brackets the mutation at the one seam that performs it:
`execute_work_item` records `intent` after screening passes and `outcome` after
the handler returns, only for a `REMOTE_WRITE` item with a chain provided (a
`safe` read records nothing). Records forward with the result report on
`POST /gateway/{runner}/result` (`GatewayResultBody.effect_records`), preserving
the push-only boundary (#2877).

**Central ingest + chain verification** (`gateway/effect_ingest.py`). The centre
ingests each record into `audit_log` (`method='GATEWAY'`,
`path='gateway.command.effect'`, `payload.provenance='store-and-forward'`) with
`parent_audit_id = gateway_command.mint_audit_id`, so the effect joins the
mint/result subtree the split lineage (#2500) already forms — **the existing
`gateway.command.mint` / `gateway.command.result` lineage is preserved, not
replaced.** Before writing anything it **verifies the chain** against the
persisted per-runner head (`runner_effect_chain`, migration `0094`): a `seq` that
is not `last_seq + 1` (a **gap** — a dropped/suppressed record), a `prev_hash`
that misses the accepted head (a **broken link**), a `record_hash` that does not
re-derive (a **tampered body**), or a record whose `runner_id` is not the
authenticated runner (a **cross-runner forge**) each raises
`EffectChainTamperError`. On a tamper the runner-facing endpoint rolls the whole
result submission back — a tampered report is **not** accepted, so the capability
stays unconsumed and the alarm below can still fire — and writes a durable
`gateway.command.effect.quarantine` **security** audit row. The keying
(`record.runner_id` bound to the token-authenticated runner) is why one runner
cannot extend another's chain.

**Un-reported-mint security alarm** (`gateway/unreported_mint.py`, the
`gateway/deadman.py` mould). A central-clock, advisory-lock-elected,
conditional-`UPDATE`-idempotent sweeper flags a minted `remote-write` capability
past `expires_at` still `consumed_at IS NULL` (`safety_level IN` the remote-write
set) — its effect was never reported (threat T4). Because the mint is audited
synchronously, the centre always knows a write capability was granted and detects
within the expiry window that its effect never came back. Each flip sets the
one-way `gateway_command.unreported_alarm_at` latch (migration `0094`) under a
`rowcount` gate and writes exactly one internal **security** audit row
(`path='gateway.command.unreported_mint'`, `payload.event_class='security'`).
This is a **security** monitor, deliberately **distinct** from the #2501 dead-man
switch's **liveness** flip (`gateway.runner.stale`): a runner can be perfectly
live and still execute-but-not-report one write — the exact gap the liveness
monitor cannot see. Gated on `GATEWAY_UNREPORTED_MINT_ENABLED` (default on),
cadence `GATEWAY_UNREPORTED_MINT_TICK_INTERVAL_SECONDS` (default 60), registered
in `main.lifespan` alongside the dead-man sweeper.

**Residual (bounded, not eliminated).** A fully compromised runner holds its own
genesis and can fabricate a *self-consistent* alternate chain. Tamper evidence
catches transit tampering and dropped records (chain gaps), **not** a lying edge;
that residual is bounded only by the composition (allowlist × credential TTL) and
by the un-reported-mint alarm, never by the effect record alone.

## Dead-man switch + mandatory heartbeat (#2501)

A runner that dies, wedges, or loses its network path must not leave its
workloads silently reporting last-known-good forever. Two halves make
runner liveness observable and enforced, both on the **central clock** —
a runner's own clock is never consulted.

**Heartbeat (piggybacked, never a dedicated endpoint).** Every
authenticated runner-plane request stamps `runner_principal.last_seen_at
= now()` on the central clock. The stamp lives in the single choke-point
all four runner-plane endpoints pass through —
`auth/runner_guard.py::assert_runner_scope` (#2498's `GET
/gateway/{runner}/next` + `POST /gateway/{runner}/result`, #2499's `GET
/checks/assignment` + `POST /checks/results` all call it, and nothing
else does). It is keyed by the token's unforgeable `runner_id` claim and
reads no request field, so `last_seen_at` is never client-controlled
(the same discipline `web_session.last_seen_at` follows). There is
deliberately **no** `POST /gateway/{runner}/heartbeat`: a healthy idle
runner still issues at least one authenticated request per tick (its tick
loop fetches the assignment every `tick_interval_seconds` — #2499's `GET
/checks/assignment`, default 60 s — even with no work), so the idle work
cycle *is* the heartbeat. This is the #1501 lesson — a
dedicated heartbeat loop can stay alive while the work loops are wedged,
which is exactly the zombie state to avoid; stamping the real work
requests measures the liveness that matters.

**Central sweeper (`gateway/deadman.py`).** An in-process interval-tick
loop the FastAPI lifespan owns (mould: `memory/expiry.py`, **not** the
DB-bound scheduler trigger loop). Each tick takes a fixed non-blocking
advisory lock (reaper mould; no-op on SQLite), selects the
`runner_assignments` rows whose runner's `last_seen_at` is behind the
cutoff and whose `stale_at IS NULL`, flips each with a conditional
`UPDATE ... WHERE stale_at IS NULL`, and writes one internal audit row
per flip (`method='INTERNAL'`, `path='gateway.runner.stale'`, payload
`{runner, lapse_seconds}`). The `stale_at IS NULL` predicate + the
`rowcount` gate keep "exactly one audit row per flip" true even when the
advisory lock is a no-op or two replicas race, and make an immediate
second tick a natural no-op.

**Threshold.** `threshold_seconds = gateway_runner_stale_after_multiplier
× GATEWAY_LONGPOLL_MAX_WAIT_SECONDS` — the multiplier (default 3) times the
gateway queue's exported `GATEWAY_LONGPOLL_MAX_WAIT_SECONDS` (30 s), i.e. a
default 90 s. The number is never re-hardcoded here; it is imported from the
gateway queue package. What 90 s must clear is the runner's real idle
cadence: the satellite runner is a sweep-then-sleep interval-tick loop
(`runner/loop.py`) that fetches its assignment every `tick_interval_seconds`
(#2499, default 60 s) — an authenticated request that re-stamps
`last_seen_at` — even when idle. There is no long-poll client on the runner,
so the 30 s unit is a convenient multiplicand, not the runner cadence.
**Invariant:** keep `multiplier × GATEWAY_LONGPOLL_MAX_WAIT_SECONDS ≥ runner
tick_interval_seconds` (90 s ≥ 60 s) or a healthy idle runner false-trips.

**Recovery is data-driven, never sweeper-driven.** The sweeper only ever
*sets* `stale_at`. An accepted result ingestion (`POST /checks/results`
or `POST /gateway/{runner}/result`) clears it via
`gateway/deadman.py::clear_runner_stale` — the only clear path.
Runner-level derived staleness clears the instant the runner's next
request re-stamps `last_seen_at`.

**Surfacing contract (#2416 / #2506).** `stale_at IS NOT NULL` maps to
the `UNKNOWN` state for every check assigned to that runner in the
five-state rollup #2506 defines (`UNKNOWN → degraded`). #2501 landed the
marker + audit trail only; the flip is observable on the
`runner_assignments` row and in the `gateway.runner.stale` audit path.

**Console fleet page (`/ui/runners`, #2589).** The read-only operator
console surface for the fleet: one row per registered runner principal
(`include_revoked=True` so a decommissioned runner still shows), with a
derived liveness badge — `revoked` (neutral), dead-man `unknown` (the
`runner_assignments.stale_at` marker, reusing #2506's five-state `unknown`
badge vocabulary), or `live` — plus a relative `last_seen_at`. It reads
the same in-process `RunnerPrincipalService.list_` the Bearer
`GET /api/v1/runner-principals` route uses, joined to the per-runner
`stale_at` via `gateway/repository.py::get_stale_markers`; liveness is
rendered from persisted state, never recomputed client-side. To carry the
liveness signal off the row, `RunnerPrincipalRead` gained an additive
`last_seen_at` field (#2589) — every accessor already projects it from the
ORM row via `from_attributes`, so the CLI `runner-principal list` /
`GET /api/v1/runner-principals` responses carry it too. Read-only: register
/ revoke stay on `meho runner-principal` (#2502).

**Settings.** `GATEWAY_DEADMAN_ENABLED` (default `true` — that is what
"mandatory" means: a runner cannot opt out of heartbeating because the
stamp is a request side effect, and central enforcement is on by
default), `GATEWAY_DEADMAN_TICK_INTERVAL_SECONDS` (default 30),
`GATEWAY_RUNNER_STALE_AFTER_MULTIPLIER` (default 3).

## Dependencies

- `httpx` (already a direct backend dependency) for the poll/report
  client; `httpx.MockTransport` stands in for the not-yet-built central
  endpoints in tests.
- `pydantic` v2 for the wire models and settings.
- `structlog` for JSON-to-stdout logging (`configure_logging`).
- Reused DB-free chassis primitives: `logging.configure_logging`,
  `connectors.registry._eager_import_connectors` / `all_connectors_v2`,
  `operations._handler_resolve` (`import_handler`, `is_unbound_method`,
  `get_or_create_connector_instance`), `auth.operator.Operator`, and the
  `net.*` `safe` handlers + their env-read probe allowlist.

## Boundaries / out of scope for #2497

- The central `GET /api/v1/checks/assignment` + `POST /api/v1/checks/results`
  endpoints (#2499) — they reuse and widen `runner/wire.py`.
- The outbound long-poll command plane
  (`GET /gateway/{runner}/next` / `POST /gateway/{runner}/result`) — #2498.
- Single-use capability-command minting + request-id dedup — #2500. The
  runner's tier-ladder executor guard is defence in depth, not the mint
  rule. The central mint wall (`mint_gateway_command`) classifies each op
  against the satellite-mint tier ladder (#3188, see above) *before* the
  policy gate: `EXCLUDED` (`dangerous`/`destructive`) refuses with
  `MintRefusalCode.OP_NOT_SAFE`, so the `destructive` tier (#3183) is
  excluded from every satellite — **deletes are never minted to a
  satellite** — while the additive `remote-write` tier fails closed until
  its composed gate is provisioned (satellite write-path decision
  #2901 / #3187).
- Heartbeat + central stale/unknown flipping — #2501.
- The scoped per-runner service principal + credential scoping — #2502.
  `MEHO_RUNNER_TOKEN` is the seam it fills; this chassis treats it as an
  opaque bearer.
- Change ops over the gateway, any inbound listener, arbitrary TCP
  proxying — out of scope by the initiative's design principles.

## References

- Initiative #2415 (design principles, grounding corrections); parent
  goal #221; first consumer #2416.
- `backend/src/meho_backplane/db/migrate.py` — module-run entrypoint
  mould; `backend/Dockerfile` — the execution-modes contract (Serve /
  Migrate / Runner).
- `backend/src/meho_backplane/topology/scheduler.py`,
  `backend/src/meho_backplane/memory/expiry.py` — the in-process
  interval-tick sweeper moulds; `backend/src/meho_backplane/scheduler/loop.py`
  — the DB-bound trigger loop the runner deliberately does **not** use.
- `backend/src/meho_backplane/operations/_handler_resolve.py`,
  `operations/dispatcher.py` (`_maybe_bind_method`) — DB-free handler
  resolution + the rebinding the executor mirrors.
- `backend/src/meho_backplane/connectors/net/ops.py`,
  `connectors/net/allowlist.py` — the `safe` targetless probe handlers
  the runner executes in v1.
