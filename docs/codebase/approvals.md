# Approvals (G11.2-T4 + T5; G11.7-T1 policy hardening; G0.20-T3 direct-op execute)

**Initiative:** #803 — G11.2 Agent identity + RBAC + approval;
#1397 — G11.7 Approval-policy hardening; #1500 — G0.20 v0.10.1 dogfood
hardening
**Tasks:** #817 (T4 — durable approval queue) + #818 (T5 — operator
surfacing channel) + #1401 (G11.7-T1 — queue humans, self-approval
guard, resume-target fix, write-op secret redaction) + #1503 (G0.20-T3
— execute a parked direct operator op on approve via every surface)

## Overview

Which calls wait for approval depends on the op's safety level and on who
calls it (an agent, a person, or a service account). The table in
[agent-permission-model.md](agent-permission-model.md#safety-levels-at-a-glance)
shows each case.

The approval substrate parks `requires_approval` / `caution` / `dangerous` / `destructive`
dispatches durably and lets an operator approve or reject them
across **REST, MCP, and CLI** transports. Two layers, one source of
truth:

- **T4 owns the queue lifecycle** (`backend/src/meho_backplane/operations/approval_queue.py`):
  `create_pending_request`, `approve_request`, `reject_request`,
  `expire_stale_requests`. Every mutation writes a synchronous audit
  row in the same transaction (the "request" row on create; the
  "decision" row on approve/reject/expire).
- **T5 wires the operator surfaces on top:** `GET /api/v1/approvals/{id}`
  (inspect), `meho_approvals_{list,get,approve,reject}` MCP tools, and
  `meho approvals {list,show,approve,reject}` CLI verbs. Since #3758 no
  approval step is published to Broadcast. `record_approval_step_event`
  records `approval.{pending,approved,rejected,expired}` only for the
  paired add-on that asked (#3027); the audit log records every step; the
  agent waiter, the bell badge and the console read the stored row.

The durable state is one `approval_request` row (table created by
`backend/alembic/versions/0023_create_approval_request.py`, T4 #817).

## Approval-TTL lifecycle (#2322)

Parked approvals expire on a TTL so a pending queue cannot grow without
bound. The lifecycle has two halves, both wired end-to-end by #2322 (the
`expired` status, the `expires_at` column, and `expire_stale_requests`
all pre-existed but had **zero production callers** and no park path ever
stamped a deadline — the machinery was shipped but inert):

1. **Stamp at park.** `create_pending_request` now stamps every parked
   row with `expires_at = created_at + APPROVAL_DEFAULT_TTL` (setting
   `approval_default_ttl_seconds`, default 14 days) so no row is ever
   parked with a null deadline. This covers **every** transport
   uniformly — REST, MCP, and run-bound dispatches all park through the
   dispatcher's `_handle_needs_approval` → `create_pending_request`, and
   the composite park path calls the same function. An explicit caller
   `expires_at` is honoured but **capped** at that same ceiling
   (`_bounded_expires_at`): the single configured TTL doubles as the
   upper bound, so a surface can ask for a *shorter* deadline (e.g. a
   preflight probe) but not an unbounded-lived one. There is deliberately
   no per-op TTL policy — one global default is the whole knob (substrate
   minimalism, #1177).

2. **Sweep periodically.** `operations/approval_expiry.py` runs a
   dedicated background sweeper (mirroring the sibling
   `agents/grant_expiry.py` and `memory/expiry.py` sweepers): a
   lifespan-owned `asyncio` task on a fixed cadence
   (`approval_expiry_tick_interval_seconds`, default 300s), gated on
   `APPROVAL_EXPIRY_ENABLED` so an operator running an external sweep does
   not double-expire. Each tick enumerates tenants and calls
   `expire_stale_requests` under a per-tenant **system operator**
   (`system:approval-expiry`, `OPERATOR` role) — the same per-tenant
   system-operator shape `topology/scheduler.py` uses, required because
   `expire_stale_requests` is tenant-scoped on `operator.tenant_id`. It
   is a *separate* loop from the cron/one-off agent scheduler
   (`scheduler/loop.py`), independent of `SCHEDULER_ENABLED`, because the
   established convention for a governance-surface expiry sweeper is its
   own opt-out + cadence.

**Legacy null-expiry rows.** Rows parked before #2322 carry
`expires_at IS NULL`. `expire_stale_requests` takes an optional
`default_ttl`; when the sweeper passes it, a null-expiry row is coalesced
against `created_at + default_ttl` (expressed as
`created_at <= now - default_ttl` so the predicate binds two plain
datetimes and stays dialect-portable, the same discipline the
grant-expiry sweeper uses) and, when that coalesced deadline has passed,
the row is expired and its `expires_at` is backfilled to the coalesced
value so the decision audit row and the persisted row agree. No schema
change or migration is needed — the coalesce lives entirely in the query
predicate.

Expired rows leave the default `?status=pending` view (the status filter
already excludes non-pending rows) and are visible under `?status=expired`
on every read surface (REST, MCP, UI queue). An `expired` row is
terminal, so it cannot be approved (the pending guard raises
`ApprovalRequestAlreadyDecidedError`) — an expired run-bound request can
never be claimed or re-dispatched (#2293).

## The params hash on requests and audit rows

Two kinds of rows carry a keyed hash of a call's params:

- **The approval request** (`approval_request.params_hash`) binds the exact
  call. It is HMAC-SHA256 with a server key over every value
  (`compute_params_hash` in `operations/_validate.py`). Before an approved
  call runs, MEHO checks the params against it (`params_hash_matches`), so a
  call that changed any value, a password too, does not match.
  The request also keeps the params themselves, because MEHO runs the
  approved call with them:
  - For most operations they are stored in plain form in
    `approval_request.params`.
  - For a `credential_write` operation they are not on the request row.
    They are encrypted in `approval_execution_payload` instead (see
    "Credential-write execution custody" below).
- **Audit rows** (`payload.params_hash` on dispatch, MCP and hosted-agent
  tool rows, and `operation_run.params_hash`) store the hash, not the params.
  They use the same keyed hash, but each secret-named value (`password`,
  `client_secret`, `api_key` and similar, the names the broadcast scrub
  uses) is replaced by a fixed marker first (`compute_audit_params_hash`).
  Two calls with the same other values get the same hash. For a call without
  secret-named values, both hashes are equal.

The server key is derived from `DATABASE_URL` (HMAC-SHA256 under a fixed
label). Every install has that value, the key is never stored, and there is
no new setting. What follows:

- Hashes can be compared inside one install. Rows written before the keyed
  hash hold a plain SHA-256: they stay readable, but they do not match new
  hashes. The column format (64 hex characters) is unchanged, so there is no
  migration.
- A request that was already waiting for approval when MEHO was upgraded
  holds the plain hash. The approval check accepts it for the request's own
  params, so the request can still be approved.
- When `DATABASE_URL` changes (for example a new database password), the key
  changes: new hashes no longer match older ones, and a request that waits
  for approval at that moment must be asked for again.
- A client cannot compute these hashes itself, because they need the server
  key. To match a request to a call, use the `approval_request_id` the call
  returns, or the `preview_hash` of a destructive call.

The satellite work item is the exception: `gateway_command.params_hash` is a
plain SHA-256 (`params_digest`), because the runner checks it again and holds
no server key. That row stores the params themselves anyway.

The effect rows a satellite forwards (`gateway.command.effect`) are audit rows
like the others:

- `params_hash` is the same audit hash, computed from the command's stored
  params. When no command matches the record's digest, it is the keyed hash
  of the runner's digest (`compute_audit_digest_hash`).
- `record_hash` and `signature` hold keyed hashes too.
- The plain values stay where they are checked: with the runner, in the chain
  head (`runner_effect_chain`) and on the `gateway_command` row.

## Credential-write execution custody (#3537)

For every operation classified as `credential_write`, the parked
`approval_request` stores no raw dispatch params and no raw composite-parent
params. It retains the existing `params_hash` swap defence plus an opaque
`execution_handle`. The original execution input is encrypted with the
dedicated `APPROVAL_HANDOFF_ENCRYPTION_KEY` in the one-to-one,
tenant-bound `approval_execution_payload` table. Approval read surfaces only
read the request row, so the ciphertext and plaintext stay out of REST, MCP,
CLI, UI, audit, and broadcast views.

Parking fails closed before its transaction commits if the key is missing or
not a valid Fernet key. The key is deliberately independent of
`UI_SESSION_ENCRYPTION_KEY`: console enablement must never decide credential
custody.

### Required: set the custody key (#3712)

**`APPROVAL_HANDOFF_ENCRYPTION_KEY` is REQUIRED** on every deployment where a
credential-write operation (for example a Vault KV write) may wait for
approval. This holds for human and service callers alike. Without a valid key,
MEHO denies the operation instead of letting it wait, with the message
`credential-write approval custody is unavailable; configure a valid
APPROVAL_HANDOFF_ENCRYPTION_KEY before parking this operation`. The Helm chart
keeps `approvalHandoff` off by default, so an install with unchanged values has
no key. Since v0.35.2 such an install denies these operations.

Not affected: a credential-write that runs at once under a standing grant (it
never waits, so it needs no key), and approvals that were already waiting
before the v0.35.2 upgrade.

Check the state before the first denial: `GET /ready` shows
`features.approval_custody` with `custody` set to `configured`, `missing` or
`malformed`. The key value never appears there. `helm install` and
`helm upgrade` also print a warning while `approvalHandoff.enabled` is false.

Set it up in three steps:

1. Create a Fernet key:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. Store it in a Kubernetes Secret. Pipe it in, so the key stays out of shell
   history and the process list:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode(), end='')" \
     | kubectl -n <namespace> create secret generic meho-approval-handoff \
         --from-file=approval_handoff_encryption_key=/dev/stdin
   ```

   Or sync the Secret through your usual secret manager (for example an
   ExternalSecret).

3. Point the chart at the Secret and run `helm upgrade`:

   ```yaml
   approvalHandoff:
     enabled: true
     secretName: meho-approval-handoff
     secretKey: approval_handoff_encryption_key
   ```

With a built-in part of the core on (`corePostgres.enabled` or
`coreKeycloak.enabled`), the install makes this key itself, in the Secret
`<release>-core-approval-handoff` (key `approval_handoff_encryption_key`).
Skip steps 1 and 2 and set `approvalHandoff.secretName` to that name. See
[core-secrets.md](core-secrets.md).

The chart uses `secretKeyRef`; never place the key in `values.yaml`. Rotate by
creating a new Fernet key in the referenced Secret and restarting all
backplane pods together. Existing unexecuted credential-write approvals were
encrypted with the old key, so reject or let them expire before rotation; they
cannot be magically backfilled or decrypted by a new key. Rows parked before
this migration remain historical rows and use their existing compatibility
path; the migration deliberately does not copy legacy plaintext into the new
custody table.

Only the winner of the durable `resumed_at` claim can decrypt and delete the
payload. A loser never reads it. Reject and expiry delete the custody row in
the same transaction as the terminal state; successful claimed execution
deletes it in the claim winner's transaction. A dispatch failure after a
claim is intentionally not retried, preserving the existing exactly-once
write rule.

## Transactional decision transitions + decision-time deadline (F12 / #274)

The TTL sweep above is a *background* safety net; it is not the only
guard. Two hardening properties make the decision paths correct
independently of the sweep and under concurrency (security review finding
F12):

1. **Decision-time deadline gate.** `approve_request` re-checks the
   deadline on the row it is about to approve (`_load_pending_for_approval`
   → `_deadline_has_passed`, the Python mirror of the sweep's
   `_deadline_passed_clause`, same legacy null-expiry coalesce). An overdue
   pending row is refused with `ApprovalRequestExpiredError` (mapped to HTTP
   409 `approval_request_expired` on REST and the console) **even if the
   sweeper is delayed or failing** — the sweep's default cadence is 300s and
   its per-tick failures are swallowed to keep the loop alive, so trusting it
   alone would leave an overdue intent approvable for a whole tick. The gate
   only *refuses* the approval; the row stays `pending` for the sweep to
   transition to `expired` with its decision audit row. `reject_request`
   does **not** apply the gate — rejecting an overdue request is harmless
   (both are non-executing terminal states).

2. **Row-locked transitions — exactly one winner.** `approve_request` and
   `reject_request` load the row `SELECT ... FOR UPDATE`
   (`_load_for_tenant(..., for_update=True)`), and the sweep selects
   `FOR UPDATE SKIP LOCKED`. A competing approve / reject / expiry on one
   row therefore serialises on the row lock: the winner commits its
   transition and its single decision audit row in one transaction; the
   loser blocks on the lock, re-reads the committed terminal state, and hits
   the pending guard (`ApprovalRequestAlreadyDecidedError`) or, for the
   sweep, skips the locked row. The result is one winning transition, one
   consistent decision record, and no conflicting overwrite. The
   notification publishes only **after** commit (unchanged), so a phantom
   event cannot outlive a rolled-back decision.

The **execution claim** carries the same discipline: `claim_resume`'s
conditional `UPDATE ... WHERE resumed_at IS NULL AND status = 'approved'`
now requires the `approved` state in the same statement as the
single-resumer latch (previously it checked only `resumed_at IS NULL`). So
no execution can follow a losing / rejected / expired transition — a
request another decision won cannot be claimed for dispatch — while the
exactly-one-resumer latch (#2293) is preserved. Every resume surface already
reaches the claim only after the decision committed `approved`, so this
tightens the invariant without changing the live control flow; it fails
closed if a resume is ever attempted against a non-approved row.

On SQLite (the unit-test driver) `FOR UPDATE` / `SKIP LOCKED` are silent
no-ops, so the concurrency guarantee is proven against real Postgres in
`tests/integration/test_approval_transactional_transitions_e2e.py`
(concurrent approve/reject and reject/expiry races, one-winner + one
decision-row + no-execution-for-the-loser), alongside the sibling
exactly-one-resumer suite.

## Subject + actor attribution on the request row (#1481)

The `approval_request` row carries the RFC 8693 two-claim attribution
shape:

- `principal_sub` = the subject — `operator.sub`, the party on whose
  behalf the parked op runs (a human, or an autonomous agent acting as
  itself).
- `principal_act` = the actor — the agent principal currently wielding
  the subject's authority on a *delegated* run, or `NULL` when there is
  no separate actor.

`create_pending_request` sources `principal_act` from
`resolve_actor_sub()` (`backend/src/meho_backplane/auth/delegation.py`)
— the same `actor_sub` contextvar the synchronous audit log reads. A
human-initiated agent run binds the acting agent via `actor_delegation()`
(`backend/src/meho_backplane/agent/invocation.py`) for the lifetime of
the run task, so the parked row records `principal_sub=<human>` +
`principal_act=agent:<name>`, keeping the approval row's lineage
in lock-step with the audit log. A direct human call (no delegation
bound) and an autonomous agent run both resolve to `principal_act=NULL`.
(Before #1481 the field read a nonexistent `Operator.identity_act`
attribute and was always `NULL`.)

### Display-name resolution alongside the sub (#3300)

Every operator-facing approvals surface — CLI `approve` / `show` /
`list`, the `/decide` response, and the console approvals modal /
history / panel — historically rendered `principal_sub` (the requester)
and `reviewed_by` (the approver) as bare OIDC `sub` GUIDs, so an
operator reviewing or auditing an approval saw an identity GUID at the
moment they most needed to know *who*. These surfaces now render a human
display name **alongside** the `sub`, never instead — the `sub` stays
the stable, machine-truthful key. This is the first slice of Initiative
#3301 (console + CLI GUID-to-name resolution).

**No live lookup — the name is hoisted at write time.** There is no
`sub` → name join table (the agent / runner principal tables key on the
Keycloak client id, not the token `sub`; see
`meho_backplane.ui.references`), so the name cannot be resolved after the
fact. Migration `0097` adds two nullable `approval_request` columns:

- `principal_name` — the requester's name, set in
  `create_pending_request` from the parking `Operator.name`.
- `reviewed_by_name` — the approver's name, set in `approve_request` /
  `reject_request` from the deciding `Operator.name`.

Both come from the JWT `name` claim — the same hoist-at-write pattern
`audit_log.principal_name` uses (#1212). The approval audit rows
(`_write_audit_row`) carry the same `payload['principal_name']` so the
name-aware audit views resolve the row's `operator_sub` too, and the
`/decide` response gains an additive `decided_by_name` beside
`decided_by`.

**Fail-open contract.** Resolution can never fail or slow a decision: no
identity provider is called; the surface reads a column already loaded on
the row. When no name was recorded — a token without a `name` claim, or a
pre-`0097` row — the column is `NULL` and every surface degrades cleanly
to the raw `sub` it showed before. The shared resolver
`meho_backplane.ui.references.subject_ref(sub, name)` returns a
`SubjectRef` whose `.display` is `name or sub` (and `None` when there is
no sub at all, e.g. an undecided request's reviewer); the console
`subject(ref)` macro in `_references.html` renders the name leading with
the `sub` alongside in a muted mono span. The CLI mirrors this:
`principalLabel` renders `<name> (<sub>)` for single-line fields
(`approve`, `show`); `principalScanLabel` renders the name for the
width-constrained `list` PRINCIPAL column, where the full sub stays
reachable via `show` / `--json`.

**No PII beyond the display name.** These surfaces resolve to a human
display name only — never email, groups, or other profile fields.
`SubjectRef` is deliberately distinct from `PrincipalRef` (the audit /
broadcast drawer's principal, which also carries email and a service
marker): the approvals contract keeps PII off these surfaces, and #1212
already carries email on audit rows where it exists — that is not
widened here.

### Reviewer-context contract (#3353)

Display names (#3300) resolve *who* asked; the reviewer-context contract
resolves *what* is being asked. A parked request persists
machine-truthful coordinates — a placeholder `op_id` (a composite child
is gated on the un-substituted path, e.g.
`POST:/vcenter/vm/{vm}/power?action=start`, so the grant is
subject-agnostic), a `target_id` GUID, a `params_hash` — none of which a
human can read at the sign-off moment. `resolve_reviewer_context`
(`operations/approval_context.py`) resolves them into a single redacted,
human-legible `ReviewerContext` that **both** operator surfaces render:
the console "Review approval request" modal and the CLI `meho approvals
show`. One resolver, two consumers, so the surfaces never drift — a
regression test (`test_console_and_rest_render_same_resolved_context`)
asserts the REST projection and the console expose the same resolved
values.

What it resolves, all fail-open to `None`:

- **target** — the registered target's `name` + `product` / `version`
  alongside the bare `target_id` GUID (tenant-scoped; a cross-tenant id
  resolves to no name, never leaking it).
- **subject** — for a path-variable op, the concrete entity the write
  touches, recovered from the *identity* fields the caller supplied on the
  gate params (a human `name` and/or the moid bound to a path variable),
  so the reviewer reads `web-01 (vm-1042)` instead of an unresolved
  `{vm}`. The gate params already carry the sub-op's full logical params
  (`connectors/vmware_rest/composites/_write.py`), so no live read is
  needed at render time.
- **parent composite** — for a composite *child* park, the op id of the
  composite that fanned it out, walked off the `request_audit_id` →
  `parent_audit_id` lineage the #3348 direct-seam fix records (see
  *Composite-child park lineage* below).
- **run context** — the `work_ref` change-ticket the automation stamps
  and the originating `run_id`.
- **blast radius** — the preview's blast-radius block when a preview
  produced one (destructive tier / #3312 preview parity), lifted off
  `proposed_effect` so both surfaces render the same block.
- **summary** — a redacted plain-language "what will happen" sentence
  built from the operation's catalog summary (or the raw op id when no
  descriptor resolved) + the resolved subject + target.

**Secret hygiene is the hard constraint.** Params stay hidden by design
(the swap-defence + re-dispatch input the read views never project). The
resolver reads **identity fields only** — the path-variable moids and a
caller-supplied `name` — never the request body, arguments, or env
values. A credential-class op (`classify_op` → `credential_*`) gets
**no** subject echo at all. Every candidate identity value is run through
the same connector-boundary redaction engine the response path uses; a
value the engine flags as secret-shaped (a JWT, a bearer token) is
dropped rather than surfaced. Tests under `test_approval_context.py`
assert no secret-keyed param and no secret-shaped value reaches the
summary.

**Fail-open everywhere.** Resolution never blocks or slows a decision:
every sub-resolution degrades to the raw id / a `None` field, and the
whole call is wrapped so a failure returns an empty context rather than
raising. The REST view carries the context on `GET
/api/v1/approvals/{id}` only (its resolution runs a few tenant-scoped
reads); the list view omits it. The field is additive — a pre-#3353
client, or a resolution miss, renders exactly what it did before.

## MCP audit status for post-gate rejections (#1481)

A `tools/call` that a tool handler rejects *after* the dispatch gates
(name / arguments / unknown-tool / RBAC / schema) — e.g. the
approval-queue's self-approval, `approval_request_not_found`, or
`approval_unauthorized` re-raised as `McpInvalidParamsError` — returns
JSON-RPC `-32602` on the wire. The MCP envelope handler
(`backend/src/meho_backplane/mcp/handlers.py`) projects that whole class
onto audit `status_code=403` ("denied") instead of the init `500`
("error"), so the audit row and the live broadcast event
(`_classify_mcp_status`) classify a clean policy rejection as denied
rather than a fake server crash. The correction sits at the dispatch
boundary (`except McpInvalidParamsError` in both `handle_tools_call` and
`handle_resources_read`), so it covers every post-gate
`McpInvalidParamsError`, not just self-approval; explicit pre-gate
branches that already set `400`/`403`/`404` pass through untouched, and a
genuine handler fault still records `500`.

## G11.7-T1 policy hardening (#1401)

Phase C of the wrapper-retirement effort moves connector **writes**
behind this queue, and ops-team operators run those writes as **human**
(`USER`) principals. Four policy changes harden the gate for that — all
reusing the existing queue/approve/resume substrate (no new table,
model, or resume endpoint):

1. **Queue humans, don't hard-deny.** `policy_gate`
   (`backend/src/meho_backplane/operations/_validate.py`) returns
   `NEEDS_APPROVAL` — not `DENY` — when a non-agent principal hits a
   `requires_approval=True` op. The op is parked + resumable, not
   denied to the operator meant to run it. Non-`requires_approval` ops a
   human has always been able to run still auto-execute (the v0.2
   default-allow is preserved), so no existing human-runnable op
   regresses.
2. **Self-approval guard (requester != approver).** `approve_request`
   raises `SelfApprovalForbiddenError` (→ HTTP 403 `self_approval_forbidden`
   on REST, invalid-params on MCP) when `operator.sub ==
   request.principal_sub`, unless the audited **emergency break-glass**
   switch `APPROVAL_ALLOW_SELF_APPROVAL=true`
   (`Settings.approval_allow_self_approval`, default `False` =
   fail-closed) is set. Both wire strings keep the
   `self_approval_forbidden` token as a prefix and append the
   exception message, so the `APPROVAL_ALLOW_SELF_APPROVAL` break-glass
   hint reaches the operator-facing REST `detail` and MCP error message
   rather than being dropped to a bare token (#1483). Even under break-glass the self-approval writes
   its decision audit row, so the use is forensically visible. **Reject
   is unguarded** — withdrawing one's own pending request is never a
   privilege escalation. The guard runs after the role check and before
   the params-hash check, so a self-approver gets the precise refusal
   reason.

   **How to audit a break-glass self-approval (#2087).** There is
   **no** `self_approved` field on the audit row — do not filter for
   one. When break-glass is enabled and a self-approval goes through,
   `_check_self_approval` (`approval_queue.py`) emits the structured
   **log event** `approval_self_approval_break_glass` (WARNING, with
   `approval_request_id`, `op_id`, `operator_sub`, `tenant_id`) and
   returns; the decision audit row written by `approve_request` via
   `_write_audit_row` carries the ordinary payload
   (`approval_request_id`, `op_id`, `connector_id`, `principal_sub`,
   `result_status`, `decision`, `reviewed_by`, optional `reason`).
   A self-approval is therefore identified **on the ledger** as the
   `approval.decision` row where the requester equals the approver —
   `payload.principal_sub == payload.reviewed_by` (the approver's
   `operator.sub`). A first-class `self_approved` audit marker would
   be net-new, separately scoped work. Whether the deploy currently
   allows self-approval at all is visible without grepping the values
   file: `GET /ready` → `features.approval_queue.effective_posture`
   is `"single_operator_break_glass"` when
   `APPROVAL_ALLOW_SELF_APPROVAL=true`, `"four_eyes_enforced"`
   otherwise (see `features.py::_approval_queue_block`).

   `APPROVAL_ALLOW_SELF_APPROVAL` is an **emergency** escape, not the
   endorsed single-operator posture: enabling it posture-wide re-opens,
   for every op, the single-account request+grant hole this guard
   closes (#1401). Single-operator tenants should park their four-eyes
   writes under an **agent-requester** (a distinct
   `principal_kind=agent` `sub`) instead
   — see [Single-operator tenants: use an agent-requester, not
   break-glass](#single-operator-tenants-use-an-agent-requester-not-break-glass-1738)
   below.
3. **Resume target re-hydration.** The REST `…/approve` route persists
   only `target_id` on the row, so it re-loads the live `Target` by id
   (`targets.resolver.resolve_target_by_id`, tenant-scoped, `deleted_at
   IS NULL`) before the `_approved=True` re-dispatch. A write op whose
   handler reads `target.host` / `target.name` / `target.fqdn` now
   resolves the correct target instead of `None`. A target soft-deleted
   between request and approval resolves to `None`, so the re-dispatch
   fails closed (structured connector error) rather than reviving a
   tombstone. Tenant-wide ops (no original target) keep `target_id IS
   NULL` → `None`.
4. **Write-op secret redaction.** The broadcast/audit sensitivity
   classifier gained two op-id classes so the new Phase-C write ops'
   secrets never land in an audit row or broadcast frame —
   `credential_write` for request-param secrets (`vault.kv.put`,
   `vault.auth.userpass.write` / `update_password`, `k8s.secret.create`)
   and `credential_mint` extended for response secrets
   (`vault.token.create`, `vault.auth.approle.generate_secret_id`). Both
   collapse to aggregate-only and are **non-upgradeable** — no per-call
   or per-tenant override may surface the credential on the feed. See
   `docs/codebase/broadcast.md`.

The `_approved=True` re-dispatch gate-bypass (the resume authorization)
is unchanged in mechanism but now matters for humans too: with humans
routed to `NEEDS_APPROVAL`, re-running the gate on resume would re-queue
the call instead of executing it, so the bypass is what lets the
approved op run.

## Delegated decisions: the reason-line convention (#3692)

Sometimes an approver decides because **another person told them to, in
writing**. The approver still decides under their own identity
(`reviewed_by`). The decision reason records the other person, their
words, and where the work is tracked. It uses three `Key: value` lines
at the end of the reason, in the style of git trailers
(`Signed-off-by:`):

```text
Ran the delete after sign-off in the change window.

Instructed-by: lead@example.com
Instruction: "please run the cleanup tonight"
Refs: #12, CHG-9
```

Rules (`parse_delegation` in `operations/approval_queue.py`, pure and
deterministic):

- A reason is **delegated** when any line starts with `Instructed-by:`.
  Keys are case-insensitive.
- The trailer lines must be the **last paragraph** of the reason (after
  a blank line), and every line in that paragraph must be `Key: value`.
  Other keys in the block are ignored.
- `Instructed-by:`, `Instruction:` and `Refs:` must all be present and
  non-empty. `Refs:` is comma-separated. Surrounding double quotes on
  the instruction are removed; the words inside are kept as written.
- A key may appear only once.

If a delegated reason breaks a rule, `DelegatedReasonInvalidError` is
raised **before** any state change, so the row stays `pending` and no
decision audit row is written. Both `approve_request` and
`reject_request` call the parser, so every decide surface gets the same
check:

| Surface | Refusal |
|---|---|
| REST `/approve`, `/reject`, `/decide` | 422, `detail` starts with `delegated_reason_invalid:` |
| Console approve / deny | the modal re-renders with a 422 banner naming what is missing |
| CLI `meho approvals approve` / `reject --reason` | posts to `/decide`; prints the 422 body |

**On the ledger.** There is no new column and no migration (the same
choice as the #2087 break-glass audit). A valid delegated decision adds
a `delegation` object to the `approval.decision` audit row's payload,
next to the verbatim `reason`:

```json
{"decision": "approved", "reviewed_by": "<approver sub>",
 "reason": "Ran the delete ...\n\nInstructed-by: ...",
 "delegation": {"instructed_by": "lead@example.com",
                "instruction": "please run the cleanup tonight",
                "refs": ["#12", "CHG-9"]}}
```

It also emits the WARNING log event `approval_delegated_decision`
(`approval_request_id`, `op_id`, `decision`, `reviewed_by`,
`instructed_by`, `tenant_id`). Find delegated decisions on the ledger by
filtering `approval.decision` rows whose payload has a `delegation` key.

**On the read surfaces.** `get_decision_delegation` reads the block back
from the decision audit row. `GET /api/v1/approvals/{id}` carries it as
the optional `delegation` field of `ApprovalRequestView` (the list view
leaves it `null`). `meho approvals show` and the `approve` / `reject`
summary print `Instructed by` / `Instruction` / `Refs`. The console shows
the same three lines on the decided modal and on the outcome card right
after a decision (`project_request_to_view(row, delegation)`).

**Reason length.** All three REST bodies (`ApproveRequestBody`,
`RejectRequestBody`, `DecideRequestBody`) and the console form cap
`reason` at `MAX_REASON_LENGTH` (2000 characters). A longer reason is a
422.

`work_ref` is unchanged: it stays the requester-side change ref. The
`Refs:` values may match it, but they do not have to.

### Delegated decisions are allowed only on lab-class deployments (#3693)

On production, an approve or reject must be the approver's own call.
A decision made on someone else's instruction is allowed only on a
lab-class deployment. MEHO has no environment object, so the deployment
says which class it is with one boot switch, like the self-approval
switch:

| Setting | Env var | Helm value | Default |
|---|---|---|---|
| `Settings.approval_allow_delegated_decision` | `APPROVAL_ALLOW_DELEGATED_DECISION` | `config.approvalAllowDelegatedDecision` | `false` (production) |

The switch is read from the environment at boot. No request field,
header, or parameter can change it.

How the check runs (`_check_delegated_allowed` in
`operations/approval_queue.py`):

- Both `approve_request` and `reject_request` call it right after
  `parse_delegation`, before any state change. So it covers REST, CLI,
  and console at once. There is no MCP decision path.
- An ordinary decision (no `Instructed-by:` line) always passes, on
  every deployment.
- An incomplete delegation block is still a 422 (`delegated_reason_invalid`),
  checked first.
- A valid delegation block with the switch off raises
  `DelegatedDecisionForbiddenError`. The row stays `pending` and no
  decision audit row is written. MEHO logs the WARNING event
  `approval_delegated_decision_refused` (`approval_request_id`, `op_id`,
  `decision`, `operator_sub`, `instructed_by`, `tenant_id`).
- With the switch on, the decision goes through and is recorded as
  described above.

How a refusal reads:

| Surface | Refusal |
|---|---|
| REST `/approve`, `/reject`, `/decide` | 403, `detail` starts with `delegated_decision_forbidden:` |
| Console approve / deny | the modal re-renders with a 403 banner |
| CLI `meho approvals approve` / `reject --reason` | posts to `/decide`; prints the 403 body |

`GET /ready` shows the stance in
`features.approval_queue.delegated_decision_posture`:
`"production_own_hand_only"` (switch off) or
`"lab_class_delegation_allowed"` (switch on).

## Single-operator tenants: use an agent-requester, not break-glass (#1738)

The self-approval guard enforces requester != approver on the stable
`sub` claim. On a tenant with **one** human operator this looks like a
deadlock: that operator parks a `requires_approval` write as a human
(`USER`) principal, then is the only identity available to approve it,
so `approve_request` raises `self_approval_forbidden`. The break-glass
switch `APPROVAL_ALLOW_SELF_APPROVAL` exists for that case — but it is an
**emergency** escape, not the everyday answer. Flipping it posture-wide
re-opens, for every op, the single-account *request + grant* hole #1401
was created to close. Whether a deploy has it set is surfaced on
`GET /ready` → `features.approval_queue.effective_posture`
(`"single_operator_break_glass"` vs the default
`"four_eyes_enforced"`, #2087).

The everyday answer is an **agent-requester**: park the write under an
**agent principal** (`principal_kind=agent`) rather than under the human.
An agent principal is a first-class identity — a Keycloak
`client_credentials` client `agent:<name>` with its own stable `sub`,
minted by `meho agent-principal register` (see
[`keycloak-agent-client.md`](../cross-repo/keycloak-agent-client.md)).
When the agent parks the request, `create_pending_request` sets
`principal_sub=<agent-sub>` (it always reads `operator.sub` —
`approval_queue.py:338`). The human operator who later approves carries a
**distinct** `sub`, so `_check_self_approval`
(`approval_queue.py:816-831`) takes its `operator.sub != request.principal_sub`
early-return and the approval clears — **with no break-glass flag and no
new tunable**, and with the full subject/actor lineage intact (the
decision audit row records the human approver; the request row records
the agent requester).

### How the agent becomes the requester

The agent must be the *subject* of the parked request, not merely an
actor on a human's behalf. That distinction is the RFC 8693 token-
exchange shape MEHO encodes (see [Subject + actor attribution on the
request row](#subject--actor-attribution-on-the-request-row-1481) above),
and it is the difference between the two agent-run entry points in
`backend/src/meho_backplane/agent/invocation.py`:

- **Autonomous / scheduled run** — `AgentInvoker.run_scheduled`
  (`invocation.py:951`) authenticates the agent under its **own**
  `client_credentials` grant and deliberately binds **no**
  `actor_delegation` (`invocation.py:1117-1118`). The agent is the sole
  subject: `operator.sub` resolves to the agent, so the parked row gets
  `principal_sub=<agent-sub>` and `principal_act=NULL`. **This is the
  agent-requester path.** A human operator with a different `sub`
  approves the row and clears the gate.
- **Human-initiated delegated run** — `AgentInvoker.run`
  (`invocation.py:795`) is launched by a human, so it binds the acting
  agent as the RFC 8693 **actor** via `with actor_delegation(...)`
  (`invocation.py:841`). Per RFC 8693 §4.1 the human stays the *subject*
  and the agent is only the *actor*: the parked row gets
  `principal_sub=<human>` + `principal_act=<agent-sub>`. The requester is
  still the human, so that same human **still cannot** approve it. Routing
  a single-operator write through a human-triggered agent run does **not**
  break the deadlock — only the autonomous/scheduled run does.

### Which identity shape clears the gate

| Identity shape | How it parks | `principal_sub` | `principal_act` | Approve by the human op |
|---|---|---|---|---|
| Direct human op | human dispatches a `requires_approval` write as `USER` | the human's `sub` | `NULL` | **Blocked** — `requester == approver` (needs break-glass) |
| Human-initiated delegated agent run (`run`) | human launches an agent; agent binds as RFC 8693 *actor* | the human's `sub` | the agent's `sub` | **Blocked** — RFC 8693 subject is the human; `requester == approver` |
| Autonomous / scheduled agent run (`run_scheduled`) | agent authenticates as itself; **no** delegation bound | the agent's `sub` | `NULL` | **Clears** — distinct `sub`, no break-glass, full audit lineage |

### End-to-end recipe (single-operator four-eyes write)

1. **Register the agent principal** —
   [`keycloak-agent-client.md`](../cross-repo/keycloak-agent-client.md)
   (`meho agent-principal register <name>` → a `agent:<name>`
   `client_credentials` client carrying `principal_kind=agent`).
2. **Author an agent definition whose `identity_ref` is that client** —
   [`agent-definition.md`](agent-definition.md); `identity_ref` is
   validated against `agent_principal.keycloak_client_id`, and
   `run_scheduled` enforces `identity_ref == agent_client_id` so the run
   provably parks under the named agent.
3. **Wire the write as a scheduled trigger under that definition** —
   [`scheduler.md`](scheduler.md); a cron or one-off
   `scheduled_trigger` fires `run_scheduled`, so the
   `requires_approval` op parks with `principal_sub=<agent-sub>`.
4. **The human operator approves the parked row** — via any surface
   (REST `/decide`, `meho approvals approve <id>`, MCP). Their `sub`
   differs from the agent's, so the gate clears and the approved op
   re-dispatches.

### Troubleshooting — `self_approval_forbidden` on a single-operator tenant

| Symptom | Cause | Fix |
|---|---|---|
| `approve` returns `self_approval_forbidden` and you are the tenant's only operator | The write was parked under **your own** `sub` — a direct human op, or a human-initiated delegated agent run (where RFC 8693 keeps you the subject). | Re-park the write under an **agent-requester**: wire it as a scheduled trigger under an agent definition (the four-step recipe above) so `run_scheduled` parks it with `principal_sub=<agent-sub>`. Then approve as yourself — your distinct `sub` clears the gate. Reserve `APPROVAL_ALLOW_SELF_APPROVAL` for genuine emergencies; it re-opens the #1401 single-account hole posture-wide. |

### Operator-facing discoverability of this story (#2669)

This section is the contributor reference; the same two single-operator
patterns now have three **operator-facing** surfaces so a solo deployer
finds them without reading source:

- **Docs-site guide** — `docs-site/guides/approvals-and-break-glass.md`
  ("Do real work → Approvals and break-glass"), the deployer-persona
  distillation of this section.
- **Console guidance** — the approvals modal
  (`ui/templates/approvals/_modal.html`) renders both patterns + a
  deep link to that guide when the Approve button is disabled *and* on a
  rejected self-approval 403 (routes' `_SINGLE_OPERATOR_DOCS_URL`,
  mirroring `ui/maturity.py`'s `MATURITY_INDEX_URL` convention). The wire
  `403` / MCP-error text still names the flag and stays authoritative.
- **Chart value** — `config.approvalAllowSelfApproval` (rendered into
  `APPROVAL_ALLOW_SELF_APPROVAL` by `configmap.yaml`) exposes the
  break-glass as a first-class, schema-documented value
  (`values.schema.json`, `enum: ["true","false"]`, default `"false"`).

## G0.20-T3 — execute a parked direct op on approve via every surface (#1503)

Before #1503 the **only** execute-after-approve path for a parked
**direct** operator op (an operator calling a `requires_approval` op
directly, not via an agent run) was REST `POST .../approve` carrying the
original `params` in-band. Approving the same parked write via `/decide`
or the MCP/CLI by-id approve surface committed the decision but never
re-dispatched — and simply re-calling the op re-parked it. So an
operator who approved a colleague's parked direct write through the
activity feed / `/decide` / MCP saw it marked approved while the write
never landed.

Two changes close that gap, scoped to the **direct**-op path:

1. **Store the params on the row.** `approval_request.params` (nullable
   JSON, migration
   `backend/alembic/versions/0036_add_approval_request_params.py`) holds
   the original dispatch params, written by `create_pending_request` at
   park time. The row already held `connector_id` / `op_id` /
   `target_id`; adding `params` makes it a complete re-dispatch
   primitive, so a surface that holds only the request id can still
   re-execute the exact original call. The column is **internal
   re-dispatch input only** — it is never serialised onto a read view
   (`_view`, the MCP `_row_to_dict`) or a broadcast frame; the redacted
   `proposed_effect` and the swap-defence `params_hash` remain the
   reviewer-facing fields. (This reverses, for the direct-op path only,
   the "keep params off the row" tradeoff #1117 made for the agent-run
   case — see [agent-runtime.md § Awaiting-approval resume](agent-runtime.md#awaiting-approval-resume-t9-1117).)

2. **Re-dispatch from the approve decision on every surface.** The
   single execute-after-approve entry point is
   `approval_queue.resume_dispatch_after_approval` — it re-hydrates the
   stored target by id (G11.7-T1 fail-closed semantics preserved) and
   re-dispatches with `dispatch(..., _approved=True)`, falling back to
   the stored `request.params` when the surface supplies none. REST
   `/approve` (caller params, hash-verified) and REST `/decide` both
   route through it; `/decide` returns the dispatch outcome (`dispatch_*`
   fields) alongside the decision. (The decision itself is REST / CLI /
   console only — there is no MCP approve verb; see the MCP section
   below.)

**The `run_id` gate (no double-execution / agent-run regression
guard).** `/decide` re-dispatches **only when
`run_id IS NULL`** — i.e. a direct operator op. An **agent-run** request
(`run_id` set) is still resumed in-process by the agent runtime once it
reads the approved row (the must-not-regress path); re-dispatching
it from `/decide` too would execute the op twice. So the agent-run
resume path is untouched: it keeps its in-memory params, ignores the new
column, and remains the sole re-dispatcher for `run_id`-bearing requests.

**Double-execution prevention (terminal-state guard).**
`approve_request` flips the row to `approved` (terminal) and commits
*before* the re-dispatch runs. A concurrent second `/decide`/approve on
the same row hits the already-decided guard (409 / `not_pending`), so the
stored params drive exactly one execution.

**Pre-0036 rows.** A row parked before migration 0036 has `params IS
NULL`. Approving it via `/decide` (no in-band params) finds nothing
to re-dispatch, so `resume_dispatch_after_approval` **fails closed** with
a structured `denied` result naming the gap — the operator resumes it via
REST `/approve` + params, exactly as before 0036.

## Surfacing the approved dispatch's result to the originating consumer (#3209)

Before #3209 the result of the approved re-dispatch was visible **only** in
the approver's inline HTTP response (the `dispatch_*` fields on `/approve` /
`/decide`). A paired, out-of-process consumer that parks an op for approval
and then *waits* on the decision could not retrieve it: `ApprovalRequestView`
carries no execution result, and `GET /api/v1/audit/by-work-ref` reduces
params to a hash and exposes no response body. For a **non-idempotent** op
that is a hard block — the concrete case is VCF
`installer.sddc.bringup.start`, where resuming in place means *polling the
SDDC task id the re-dispatch returned*, not re-submitting (a second submit
starts a second bring-up). So `meho-automation`'s resume-in-place engine
(#76) had to **re-park** non-idempotent ops instead of resuming them.

The fix is a durable capture + a principal-scoped read, both composing with
the existing exactly-one-resumer substrate rather than duplicating it:

1. **Capture at the winning resume path.** `resume_dispatch_after_approval`
   — the single execute-after-approve entry point shared by REST `/approve`
   \+ `/decide`, MCP by-id approve, and the async approve background task
   (#3079) — persists the re-dispatch's reduced result envelope onto
   `approval_request.resume_result` (nullable JSON, migration `0096`) right
   after the dispatch returns. Only the resumer that **won**
   `claim_resume` (#2293) reaches the capture (`persist_resume_result`), so
   it runs at most once per approved request; a loser that no-ops
   `already_resumed` leaves the winner's result untouched. The stored value
   is `OperationResult.model_dump(mode="json")` — the **same** reduced /
   handle-shaped envelope the approver sees inline, so a set-shaped response
   rides a JSONFlux `handle` (v0.1-spec §4), never a raw body. The capture is
   **fail-soft**: the dispatch's own synchronous audit row already committed
   inside `dispatch()` (the v0.1-spec §6 record of what executed), so a
   capture-write failure logs `approval_resume_result_persist_failed` and is
   swallowed rather than breaking the approve path. NULL until captured: a
   `pending` / `rejected` / `expired` row, an **agent-run** request resumed
   in-process (the in-process waiter re-dispatches on the agent's own task and
   does not route through this helper — the agent already consumes the result
   in-loop, so it needs no surfacing), and every pre-0096 row all keep NULL.

2. **Principal-scoped read.** `GET /api/v1/approvals/{id}/result`
   (`read_approval_result`) returns the captured envelope, but — unlike the
   rest of the approvals surface — it is **not** `operator`-gated. Any
   authenticated principal may call it, and the service then enforces that
   only the request's **owner** (`operator.sub == principal_sub`, the party
   that parked the op) reads it; every other principal, operator role or not,
   gets `ResultAccessForbiddenError` → 403 `not_request_owner`. This is what
   lets a non-operator service/agent principal — the paired add-on that
   dispatched the op — retrieve *its own* result while no one else can.
   Tenant isolation is upstream (`get_request` → 404 on a cross-tenant id,
   never reaching the owner guard). The read writes one synchronous
   `approval.result` audit row (`method='APPROVAL'`, `status_code=200`,
   parent-linked into the request's replay subtree #2086), recording the
   request `status` and `resume_result_present` so a poll-until-ready pattern
   is visible on the ledger without the payload ever landing on it.

The response (`ApprovalResultView`) carries `status`, `resumed`
(`resumed_at is not None`), `reviewed_by` / `decided_at`, and `result` (the
captured envelope, or `null` while unready). A consumer polls until
`status == "approved"` and `result` is populated, then resumes by poll on the
returned task id. When the approved run failed on an upstream vim fault,
`result.extras.upstream_fault` carries the fault type and full faultstring
(#3567), so the owner can see why without the approver-owned
flight-recorder handle. The `resume_result` **payload** is internal to this
surface — like `params` it is never projected onto the default read view
(`_view`) or a broadcast frame. Only a short summary of it (status, error
code, audit id) appears on the list and show views; see "Approved, but the
run failed" below (#3543).

### Who owns a result handle from an approved run (#3548)

The approved op runs under the **approver's** `Operator`. Before #3548 a
large result spilled into a handle owned by the approver's `sub`. But
`/result` serves only the **parker**, so the parker got an envelope with a
handle it could not open (`handle_not_found`).

Now the parker owns it:

- `_dispatch_resume_with_bound_context` binds
  `operations.reducer.handle_owner_sub_var` to `request.principal_sub` for
  the run (token reset in `finally`).
- `dispatcher._reduce_or_error` uses that value as
  `reducer_context["operator_sub"]` when it is set. Otherwise it uses the
  dispatching operator's `sub`, as before.
- The tenant does not change. The approver can only load rows from its own
  tenant, so the parker's tenant is the same.
- Every spill in the run follows the binding, including the child ops of a
  resumed multi-step (composite) op.

What this means:

- One owner per handle. The parker can page and query it with
  `result_query`. Any other principal still misses, **including the
  approver**. The approver still sees the summary and sample rows inline. The
  approver did not ask for the data.
- The audit model is unchanged. The run's audit row still names the
  approver as the actor. The parker's `result_query` reads run under the
  parker, so they match the parker's `approval.result` row.
- Only the operator-surface resume does this. A normal dispatch, and the
  in-process agent-run resume (it already runs as the parking agent), keep
  handles owned by the caller.

**Relation to the #3027 add-on step event.** `publish_approval_event`
already durably records the *decision* (`approval.approved` / `.rejected`)
for the paired add-on that requested the approval
(`AddonStepEventService.record_if_owned_committed`). That answers "was it
approved?"; the `/result` surface answers "what did the approved re-dispatch
produce?" — complementary, not a duplicate.

## The audit row of an approved run (#3864)

When a person approves a request, MEHO runs the operation. The run writes its
own audit row, separate from the `approval.decision` row. The decision row
only says "approved". The run row says whether the change landed.

What the run row holds:

- `method='DISPATCH'`, `path` and `payload.op_id` set to the real op (for
  example `keycloak.user.create`).
- `payload.result_status` is `ok` or `error`. A failed run also has
  `payload.error` with the `error_code` and the safe details (#2680).
- `payload.approval_request_id` names the request it ran for.
- `parent_audit_id` points at the request's `approval.request` row, next to
  the decision row (#2086). The agent-run resume sets `parent_audit_id` the
  same way, but its run row has no `payload.approval_request_id` (follow
  `parent_audit_id` to the request), and its `operator_sub` is the agent
  that made the call, not the approver.
- `operator_sub` is the approver, who ran it.

The approve response (`dispatch_status`, `dispatch_error`) and the stored
`resume_result` carry the same outcome. `resume_result.audit_id` is the id of
the run row, for a failed run too.

Before #3864 the REST `/approve` and `/decide` routes left their own log
labels (`audit_op_id="approval.decide"` and siblings) in place during the run.
The dispatcher copies every `audit_*` label into the run row, so the run row
was labelled `approval.decide`. A search for the real op found the request
and the decision, but no run. `_dispatch_resume_with_bound_context` now clears
those labels for the run (`_run_audit_context`) and puts them back after. The
route's own row keeps `approval.decide`.

Two failures that must never read as success, both covered by
`tests/test_approval_failed_run_record.py`:

- **The password ref cannot be read.** `keycloak.user.create` and
  `keycloak.user.reset_password` read the password from `password_secret_ref`.
  When Vault refuses, cannot find, or cannot be reached for it (a connection
  failure or timeout during the read), the run fails with
  `vault_credentials_read_error`. The error shows only the ref prefix (for
  example `tenants/…`) and the error class. No user is created.
- **The Keycloak account may not write.** A 403 from Keycloak fails the run
  with `connector_http_403`.

## Retry-safe approval requests, binding identity, and run phase (#3486)

A durable client, such as an add-on that drives a two-person governed
delete, retries after a crash or restart. Three things make that safe.

### 1. Send an `idempotency_key` so a retry gets the same request

`call_operation` (REST `POST /api/v1/operations/call`, MCP `call_operation`)
takes an optional `idempotency_key` (1 to 200 characters,
`CallOperationBody.idempotency_key`). It only matters when the call waits
for approval.

- **First call.** The new `approval_request` row stores the key
  (`idempotency_key` column, migration `0105`).
- **Retry of the identical call.** If the same principal in the same tenant
  has a `pending` or `approved` request with this key,
  `create_pending_request` returns that request. The reply is the normal
  `awaiting_approval` envelope (HTTP 200) with the **same**
  `extras.approval_request_id` and `extras.approval_request_reused: true`.
  No new row, no new `approval.request` audit row, no second
  `approval.pending` event. The first row's audit record stands.
- **Same key, different call.** "Identical" means the same `op_id`,
  `connector_id`, target, `params_hash`, `preview_hash`, and `work_ref`. If
  any of them differs, the call is refused with `status="denied"` and
  `extras.error_code="idempotency_key_conflict"`, and
  `extras.approval_request_id` names the request that holds the key. Nothing
  is parked. The refusal writes a `denied` audit row.
- **When the key is free again.** A `rejected` or `expired` request no
  longer holds its key, so the same key parks a new request. An `approved`
  request keeps its key even after it ran: a retry then returns the request
  that already ran, so the op is never run twice.
- **A late retry.** A request can pass its deadline (`expires_at`) while
  it is still `pending`, because the expiry sweeper runs only every few
  minutes, or not at all when it is off. Such a request can no longer be
  approved, so a retry does not get it back. Instead, `create_pending_request`
  locks it and expires it in the same transaction (`_lock_and_expire_pending`,
  the same steps and the same `approval.decision` audit row as
  `expire_stale_requests`). Like the sweeper, it records the expiry under
  the system identity `system:approval-expiry` (`expiry_operator` in
  `approval_queue.py`), not under the requester. Then it parks a new
  request with the same key.
  The reply carries the new `approval_request_id` and no
  `approval_request_reused`. After commit the dispatcher publishes
  `approval.expired` for the old request and `approval.pending` for the
  new one.
- **Who can match.** The key is scoped to `(tenant_id, principal_sub)`. Two
  requesters, or two runs with different `work_ref` values, never share one
  request. Without a key, nothing changes: every call that waits for
  approval makes a new request.

**Races.** A partial unique index,
`approval_request_idempotency_key_idx` on `(tenant_id, principal_sub,
idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN
('pending', 'approved')`, makes sure two racing calls cannot both insert.
The losing insert runs in a savepoint (`_insert_idempotent_request`). When
the index refuses it, the savepoint rolls back and the winner's row is
returned the same way as a normal retry.

Two late retries can also race to expire the same overdue request. Only one
gets the row lock. When the other's locked re-read finds no `pending` row,
the row changed under it: another retry expired it and parked a replacement,
or a sweep, reject or approve got there first. `_resolve_idempotent_request`
then looks the key up again and returns what holds it now, for example the
replacement as a reused request. It never returns the expired row it read
first. That second lookup refreshes the row the session already holds
(`populate_existing`). This matters when an approve commits while the late
retry waits for the lock: the row is now `approved`, and the retry returns
it. With the cached `pending` state it would try to expire the row again,
miss again, and never return.
`tests/integration/test_approval_idempotent_retry_pg.py` runs both races
(two late retries; a late retry against an approve) on a real Postgres.

**What does not change.** The lookup runs inside `create_pending_request`,
after every park gate in `_handle_needs_approval`. So a retry still goes
through the policy gate, the destructive-tier `preview_hash` check (#3197),
and the permission preflight. A retry with a stale `preview_hash` is refused
with `preview_hash_mismatch`, as before. The key is not used for the
sub-op requests a composite (multi-step) op makes, and not on the resume
path. Tenant isolation and the one-resumer latch (`resumed_at`) are
unchanged.

### 2. `preview_hash` on list and show

`ApprovalRequestView` (`GET /api/v1/approvals` and `GET
/api/v1/approvals/{id}`) now carries `preview_hash`, the destructive-tier
binding stored at park time. With `op_id`, `target_id`, and `params_hash`, a
client can match a request to the row it previewed. It is `null` for a
non-destructive request. The field is additive.

### 3. `result_state`: has the approved op run yet?

`status` keeps its four values (`pending` / `approved` / `rejected` /
`expired`), so the migration `0023` `CHECK` constraint and every existing
client are unchanged. Next to it, the list and show views carry
`result_state`, worked out from the row by `approval_result_state` (it is
not stored):

| `result_state` | Meaning |
|---|---|
| `null` | `status` is not `approved`. |
| `not_started` | Approved. No resumer has claimed the run yet (`resumed_at IS NULL`). |
| `started` | A resumer claimed the run (`resumed_at` is set), but no result is stored yet. The op may still be running. |
| `executed` | The stored result (`resume_result`) has `status == "ok"`. |
| `failed` | A result is stored and its `status` is not `ok`. |

So a client that lists or shows requests can tell "approved, not run yet"
from "approved and run" without polling the owner-only `/result` read. The
result body itself stays on `/result` only; the views show the phase, not
the payload.

Both resume paths store the result (`persist_resume_result`): the operator
surface (`resume_dispatch_after_approval`) and the in-process agent waiter
(`_resume_approved_or_already_resumed` in `agent/approval_wait.py`). So a
request the agent resumed also reaches `executed` or `failed`. Limits of
`started`: the result write is best-effort, and a re-dispatch that raised
instead of returning an envelope stores nothing. In those cases the row
stays `started`.

### 4. Approved, but the run failed: `resume_outcome` (#3543)

`status` stays `approved` even when the run after the approval failed (for
example a Vault write that Vault refused). `result_state` already tells
`executed` from `failed`. Next to it, the list and show views carry
`resume_outcome`, a short summary read off the stored result by
`operations/approval_outcome.py` (`resume_outcome`):

| Field | Meaning |
|---|---|
| `resumed_at` | When MEHO started the run (the `resumed_at` claim). |
| `result_status` | `status` of the stored result: `ok`, `error`, `denied`, ... `null` while no result is stored. |
| `error_code` | `extras.error_code` of a failed run, for example `connector_error`. |
| `audit_id` | The audit row the dispatcher wrote for the run. |

`resume_outcome` is `null` for a request that is not approved, and before
the run starts. The result body and the free-text `error` stay on the
owner-only `/result` read: an odd or missing field reads as `null`, never
raises.

Where it shows:

- **REST** `GET /api/v1/approvals` and `GET /api/v1/approvals/{id}`: the
  `resume_outcome` field.
- **MCP** `meho_approvals_list` / `meho_approvals_get`: `result_state` and
  `resume_outcome`, same shape.
- **CLI** `meho approvals list`: a `RUN` column (`not-run`, `started`,
  `ok`, `FAILED`, or `-` when the request is not approved). `meho approvals
  show` and `approve`: `Run:`, `Run started:`, `Run result:`, `Error code:`
  and `Run audit:` lines, with the `meho audit show <id>` command for the
  audit row.
- **Console** `/ui/approvals`: a run badge next to the `approved` pill in
  the history list (`ran OK`, `run failed`, ...). The detail modal of a
  failed run shows a red banner with the error code and a link to the audit
  row (`/ui/audit?audit_id=<id>`). The card after an approve says "Approved,
  but the run failed" when the run returned an error. When another resumer
  (usually the waiting requester) already ran the operation
  (`already_resumed`), it shows the normal "Request approved" card.

**Decision: reads of an approval request stay as they are here.** A client
that polls `GET /api/v1/approvals/{id}` until the run ends makes one
routine REST read per poll. Routine reads moving off the audit log and into
request usage records is the job of #3757 (it retires the per-request
wrapper rows), not of this change. The owner-only `/result` read keeps its
`approval.result` audit row on purpose: it hands out the result payload,
which is a data release worth a record. With `result_state` and
`resume_outcome` on the list and show views, a client no longer needs to
poll `/result` to learn whether the run ended.

**Pinned by tests:** `tests/test_approval_resume_outcome.py` (summary
shape, odd envelopes, a resume that errors shows on REST show, list and
MCP), `tests/test_ui_approvals.py` (run badge, failed-run modal, failed
approve card) and `cli/internal/cmd/approvals/approvals_test.go`.

### The add-on governed-destroy round trip (#3478)

For the add-on that proposes a destructive op: send an `idempotency_key`
with the `call_operation` that carries the `preview_hash`. Derive the key
from something stable on your side, for example your run id plus the row
you want to delete. If you crash before you store the
`approval_request_id`, send the identical call again with the same key. You
get the same request back. Then watch `result_state` on `GET
/api/v1/approvals/{id}`, and read the result body from `/result` once it is
`executed` or `failed`.

**Pinned by tests:** `tests/test_approval_idempotent_park.py` (retry,
scoping, conflict, race, `result_state` on the REST list and show after both
resume paths), `tests/integration/test_approval_idempotent_retry_pg.py` (two
late retries, and a late retry against an approve, on a real Postgres) and
`tests/migrations/test_migration_0105_add_approval_request_idempotency_key.py`.

## `proposed_effect` builder hook (#1437)

`ApprovalRequest.proposed_effect` holds what the reviewer sees in the
queue. By default `create_pending_request`
(`backend/src/meho_backplane/operations/approval_queue.py`) stores an
identifier-only summary — `{op_id, connector_id, target_id}`. Some ops
can do better: they can compute a **side-effect-free preview** of what
the approved call would do, so the reviewer reads the diff in the queue
rather than only in the post-approval op result.

The per-op preview is opt-in via a builder registry in
`backend/src/meho_backplane/operations/_preview.py`:

- `register_preview_builder(op_id, builder)` registers an
  `async (PreviewContext) -> dict | None` callable for an op-id.
  `PreviewContext` carries the resolved connector instance + descriptor +
  operator + target + params.
- At the park point, `dispatcher._handle_needs_approval` resolves the
  connector instance (same path the execute branch uses) and calls
  `build_proposed_effect`. A bespoke builder's result — wrapped as
  `{op_class, preview}` — becomes the base envelope; an op with no bespoke
  builder gets the **generic params-echo default**
  (`{op_class, params_echo}`, see invariant 1); when the builder declines
  (returns `None`) the dispatcher's `_build_proposed_effect` seam
  substitutes the identifier-only base. Onto whichever base it has, the
  seam stamps the catalog `descriptor.safety_level` (#1855) before handing
  the envelope to `create_pending_request` as `proposed_effect` (see
  "Catalog `safety_level` on every envelope" below). `_build_proposed_effect`
  returns `None` — and the caller stores its own bare identifier-only
  default — only when connector resolution / hook execution itself raises.

Two park points stamp through this same `_build_proposed_effect` seam: the
dispatcher's `_handle_needs_approval` (above) and the composite direct-seam
`enforce_subop_policy` (`operations/composite.py`), which re-applies the
policy gate around a direct-session write sub-op. Before #294 (security
review S06) only the dispatcher path stamped it — the composite seam parked
with the bare identifier-only default, so a parked `dangerous` sub-op's row
carried no `safety_level` and the #3290 no-self-approval-under-break-glass
carve-out silently did not fire for it (nor did the reviewer row show its
severity or blast radius). The seam now builds the same envelope and, for a
`destructive`-tier sub-op, routes through `_destructive_binding_refusal` to
**fail closed** when no `preview_hash` + blast-radius binding is present
(#3197) — identical to the dispatcher.

Three invariants make the hook safe to wire on the park path:

1. **Generic echo default, opt-in bespoke builder (#1856); safety_level
   on every envelope (#1855).** An op *without* a registered builder no
   longer yields `None` — it gets a **generic params-echo default**: the
   requested params are echoed under a `params_echo` envelope key (distinct
   from a computed `preview`), redaction-safe (see invariant 3), so every
   approval-gated op (keycloak, nsx, sddc, vcfa, …) gets param-level
   legibility for free instead of showing the approver only `{op_id,
   connector_id, target_id}`. A registered builder still wins (no
   double-echo). The identifier-only default is reached only when the op is
   credential-class *and has no bespoke builder* (the generic echo is
   suppressed — see invariant 3), its params are empty, or a registered
   builder declines. Independently, the dispatcher seam layers the catalog
   `safety_level` on top of whichever base it has (below), so the parked
   row always names its severity — the durable row is no longer
   byte-identical to the pre-#1855 identifier-only default.
2. **Fail-soft, never silent.** A builder that raises (a dry-run that
   hits the API and errors, a preview listing read that can't execute)
   never blocks the park — the safety-relevant action always proceeds.
   But the failure is visible (#1628): the hook returns
   `{op_class, preview_unavailable: true, preview_error}` and the
   dispatcher merges that marker onto the identifier fields, so the
   parked row reads "blast-radius unknown: <reason>" instead of a bare
   identifier default a reviewer can't tell from a small action. The
   reason string is the exception's type + message. The message is
   redacted with the packaged default credential policy (the same one
   the `connector_error` envelope uses) and then truncated at 500 chars,
   because a builder fault can echo an upstream error body — for example
   a Kubernetes admission webhook denial that quotes a password (#3876).
   Redaction runs before the cut so a secret cannot survive as a
   fragment. A builder that *declines* (returns `None` — op not
   previewable from these params) still collapses to the identifier-only
   default with no marker, as do connector-resolution faults outside
   the hook.
3. **Redaction-safe.** `build_proposed_effect` classifies the op via
   `classify_op` (the same single-sourced sensitivity classification used
   for broadcast/audit redaction, #1401) and **suppresses the generic
   params-echo default** for any credential class (`credential_read` /
   `credential_mint` / `credential_write`) — the generic echo can only do
   generic key-name / value-shape redaction and is not trusted to scrub a
   connector-specific secret shape, so a credential-class op with no
   bespoke builder collapses to the identifier-only default. A **bespoke
   builder is the deliberate exception (#1857)**: like the
   permission-preflight hook, it is trusted to own its own field
   discipline and runs even for a credential-class op (e.g. the keycloak
   user-create preview scrubs the inline password via the connector's own
   `redact_secret_fields` before returning). Builders are themselves
   expected to return identity-only / scrubbed summaries. The generic
   params-echo default (#1856) scrubs the echoed params with **two**
   passes that reuse the existing redaction discipline:
   `_scrub_secret_param_keys` masks values keyed by a secret-shaped
   name (`password` / `admin_password` / `rootPassword` /
   `client_secret` / `api_key` / … — recursively, since the
   connector-boundary engine walks Mappings key-by-key without inspecting
   the *key*). It uses `broadcast.events.is_secret_param_name`, the same
   name check as the broadcast params scrub, so any param the broadcast
   path flags as secret is also masked in the echo (#3808). It walks the
   same containers too: every mapping and every sequence other than a
   string (a list or a tuple). It also uses the same value rule: only a
   string, mapping or sequence value is masked.
   A bare boolean, number or `null` under a secret-shaped name (for
   example `bind_secret_id: false` or `secret_id_ttl: 600`) stays
   visible, because it is a setting, not a secret, and the approver
   needs to see it. Then
   `apply_connector_boundary_redaction` (the same per-`(connector_id,
   tenant, op)` pipeline the response path and the dispatch-request
   preview run) catches secret *value* shapes (JWTs, bearer tokens,
   kubeconfig blobs, labelled `key=value` strings in a single leaf).

The only builder wired in #1437 is **`k8s.apply`**: it re-invokes the
`k8s_apply` handler with `dry_run="server"` forced on (the API's
`?dryRun=All`), so nothing persists and the per-document summary
(resource identity + `resourceVersion` + `uid`) is the diff-preview the
reviewer reads. When the API server rejects the dry-run (for example an
admission webhook denial), the handler raises `ApplyRejectedError` and the
row gets the `preview_unavailable` marker with the server's message in
`preview_error`, so the reviewer sees that the apply will be rejected
(#3876). The ArgoCD write ops register their own builders the same
way (#1452): `argocd.app.set` / `argocd.appproject.update` populate
`{before_spec, after_spec}` and `argocd.app.delete` populates
`{cascade_resources}` from read-only GETs against the live
Application / AppProject — wired in
`connectors/argocd/ops_write_preview.py`. The `secret.move` broker op
populates a ref-only `{action, source, sink}` summary with no store I/O
(#1580, `connectors/secret/move_preview.py`). The 8 vmware write
composites register builders in
`connectors/vmware_rest/composites/_write_preview.py` (#1608): the
fan-out composites (`vm.power.bulk`, `host.evacuate`,
`host.detach_from_vds`, `cluster.patch`) resolve their entity set via
the same read-only listing helpers the handlers use — `{..., resolved,
total_resolved}` with the list capped — and the single-entity
composites echo their params; see
[`connectors-vmware-rest.md`](connectors-vmware-rest.md) "Park-time
approval previews". The Keycloak write ops register bespoke builders in
`connectors/keycloak/ops_write_preview.py` (#1857): `keycloak.realm.create`
populates `{resource, realm, representation}`, `keycloak.user.create`
populates `{resource, username, realm, representation}` (the inline
password scrubbed to `***REDACTED***` via the connector's own
`redact_secret_fields` — and it runs *despite* `keycloak.user.create`
classifying as `credential_write`, the bespoke-builder exception to
invariant 3). `redact_secret_fields` covers the Keycloak representation
secret fields *and* the generic credential key spellings (notably
`password`, case-insensitively), so the bespoke builders are at least as
strict as the generic echo they bypass — a
`RealmRepresentation.smtpServer.password` (or any `password`-keyed field
in an `additionalProperties` representation) never reaches the durable
row. `keycloak.role_mapping.assign` populates
`{resource, username, id, realm, granted_roles}` (the granted realm role
names). Further connectors register their own builders as needed.

## Credential writes: what the approver sees (#4850)

A credential write is an op pinned `credential_write` in
`broadcast/events.py` (`_CREDENTIAL_WRITE_OPS`). When it waits for
approval, the generic echo of params is turned off (invariant 3). So the
approver sees only what a bespoke preview builder shows. Without one,
the approver sees the op id, the target and the reason
`credential_write_redacted`, and does not know which account, secret or
host changes.

Every credential write that can wait for approval now has a builder.
Each builder shows what changes and never a secret value. These nine
got one in #4850; `k8s.secret.update` came later with its own builder
(`connectors/kubernetes/ops_secret_write_preview.py`):

| Op | Who waits for approval | The approver sees | Never shown |
|---|---|---|---|
| `keycloak.user.reset_password` | every call | user (`username` / `id`), realm, Vault path of the new password (`password_secret_ref`, mount, field), `temporary` | the password |
| `vault.auth.userpass.update_password` | every call | mount, username, `sets_password` | the password |
| `vault.auth.userpass.write` | every call | mount, username, `sets_password`, `token_policies` and whether they are replaced or kept | the password |
| `k8s.secret.create` | every call | name, namespace, type, all key names, inline key names, the `*_secret_ref` store paths per key, the `*_secret_mount` store mounts | any value |
| `k8s.secret.update` | every call | name, namespace, `mode`, the key names that are set, inline key names, the `*_secret_ref` store paths per key, the `*_secret_mount` store mounts, the key names the Secret has now, in `replace` mode the key names that are removed | any value |
| `k8s.job.create` | every call | name, namespace, service account, per container: name, image, env var names, `envFrom` Secret / ConfigMap names, command size in bytes | env values, the command and args text |
| `sddc.host.validate` | an agent's call | host count; per host: `fqdn`, `username`, `storageType`, `networkPoolId` | the ESXi password and any other spec field |
| `sddc.host.commission` | every call | the same as `sddc.host.validate` | the same |
| `holodeck.router.patch` | every call | HoloRouter version, whether the call only checks (`verify_only`, a 9.1 router), the patch ids and the module file each one edits | nothing secret exists; the op takes no params |
| `vmware.composite.vm.guest.file.read` | an agent's call | VM, guest path, whether MEHO downloads the bytes (`fetch_content`, `max_inline_bytes`), the guest login's store path (`guest_secret_ref`) | the guest login |

`k8s.secret.delete` is not a credential write (its params are only the
Secret's name and namespace). It is `destructive`, so its preview must carry
a `blast_radius` block: the approver sees the Secret (namespace, name, type,
key names) and every Pod, Deployment, StatefulSet, DaemonSet and CronJob in
the namespace that uses it, never a value. A missing Secret fails the
preview, so nothing waits for approval. See
[`connectors-kubernetes.md`](connectors-kubernetes.md).

Each builder copies the fields it shows by name. It never walks the
whole params, so a new or extra field (a password in an open
`HostCommissionSpec`, say) does not reach the stored request. All nine
read only the params (and the target's version for Holodeck) and make
no network calls. The builders live next to their connectors:
`keycloak/ops_write_preview.py`, `vault/userpass_write_preview.py`,
`kubernetes/ops_write_preview.py`, `sddc_manager/host_commission_preview.py`,
`holodeck/router_patch_preview.py` and
`vmware_rest/composites/_guest_file_read_preview.py`.

**CI keeps it that way.** `tests/test_credential_write_preview_conformance.py`
runs every typed-op registrar. It takes each registered op that is in
`_CREDENTIAL_WRITE_OPS` and can wait for approval (`requires_approval`,
or a `safety_level` above `safe`), and fails when the op has no builder.
An op that must stay without a preview goes into
`_NO_PREVIEW_CREDENTIAL_WRITES` in that test, with the reason. The list
is empty today. Two credential writes have no builder and are not in the
sweep: `vmware.composite.vm.guest.env.read` and
`vmware.composite.vm.guest.process.list`. They are `safe`, so no call of
theirs waits for approval by default.

## Catalog `safety_level` on every envelope (#1855)

The per-op preview hook is opt-in, so before #1855 a parked op with no
registered builder carried only `{op_id, connector_id, target_id}` — a
reviewer could not tell a `dangerous` op (e.g. `keycloak.realm.create`)
from a `caution` op (e.g. `keycloak.user.create`) on the row alone.

`dispatcher._build_proposed_effect` now stamps the catalog
`descriptor.safety_level` onto **every** parked op's `proposed_effect`,
alongside `op_class` / `preview` (and `permission_preflight` when that
hook fired). The value (`safe` / `caution` / `dangerous`) is read
straight off the operation descriptor — op-identity metadata, never
recomputed — so the severity on the durable row is exactly what the
catalog declares.

It is layered at the **dispatcher seam**, not inside the per-op
`build_proposed_effect` builder, so it rides three bases uniformly: the
built `{op_class, preview}` envelope, the identifier-only default for
no-builder / declined ops, and the `preview_unavailable` fail-soft
marker. The `op_class` / `preview` / marker envelope built by
`build_proposed_effect` itself is unchanged; `safety_level` is added on
top. The only path that does **not** carry it is the bare identifier
default the caller stores when `_build_proposed_effect` returns `None`
(connector-resolution / hook fault) — that degraded path is unchanged.

## Destructive tier: preview-hash binding + mandatory blast-radius (#3197)

The `destructive` safety tier (the fourth `safety_level`, #3196 —
`safe < caution < dangerous < destructive`) adds two fail-closed
requirements on top of the standard park, both landing at the dispatcher
seam. They implement requirements 2 and 3 of
[`docs/decisions/governed-delete-operations.md`](../decisions/governed-delete-operations.md).
Neither applies to any lower tier; a `safe` / `caution` / `dangerous` op
parks exactly as before.

**Requirement 2 — preview-result-hash binding.** `preview_operation`
(`operations/_request_preview.py::preview_dispatch`) now emits a
`preview_hash` on its `status="ok"` envelope:
`compute_preview_hash` takes a stable SHA-256 over the canonicalised
*resolved request* projection (`connector_id` / `op_id` / `method` /
`resolved_path` / `query` / `redacted_body`) — the literal would-be
request, the same canonicalisation discipline as `compute_params_hash`.
An agent/operator previews the destructive op, then presents that hash on
the subsequent `call_operation` (top-level `preview_hash` arg on
`CallOperationBody` / the MCP tool schema, a MEHO dispatch control like
`work_ref` — never a connector op param, so it stays out of `params_hash`
and the op's `parameter_schema`).

When such an op reaches `needs-approval`, `_handle_needs_approval` calls
`_destructive_binding_refusal` **before** it writes any row:

- no `preview_hash` presented → `denied` / `preview_binding_required`;
- the dispatcher **recomputes** the authoritative hash by resolving the
  same preview server-side (`preview_dispatch` — read-only, no egress) and
  comparing. A non-resolvable preview → `preview_binding_required`; a
  mismatch (params swapped between preview and call, or a forged/stale
  hash) → `preview_hash_mismatch`.

**Every refusal is recorded (#4647).** A `destructive` call that is
refused before it can wait for approval, with `preview_binding_required`,
`preview_hash_mismatch` or `blast_radius_required`, writes one `denied`
audit row on the op's own path through `dispatcher._audit_denied_and_return`,
the same writer a policy denial and the #3542 preflight refusal use:
`event_class=policy_decision`, synthetic status 403,
`payload.result_status=denied`, and the refusal code under
`payload.error.error_code`. No approval row is written and nothing reaches
the vendor. A refused `destructive` step inside a multi-step operation
(`composite.enforce_subop_policy`) writes the same row on the step's path,
linked to the parent operation's audit id in `parent_audit_id`. The parent
operation gets its own `denied` execution row from the dispatcher when the
handler returns the refusal (#4275).

Two system faults in `_handle_needs_approval` write no audit row on
purpose. Neither is a decision about the call; both are a MEHO-side fault
with no fitting audit event class. They leave different traces:

- A missing or malformed approval encryption key
  (`ApprovalHandoffKeyError`) returns a `denied` result whose message names
  the key setting. Nothing is logged. The returned result is the only
  record.
- A failure while saving the approval request is logged as
  `approval_queue_create_failed` (with the op id and the operator) and
  returns a `denied` result.

The verified hash is persisted on `ApprovalRequest.preview_hash` (nullable
`Text`, migration `0086`) — **distinct** from `params_hash` (that hashes
the request *params*; this hashes the *preview result* the approver was
shown). At approve time `_load_pending_for_approval` re-verifies it
(`_check_destructive_preview_binding`): a `destructive` row
(`proposed_effect.safety_level == "destructive"`) with no `preview_hash`
raises `PreviewBindingMissingError` → HTTP 422 on every approve surface
(REST `/approve`, `/decide`, the console). This extends the existing
`params_hash` swap-defence to the preview binding. The resume path
(`dispatch(..., _approved=True)`) skips the policy gate, so the binding is
enforced once at park and once at approve, never re-checked on execution.

**Requirement 3 — mandatory blast-radius statement.** A destructive op
cannot park with only the identifier-only default: its `proposed_effect`
must carry a top-level `blast_radius` block — `object` (object identity),
`children` (the enumerated child objects, a list; empty is a valid
explicit statement), and `irreversibility` (the irreversibility class).
A destructive op's preview builder returns the block inside its `preview`
dict; `dispatcher._build_proposed_effect` **promotes** it to the top level
(mirroring the `safety_level` / `op_class` stamping) so the mandatory-block
gate and the console modal read it without reaching into `preview`.
`blast_radius_missing_reason` (`operations/_preview.py`) validates it; a
missing/malformed block → `_handle_needs_approval` refuses the park with
`denied` / `blast_radius_required` (naming the missing field) rather than
falling back to the identifier-only default. When the block is missing
because the op's preview builder raised, the reason also carries the
builder's error. A builder raises on purpose to refuse a delete before it
waits for approval (the Keycloak delete guards, #3944), so the caller reads
why it was refused. `result_blast_radius_required` runs the reason through
`_sanitize_free_text` (secret-pattern redaction, then a 256-character cap),
like every other exception text a result builder emits. So a `destructive` op that
registers no blast-radius preview builder is un-parkable by construction —
fail-closed. That refusal is **runtime-only**, though: a posture sweep that
promotes an op to `destructive` without adding its builder ships an
un-parkable op that is green in CI and only breaks on first use. #3312 adds
the registry-driven CI conformance sweep
(`tests/test_destructive_builder_conformance.py`) that enumerates every
registered `destructive` descriptor (every connector, both source kinds) and
fails the build if any lacks a registered proposed-effect builder — moving
the failure from first-use to CI. The console modal
(`ui/templates/approvals/_modal.html`) renders the block as an error-tinted
"what this destroys" card above the generic field table.

**Scope note.** The gate lives on the top-level dispatch park path
(`_handle_needs_approval`). The composite-subop park path
(`operations/composite.py::enforce_subop_policy`) parks via its own
`create_pending_request` call and is **not** gated here — a destructive op
reached as a composite subop is a separate governance seam (a follow-up,
not this task). In practice an agent cannot reach it: the destructive tier
is `DENY` for agent principals at `resolve_verdict`.

## First governed delete: `vmware.composite.vm.destroy` (#3198)

Requirement 1 of
[`docs/decisions/governed-delete-operations.md`](../decisions/governed-delete-operations.md)
— one real delete family modeled into the tier end-to-end. The op is
`vmware.composite.vm.destroy` (`safety_level="destructive"`,
`requires_approval=True`; the dual-arm handler + blast-radius preview
builder are documented in
[`connectors-vmware-rest.md`](connectors-vmware-rest.md)). Wiring it in
surfaced — and this task closed — three gaps in the tier's *human*-side
enforcement that #3196/#3197 left, plus one machinery limitation:

- **A composite/typed op is now previewable when it is `destructive`
  (#3198) or `requires_approval` (#3312).**
  `preview_dispatch` returns `unavailable` for non-ingested ops (they have
  no single literal HTTP request), which would make the requirement-2 hash
  binding impossible on the composite surface. The gate in
  `_resolve_previewable_descriptor` now lets a governed-tier non-ingested op
  through to `build_composite_preview` (`operations/_composite_preview.py`),
  which binds the *logical request tuple*: the redacted params ride the
  `redacted_body` slot, so `compute_preview_hash` (unchanged) is
  param-sensitive while a `COMPOSITE` sentinel `method` + the `op_id` as
  `resolved_path` name the op. #3312 additionally extends the gate to any
  **non-credential-class** `requires_approval` op (canonical case the
  `dangerous`-tier typed `vault.kv.delete`) and layers the **reused park-time
  `proposed_effect`** onto the envelope, so the calling agent reads the same
  effect block the approver sees pre-dispatch instead of `preview_unavailable`.
  That reuse runs `build_proposed_effect` with **no connector instance**
  (egress-free): a pure builder populates, a live-read blast-radius builder
  declines. The `proposed_effect` key is outside the hashed projection, so the
  requirement-2 binding is unperturbed. A **credential-class** op (`vault.kv.put`
  / `k8s.secret.create` — `classify_op` ∈ the aggregate-only sensitivity set)
  is deliberately **excluded** and keeps its `unavailable` contract: its secret
  rides in the request params, and the synthetic preview's `redacted_body` slot
  uses only connector-boundary value-shape redaction, which cannot be trusted to
  scrub a structured secret — the same reason `build_proposed_effect` suppresses
  credential-class request detail at park time. Every op outside the two governed
  tiers likewise keeps its `unavailable` contract.
- **The USER auto-execute hole.** `_non_agent_verdict` (`operations/_validate.py`)
  previously parked a human only on `requires_approval`; the
  `safety_level→park` mapping lived on the service-principal branch alone.
  A USER dispatching a `destructive` op declared `requires_approval=False`
  would therefore auto-execute, bypassing the whole gate. The verdict now
  parks the `destructive` tier for **every** non-agent principal,
  unconditionally — and no standing grant can pre-clear it
  (`consult_and_record_grant` already refuses the tier).
- **The self-approval break-glass hole.** `_check_self_approval`
  (`operations/approval_queue.py`) honoured `APPROVAL_ALLOW_SELF_APPROVAL`
  for every tier. It now refuses self-approval of a `dangerous` **or**
  `destructive` row *unconditionally* (`_NO_SELF_APPROVAL_TIERS`) — the
  break-glass switch does not reach either tier (decision requirement 1).
  #3198 scoped this to `destructive` only, but a delete-class op can
  legitimately register on the reversible `dangerous` tier (e.g. a KV
  secret-version soft-delete) while still being the four-eyes-critical
  action the requirement protects, so #3290 widened the refusal to cover
  it; `caution` / `safe` still honour break-glass. A single-operator
  tenant uses the agent-requester pattern, never self-approval.
- **Registration fail-fast.** `_register_in_session` (`operations/typed_register.py`)
  now rejects a `destructive` descriptor declared `requires_approval=False`
  at registration time — an inconsistent row can't exist. Belt-and-suspenders
  with the runtime park guarantee above.

Conformance for the op — no agent path (agent verdict `DENY`), no standing
grant (`ServicePrincipalGrant` refuses), no self-approval even under
break-glass, no satellite mint (`MintRefusalCode.OP_NOT_SAFE`), dispatch
refused without a matching hash, park refused without a blast-radius block,
and the full preview→park→human-approve→audited-resume flow — is pinned in
`tests/test_connectors_vmware_rest_vm_destroy.py`.

### Second governed delete: `bind9.record.delete` (#3231)

The bind9 arm of the tier — and the **first `destructive` op on a typed SSH
connector** (the vm.destroy machinery above needed no change to reach it, so
this Task added no gate code, only the op). `bind9.record.delete` deletes
exactly one DNS record scoped by `(zone, name, type, rdata?)`, for the
DNS-retirement leg of a governed environment teardown in a shared zone. It
reuses everything above unchanged: the typed op is `destructive` +
`requires_approval=True`, so it folds into the delete-shaped classifier via
the single-source `safety_level` (`_delete_shaped_reason_by_descriptor`); the
`destructive` previewability gate already covers typed ops (not only
composites); its blast radius (`ops_record_delete_preview`) names the exact
record + its sibling values with `irreversibility="recreatable"`. Fail-closed
structured refusals mirror `vm.destroy`'s `not_powered_off`: `not_found`,
`ambiguous` (candidates named), `unmanaged_zone`. Conformance is pinned in
`tests/test_connectors_bind9_record_delete.py` — including a monkeypatch test
proving the grant refusal folds via the single source with the op-id glob
patterns blanked (the #3213 fail-open guard). See
`connectors-bind9.md` for the handler/preview layout.

## Uniform op-identity envelope (#2681)

The preview base varies by outcome — a bespoke `{op_class, preview}`, the
generic `{op_class, params_echo}`, the `preview_unavailable` marker, or the
empty base when the op declines. Before #2681 the identifier fields
(`op_id` / `connector_id` / `target_id`) rode **only** the
declines/raises paths, so the parked `proposed_effect` field-set swung with
runtime state: an `argocd.app.delete` parked against a *nonexistent* app
(cascade builder raises → identifier fields present) had a different schema
from the same op against a *live* app (builder succeeds → identifier fields
absent). A consumer could not rely on one schema per connector.

`dispatcher._build_proposed_effect` now stamps the op-identity fields
(`op_id` / `connector_id` / `target_id`) **and** `op_class` onto **every**
non-`None` envelope via `setdefault`, alongside `preview_populated` /
`safety_level`. `setdefault` never overrides a value the preview hook
already supplied (its own `op_class`, or an identifier a builder chose to
echo). The bespoke `preview` XOR generic `params_echo` **content** key
legitimately still varies and is *not* unified — so the contract is "the
op-identity + metadata field-set is uniform," not "`set(effect.keys())` is
identical." `target_id` is present only when the target carries a UUID id
(a targetless op omits it uniformly). This is verified by the park-envelope
parametrisation over all seven ArgoCD write ops plus the nonexistent-app
delete case in `tests/test_connectors_argocd_write_e2e.py`.

Relatedly, `classify_op` gained the ArgoCD write verbs so all seven ops
classify `op_class: write` rather than four of them falling through to
`other`. All four verbs that were misclassified — `argocd.app.set` /
`.sync` / `.rollback` / `.refresh` — are pinned by **exact op-id** in
`_WRITE_OPS` rather than added to the global write-suffix set. A `.set` /
`.sync` / `.rollback` suffix would classify the dotted argocd op-ids
correctly, but `_MEHO_FLAT_WRITE_SUFFIXES` is auto-derived from
`_WRITE_SUFFIXES` (`.` → `_`), so it would also relabel `meho_`-prefixed
MCP-tool ops such as `meho_broadcast_overrides_set` from `other` to
`write`; a `.refresh` suffix would likewise relabel the read-class
`topology.refresh` at render time. Exact-pinning the dotted argocd op-ids
avoids both collisions. See [`events.md`](events.md).

## Permission preflight hook (#1504)

The generic `proposed_effect` *preview* above is suppressed for
credential-class ops without a bespoke builder — but a credential write
(`vault.kv.put`) is precisely the op most
likely to be **denied** by Vault *after* a human spends a four-eyes
review approving it (the `meho-mcp` role grants `read` but no
`create`/`update` on the write path). To surface that at park time
without violating the redaction rule, `_preview.py` adds a **second**,
parallel registry: `register_permission_preflight(op_id, preflight)` +
`build_permission_preflight`.

A permission preflight is distinct from a preview:

- A **preview** says *what the write would do* (request/response shape) —
  so it is suppressed for credential-class ops.
- A **permission preflight** says *whether the dispatching identity is
  authorized to perform the write* — it returns only authorization
  metadata (Vault capability names, never a secret value), so it runs
  for **every** registered op regardless of sensitivity class. The
  credential-class suppression that gates previews does **not** apply.

At the park point, `dispatcher._build_proposed_effect` runs **both**
hooks and merges them: the preview (or the identifier-only default when
there is no preview) is the base, the preflight result is attached
under `proposed_effect["permission_preflight"]` when it fired, and the
catalog `safety_level` (#1855, above) is stamped on top. Both hooks are
opt-in and fail-soft — a preflight that raises degrades to no banner and
the park proceeds.

**A will-be-denied verdict refuses the park (#3542).** When the preflight
returns `will_be_denied: true`, MEHO does not write an approval row.
`dispatcher._preflight_denied_refusal` runs after `_build_proposed_effect`
and the destructive-tier checks, at both park points (`_handle_needs_approval`
and the composite sub-op seam `composite.enforce_subop_policy`). It
returns `status="denied"` with `error_code="write_capability_denied"` and
`extras` `path` / `granted` / `required` / `reason` / `doc_ref` (the write
policy, `connector-vault-policy.md#6-kv-v2-write-surface`). So no person
spends an approval on a write the identity cannot perform. The caller adds
the write grant and calls again. Both park points still write one `denied`
audit row for the attempt on the refused op's own path (the refusal details
ride under `payload["error"]`) and publish one `denied` broadcast event, the
same as an ordinary policy denial. They share one writer,
`dispatcher._audit_denied_and_return`. For a refused step inside a
multi-step operation the row is written by the sub-op gate and carries the
parent operation's audit id in `parent_audit_id`. The parent returns the
step's result as is and gets its own `denied` execution row from the
dispatcher (#4275); the step's row records the step's decision. A plain
policy "no" for a step writes the same step row. Every row the step gate writes (this
`denied` row and the `approval.request` row of a step that waits) carries
the step's own policy result in `policy_decision`, `needs-approval`, not
the parent's (#4646). The gate binds the step's result for its own call
only, so the parent operation's own audit row keeps the parent's result. Before #3542 the verdict was only a
warning and the op parked anyway; rows parked then still carry
`write_capability_warning`, which the console modal shows as a banner.

The KV-v2 write ops (`vault.kv.put` / `vault.kv.patch` /
`vault.kv.delete`) register a preflight that probes
`POST sys/capabilities-self` on the target `<mount>/data/<path>` and
returns `{check, path, required, granted, will_be_denied, principal_sub}`
— see
[`docs/codebase/connectors-vault.md`](connectors-vault.md) "Park-time
write-capability preflight" and
[`docs/cross-repo/connector-vault-policy.md`](../cross-repo/connector-vault-policy.md)
§6 for the write policy + verify command. **Whose token:** the preflight
runs under the dispatching operator's token, but the approved re-dispatch
runs under the **reviewing** operator's token
(`resume_dispatch_after_approval`); both share the `meho-mcp` role policy,
so the preflight is the right early signal while the reviewer must carry
the same grant.

`k8s.secret.read_to_ref` (#3844) also registers a preflight. The op reads
a Kubernetes Secret and writes the value to Vault through the
`vault.kv.put` handler, so it gets the same `vault.kv.put` check on the
path it writes to: `secret/data/tenants/<tenant_id>/<register_as>`. Its
preview stays hidden (it is a credential op), but the check still runs,
so a write Vault would refuse is refused at park time instead of after
approval. See
[`docs/codebase/connectors-kubernetes.md`](connectors-kubernetes.md).

## Transports

### REST (`backend/src/meho_backplane/api/v1/approvals.py`)

**Access model — approve-only, decoupled from dispatch (#3243).** Every
row below except `/result` is gated by `require_approvals_access`
(`auth/rbac.py`), which admits a principal that is **either**
`operator`-or-higher on the linear tenant-role lattice **or** carries the
orthogonal `approver` capability (`Operator.approver`, lifted from the
optional fail-closed `approver` JWT claim). This lets a dedicated approver
be provisioned as `read_only` role + `approver=true`: it can clear a
four-eyes gate here yet is denied `POST /api/v1/operations/call` (still
`require_role(TenantRole.OPERATOR)`, and `read_only` ranks below
`operator`), so "approve but never operate" is now expressible at the role
layer instead of resting on operator discipline. The gate has a
service-layer twin (`_check_reviewer_role`) applying the same rule for
direct queue callers, and the `sub`-keyed self-approval refusal (#1401) is
unchanged — an approver cannot approve a request it parked. A refused
attempt is 403 `insufficient_role` + an `insufficient_approvals_access` log
line. The `/result` read is deliberately **not** on this gate: it is
principal-scoped on `principal_sub`, so an approver deciding a request it
did not park gains no result-read rights.

**Human-only on every transport (meho-internal#289).** `require_approvals_access`
gates on *role/capability*, not on *what authenticated the request* — and an
agent principal is minted `tenant_admin` with the REST audience
(`auth/agent_principals.py`), so its client-credentials token clears that gate.
The three decision verbs (`approve` / `reject` / `decide`) therefore carry a
second, transport-independent dependency — `require_human_principal`
(`auth/rbac.py`) — evaluated **before** the role floor: it refuses any machine
`principal_kind` (`agent` / `service` / `runner`, the `MACHINE_PRINCIPAL_KINDS`
frozenset in `auth/operator.py`, via the shared `is_human_principal` split) with
403 `human_principal_required` and a remediation naming the console / CLI human
path. This is the REST peer of the MCP transport's human-only block
(`mcp/human_only.py`): removing the decision verbs from the agent surface there
and refusing the machine kind here are **one** policy — approval is a human
decision (v0.1-spec §7) — not two per-transport rules, and both consult the same
`is_human_principal` split. The service-layer twin `_check_reviewer_role`
enforces the same refusal for direct queue callers, raising `NonHumanApprovalError`
(a `UnauthorizedApprovalError` subclass, so the routes still map it to 403), so
the guard holds even for a caller that reaches the queue below the REST
dependency. The **read** plane is deliberately unchanged — `GET` list/show stay
on `require_approvals_access` alone — so a `service` principal (the
meho-automation approval bridge) still lists the tenant queue. The gate removes
only machine kinds: the `approver` capability model (#3243) and the seniority
self-approval rules are preserved unchanged for **human** principals.

The realm-side provisioning path for the `approver` claim — a group-based
Keycloak protocol-mapper recipe (`kcadm.sh` primary, Admin Console
secondary), claim-name alignment with `JWT_APPROVER_CLAIM_NAME`,
token-decode + live decouple verification, and a rollback note — lives in
[`../cross-repo/keycloak-tenant-claims.md`](../cross-repo/keycloak-tenant-claims.md#the-approver-approve-only-capability--group-based-recipe-3243).

| Verb | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/approvals` | operator **or** approver | List, newest first, filtered by `status` (default: `pending`; `all` = every state, #5129). Optional `limit` (1..500, no limit when omitted) and `offset`. |
| `GET` | `/api/v1/approvals/{id}` | operator **or** approver | **Inspect one request** (T5). 404 on cross-tenant. |
| `GET` | `/api/v1/approvals/{id}/result` | **request owner** (any role) | **Read the approved dispatch's result** (#3209). Principal-scoped: only `principal_sub` reads it; any other principal gets 403 `not_request_owner`. Writes an `approval.result` audit row. |
| `POST` | `/api/v1/approvals/{id}/approve` | **human** operator **or** approver | Approve. Requires `params` (hash-verified). Re-hydrates the target by id, then re-dispatches with `dispatch(..., _approved=True)`. 403 `self_approval_forbidden` when the approver is the requester and break-glass is off (G11.7-T1); 403 `human_principal_required` for a machine `principal_kind` (#289). |
| `POST` | `/api/v1/approvals/{id}/decide` | **human** operator **or** approver | Decide (`approved` / `rejected`) by id alone. For an approved **direct** op (`run_id IS NULL`) re-dispatches with the **stored** params (#1503) and returns the outcome in `dispatch_*`; for an agent-run request records the decision only (the agent runtime resumes). 403 `human_principal_required` for a machine `principal_kind` (#289). |
| `POST` | `/api/v1/approvals/{id}/reject` | **human** operator **or** approver | Reject. The op never executes. 403 `human_principal_required` for a machine `principal_kind` (#289). |

### MCP (`backend/src/meho_backplane/mcp/tools/approvals.py`)

Two **read** `meho_approvals_*` tools (both `TenantRole.OPERATOR`-gated):

- `meho_approvals_list` — `status` filter (`pending` default), `limit`/`offset`.
- `meho_approvals_get` — full detail by id.

The decision verbs — `approve` and `reject` — have **no MCP path under
any claim set** (#3155, Initiative #3153). Approving or rejecting a
parked operation is a *human* decision (v0.1-spec §7); a model session
that parked an op must not hold the button that clears its own gate (the
self-approval hole `#3143` F4 surfaced). They stay on the REST / CLI /
console surfaces above, unchanged. Their wire names are pinned in
`backend/src/meho_backplane/mcp/human_only.py`, so a `tools/call` on
`meho_approvals_approve` / `meho_approvals_reject` is denied before the
registry lookup with a remediation naming `meho approvals approve|reject`
and the operator console.

RBAC on the surviving read tools is enforced at two layers: the MCP
registry filter hides them from non-operators in `tools/list`, and the
dispatcher re-checks `required_role` at `tools/call`.

### CLI (`cli/internal/cmd/approvals/`)

`meho approvals list / show <id> / approve <id> / reject <id> [--reason]`
verbs that hit the REST surface via the generated typed client. `list`
has a `RUN` column and `show` / `approve` print how the run of an approved
request ended (#3543, see "Approved, but the run failed" above).
`list` shows only pending requests unless `--status` is set; `--status all`
shows every state. An empty list names the state it filtered on and points
to `--status all` (#5129). `--limit` / `--offset` go to the server as
`limit` / `offset`. When `--offset` skips every match, the empty message
says the page is empty and points to a smaller `--offset`, so it does not
claim the tenant has no requests.

### Operator console (`backend/src/meho_backplane/ui/routes/approvals/`)

A session-BFF surface (cookie session + CSRF double-submit, not the
Bearer REST routes — a browser carrying only the BFF cookie cannot auth
those). Every read + decision derives `tenant_id` from the validated
`UISessionContext` only, and calls the `approval_queue` service
in-process (same in-process-audit binding the REST routes get).

**Access model — operator-or-approver, parity with REST (#3243 / #3270 /
#3282).** The console decide path carries the **same** access model the
Bearer REST plane adopted in #3243/#3270, reached through the *same*
shared service gate rather than a console-local role check. The BFF
reconstructs the full `Operator` via `_resolve_operator` →
`verify_access_token_with_refresh` → `_operator_from_claims` (which lifts
the orthogonal `approver` claim, `Operator.approver`), then calls
`approve_request` / `reject_request`; their `_check_reviewer_role` admits
a principal that is `operator`-or-higher **or** carries `approver`, and
refuses only a `read_only` principal *without* the flag (403, re-rendered
in the modal banner naming both levers). So a dedicated approver
provisioned as `read_only` role + `approver=true` can clear a four-eyes
gate through the console exactly as it can over REST/CLI. The read
surfaces (badge / panel / history / detail) stay gated on the BFF session
alone — any authenticated console session may glance the tenant-scoped
queue, unchanged. Two invariants are preserved and pinned in
`test_ui_approvals.py`: the `sub`-keyed self-approval refusal (#1401) is
orthogonal to the capability (an approver still cannot decide a request it
parked), and the capability grants **no other console surface** —
`operator.approver` is consulted only by `require_approvals_access` (REST)
and `_check_reviewer_role` (service), never by dispatch or any
`require_role`-gated admin plane, so `read_only + approver` remains denied
the operations/dispatch console and every operator plane. This is a
verify-and-pin of parity #3270 already established at the service layer —
no new gate, service call, route, or migration.

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/ui/approvals/badge` | Live **pending** count for the app-shell bell. Always `status='pending'` — it counts actionable work, not history. |
| `GET` | `/ui/approvals` | Content-negotiated. A normal navigation (no `HX-Request`) → the **full-page console**: status tabs (Pending / Approved / Rejected / Expired / All), a `work_ref` filter, and the decision-history list. The bell's `hx-get` (`HX-Request: true`) → the existing pending **panel** modal fragment (unchanged). (G10.8-T #1827) |
| `GET` | `/ui/approvals/list` | Decision-history partial — the HTMX swap target for the status tabs / `work_ref` filter / "Load more" offset pager. Reuses `list_pending(status=…, work_ref=…, offset=…)`; `tab=all` passes `status=None`. Pages with a real offset, not the badge's 50-row glance cap. (G10.8-T #1827) |
| `GET` | `/ui/approvals/{id}` | Request-detail modal. A pending row offers Approve/Deny; a **decided** row renders read-only with a decision banner ("Approved/Rejected by X at T"). An approved row also shows how its run ended; a failed run turns the banner red (#3543). The `proposed_effect` envelope renders **structurally** (#2447), not as raw JSON (below). |
| `POST` | `/ui/approvals/{id}/approve` | Approve in-process + re-dispatch the parked op + fail-open broadcast. |
| `POST` | `/ui/approvals/{id}/reject` | Reject in-process + broadcast; the op never runs. |

The console is **read-only over substrate that already exists** — it adds
no new service call, no `api/v1` Bearer route, no CLI verb, and no
migration. It does, however, register its `/ui/*` routes into the FastAPI
OpenAPI document (the UI routers are not `include_in_schema=False`), so a
new or changed `/ui/approvals*` route — e.g. the `partial=rows` query the
"Load more" pager added — DOES enter `cli/api/openapi.json` and the
generated client. Re-snapshot the OpenAPI doc (`cd cli && make
snapshot-openapi && make generate`) whenever a `/ui/approvals*` route is
added or its signature changes, or the "CLI API snapshot freshness" check
goes red. The internal `params` / `params_hash`
columns are **never** projected onto any UI view, the badge, or a
broadcast frame: the `render.project_request_to_view` projection omits
them by construction. Live updates ride the app-shell bell's body-wide
`meho:approval-bump` / `meho:approval-decided` events — the history list
re-fetches its active tab on them, so a decision made elsewhere drops out
of the open Pending tab without a reload. The decision **reason** lives on
the `audit_log` decision row, not the `ApprovalRequest` row, so the
history view shows who/when (`reviewed_by` / `decided_at`) but not the
free-text reason (an `audit_log` read deferred to a follow-up).

### Structured `proposed_effect` render in the modal (#2447)

`approvals/_modal.html` renders the parked `proposed_effect` envelope
**structurally** rather than as one opaque JSON `<pre>`: a `safety_level`
badge (mirroring the ops-launcher `safety_badge` mold — error for
`dangerous`, warning for `caution`) + an `op_class` chip in the header;
a `role="alert"` warning banner when `write_capability_warning` is set
(#2331; new parks with that verdict are refused since #3542, so only older
rows carry it); an error banner when `preview_unavailable` is true, carrying
`preview_error` (#1628); a reason-keyed notice when `preview_populated`
is false (`credential_write_redacted` → elevated-risk redaction copy,
`connector_did_not_populate` → a neutral "no preview" note, #2332); and a
key-value table of the operation-specific fields (the bespoke `preview`,
else the generic redaction-safe `params_echo`, #1856). The full envelope
stays available behind a default-closed `Show raw JSON` `<details>`. The
render is **generic** — no per-`op_id` branching; op semantics already
live in the server-side preview builders (`operations/_preview.py`), so
the modal just projects whatever keys the dispatcher stamped. Every key
is read via `.get` on the real dict, so a missing key degrades cleanly
under `StrictUndefined`. Template-only: no route, projection, or schema
change (`render.project_request_to_view` already passes `proposed_effect`
through verbatim).

## Broadcast events (T5)

`approval_queue.publish_approval_event` publishes one event per
lifecycle step, fail-open (a broadcast outage never blocks the durable
decision). Each call site lifts the decision row's
`request._audit_id` and publishes **after** the transaction commits, so
a phantom event cannot outlive a failed transaction:

| Stage | `op_id` | When | Call site |
|---|---|---|---|
| Create | `approval.pending` | `create_pending_request` (dispatcher parks a `needs-approval` verdict). | `operations/dispatcher.py::_handle_needs_approval` |
| Approve | `approval.approved` | `approve_request` succeeds. | `api/v1/approvals.py` (REST), `mcp/tools/approvals.py` (MCP) |
| Reject | `approval.rejected` | `reject_request` succeeds. | `api/v1/approvals.py` (REST), `mcp/tools/approvals.py` (MCP) |
| Expire | `approval.expired` | `expire_stale_requests` (periodic sweeper) per expired row, or a late retry with the same idempotency key that finds its request still `pending` past the deadline. Both use the `system:approval-expiry` identity. | `operations/approval_expiry.py::_sweep_one_tenant` (commits then publishes per returned row's `_audit_id`); `operations/dispatcher.py::_handle_needs_approval` (publishes the `_expired_request` before the new `approval.pending`) |

Payload: `approval_request_id`, `decision`, `connector_id`,
`approval_op_id`. The event's `audit_id` field is the decision row's
primary key (FK to `audit_log.id`); subscribers that want the full row
query `audit_log` by this id.

## Audit rows

Two rows per request lifecycle (the synchronous-audit invariant):

| Row | `path` | `status_code` | Written by |
|---|---|---|---|
| Request | `approval.request` | `202` | `create_pending_request` |
| Decision | `approval.decision` | `200` (approved) / `403` (rejected) / `410` (expired) | `approve_request` / `reject_request` / `expire_stale_requests` |

### Session-replay lineage (#2086)

The park → decide → execute chain reconstructs as **one subtree** on the
G8.2 replay surface (`GET /api/v1/audit/sessions/{id}/replay`), which
anchors on `audit_log.agent_session_id` and walks `parent_audit_id`.
Before #2086 every row in the chain carried NULLs for both, so an
approval-gated dispatch was invisible to replay (`{root: [],
row_count: 0}`) even with `MCP_REQUIRE_SESSION_ID=enforced` — the
approve/resume surfaces run on a *different task* (the approver's
request) than the parking dispatch, so the session contextvars bound at
the parking boundary are gone by decision time.

The fix follows the `work_ref` durability mold (#1659): persist at park,
re-hydrate later. Two columns on `approval_request` (migration `0053`):

- `agent_session_id` — the parking session, resolved at
  `create_pending_request` time by
  `operations/_audit.py::resolve_agent_session_id()`:
  `agent_session_id_var` (agent-run loop) first, then the MCP
  transport's `mcp_session_id` structlog contextvar (a direct operator
  dispatch over MCP). NULL outside any session.
- `request_audit_id` — the `approval.request` audit row's id
  (pre-generated so it can land on the row in the same transaction).

`approval_queue._write_audit_row` reads both off the row (never the
approver's contextvars): the request row anchors on the session with no
parent (chain root); decision rows anchor on the same session and
parent-link to `request_audit_id`.

The executed-dispatch row must also carry `parent_audit_id` for the
chain to replay as one subtree, and both resume paths bind it:

- **Operator surfaces** (REST `/approve` + `/decide`, MCP, UI) —
  `resume_dispatch_after_approval` →
  `_dispatch_resume_with_bound_context` re-binds `agent_session_id_var`
  \+ `parent_audit_id_var` (token-scoped, alongside `work_ref_var`)
  around the `dispatch(..., _approved=True)`, because the approver runs
  on a fresh task whose contextvars are unset.
- **In-process agent-run resume** (#2323, completing #2086) — the
  broadcast-driven agent waiter
  (`agent/approval_wait.py::_resume_approved_or_already_resumed`)
  re-dispatches on the agent's *own* task, where `agent_session_id_var`
  is still bound but `parent_audit_id_var` holds the original top-level
  value (`None` for a plain tool call). It loads the parked row's
  `request_audit_id` (tenant-isolated `get_request`) and binds
  `parent_audit_id_var` around `call_operation_with_approval`, so the
  agent-run chain no longer orphans its executed dispatch as a second
  root.

Net result on either path: >= 3 session-anchored rows per approved
chain, tree shape `approval.request` → {`approval.decision`, `<op_id>`}.
Pre-0053 rows keep NULLs and simply stay out of replay (the pre-fix
behaviour).

### Composite-child park lineage (#3348)

A **composite** that fans out to a governed write can park a *child*
sub-op rather than itself. Two seams reach the park:

- the recursive `dispatch_child` seam (`get_dispatch_child`), which
  re-dispatches the child through `dispatch()` and hits
  `_handle_needs_approval`; and
- the direct-session `enforce_subop_policy` seam
  (`operations/composite.py`, #2254), which a write composite migrated
  off `dispatch_child` calls **before** each governed sub-call and which
  parks via its own `create_pending_request`.

On either seam the parked child's `approval.request` audit row must
carry `parent_audit_id` = the **composite dispatch's own `audit_id`**, so
the G8.2 replay walk can attribute the fanned-out write to the composite
that issued it — the same linkage the approval entry surfaces through its
`request_audit_id` anchor. `create_pending_request`'s audit writer reads
that value from `parent_audit_id_var` (the row being written *is* the
`request_audit_id` row, so it takes the contextvar rather than
self-parenting — see the `stored_parent`/`else` branch in
`approval_queue._write_audit_row`).

The contextvar is bound for the **whole composite handler body** in
`operations/_branches.py::dispatch_composite` (bound to the composite's
`audit_id`, token-reset in `finally` before the dispatcher writes the
composite's own DISPATCH row, so the composite never self-parents). The
recursive seam additionally binds it internally per child dispatch;
binding at the handler-body boundary is what extends the same linkage to
the **direct** seam. Before #3348 only the recursive seam bound it, so a
service principal whose composite parked a child on the direct seam wrote
`parent_audit_id = NULL` — an orphan approval the replay walk could not
tie back to its composite.

A composite which returns the child's `awaiting_approval` result
**verbatim** (the usual write-composite shape) gets its own DISPATCH row
from the dispatcher since #4275: an `operation_execution` row with
`result_status="awaiting_approval"` under the composite's `audit_id`,
saved last, after the handler returns. The child's `approval.request` row
records that id as its parent, so the parent row exists once the call
returns. The composite's row keeps `awaiting_approval` forever; the
approved work gets the second run's row, whose parent is the
`approval.request` row. See "Results a handler returns" in
[`audit_query.md`](audit_query.md). Reproducing the whole composite step
on approve is a separate concern (#3351, below).

### Composite-child resume: re-enter the parent, not the child key (#3351)

A parked composite **child** cannot be resumed by re-dispatching its own
stored `op_id`. `enforce_subop_policy` records the sub-op's *governance
key* + identity-only gate params, and that pair is not a dispatchable
descriptor call:

- a VI-JSON vim child (`POST:/Folder/{moId}/CreateVM_Task`,
  `POST:/VirtualMachine/{moId}/ReconfigVM_Task`, …) requires a `{moId}`
  path var the gate params omit and carries no assembled `ConfigSpec`
  body (the composite builds the wire body separately, after the gate),
  so the generic re-dispatch fails `invalid_params` / `connector_error`;
- a REST `?action=` child (`POST:/vcenter/vm/{vm}/power?action=start`)
  bakes the action verb into the `op_id`, and that keyed form has **no
  ingested descriptor**, so the generic re-dispatch returns `unknown_op`.

Before #3351 the resume re-dispatched the stored key regardless, so the
approve produced a red resume row that executed nothing and still burned
the exactly-one-resumer claim (`resumed_at`), leaving the op
un-resumable. The only thing that "worked" was an out-of-band orchestrator
re-running the whole blueprint step after the decision — and even that
re-parked, because the approval did not clear the sub-op gate.

The fix re-enters the **parent composite** on resume:

- `dispatch_composite` binds `composite_dispatch_var` = the composite's
  `(op_id, params)` for the handler body (co-located with the
  `parent_audit_id_var` bind above). `enforce_subop_policy` reads it and
  stores it on `ApprovalRequest.resume_parent`
  (`{"op_id": …, "params": …}`, migration `0099`) at park time. NULL for a
  direct-op park.
- `resume_dispatch_after_approval` sees `resume_parent` and re-dispatches
  the **parent composite** (`_approved=True`, so its top-level gate is
  skipped) instead of the sub-op key, with `composite_resume_var` set to
  the approved sub-op's `(op_id, params_hash)` **and**
  `composite_resume_scope_var` set to the same pair (the scope var is not
  consumed on the match, so the guard below can still see it is inside a
  resume after the approved sub-op has cleared).
- On that re-run, `enforce_subop_policy` matches `composite_resume_var`
  and clears the one approved sub-op (auto-executes) instead of
  re-parking — reproducing the whole governed step through the normal
  dispatch path. The match is on `(op_id, params_hash)`, so the clear pins
  to the exact entity approved and consumes the var.

#### The actual contract: one resume completes the composite (#3351 review B1)

The resume does **not** re-park "one gate at a time". It re-enters the
composite under the **approving reviewer's** identity, and every governed
sub-op currently shipped is `dangerous` + `requires_approval=False` — a
verdict a `USER` auto-executes (`policy_gate` default-allow;
`_non_agent_verdict` re-gates a `USER` only on `safety_level=="destructive"`
or `requires_approval==True`). So the reviewer runs the approved sub-op
**and every remaining governed sub-op in the same pass**: a multi-governed-leg
composite (e.g. `vm.create` with `power_on_after_create=True`, which parks at
create then powers on) completes in a **single** resume, each leg firing
exactly once. The invariant the correctness relies on is therefore explicit:
**every governed sub-op of a composite must be `dangerous` +
`requires_approval=False`** so the reviewer auto-executes all remaining legs.

A **multi-gate re-park** — a governed sub-op a reviewer would *not*
auto-execute (`destructive` or `requires_approval=True`) — is **not
supported** by this re-entry mechanism. The mechanism clears exactly one
sub-op and keeps no record of already-executed legs, so a second park +
second resume would re-enter the composite from the top and re-run every
earlier governed leg — a duplicate write (the reviewer reproduced a duplicate
`CreateVM_Task` this way). Rather than allow that, the guard **fails closed**:
while `composite_resume_scope_var` is set, if any sub-op *other* than the
approved one reaches `NEEDS_APPROVAL`, `enforce_subop_policy` returns
`composite_resume_multi_gate_unsupported` — a distinct error naming the
composite, the approved sub-op, and the sub-op that would have parked — and
does **not** park a second request or continue. It is audited via the
composite's own dispatch audit row. This is enforced in
`enforce_subop_policy` (the `NEEDS_APPROVAL` branch, keyed off the unconsumed
`composite_resume_scope_var`) and is unreachable for shipped composites.

#### Continuing where it stopped (#4217)

Some steps run after the composite's main change already exists: the
rename or power-on of a VM that was just deployed, the delete that cleans
up a failed deploy, or `vm.create`'s rollback delete. Running the whole
composite again for such a step repeats the main change (it deploys a
second VM). So these steps tell the seam two things:

- `resume_state`: JSON the composite saves on the approval row, under
  `resume_parent["state"]`. During the re-run, `composite_resume_binding`
  binds it on `composite_resume_state_var`, and the composite reads it with
  `composite_resume_state(op_id)` (only the parent composite gets it). The
  composite uses it to continue at the step that waited, with the same
  parameters, so the approved sub-op still matches its stored parameter
  hash. The state must pin what the step acts on: the object id, or the
  resolved parameters. It must not hold a name the re-run would look up
  again, because by then the name may point at something else, and the
  step would no longer be the one the person approved.
- `after_main_effect=True`: this step runs after the main change. The row
  stores `resume_parent["after_main_effect"] = true`.

**The safety-check change (the relaxed guard).** The multi-gate guard above
does not apply to an `after_main_effect` step. Such a step may wait for
approval even during a re-run, because its own re-run continues at that
step and repeats no earlier write. `enforce_subop_policy` checks
`if resume_scope is not None and not after_main_effect`. Steps without the
mark still fail closed (`test_vm_create_multi_gate_resume_fails_closed_no_double_execute`).

**Who the new request belongs to.** A step that waits again during a
re-run gets a new approval request. That request names the requester of
the request being re-run (`principal_sub`, `principal_act`,
`principal_name`), not the reviewer who runs the re-run. Both resume paths
pass that requester to `composite_resume_binding`, which binds
`resume_requester_var`; `enforce_subop_policy` hands it to
`create_pending_request(requester=...)`. So the reviewer path and the agent
path save the same requester.

**The risk, and the two checks that limit it.** With the guard relaxed, safety
depends on the composite: its re-run must really skip every earlier write.
Two checks in `enforce_subop_policy` stop a composite that gets this wrong:

- `after_main_effect=True` without `resume_state` is refused
  (`composite_resume_state_missing`), in every run, before the policy
  check. Without state the re-run could only start from the beginning.
- During the re-run of an `after_main_effect` park, until the approved
  sub-op has cleared, any other governed write that is not itself
  `after_main_effect` is refused before it runs, whatever the policy
  verdict (`composite_resume_repeats_write`). That is a write the first run
  already did.

What is left: a composite can still repeat an earlier step that is itself
marked `after_main_effect` (for example a second removal in the same
cleanup), so only mark steps that are safe to run again. Ungoverned reads
are not checked.

Steps that use the switch today: in `vm.deploy_from_library` the rename,
the power-on, the cleanup VM delete and the vApp destroy; in `vm.create`
the rollback delete; in `vm.import_from_library` the power-on. Tests:
`test_operations_subop_policy_gate.py` (the seam checks, on their own) and
the `#4217` tests in `test_connectors_vmware_rest_composites_write_e2e.py`
(through the real dispatcher, including three approvals in a row where
each write runs once).

A direct (non-composite) park is unchanged — `resume_parent` is NULL, so
the resume keeps the generic re-dispatch of the stored `op_id`.

#### Both resume surfaces read `resume_parent` (#3421)

Two code paths can resume a request that waits for approval, and both
handle a composite (multi-step operation) sub-op the same way:

- the operator surface, `resume_dispatch_after_approval`
  (`operations/approval_queue.py`);
- the in-process agent waiter, `_resume_approved_or_already_resumed`
  (`agent/approval_wait.py`).

Both call one helper, `composite_resume_binding` in
`operations/approval_queue.py`. When the row has a `resume_parent`, the
helper returns the parent composite's `(op_id, params)` and sets
`composite_resume_var` and `composite_resume_scope_var` to the approved
sub-op while the parent runs again. When `resume_parent` is NULL it returns
`None` and sets nothing. Because both paths share it, they cannot drift
apart. `test_operator_and_agent_surfaces_resume_the_same_row_identically`
checks that both paths run the same row the same way.

What the agent waiter does:

- **Composite sub-op waiting for approval.** It runs the parent composite
  again (`_approved=True`) with the row's `connector_id`, the parent `op_id`
  and the parent params. It keeps the rest of the agent's original call
  (`work_ref`, …). The approved sub-op clears its gate and runs once. Before
  #3421 the waiter re-sent the agent's call without clearing the sub-op, so
  the sub-op created a second request and nothing ran.
- **Target check.** The re-run uses the target the row saved by id
  (`rehydrate_resume_target`, the same check the operator surface runs), not
  the target name from the agent's original call. While the request waited,
  that name may have moved to another target: the target was renamed, or
  deleted and a new one created with the same name. Loading by id means the
  approved write can only run against the original target. If the saved
  target no longer resolves (soft-deleted or revoked), the waiter returns a
  `denied` envelope and runs nothing. This check runs before the
  one-resumer claim, so a refused resume leaves the claim free.
  Covered by `test_composite_resume_uses_saved_target_when_name_was_reassigned`
  and `test_composite_resume_fails_closed_when_saved_target_deleted_and_name_reused`
  (unit) and the two `test_agent_waiter_composite_resume_*` tests (end to
  end).
- **Direct request (no `resume_parent`).** It runs the agent's original
  `call_arguments` again, including the target name, with one change: the
  `connector_id` is the catalog the request row stored. The park may have
  run on a sibling catalog of the one the agent named (for example the agent
  named `vmware-rest-9.0` for an 8.0.3 vCenter, so the park stored
  `vmware-rest-8.0`). That stored catalog is the one policy checked and the
  approver saw. An approved call never switches catalogs, so the resume runs
  on the stored catalog or is refused with `unqualified_target_version` when
  the target's detected version no longer fits it. Covered by
  `test_agent_resume_runs_on_the_stored_sibling_catalog` and
  `test_agent_resume_is_refused_when_the_target_version_moved`.
- **Credential-write sub-op.** The row stores only the parent `op_id`; the
  parent params live in the encrypted custody copy. For a composite sub-op
  the claim winner consumes that copy to get the parent params. For a direct
  request it still discards the copy.

One difference stays on purpose: the agent waiter re-enters the composite
as the **agent**, not as the reviewer. So every other governed sub-op is
checked against the agent's own grants. If one would need its own approval,
the multi-gate guard above fails closed
(`composite_resume_multi_gate_unsupported`) on the agent path too, unless
that step is marked `after_main_effect` (#4217): then it waits for its own
approval. If the agent has no grant for it, that sub-op is denied.

Note: every shipped governed composite is `requires_approval=True` at the
top level, so an agent's first call makes the composite itself wait for
approval (`resume_parent` NULL). A sub-op request with `resume_parent` set
happens later, inside the resumed composite. Today the agent waiter does not
wait on that second request; an operator surface resumes it. The waiter's
composite branch makes the agent path correct whenever it does resume such
a row.

## MCP elicitation URL-mode (forward-looking)

When an in-loop agent hits a `needs-approval` verdict, the agent
runtime can use the row's `id` (returned from `meho_approvals_get`) to
construct an elicitation URL of the form
`meho://approvals/{request_id}/decide`. MCP-2025-11-25 hosts that
support elicitation URL-mode can open this URL in the operator's
decision UI; until that lands, the operator approves/rejects via the
explicit `meho_approvals_{approve,reject}` tools.

## Agent-runtime resume on `approval.{approved,rejected}` (T9 #1117)

The operator/agent split, keyed on whether the parked request belongs to
an **agent run** (`run_id` set) or a **direct operator op** (`run_id IS
NULL`):

| Path | Decision capture | Re-dispatch (agent run) | Re-dispatch (direct op) |
|---|---|---|---|
| REST `/approve` with `params` | inline | inline (`_approved=True`, the human-driven express lane) | inline (caller params, hash-verified) |
| REST `/decide`, MCP, CLI | durable row + broadcast | **agent runtime**: the wrapped `call_operation` in `meho_backplane/agent/toolset.py` + `meho_backplane/agent/run.py` blocks on `meho_backplane.agent.approval_wait.wait_for_approval_decision` and re-invokes the dispatcher via `call_operation_with_approval` on approval, or surfaces the rejection to the model. | **inline** (#1503): the decision drives `resume_dispatch_after_approval` with the **stored** params (`run_id`-gated so this never fires for an agent run). |

For an agent-run request the broadcast-driven runtime resume is the only
re-dispatch path; `/decide`/MCP record the decision only. For a direct
operator op (#1503) `/decide`/MCP re-dispatch inline with the stored
params. See [agent-runtime.md § Awaiting-approval resume](agent-runtime.md#awaiting-approval-resume-t9-1117)
for the agent-run substrate's full shape, including the wait timeout
(`Settings.agent_approval_wait_timeout_seconds`, default 30 min) and the
fail-open semantics on broadcast outage.

## An add-on proposes a governed destroy (#3478)

A paired add-on signs in as a **service principal** (client credentials,
`principal_kind=service`). It **can** drive a destructive op. It cannot run
the op alone: a human must approve it. It needs no standing grant. This is
the supported way for an add-on to do two-person deletes without a person
typing each delete on the CLI.

The rule that confuses people: the destructive tier is DENY **only for
agent principals**. For service and user principals it waits for approval.
The rule is recorded in
[`governed-delete-operations.md`](../decisions/governed-delete-operations.md)
("Who may propose").

| Principal kind | Destructive op | Code |
|---|---|---|
| `agent` | Refused (DENY). Nothing is parked. | `auth/permissions.py:131` `_SAFETY_DEFAULT` + `:154` `_SAFETY_CEILING`; agent branch of `operations/_validate.py:450` `policy_gate` |
| `service` | Waits for approval. No grant can approve it in advance. | `operations/_validate.py:362` `_non_agent_verdict` (the `destructive` branch); `operations/service_grants.py:758` `consult_and_record_grant` refuses the tier before any grant lookup |
| `user` | Waits for approval. | `operations/_validate.py:362` `_non_agent_verdict` |

### The round trip

The add-on needs the `operator` role to call `POST /api/v1/operations/call`
and `/preview`. Paths are under `backend/src/meho_backplane/`.

1. **Preview.** The add-on calls `preview_operation` (REST
   `POST /api/v1/operations/preview`) with the exact
   `(connector_id, op_id, target, params)`. The reply carries a
   `preview_hash`. The reply does **not** carry the blast-radius statement.
2. **Call with the hash.** The add-on calls `call_operation` with the same
   values plus `preview_hash` (`operations/meta_tools.py:427`
   `CallOperationBody.preview_hash`, passed on to `dispatch` at `:1131`).
3. **Park.** The policy gate returns `NEEDS_APPROVAL`, so the dispatcher
   reaches `_handle_needs_approval` (`operations/dispatcher.py:2487`). For a
   destructive op it runs `_destructive_binding_refusal` (`:2364`, called at
   `:2567`) before it writes any row. That check recomputes the preview hash
   on the server and needs a valid blast-radius block on `proposed_effect`.
   A missing or wrong hash, or a missing block, is refused, and no row is
   written. On success the row stores `preview_hash`, and the blast radius
   lives on the row's `proposed_effect`. The add-on gets
   `status="awaiting_approval"` and `extras.approval_request_id`. It should
   store that id.
4. **A human approves.** The approver must be a different person: there is
   no self-approval for this tier, not even with break-glass
   (`operations/approval_queue.py:1835` `_check_self_approval`). The approve
   step checks the preview hash again (`:1926`
   `_check_destructive_preview_binding`). The decision verbs are human-only
   on every surface, so the add-on cannot approve its own request.
5. **The op runs under the approver.**
   `resume_dispatch_after_approval` (`operations/approval_queue.py:1221`)
   re-runs the dispatch with the **approver's** `Operator`. So the approver's
   role is what counts when the op runs, and the audit row for the delete
   names the approver. The result is saved on the row
   (`_persist_resume_result`, `:1359`). A large result goes into a result
   handle that the add-on owns (see "Who owns a result handle" above).
6. **The add-on reads the result.** The add-on polls
   `GET /api/v1/approvals/{id}/result` (`api/v1/approvals.py:614`,
   `read_approval_result` at `operations/approval_queue.py:2062`). Only the
   principal that parked the op may read it; anyone else gets 403
   `not_request_owner`, even the approver. The reply has `result: null`
   until the op has run.

A paired add-on can also read the approval outcome from its durable
step-event stream ([`addon-step-events.md`](addon-step-events.md), #3027).
That says *whether* the op was approved; `/result` says *what the run
returned*.

**Pinned by tests:**

- `tests/test_connectors_harbor_robot_delete.py::test_service_principal_proposes_destroy_and_adopts_result`
  — the full round trip for a service principal, including the `/result`
  read by the parker and the 403 for the approver.
- `tests/test_service_principal_grant_gate.py::test_service_principal_destructive_parks_and_grant_never_satisfies`
  — a destructive op parks for a service principal even with a live
  matching grant.
- `tests/test_connectors_harbor_robot_delete.py::test_agent_principal_is_denied`
  — the same op is refused for an agent principal.

## Service-principal standing grants + safety_level gate (#3151 / #3152)

The non-agent gate (`_non_agent_verdict`, `operations/_validate.py`) gained
two paired behaviours for **service** principals (`principal_kind =
service`), leaving the human `USER` path unchanged:

- **`safety_level` is now consulted** (#3152, resolved as option 1): a
  mutating `caution` op — and any `dangerous` op — parks even without
  `requires_approval`. Previously only `requires_approval` parked a
  non-agent op, so a `caution` mutation (`POST:/vcenter/vm/{vm}/hardware/disk`)
  executed ungated for a service principal. A human `USER` operator is
  **not** subject to this (they are their own approver).
- **Standing scoped auto-approval grants** (#3151): an operator issues a
  `(principal_sub, op_id, connector_id, target_id)` grant — the persistent
  form of an approve decision — so a service principal's repeated parks
  during an unattended run auto-approve instead. Each use writes an
  `approval.decision` / `auto-approved` audit row (this section's shape),
  carrying `grant_id` — same ledger visibility as a human decision.
- **Lineage ids on the auto-approval row** (#3695): the grant-use row is
  written at decision time with the same ids the parked path keeps, so an
  auto-approval links to the call it cleared:
  - `request_id` — the request's correlation id. The call's `DISPATCH` row
    now carries the same `request_id` (it was always NULL before), so you
    can find the call an auto-approval cleared, and its flight-recorder
    trace, by matching `request_id`. In a multi-step operation all child
    rows share the request id; use `parent_audit_id` to tell them apart.
  - `agent_session_id` — the agent-run or MCP session
    (`resolve_agent_session_id()`). NULL for a plain REST call, which has
    no session.
  - `parent_audit_id` — the multi-step parent's audit id
    (`parent_audit_id_var`) when the cleared call is a child step. NULL for
    a top-level call.

  There is still no row while a call runs; a call only gets its `DISPATCH`
  row when it ends. For long calls use async mode (`async: true` on REST
  `/call` or on MCP `call_operation`): the `operation_run` row shows the
  call as `running` with `started_at` for its whole run. See
  [`async-governed-dispatch.md`](async-governed-dispatch.md).

Grants are operator-role REST at `/api/v1/service-principals/grants`;
delete-shaped ops are refused at creation (the grant list is the *floor*
of what runs unattended, never a bypass of a modeled destructive gate).
Full design: [`service-principal-grants.md`](service-principal-grants.md).

## Sandbox mode (#4622)

On an instance in **sandbox mode** (decision record
[`docs/decisions/sandbox-mode.md`](../decisions/sandbox-mode.md)) the
destructive-tier rule above has one exception: a standing grant made in
the web console by a tenant admin, a **delete grant**, may cover a delete
by a service principal. The instance holds only disposable test systems,
so nothing waits for a person there. Everything else in this document
stays as it is: people and agents keep their rules, a run approval never
covers a delete, satellites get no delete, and self-approval stays
forbidden.

- The mode is one instance-wide switch, set at install time and fixed for
  the life of the database (`meho_backplane/sandbox_mode.py`). Every other
  instance shows `"sandbox_mode": "off"` on `/ready` and
  `GET /api/v1/health`, and every audit row carries
  `audit_log.sandbox_mode = false`.
- A `destructive` call under a delete grant still needs its preview hash
  and a valid blast radius. The gate runs the same checks the park path
  runs (`_destructive_binding_refusal`) before it records the grant use,
  so a refused call leaves no `approval.decision` row. The grant-use row
  stores the hash and the blast radius in `payload`.
- A delete below `destructive` (the verb `DELETE`, the `destructive` tag,
  a delete name, a delete step) runs under a delete grant with no hash;
  its grant-use row stores the proposed effect.
- A `destructive` step gate or child call stays refused: it cannot carry a
  preview hash (`preview_binding_required`, as today).
- Delete grants live in `sandbox_delete_grant`, which an older build and an
  instance with the mode off never read: after a rollback every delete
  waits for a person again.
- With the mode on, the park reasons add that a delete grant made in the
  web console may cover the delete; with it off every reason text is
  unchanged.

Details: [`service-principal-grants.md`](service-principal-grants.md#sandbox-mode-4622).

## References

- `backend/src/meho_backplane/operations/approval_queue.py` — queue lifecycle (T4) + read helpers + broadcast (T5) + park-time `expires_at` stamping and `expire_stale_requests` coalesce (#2322).
- `backend/src/meho_backplane/operations/service_grants.py` — service-principal standing grants + the grant-use `approval.decision` audit row (#3151 / #3152); see [`service-principal-grants.md`](service-principal-grants.md).
- `backend/src/meho_backplane/operations/approval_expiry.py` — periodic per-tenant approval-TTL sweeper (#2322).
- `backend/src/meho_backplane/api/v1/approvals.py` — REST routes.
- `backend/src/meho_backplane/mcp/tools/approvals.py` — MCP tools.
- `cli/internal/cmd/approvals/` — CLI verbs.
- `backend/alembic/versions/0023_create_approval_request.py` — schema.
- `backend/alembic/versions/0036_add_approval_request_params.py` — `params` column for direct-op approve re-dispatch (#1503).
- `backend/alembic/versions/0053_add_approval_request_session_lineage.py` — `agent_session_id` + `request_audit_id` lineage columns for session replay (#2086).
- `backend/alembic/versions/0096_add_approval_request_resume_result.py` — `resume_result` column: the reduced result envelope of the approved re-dispatch, surfaced to the originating consumer (#3209).
- `backend/src/meho_backplane/operations/approval_queue.py::read_approval_result` — principal-scoped result read + `approval.result` audit row (#3209).
- `backend/src/meho_backplane/api/v1/approvals.py::get_approval_result` — `GET /api/v1/approvals/{id}/result` (#3209).
- `backend/src/meho_backplane/operations/approval_queue.py::resume_dispatch_after_approval` — the single execute-after-approve entry point shared by every operator surface (#1503).
- `backend/src/meho_backplane/agent/approval_wait.py` — agent-runtime resume substrate (T9 #1117): broadcast subscription + re-dispatch on approval.
- `backend/src/meho_backplane/operations/meta_tools.py` — `call_operation_with_approval` (the gate-bypass re-dispatch entry point).
