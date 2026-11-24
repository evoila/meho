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
shows each case. In short (#3325):

- **A person in the web interface:** `safe` and `caution` run at once.
  `dangerous`, `destructive` and ops marked `requires_approval` wait.
- **Every call made with a token, and every agent:** MCP, the `meho`
  command line and REST, also with a person's own token. Only `safe` runs
  at once. `caution`, `dangerous` and `destructive` wait.
- **Service principals** (fixed automation) keep their standing-grant
  rule: a grant can let `caution` and `dangerous` changes run; without
  one they wait. A person can also approve the operation list of one
  automation run once ([run-approvals.md](run-approvals.md)).
  `destructive` always waits for a person. The one exception is a test
  install in sandbox mode: there, a delete grant made in the web console
  can cover a delete (see "Sandbox mode" below).

A person approves or rejects a waiting request on **REST, the `meho`
command line and the web interface** only. Approve and reject have no
MCP path. Who may approve which request follows the approval groups (see
[The approval groups and how a person approves](#the-approval-groups-and-how-a-person-approves-3325)).
Two layers, one source of truth:

- **T4 owns the queue lifecycle** (`backend/src/meho_backplane/operations/approval_queue.py`):
  `create_pending_request`, `approve_request`, `reject_request`,
  `expire_stale_requests`, `withdraw_request`. Every mutation writes a
  synchronous audit row in the same transaction (the "request" row on
  create; the "decision" row on approve/reject/expire; the
  `approval.withdraw` row when the sender takes the request back).
- **T5 wires the operator surfaces on top:** `GET /api/v1/approvals/{id}`
  (inspect), the two read MCP tools `meho_approvals_list` and
  `meho_approvals_get` (see [MCP](#mcp-backendsrcmeho_backplanemcptoolsapprovalspy)), and
  `meho approvals {list,show,result,approve,reject,withdraw}` CLI verbs. Since #3758 no
  approval step is published to Broadcast. `record_approval_step_event`
  records `approval.{pending,approved,rejected,expired,withdrawn}` only for the
  paired add-on that asked (#3027); the audit log records every step; the
  agent waiter, the bell badge and the console read the stored row.
- **Who reads the `env` values of `linux.script.run`.** The REST list and
  detail (`GET /api/v1/approvals`, `GET /api/v1/approvals/{id}`) give a
  person's login the whole stored `proposed_effect` of a `linux.script.run`
  request. An agent, service or runner login gets it through
  `proposed_effect_for_agent`, the same as the MCP read tools: with the `env`
  names, without the `env` values (`_PERSON_ONLY_REST_CARD_OPS` in
  `api/v1/approvals.py`). The REST answer for every other op is the stored
  `proposed_effect`, for every caller.

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

**An approved request that nobody starts ends too.** The same sweep ends
an `approved` request whose run nobody claimed (`resumed_at` is empty)
within `APPROVAL_DEFAULT_TTL` after its decision
(`expire_unclaimed_approved_requests` in
`operations/approval_stored_values.py`). It moves to `expired` with the
same decision audit row, and its `expires_at` becomes the end of the time
it had to start (`decided_at + APPROVAL_DEFAULT_TTL`), so every surface
shows the usual expired reason. `end_run_input` runs on it: the encrypted
handoff row is deleted, each secret-named value of the stored params
is replaced by the marker, and the parent of a step of a multi-step
operation keeps only its op id (`resume_parent_without_inputs`). A claim of its run after that finds no
`approved` row and is refused; an agent that still waits for it reads the
usual expired answer (`approval_expired`). So a stored request keeps the
values of its call at most `APPROVAL_DEFAULT_TTL` after the decision.

**The start-up clean-up.** At every start, right after the release check
below, `hide_values_of_requests_that_cannot_run` reads every request that
is not `pending`, was never started (`resumed_at` is empty) and cannot
run: a rejected, expired or withdrawn request, and an approved
request of another release (an approval covers one exact release; without
a release identity no approved request counts). Each one gets the
`end_run_input` clean-up: its secret-named values and the param a
connector hides become the marker, the parent of a step keeps only its
op id, and its handoff row is deleted. This covers rows stored by an
older release, for example a `k8s.apply` of a Secret whose manifest is
still in `params`, or in the params of its parent. An approved request of
another release also ends as `expired` in the same transaction, with one
decision audit row and an `approval.expired` step event after the commit.
The audit row and the request read the same reason: `release_changed`
while its time to start had not run out, else `deadline_passed` (the audit
row then has no `reason`). So during a rolling update a MEHO that still
runs the older release cannot start a request whose inputs are gone: its
claim finds no `approved` row, and the database refuses any write that
sets `resumed_at` on a `rejected`, `expired` or `withdrawn` request (the
CHECK `ck_approval_request_ended_not_started`, migration `0184`). A claim
of an older release that read the row before the clean-up and writes after
it fails there. It reads 500 rows per
transaction, in the order of their id, writes only rows that change, and
logs the counts (`approval_stored_values_cleaned`). A failure is logged and
the start goes on.

## An approval covers one exact release (#5030)

A person approves one exact release of MEHO. When MEHO is updated, every
request that still waits from an older release ends, and the requester
asks again under the new release. MEHO is updated about every week, and a
request can wait up to 14 days, so an update often ends waiting requests.
This costs one more approval for a request that waits across an update.
It is the simple and strict choice.

**What MEHO records.** `create_pending_request` stores the running
release on the new column `approval_request.release_id` (migration
`0126`). The value comes from `version.running_release_id()`: the full
commit hash (`GIT_SHA`), plus the chart version (`CHART_VERSION`) when it
is set, plus a short digest of the installed connector packages (name,
version and wheel digest of each) when the image carries any. These are
the same values `GET /version` shows. So an image with the same commit but
a new chart, or a new, updated or removed connector package, is another
release, and the waiting requests end at its start.

**At start-up.** `main._run_lifespan_startup` calls
`approval_expiry.expire_approvals_from_older_release()`. For each tenant
it calls `approval_queue.expire_older_release_requests()`, which moves
every `pending` request whose `release_id` is not the running release to
`expired`. A request with no `release_id` (saved before this change)
counts as an older release, so the first start with this change ends it
too. The expiry reuses the TTL path (`_expire_request`): one
`approval.decision` audit row each, under `system:approval-expiry`, with
`reason: "release_changed"`, `request_release` and `running_release` in
the payload. A request already past its deadline expires as the TTL sweep
would expire it, with no extra reason. This runs whether or not
`APPROVAL_EXPIRY_ENABLED` is set: it is part of the approval rule, not the
TTL clean-up. A failure is logged and the start goes on; the check below
still stops any old approval from running. Right after it, the start-up
clean-up above ends every approved request of another release that was
never started, with the same reason.

**Before an approved run.** Both resume paths call
`approval_queue.refuse_resume_if_release_changed()` before they run
anything: the operator surfaces (`resume_dispatch_after_approval`) and the
in-process agent waiter (`agent/approval_wait.py`). When the request's
`release_id` is not the running release, the operation does not run and
the vendor gets no call:

1. The one-resumer claim (`claim_resume`) is taken, so the request can
   never run later. If another resumer took it first, the usual
   `already_resumed` result comes back. The claim winner then deletes the
   request's stored run input (`approval_handoff.discard`), the same as
   every other way a request ends. The claim itself already replaced the
   secret fields of the stored params with a marker (see "The params hash
   on requests and audit rows").
2. One audit row records the refusal: `path = "approval.resume"`,
   `status_code = 409`, `result_status = "release_changed_while_waiting"`,
   both releases in the payload.
3. The caller gets a `denied` result with
   `error_code = "release_changed_while_waiting"`, and the refusal is
   stored as the request's run result. So every read surface shows
   `result_state = "failed"` with that error code.

The same three steps (`approval_queue._refuse_resume`) record a run
refused because its target changed or no longer resolves
(`refuse_resume_if_target_changed`, see
[An approval covers one target and one preview](#an-approval-covers-one-target-and-one-preview)).
The audit row then carries `result_status = "target_changed_since_approval"`
or `"target_gone_since_approval"` and the `target_id`.

This covers a request that was approved just before an update and had
not run yet. A remote-write mint for a satellite runner checks the same
rule: an approval from an older release mints nothing
(`REMOTE_WRITE_GATE_UNSATISFIED`, logged like the other mint refusals,
and the approval stays unused).

**No release identity.** A build with no `GIT_SHA` (or `GIT_SHA=unknown`)
has no release identity. Only local development runs like this; every
published image sets `GIT_SHA`. Then MEHO stores no `release_id`, ends no
request at start-up, and refuses no run. The start-up check logs one
`approval_release_binding_off` warning so this is visible. A request made in such a run has
no `release_id`, so the first start of a real release ends it.

**What the requester sees.** The read views (REST
`ApprovalRequestView`, the MCP approvals tools, the console) carry three
added fields: `release_id`, `expired_reason` (`deadline_passed` or
`release_changed`, only for an `expired` request) and `expired_message`,
a plain sentence with the advice to ask again. `meho approvals show`
prints `Release:` and `Expired:` lines. The reason is read off the row and
not stored: only the start-up expiry ends a request before its deadline,
so an `expired` row with `decided_at < expires_at` expired because the
release changed. The console modal shows such a request with the time
the update ended it (`decided_at`), not the deadline it never reached.
An expired request that was approved first keeps its reviewer. The
console names that person as the one who approved it: the modal reads
"Expired · approved by X" and the list row reads "approved by X".
For a run MEHO refused before it ran, the console says why (MEHO was
updated, the target changed, the target is gone, or the preview changed),
that the operation did not run, and that the requester must ask again; it
does not say the operation ran and failed. The sentences are in
`ui/routes/approvals/render.py` (`RESUME_REFUSAL_MESSAGES`), keyed by the
error code of the stored result.

**During a rolling update.** The chart's default update strategy is
`Recreate` (#5024): the old pod stops before the new pod starts, so this
does not happen. With `updateStrategy.type: RollingUpdate`, for a short
time old and new pods run side by side. A new pod ends the waiting requests it finds when it starts. A
request that an old pod saves after that point still waits; if it is
approved, a new pod refuses to run it. Once both releases have this
check, an old pod also refuses to run a request made by a new pod. In both cases nothing from the wrong release
runs; the requester asks again.

**Later, not built yet: a finer binding.** Each operation could get an
execution version: a checksum of its handler code, the shared code it
uses, its schemas, its risk level, its approval flag and its steps. Then
an update would end only the requests whose operation changed. That needs
its own design.

## An approval covers one target and one preview

A person approves one call against one target, as it is set up when the
request is made. Every approved run checks this right before it runs: the
operator surfaces (`resume_dispatch_after_approval`), the in-process agent
waiter (`agent/approval_wait.py`) and the re-run after a failure
(`approval_rerun.py`). The checks live in
`operations/approval_resume_checks.py`.

**Target settings.** `create_pending_request` stores
`approval_request.target_hash` (migration `0130`): a SHA-256 over the
target's connection fields (`TARGET_HASH_FIELDS`: `product`, `host`,
`port`, `fqdn`, `secret_ref`, `secret_field_map`, `auth_model`,
`vpn_required`, `verify_tls`, `tls_ca_pin`, `tls_server_name`, `extras`).
It reads the stored row, not the in-memory target of the call, so a
per-call `fqdn` does not count (the agent waiter applies it again after
the check, see below). `rehydrate_resume_target` loads the target
by id as before and compares the hash. When it differs, the run is refused
with `error_code = "target_changed_since_approval"` and nothing is sent.
A target that no longer resolves (deleted or revoked after the request
was made) refuses the run with `error_code = "target_gone_since_approval"`.
The requester asks again. A rename, new aliases, notes, a new detected
version or a new fingerprint do not change the hash. A request with no
target, and a row saved before the column, has no hash and is not checked.

Both resume paths call the check through
`refuse_resume_if_target_changed`, which records a refusal the same way as
the release refusal (see "Before an approved run" above): the one-resumer
claim is taken, so the request never runs later; the stored run input is
deleted and the secret-named params are hidden; one `approval.resume`
audit row carries the error code as `result_status`; and the refusal is
stored as the request's run result, so REST, MCP and the console show
`result_state = "failed"` with that error code. The re-run after a failed
run calls `rehydrate_resume_target` directly and records its own refusal
(`approval.rerun`).

**Preview.** A request with a `preview_hash` (every `destructive` request)
builds the preview again with the target and params of the run
(`preview_changed_since_request`). When the hash differs, or the preview no
longer builds, the run is refused with
`error_code = "preview_changed_since_approval"`. The one-resumer claim is
already taken at that point, so the refusal is stored as the request's run
result and the request never runs later.

**Object.** The preview hash of a typed or multi-step op covers the call's
parameters, not the object they name. So, for the same requests (a
`preview_hash` is stored), MEHO also builds the approval card again right
before the run, against the live system, and compares the object the card
names (`proposed_effect.blast_radius.object`) with the live one
(`operations/approval_object_check.py`, `object_changed_since_request`):

- The card has an `id` or `key`: the live object must have the same value.
  Keycloak user, group and client deletes by name, Harbor robot deletes.
- The card has no id or key: the live object must have the same fields,
  without counts and yes/no flags. pfSense alias, gateway and static-route
  deletes, DNS record deletes, a Vault secret path, a Kubernetes Secret.
- The card lists the record values the call removes (children of kind
  `record_value`: the BIND9 and Windows DNS delete and remove cards): the
  live card must list the same values, in any order. `card_object` adds
  them, sorted, to the object as `record_values`. So a delete approved for
  value X is refused when the live value is Y. A BIND9 delete with a pinned
  `rdata` removes only that value, so its other values do not count.
- The card says the object is absent (`present: false`, an empty id, or a
  `match_count` of zero; the approver saw "nothing to delete"): the live
  card must say so too. An object that appeared under that name since the
  request refuses the run (`REASON_APPEARED`). The DNS cards (BIND9 and
  Windows DNS) name the record set, so their object stays the same with or
  without records; `match_count` says how many values the call removes,
  and `card_object` keeps a zero as `present: false` on the object. For a
  BIND9 delete with a pinned `rdata`, the count is the values equal to it,
  not the sibling values the children list.
- The live card says the object is gone: the run goes ahead and the
  handler answers "already absent" or "not found" itself.
- The card does not build now (the op does not resolve, the builder
  declines, or it fails while reading, for example a user is found but
  the read of their groups fails): the run is refused
  (`REASON_UNREADABLE`). MEHO cannot tell which object the handler would
  find, so an unreadable object is never treated as the approved one, and
  not as a gone one either. A builder that declines for an absent object
  (pfSense) refuses the run too; a new request is then refused at park
  time with `blast_radius_required`, which tells the requester the object
  is not there.

A changed, appeared or unreadable object refuses the run with
`error_code = "object_changed_since_approval"`; `extras.reason` says
which. The refusal is stored as the run result like the preview refusal.
A request without a card object (no blast radius) is not checked. The
re-run path reports the three checks as `target_changed`,
`preview_changed` and `object_changed`.

**State on the card.** Some approval cards show the object as it is now,
next to what the call changes (for example the roles a user has now). A
connector registers such an operation with `register_state_pin(op_id,
card_key)` (`operations/approval_state_pin.py`). The card holds the state
under `card_key` and says `<card_key>_available: true`. Right before every
approved run, with or without a preview hash,
`state_changed_since_request` builds the card again and compares the
state:

- the same state: the run goes ahead;
- another state: the run is refused (`REASON_STATE_CHANGED`: "The object
  changed since the approval; send the request again.");
- the approved card did not show the state (MEHO could not read it when the
  request waited, or the request is older than the card), or MEHO cannot
  read it now: the run is refused (`REASON_STATE_UNREADABLE`).

A connector can name the part of the state that the run compares with
`register_state_pin(..., compared=<function>)`. Keycloak's
`group.member.add` compares only the group id and its roles, not the
member list. A re-run after a first run that failed part way is refused
when the first run already changed the object.

The refusal has the `object_changed_since_approval` error code, in the
shared resume check and in the re-run (`object_changed`). The Keycloak
writes that use it are listed in
[connectors-keycloak.md](connectors-keycloak.md#writes-that-are-changes-of-rights).

## Making or changing a target always waits for a person

`targets.register` and `targets.update` both need approval
(`requires_approval`), and their `host` and `fqdn` carry the address
marker (`x-meho-caller-address: true`). So every call of them waits until
a person with `tenant_admin` approves it, whoever makes it: an agent (also
with an agent grant), a service login (also with a standing grant, or in a
run with a run approval) and a person with a token. Only a person in
the web console writes a target at once. A settings file that adds or
changes a target is applied only in the web console
([settings.md](settings.md)). The approval card shows the
values the person must see: for a new target every sent field, also each
`extras` key and value; for a change the old and the new value of each
sent field. Values under a secret-like name are hidden. Details:
[targets.md](targets.md).

## The params hash on requests and audit rows

Two kinds of rows carry a keyed hash of a call's params:

- **The approval request** (`approval_request.params_hash`) binds the exact
  call. It is HMAC-SHA256 with a server key over every value
  (`compute_params_hash` in `operations/_validate.py`). Before an approved
  call runs, MEHO checks the params against it (`params_hash_matches`), so a
  call that changed any value, a password too, does not match.
  The request also keeps the params themselves, because MEHO runs the
  approved call with them:
  - A credential write, and a `k8s.apply` whose manifest holds a Secret
    or a secret-looking value (the param a connector declares with
    `declare_hidden_stored_param`),
    keep the params, and the params of a parent multi-step call, only in
    the encrypted one-time handoff (`approval_handoff`). The row keeps no
    plain copy, also not when the request is approved and never runs.
    Without a valid `APPROVAL_HANDOFF_ENCRYPTION_KEY` MEHO refuses such a
    call instead of letting it wait.
  - For most operations they are stored in `approval_request.params`,
    in plain form only while the request waits. As soon as the run claim
    is won (`claim_resume`), or the request is rejected, withdrawn or
    expires (`end_run_input`, also an approved request that nobody
    started in time), each secret-named value in that column (a
    password or a licence key inside a deployment spec; the name rule of
    the broadcast scrub) is replaced by the marker `[REDACTED:param_name]`.
    The other fields keep their value. A row without such a field is not
    rewritten. Migration `0132` did the same, once, for every row that was
    already decided. A re-run of a failed run
    (`POST /api/v1/approvals/{id}/rerun`) then needs the params sent
    again, as for a credential write; the stored copy with markers never
    runs (`_stored_call` treats it as not stored).
    A marker the requester sent as a value is different (#6064). Example:
    an object read from MEHO and passed back unchanged, like the
    `expected_before` of a FortiGate interface update. Such params still
    match `params_hash`, because the hash covers the marker too, so the
    approved run uses them as sent (`_stored_params_can_run`). Stored
    params with a marker that do not match the hash are the ones MEHO
    hid; they never run.
  - For a `credential_write` operation they are not on the request row.
    They are encrypted in `approval_execution_payload` instead (see
    "Credential-write execution custody" below).
  - When the requester asked for the hidden values of the answer
    (`reveal_secret: true`), the choice is stored in its own column,
    `approval_request.reveal_secret` (migration `0131`). It is not a
    parameter of the operation: it is not in the params, not in the
    encrypted payload and not part of the hash. The run reads it from the
    row and honours it. Such a request runs only once, when the person who
    asked fetches the value, and never runs again (see "An approved secret
    reveal runs when the person who asked fetches it" below). A flag sent
    again with the params of a run comes off before the hash check and
    does not change the stored choice (see
    [redaction.md](redaction.md#secret-named-fields-in-every-answer-reveal_secret-for-people-and-service-principals)).
  - Every approval surface shows the choice, so the approver knows that
    approving lets the requester see the secret values once. REST
    (`ApprovalRequestView.reveal_secret`) and MCP (`meho_approvals_list` /
    `meho_approvals_get`) carry the field (`false` when not asked). The
    console request window names it in the detail list on every status
    (`data-approval-reveal-secret`) and in the warning above Approve of a
    pending request (`data-approval-gate-reveal-secret`). The in-order
    window marks the step (`data-approval-in-order-reveal-secret`). The
    CLI prints the line "Hidden values: ..." in `meho approvals show` and
    in the prompt of `approve` for several requests or in order
    (`cli/internal/cmd/approvals/reveal.go`). Tests:
    `tests/test_approval_surfaces_reveal_secret.py`,
    `cli/internal/cmd/approvals/reveal_test.go`.
  - A service principal's `reveal_secret: true` always waits for a
    person's approval, also on a call the policy gate would run at once
    (`dispatcher._reveal_waits_for_approval`). So the request reaches
    these surfaces before any value is returned.
- **Audit rows** (`payload.params_hash` on dispatch, MCP and hosted-agent
  tool rows, and `operation_run.params_hash`) store the hash, not the params.
  They use the same keyed hash, but each secret-named value (`password`,
  `client_secret`, `api_key` and similar, the names the broadcast scrub
  uses) is replaced by a fixed marker first (`compute_audit_params_hash`).
  Two calls with the same other values get the same hash. For a call without
  secret-named values, both hashes are equal.

The server key is HMAC-SHA256 of `PARAMS_HASH_KEY` under a fixed label.
`PARAMS_HASH_KEY` is a secret of its own. The same key also makes the audit
digest hashes and the value fingerprints (`compute_audit_digest_hash`,
`compute_value_fingerprint`). The key itself is never stored. What follows:

- **A new database password keeps every hash.** The key does not come from
  `DATABASE_URL`, so a password rotation changes no hash and no
  fingerprint, and a request that waits for approval stays valid.
- **Where the key comes from.** With a built-in core part on, the install
  makes it once in the Secret `<release>-core-params-hash` (key
  `params_hash_key`, 32 random characters) and keeps it on every upgrade
  ([core-secrets.md](core-secrets.md)). Your own Secret:
  `paramsHashKey.secretName` (and `paramsHashKey.secretKey`), with at least
  32 random characters.
- **Without the key.** When `PARAMS_HASH_KEY` is empty (no built-in part and
  no `paramsHashKey.secretName`), MEHO makes the key from `DATABASE_URL`
  under the same label. Then a new database password changes the key: new
  hashes do not match older ones, and a request that waits for approval at
  that moment must be asked for again. MEHO logs `params_hash_key_missing`
  at start, and the first-start destination check does not run
  ([first-start.md](first-start.md)).
- **Requests made before the key was set.** With `PARAMS_HASH_KEY` set, the
  approval check also accepts the hash under the key made from
  `DATABASE_URL`, for the request's own params only. So a request made
  before the key was set (for example by an older pod during a rolling
  update) can still be approved. This works while `DATABASE_URL` is the
  same as when the request was made. Audit rows written before the key was
  set keep their hashes. They do not match new hashes of the same params.
- **Rows with a plain SHA-256.** Rows written before the keyed hash hold a
  plain SHA-256: they stay readable, but they do not match new hashes. A
  request that was already waiting for approval then holds the plain hash,
  and the approval check accepts it for the request's own params. The
  column format (64 hex characters) is the same for all of them, so there
  is no migration.
- **Rotating `PARAMS_HASH_KEY`.** Change the key only when you must, for
  example when it was exposed. Steps: decide or withdraw every request that
  waits for approval; put a new value of 32 random characters into the
  Secret; restart the backplane pods. With the built-in core:

  ```bash
  kubectl -n <namespace> patch secret <release>-core-params-hash --type merge \
    -p "{\"stringData\":{\"params_hash_key\":\"$(openssl rand -hex 16)\"}}"
  kubectl -n <namespace> rollout restart deployment/<release>-meho
  ```

  After that, requests made under the old key must be asked for again, and
  audit hashes and fingerprints from before and after do not match.
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
APPROVAL_HANDOFF_ENCRYPTION_KEY before parking this operation`. In the Helm
chart, `approvalHandoff.enabled` is unset by default. Unset means on when a
built-in core part is on, and off when none is. `corePostgres.enabled` is
`true` by default, so an install with unchanged values has the key. An
install with every built-in core part off and no key of its own denies these
operations (since v0.35.2).

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

With a built-in part of the core on (`corePostgres.enabled`,
`coreKeycloak.enabled` or `coreVault.enabled`), the install makes this key
itself, in the Secret `<release>-core-approval-handoff` (key
`approval_handoff_encryption_key`). Skip all three steps: with
`approvalHandoff.secretName` empty, the chart uses that Secret. See
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

### The session of a CLI call, and the origin columns

Every request and audit row can say which session made it, also when
several sessions share one login. `meho login` makes a random session id
(a version-4 UUID) and saves it with the token. The CLI sends it in the
`Meho-Session-Id` header on every call. A token refresh keeps the id. The
next `meho login` makes a new one.

The audit middleware (`backend/src/meho_backplane/audit.py`) reads the
header and binds it to `client_session_id_var`
(`backend/src/meho_backplane/operations/_audit.py`). A header that is not
a UUID is ignored. `resolve_audit_session_id()` returns it last, after an
agent run and an MCP session. So the operation rows of a CLI call, the
HTTP row of the call and a parked approval request all carry it in
`agent_session_id`. Only the writers of these rows use it. The coordination
hints for agents (`resolve_agent_session_id()`) never see it, and the
console does not treat such a row as an agent row. The id is a label the
client sends. It tells sessions apart; it does not say who the caller is. A CLI that has no saved session id (a login from an
older CLI) sends no header; running `meho login` again fixes that.

The CLI sets the header with `api.SetSessionIDHeader` on every backplane
call: the generated client, and the hand-built requests too (`meho
targets import`, `meho event-source`, `meho broadcast watch`, `meho
status --watch`). The retry after a token refresh sends it again.

A parked approval request stores `session_from_client=true` when its
session id came from the CLI header (migration 0180). When the approved
operation runs, `resume_dispatch_after_approval()` binds that id to
`client_session_id_var`, not to `agent_session_id_var`. So the audit rows
of the run carry the requester's CLI session, and the run is not an agent
session: the coordination hints add nothing to its answer. The approver's
own CLI session never labels the requester's run. The approve, reject and
expiry rows of such a request also store `session_from_client: true`,
written by `_write_audit_row`.

`ApprovalRequestView` has `agent_session_id` next to `work_ref`.
`meho approvals list` shows two more columns: `WORK-REF` and `SESSION`
(the first 8 characters of the session id; `--json` has the whole id).
Both show `-` when empty. With them, a person can see which session
parked a request and for which change ticket.

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
fact from the `sub` alone. Migration `0097` adds two nullable
`approval_request` columns:

- `principal_name` — the requester's name, set in
  `create_pending_request`.
- `reviewed_by_name` — the approver's name, set in `approve_request` /
  `reject_request` from the deciding `Operator.name`.

This is the same hoist-at-write pattern `audit_log.principal_name` uses
(#1212). The approval audit rows (`_write_audit_row`) carry the same
`payload['principal_name']` so the name-aware audit views resolve the
row's `operator_sub` too (the payload's other pairs are under "Who the
payload names" in "Audit rows"), and the `/decide` response gains an additive
`decided_by_name` beside `decided_by`.

**Where the requester's name comes from (#3885).** An agent's token
(`client_credentials`) has no `name` claim. But at write time MEHO has
the token's client id (`Operator.client_id`), and a registered agent is
found by it. So one helper,
`meho_backplane.auth.display_name.principal_display_name`, picks the
name in this order:

1. The token's `name` claim. A person's name always wins.
2. For an agent token: the registered agent's name
   (`AgentPrincipal.name`), found by `keycloak_client_id ==
   Operator.client_id` in the caller's tenant. A revoked agent still
   counts.
3. For any machine token (agent, service, runner): the client id itself,
   for example `agent:<name>`.

No name in any step: nothing is stored, as before. Every place that
stores the caller's name calls this helper: the approval request and its
audit rows, the operation run and refusal rows (`write_audit_row`), the
MCP tool rows (`write_mcp_audit_row`) and the tool rows of MEHO's own
agent runs (`write_agent_tool_row`).

**Machine requesters are marked (#3885).** Migration `0149` adds
`approval_request.principal_kind` (nullable text): the requester's
`principal_kind` (`user` / `agent` / `service` / `runner`), saved at park
time. A composite step that waits again during a re-run keeps the kind of
the original requester (`ResumeRequester.kind`). REST returns it as
`ApprovalRequestView.principal_kind`. The console `subject(ref)` macro
shows a badge with the kind next to a machine requester, and the CLI adds
it in brackets: `show` prints `incident-triage (<sub>) [agent]`, and the
`list` PRINCIPAL column puts it first (`[agent] incident-triage`) so a cut
column still shows it. A person, or a request made before `0149`, gets no
marker.

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
  needed at render time. When the params name no subject, the resolver
  uses a `subject` string the preview builder stored under
  `proposed_effect.preview`, with the same credential-class and redaction
  rules. `k8s.apply` names its objects there, for example
  `Deployment/prod/web, Service/prod/web` (#3877).
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
classify a clean policy rejection as denied
rather than a fake server crash. The correction sits at the dispatch
boundary (`except McpInvalidParamsError` in both `handle_tools_call` and
`handle_resources_read`), so it covers every post-gate
`McpInvalidParamsError`, not just self-approval; explicit pre-gate
branches that already set `400`/`403`/`404` pass through untouched, and a
genuine handler fault still records `500`.

## A credential call always waits for a person

A call that reads, writes or makes a credential (the classes
`credential_read`, `credential_write` and `credential_mint`) always waits
for a person. No standing grant, no agent grant and no run approval covers
it. One rule says so: `service_grants.never_covered_reason`, with
`credential_grant_reason` for these classes. Grant create, run approval
create and agent grant create (an exact op id with `auto-execute`) refuse
such an operation with the reason "A standing grant, an agent grant or a
run approval never covers 'OP': it reads, writes or makes a credential, so
a person approves each call. Leave it out." The call of an agent or a
service login waits also at level `safe`. The approval of a parent
multi-step run does not cover such a step or child call: it waits for its
own approval (`_covered_by_approved_run`). This holds also when the person
who approved an earlier step has the rights to run it, and whatever level
the step declares (`composite.credential_step_waits_after_approval`). When
the person approves the step, the multi-step run goes on with this step
cleared; when the person rejects it, the run ends and the step never runs.
A child call (`dispatch_child`) that waits is its own request. When a
person approves it, only the child runs, and the multi-step run does not
go on. An install or tenant override does not turn
off the approval that the code turns on for an operation that writes or
makes a credential. Details:
[service-principal-grants.md](service-principal-grants.md#credential-calls),
[run-approvals.md](run-approvals.md),
[agent-permission-grants.md](agent-permission-grants.md).

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
   denied to the operator meant to run it.

   **Only the web interface counts as a person (#3980).** For a person,
   what waits depends on how the call arrives, not on whose token it is:
   - **Web interface** (`Operator.web_session` is `True`): `safe` and
     `caution` run at once; `dangerous`, `destructive` and
     `requires_approval` wait for an approval click.
   - **Any call made with a token** — MCP (for example an AI tool), the
     `meho` command line, REST, also a person's own token, and every
     in-process caller without a web session (runbook verify steps, the
     legacy connector route): only `safe` runs at once. Everything else
     waits. MEHO does not try to tell who is behind a token, because an
     AI tool can use a person's login.

   The REST target writes (`POST`, `PATCH`, `DELETE /api/v1/targets`)
   and the four REST topology graph writes follow the same rule. A
   person's token call goes to the matching `targets.*` or `topology.*`
   operation through the dispatcher and answers 202 with the approval
   request id (`api/v1/_wait_for_approval.py`); the console writes at
   once. See [targets.md](targets.md) and [topology.md](topology.md).
   `meho targets import` sends one write per row, so each row makes its
   own approval request. For a large file, a person imports it in the web
   console, or runs the import with `--work-ref`. Then a person goes
   through all its rows with
   `meho approvals approve --filter-work-ref <ref> --in-order`, which asks
   for a yes at each row. The person who ran the import can approve its
   rows this way only after a fresh approval sign-in.

   Only `verify_access_token_with_refresh`
   (`backend/src/meho_backplane/ui/auth/refresh.py`) sets
   `web_session=True`. No token claim, header or request field can set
   it. The rule lives in `_person_park_reason` (`_validate.py`). Service
   principals keep their standing-grant rule (`_service_safety_gate_reason`).

   **Own approval of a token `caution` change.** When a person's `caution`
   change waits only because it came with a token, the park stamps
   `proposed_effect["waits_because"] = "token_call"`
   (`waits_only_for_token_call`, stamped in `_build_proposed_effect`).
   The same person may then approve it with a click in the web interface
   (`approval_rule` `token_call_self_approval`). The same approval on the
   command line or REST is refused with `SelfApprovalForbiddenError`,
   which points to the web interface. A change marked `requires_approval`
   never carries the reason and keeps the group rules below.
2. **Self-approval guard (requester != approver).** `approve_request`
   raises `SelfApprovalForbiddenError` (→ HTTP 403 `self_approval_forbidden`
   on REST; the console shows the plain text) when `operator.sub ==
   request.principal_sub` and none of the self-approval rules below
   allows it. The person is not a MEHO-Admin, the change did not
   wait only because it came with a token, and the audited **emergency
   break-glass** switch `APPROVAL_ALLOW_SELF_APPROVAL=true`
   (`Settings.approval_allow_self_approval`, default `False` =
   fail-closed) does not cover it. There is no MCP form of this error:
   approve has no MCP path. The REST `detail` keeps the
   `self_approval_forbidden` token as a prefix and appends the
   exception message. It says only who may approve instead and where to
   ask (the Approvals page of the web interface). The groups come from
   `_approver_kinds`: today's group rule (`policy.routing.today_kinds`),
   and with approver routing on, only the kinds the request stored as
   well. The text names no setting and no other way to approve the
   change. Even under break-glass the self-approval writes
   its decision audit row, so the use is forensically visible. Reject
   has no self-guard. To take back your own pending request, do not
   reject it: use withdraw (see
   [The sender withdraws its own request](#the-sender-withdraws-its-own-request-4987)).
   The guard runs after the role check and before
   the params-hash check, so a self-approver gets the precise refusal
   reason.

   **Who may approve, by group (#3328).** `_check_approver_group`
   (`approval_queue.py`) decides, after the role check. The rules come
   from [the decision record](../decisions/approval-groups-and-self-approval.md):
   - A MEHO-Admin (`Operator.may_self_approve`)
     may approve their own change at every risk level. Everyone else gets
     `SelfApprovalForbiddenError` for their own change, unless break-glass
     covers it (`safe` and `caution` only).
   - Another person's `dangerous` or `destructive` change may be approved
     only by a tenant admin. Everyone else gets
     `ApprovalGroupForbiddenError` (a subclass of
     `UnauthorizedApprovalError`; REST answers 403
     `approval_group_required: <text>`).
   - Another person's lower-risk change keeps the reviewer rule
     (operator-or-higher, or the `approver` flag).
   - A request without a stamped `safety_level`, or with a value that is
     not one of the four level names, counts as `destructive`
     (`approval_risk_level`): only a person who may approve another
     person's `destructive` change may approve it, and break-glass never
     covers it. Its approval sign-in is single-use, as for any
     `destructive` request (`_check_sign_in_use`).

   **When the preview fails.** At park time the dispatcher builds the
   approval card with `_build_proposed_effect`. When that build raises,
   `_build_park_effect` stores `_preview_failed_effect` instead: the op
   identity, the catalog `op_class` and `safety_level`,
   `preview_populated: false` and `preview_failed: true`. So the request
   keeps the risk level of the operation, and the approval rules above
   read it. The approval card says "MEHO could not build the preview of
   this change." A `destructive` operation still refuses to park without
   its preview binding and blast radius. The multi-step park
   (`composite.py`) uses the same helper.

   **How to audit a self-approval.** The `approval.decision` audit row
   carries `reviewed_by` (the approver's `sub`), `self_approved` (true
   when requester and approver are the same `sub`) and `approval_rule`:
   `sa_operator_self_approval`, `break_glass_self_approval`,
   `sa_operator_or_admin`, `reviewer` or `token_call_self_approval`
   (#3980). The `approval.approved` add-on
   step event carries the same three fields.
   A break-glass self-approval also logs the WARNING event
   `approval_self_approval_break_glass` (with `approval_request_id`,
   `op_id`, `operator_sub`, `tenant_id`). Whether the deploy currently
   allows break-glass at all is visible without grepping the values
   file: `GET /ready` → `features.approval_queue.effective_posture`
   is `"single_operator_break_glass"` when
   `APPROVAL_ALLOW_SELF_APPROVAL=true`, `"four_eyes_enforced"`
   otherwise (see `features.py::_approval_queue_block`).

   `APPROVAL_ALLOW_SELF_APPROVAL` is an **emergency** escape, not the
   endorsed single-operator posture: enabling it posture-wide re-opens,
   for every `safe` and `caution` op, the single-account request+grant
   hole this guard closes. On a single-operator tenant, make the
   operator an Admin (MEHO-Admins): they approve their own change on
   purpose, with a click in the web interface. See
   [Single-operator tenants](#single-operator-tenants-make-the-person-an-admin)
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

## The approval groups and how a person approves (#3325)

The rules come from the decision record
[approval-groups-and-self-approval.md](../decisions/approval-groups-and-self-approval.md).
Initiative [#3325](https://github.com/evoila/meho/issues/3325) has the
full table of who may approve what. The code is `_check_approver_group`
and `_check_self_approval` in `operations/approval_queue.py`.

**Who may approve.** The person's login group decides. MEHO reads the
group only from the signed login token (`Operator.may_self_approve`,
read by `auth/jwt.py` from the `meho_tenants` entry of the current
tenant, or from the `groups` claim on a site with one tenant). No
request field, header or command-line flag can set it.

| Group | Their own change | Another person's `safe` or `caution` change (also one marked `requires_approval`) | Another person's `dangerous` or `destructive` change |
|---|---|---|---|
| MEHO-Admins | yes, at every risk level, on purpose | yes | yes |
| MEHO-Operators (also MEHO-SA-Operators, read as Operators) | only a `caution` change that waited because it came with a token, with a click in the web interface | yes | no |
| MEHO-Readers (also MEHO-Approvers, read as Readers) | (they cannot run changes) | no | no |

MEHO reads the old group MEHO-SA-Operators as Operators and MEHO-Approvers as
Readers. The default groups set no `approver` flag and no self-approval flag
(`_GROUP_GRANTS` in `auth/operator.py`). MEHO keeps the `approver` flag in the
code. No role gives it today; roles that customers define come later. The old
names stay known until they are removed.

- Agents, service principals and runners never approve, on any path
  (`NonHumanApprovalError`, checked first).
- The emergency switch `APPROVAL_ALLOW_SELF_APPROVAL` adds only one
  thing: anyone may approve their own `safe` or `caution` change. It
  never reaches `dangerous` or `destructive`.
- There are no exceptions per system, per connector or for a limited
  time.
- An op with a role floor (for example `targets.register` and the
  `topology.*` writes) also needs an approver with that role, whatever
  the group (`_check_approver_role_floor`).
- The groups decide *which people* may approve. They never open an MCP
  path: approve and reject stay human-only (`mcp/human_only.py`).

**How a person approves.** The web interface is the normal place.

- **Web interface:** a click on **Approve**. When the approval level is
  required (see below), the console first sends the person to its own
  approval sign-in (the client `meho-web-approve`, #6282). Otherwise the
  sign-in behind the console session must be recent
  (`FRESH_SIGN_IN_MAX_AGE_SECONDS`, default 15 minutes) and must have
  asked for the password or a passkey. A `destructive` change needs a
  sign-in of its own, at most `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS` old
  (default 5 minutes). When the sign-in cannot approve, the console
  offers **Sign in to approve**.
- **Command line:** `meho approvals approve <id>` opens a browser
  sign-in made only for approvals (the client `meho-cli-approve`, #3979,
  #5515). It asks for the password, and for the code from the person's
  authenticator app when the realm asks for one. The same age limits
  apply. One sign-in may approve several requests inside the window; a
  `destructive` change needs a sign-in of its own. The stored `meho
  login` token, an MCP token and an API token never approve.
- **REST:** the same rule as the command line: the token must come from
  the approval sign-in. Details:
  [Approve needs a fresh approval sign-in](#rest-backendsrcmeho_backplaneapiv1approvalspy).
- **Reject** needs no fresh sign-in, on every surface.

**The approval sign-in flow is checked (#6280).** The client
`meho-cli-approve` must sign in through its own Keycloak login flow
`meho approval sign-in level`: one conditional sub-flow with the level
step (approval level 2, maximum age 0), the password form, then the
one-time code step when the person has a code app. No other step. The built-in realm has it
from the realm file; the bootstrap Job makes it and binds the client on
every upgrade, and stops when `meho-admin` lacks `manage-realm`
(docs/codebase/core-bootstrap.md). On a customer's own Keycloak an admin
sets it up by hand or with `keycloak.authflow.ensure_approval_flow`.
`auth/approval_sign_in_flow.py:read_approval_sign_in` reads the realm with
the backplane's Keycloak admin client and puts the state into
`/api/v1/health` (and `meho_status`) as `approvals.sign_in`:

| `state` | Meaning |
|---|---|
| `ok` | The flow has exactly the steps of the realm file, and the client's browser flow is this flow. |
| `flow_missing` | The realm has no flow with that name. |
| `flow_differs` | The flow has other steps; `detail` names the first difference. The realm stage and the operation never bind the client to it. |
| `not_bound` | The flow is right, but the client is missing, has no own browser flow, or uses another flow (`detail`). |
| `not_checked` | The check could not run; `detail` is a reason word. A setup MEHO knows: `keycloak_admin_not_configured`, `no_right_to_read_login_flows`, `no_right_to_read_realm`, `no_right_to_read_clients`. A failed read (`read_failed`): `keycloak_timeout` (5 seconds for the whole check), `keycloak_unexpected_answer`, `keycloak_unreachable` or `keycloak_check_error` (any other error). |

A failed read is never `ok`. The answer is kept for 60 seconds per
admin URL and client (`_KEEP_S`), because `/api/v1/health` and
`meho_status` are called often; a fix in Keycloak shows up within a
minute. A failed read is kept only 5 seconds (`_KEEP_FAILED_S`), so one
short error does not hold approvals back for a minute. The expected steps are the ones of
`keycloak.authflow.ensure_approval_flow` (`approval_flow_difference` in
`connectors/keycloak/ops_authflow.py`), so the check, the operation and the
realm file agree. `meho doctor` turns the state into its line `approval
sign-in` (`cli/internal/cmd/doctor_approval.go`): `ok` passes, every other
state is a warning with the fix. Under `auto`, the approve path reads this
check to decide whether it requires the approval level (see below).

**The approval level (#6281).** With no level names in the realm,
Keycloak gives `acr` `"1"` to every sign-in that asked for credentials,
whatever flow ran. So the realm names its levels in the realm attribute
`acr.loa.map`, one table shared with the passkey touch:

| Level | `acr` | Given by |
|---|---|---|
| 1 | `"1"` (no name) | any normal sign-in |
| 2 | `meho-approve` | the approval sign-in flow (its level step: level 2, maximum age 0) |
| 3 | `meho-touch` | the passkey touch flow |

A higher level counts where a lower one is asked for
(`APPROVAL_LEVEL_ACRS` in `auth/approval_sign_in.py`). `meho approvals
approve` sends `acr_values=meho-approve` (`cli/internal/auth/freshsignin.go`).
Keycloak's level condition is true on the first pass of a sign-in, so the
flow reaches level 2 also without the request. A sign-in through a
company login does not run the flow's steps: Keycloak gives it no level,
so `acr` stays `"1"`.

`APPROVAL_SIGN_IN_LEVEL` (`off` | `observe` | `auto` | `required`,
default `auto`) decides what `check_fresh_approval_sign_in` does with a
token of the approval client whose `acr` is not one of those names
(`has_approval_level`). The routes resolve the setting first with
`sign_in_level_in_force` (`auth/approval_sign_in_flow.py`). `auto` is
`observe` only when MEHO knows for sure that the realm does not give the
level: the read worked and the state is `flow_missing`, `flow_differs` or
`not_bound`, or the state is `not_checked` for a setup MEHO knows (no
admin client, no right to read). In every other case `auto` is
`required`: when the state is `ok`, and also when the read failed. A
failure means "refuse", never "allow". A caller that passes no resolved
value gets an unresolved `auto` as `required` (fail closed).

- `off`: accepted as before;
- `observe`: accepted, the warning `approval_sign_in_level_missing` is
  logged, and `approval_auth.level_missing` is `true` in the
  `approval.decision` audit row (next to the `acr` it saw);
- `required`: the same `401` as an old sign-in. When the level is the
  one missing part, the detail ends with `LEVEL_MISSING_REMEDIATION`
  ("Your sign-in did not reach the approval level. An admin must check
  approvals.sign_in in /api/v1/health."). When `auto` requires the
  level only because the read failed, the detail ends with
  `LEVEL_UNCHECKED_REMEDIATION` instead ("MEHO could not check the
  approval sign-in of the realm. Try again in a moment."). The refusal
  log `approval_sign_in_not_fresh` carries `approval_level`.

The `approval.decision` audit row records the mode in force for the
approve in `approval_auth.level_in_force` (`off`, `observe` or
`required`), next to the `acr` it saw.

**The console approval asks for the same level (#6282).** The console's
normal login (`meho-web`) is bound to the passkey touch flow and never
gives `meho-approve`, also right after a new login. So the console has
its own approval client, `meho-web-approve`
(`UI_KEYCLOAK_APPROVE_CLIENT_ID`): a confidential client with the secret
of `meho-web`, bound to the same flow `meho approval sign-in level`, used
only for the sign-in before an approve. Its redirect URI is
`<public URL>/ui/auth/approval-callback`.

- `GET /ui/auth/approval-sign-in?return_to=…`
  (`ui/auth/approval_sign_in_routes.py`) needs a console session. It
  sends the browser to Keycloak with this client, PKCE, `prompt=login`,
  `max_age=0` and `acr_values=meho-approve`. The pending flow keeps the
  session id, because the browser leaves out the `SameSite=Strict`
  session cookie when a login server on another site sends it back.
- `GET /ui/auth/approval-callback` checks `state` and the login-binding
  cookie like the normal callback, exchanges the code with the approval
  client, checks the token through the JWT chain, and keeps it
  (Fernet-encrypted, `web_session.approval_token`, migration 0176) only
  when `azp` is the approval client and `sub` is the session's person.
  Else `403 approval_sign_in_other_person`. The normal login is
  unchanged.
- The approve (`ui/routes/approvals/sign_in.py`,
  `auth/web_approval_sign_in.py:check_web_approval_sign_in`) resolves the
  setting with `sign_in_level_in_force(…, client_id=meho-web-approve)`,
  so `auto` reads the binding of the console's approval client. When
  `UI_KEYCLOAK_APPROVE_CLIENT_ID` is empty, `auto` only records (the
  console has no approval sign-in to offer); an explicit `required`
  still refuses.
  `required`: only the kept token approves, and only with the approval
  level, a recent `auth_time`, `acr` not `"0"` and the same person; the
  modal shows **Sign in to approve** with the approval sign-in link in
  place of the button. `observe` and `off`: the console approves as
  before on the session's own sign-in (or on a kept token that passes
  the other checks); `observe` logs `approval_sign_in_level_missing` and
  sets `approval_auth.level_missing`.
- The `approval.decision` row records `channel` (`web` or `rest`),
  `acr` and `level_in_force` on both paths.

Known limit: once the console needs the level, a person who signs in
only through a company login (no MEHO password) cannot approve in the
console and not on the command line, until they have a MEHO password and
a one-time code. On built-in Keycloak with the default `auto` this starts
with the upgrade: it adds `meho-web-approve` and binds it. A company
login started from the approval page does not run the flow's steps, so
its token has no level and MEHO refuses it
(`test_core_company_login_keycloak.py` proves it). What the operator can
do: give these approvers a MEHO password and a one-time code, or set
`APPROVAL_SIGN_IN_LEVEL=observe` for now. The approval page still shows
the company login's button. Keycloak can set a login theme for one
client, and such a theme could hide the button; MEHO does not ship one
yet.

`/api/v1/health` shows the value in `approvals.sign_in_level`,
what is in force in `approvals.sign_in_level_in_force`, and
`approvals.sign_in_level_warning` whenever the level is not required in
practice, and its own warning when the read failed ("MEHO could not
check the approval sign-in of the realm ..."). The console's approval
client has its own state and level in `approvals.web_sign_in` and
`approvals.web_sign_in_level_in_force`
(`approval_sign_in_flow.read_web_approval_sign_in`; an empty
`UI_KEYCLOAK_APPROVE_CLIENT_ID` is `not_bound`). Under `auto` the two
clients can differ, so the warning also says when the console does not
require the level ("The console does not require the approval level
...") or MEHO could not read the console's client ("MEHO could not
check the console approval client ..."); the sentences of both clients
are joined. A console refusal for a missing level names
`approvals.web_sign_in` ("An admin must check approvals.web_sign_in in
/api/v1/health."). When MEHO has no admin
client, or the admin client may not read, the warning names that fix:
the admin client settings, or the realm-management role `view-realm`
(`view-clients` for `no_right_to_read_clients`). `meho doctor` shows the last two in its line `approval level`.
`approvals.sign_in` is `ok` only when the realm really gives the level:
the flow steps, the level step settings (`loa-condition-level` 2,
`loa-max-age` 0), `acr.loa.map` with `meho-approve` = 2 and no other name
on level 2, no own `acr.loa.map` on the client, and the binding. Direct
grant or device grant on the client is `flow_differs` too. The
realm stage (`core/keycloak_flows.py:ensure_levels`, and step settings in
`ensure_flows`), `meho admin keycloak bootstrap-clients` and
`keycloak.authflow.ensure_approval_flow` all add `meho-approve` to
`acr.loa.map` and keep the names already there. All three stop before
they write when another name already has level 2, or when the approval
client has its own `acr.loa.map` (shared rules in
`core/keycloak_levels.py`; the Go copy is in `flows.go`). The realm stage
makes only the login flows that the realm file binds to a client other
than `meho-web` (`stage_flows`), so it never makes or binds a flow of the
console client, such as the passkey touch flow. The flow got a new name
with the level, so an upgraded realm gets the new flow and the client is
bound to it; the earlier flow `meho approval sign-in` stays unused.

Every approval records who approved (`reviewed_by`), whether it was a
self-approval (`self_approved`), which rule allowed it (`approval_rule`)
and how the person proved they were present (`approval_auth`: channel
`web` or `rest`, method `fresh_sign_in` or `one_time_code`).

With `AUDIT_CLIENT_IDENTITY` on (off by default), approvals also name the
login client (see the `client` block in [audit_query.md](audit_query.md)):

- The request keeps the requester's `client` block in its stored effect
  (`proposed_effect["client"]`). No new column. A request made again for
  an earlier requester (a step of a multi-step operation that waits again)
  keeps that requester's block: `requester_of` reads it from the approved
  request, and `ResumeRequester.client` carries it. The reviewer who runs
  the step is never named there.
- The `approval.decision` row of an approve adds `approver_azp` and
  `approver_channel` (`web` or `token`) next to `approval_auth`.
- For an approve over REST (`/approve` and `/decide`, so also `meho
  approvals approve`), the row also keeps the `User-Agent` header, cut to
  256 characters: `"approver_user_agent": {"value": "...",
  "self_declared": true}`. The tool sends this text about itself, and MEHO
  cannot check it.

## Changes of rights: who the change is for

`operations/grant_approval.py` checks the Keycloak writes that change who
has rights: `keycloak.group.member.add`, `keycloak.role_mapping.assign`,
`keycloak.role_mapping.client.assign` and `keycloak.group.update_attributes`.
It also checks two writes that change how a login signs in:
`keycloak.user.update` (a new email or user name) and
`keycloak.user.credential.delete` (a passkey or one-time code removed).
The connector registers them
(`connectors/keycloak/ops_write_grant.py`) and adds a `grant` block to the
preview of the waiting request:

- `user_id`: the login the change is for. A user name is resolved to its
  id when the request is parked.
- `group_id`: the group the change is for, resolved the way the handler
  resolves it. An id in the params wins; without one, the name is looked
  up on its level (`parent_id`). So a request cannot name one group and
  point at another by id.
- `login_realm`: the target manages a realm of MEHO's own login, see
  `_backplane_login_realms()`.
- `platform_admin`: the group the handler will change is the platform
  admin group, the roles include the platform admin role, or the change
  gives a role of the `realm-management` client (in any case). For a role
  change in the login realm, the parts of the role count too (see "A role
  is judged by what it really gives" below).
- `members`: for a group attribute change in the login realm, the ids of
  the logins in the group (up to 1000, read page by page). `None` when
  they could not be read, when Keycloak answers with something that is
  not a list (an error object, `null`), or when the group has more. The
  card shows it; no approval rule reads it.

A value that could not be read is `None`, never a guess.

The group rule (`_check_approver_group`, used by `approve_request` and
`approve_right`) calls `check_rights_change_approval`, then
`check_grant_approval`. The second refuses with `ApprovalRoleFloorError`
(403):

- when the login or the group the change is for is unknown (the lookup
  failed when the request was parked). Nobody can approve such a request;
  the requester sends a new one;
- in the login realm, when the approver is not a platform admin, for
  every change of rights there; the text says why when the change can
  give platform admin rights (a group or role that gives them; see "How
  MEHO decides that a group change gives platform admin rights" and
  "Every Keycloak write in the login realm needs a platform admin" in
  [connectors-keycloak.md](connectors-keycloak.md));
- in the login realm, when the approver is the login that asked, unless
  it asked in the web console (`asked_in_web_console` in
  `proposed_effect`, set by `create_pending_request` on every change of
  rights it parks, `ASKED_IN_WEB_CONSOLE`) and approves in the web
  console too;
- when the change is a `secret.move` that sets the password of the
  approver's own login (`FOR_LOGIN`).

`register_grant_op` takes `applies`, a check of the params: only a request
that passes it counts (`secret.move` with a Keycloak side). Such an op does
not count in `is_rights_change_op`. With `per_call=False` an op is checked
here too, but it gets no per-call mark and does not count in
`is_rights_change_op`: it is a change of rights only in the login realm
(the Keycloak writes that only take away). Such an op can register a check
by its params (`register_grant_cover_check`, `operations/grant_cover_check.py`):
when the check says so, the policy gate looks up no standing grant and no
run approval for the call, and the shadow policy evaluator does the same.
A card can name the login of a change whose params name none: the `grant`
fact `for_login` (`FOR_LOGIN`, set by a `secret.move` that sets a password
in the login realm). Then that login never approves it, and an unknown
login cannot be approved. `register_harmless_keys_rule`
records an op whose card carries `harmless_keys_only`
(`HARMLESS_KEYS_ONLY`); `grant_changed_since_request` refuses the run of
such a request when the approved card does not have it
(`GRANT_REASON_OLD_CARD`), in every realm. The Keycloak rule that uses it
is in "Every Keycloak write in the login realm needs a platform admin" in
[connectors-keycloak.md](connectors-keycloak.md). `platform_admin_reason(request)` gives
the reason a platform admin must approve (from `platform_admin_reason` in
the `grant` block), or `None`; the refusal text and the console card show
it.

An admin may approve a change of their own login and a change of a group
they are in, except a `secret.move` that sets the password of their own
login (`FOR_LOGIN`). In the login realm the approver is the login that
asked (`principal_sub`) only when that login asked in the web console; a
request asked with a token, the command line, MCP or by an agent waits
for a second platform admin. A person behind the agent, service or
runner login that asked may approve it. A request without the `grant`
block, or with a group MEHO could not read, gets the strict rule. The decision audit row carries `grant_user_id`,
`grant_group_id` (when the change names a group), `grant_for_approver`,
`grant_in_login_realm` and, when the request named one, `grant_username`.
MEHO does not link two logins of one person.

**The run is bound to the ids the approver saw.** The handlers resolve a
user name or a group name again when the change runs. A name can move to
another account or group while the request waits. So right before every
approved run, `grant_changed_since_request` builds the card again against
the live system and compares `user_id` and `group_id` with the stored
ones. A changed id, or one that cannot be read, refuses the run with the
`object_changed_since_approval` error code. The check runs for every
change of rights, also without a preview hash, in the shared resume check
`refuse_resume_if_preview_changed` (the approve surfaces and the agent's
own resume) and in the re-run after a failure (`approval_rerun`).

**The run is also bound to the facts that decided who may approve.** The
same check refuses the run:

- when the change now needs a stricter approval than the one it got: the
  live card says login realm or platform admin where the saved one said
  no, or cannot say which it is;
- for an operation registered with `bound_ids` (a card key and a refusal),
  when the live card lists other ids under that key than the saved card.
  `keycloak.user.credential.delete` uses it for `will_remove`, so the run
  removes only the credentials the approver saw. This check runs before the
  handler reads the system again, so the handler also compares its own
  read with the approved card (`approved_preview`) before it deletes.

### A role is judged by what it really gives

A role can be made of other roles, and those can be made of more roles.
For `keycloak.role_mapping.assign` and `keycloak.role_mapping.client.assign`
in the login realm, the connector reads the parts of every named role, on
every level (`ops_write_effective_roles.py`), and puts them on the card as
`effective_roles`. Each entry has `id`, `name`, `client_id` (`None` for a
realm role), `realm_management` and `login_role`. `realm_management` marks
a role of the `realm-management` client. `login_role` marks a realm role
that MEHO's login turns into platform admin rights. As at login, that is
only the realm role whose name is exactly `JWT_PLATFORM_ADMIN_ROLE_NAME`
(same case). When the setting is unset, no role has this mark.

If any part has one of these marks, `platform_admin` is true, so a platform
admin must approve. If no part has a mark, `platform_admin` is false. The
name of the role that was asked for does not decide. If MEHO cannot read all the parts (a read fails, a role is
missing, more than 500 roles, nesting deeper than 10 levels),
`effective_roles` is `None` and `platform_admin` is the strict case.
Outside the login realm the list is empty and nothing changes.

The run is bound to the list. Both ops are registered with `bound_ids` on
`effective_roles`: if the live list has other role ids than the approved
one, the run is refused and a new request is needed. A request parked
before this rule has no list, so it is refused at the run too, in every
realm. Both ops are also registered with `bound_ids_missing`, so this
refusal says plainly: "This request was made before MEHO read what a role
gives. Send it again."

## The rights-change mark: an admin approves it in the web console

A waiting request can carry one mark: "this is a change of rights"
(`ApprovalRequest.rights_change`). Only an admin approves it, and only
with a click in the web console (`Operator.web_session`). The admin may be
the person who asked: the decision row then has `self_approved: true` and
the rule `sa_operator_self_approval`. No other group approves it, at any
risk level. The break-glass switch `APPROVAL_ALLOW_SELF_APPROVAL` does not
clear the mark.

**One check.** `Operator.may_approve_rights_change()` decides who is an
admin: a person in a chosen tenant with the `tenant_admin` role or
platform admin rights. It is used by `check_rights_change_approval` in
`operations/grant_approval.py`, which the group rule
(`_check_approver_group`) runs first. So the approve and the "may I
approve?" sign (`approve_right`) give the same answer. The refusal is one
sentence:

- not an admin: "This is a change of rights. Only an admin may approve it."
- an admin on the command line or over REST (no web session): "This is a
  change of rights. Approve this change of rights in the web console."
  This holds also for another admin's request.

The REST detail is `rights_change_required: <that sentence>`, and
`may_approve_reason` is `rights_change`.

Run approvals (`RunApprovalService.approve`): a list with a rights-change
operation is refused for everyone (`rights_change_required`): a run
approval list never covers a change of rights. A list knows the operation
but not the realm, so every registered group or role operation counts.

**An admin's own call in the web console runs at once.**
`operations/rights_change_console.py` decides it in the policy gate
(`_non_agent_verdict` in `operations/_validate.py`). The call runs at once
when all of this holds:

- the operation is registered with `register_rights_change_op` or
  `register_grant_op` (`is_rights_change_op`);
- it is not `destructive`, it has no "always wait" mark
  (`policy_always_wait`, from an override or a classification rule), and
  it does not read, write or make a credential (`credential_call_waits`).
  Its params carry no credential either (`params_write_credential`, with
  the check a connector registers with `register_credential_params_check`;
  the Keycloak writes register `carries_credential`, which looks for keys
  such as a client's `secret`, a user's `credentials` or a `password`).
  Those always wait. A change of
  rights only by its params (`register_rights_change_check`, for example a
  `secret.move` with a `keycloak:` side) always waits too;
- the caller is a person with a web session and an admin
  (`may_approve_rights_change`). A token call (command line, REST, MCP),
  also the admin's own token, an agent, a service login, a hosted agent
  run (the stricter of the caller and the agent) and an Operator wait.

Before it runs, `console_check` runs the operation's check before a wait
(`park_check_refusal`); a refusal there is a `deny`. Then the call waits,
as every other change of rights does, when:

- the console session lacks the sign-in a console approve needs
  (`has_web_approval_sign_in` in `ui/routes/approvals/sign_in.py`, the
  same rule as `web_approval_auth`: a fresh sign-in, and the approval
  sign-in level where `APPROVAL_SIGN_IN_LEVEL` asks for it). The session
  id is `Operator.web_session_id`;
- the approval card, built against the live system (`live_card`), does
  not build, or does not say who the change gives rights to: a
  `register_grant_op` operation needs the `grant` block. Outside the login
  realm the block must name the one login or group of the change; in the
  login realm the block's realm facts are enough. Only an operation
  registered with `register_no_login_realm_op` (`policy.apply`,
  `policy.classify`) needs no such block.

Then `may_make_alone` runs the check of who may make the change on that
card, the same rule as `check_grant_approval` on approve. When the admin
may not make it alone (in the login realm: an admin who is not a platform
admin), the call waits, and a platform admin approves it. In the login
realm the guard of `connectors/keycloak/ops_meho_own_login_guard.py`
refuses many of these writes first, before any wait (a delete or most
changes of a client MEHO signs in with, a new mapper on it, most realm
keys), and no approval lets them run. A change that can change the
approval sign-in and that the guard lets through never runs alone and
is never approved by the login that asked or by a person behind it
(`people_behind`): a connector registers a check
of such a request with `register_approval_sign_in_check`
(`operations/approval_sign_in_writes.py`; the Keycloak checks are in
`connectors/keycloak/approval_sign_in_writes.py`). For Keycloak these
are: a `client.update` of a sign-in client, or one that sets the
`clientId` of one on another client; a `client.create` with the
`clientId` of a sign-in client; a `client.delete` of a sign-in client; a
`protocol_mapper.create` on a sign-in client or of a sign-in claim; a
scope mapper update of a sign-in claim; and a `realm.update` of other keys
than names, mail, events and languages. The sign-in clients are the CLI
and web approval clients, the console login client and the client of the
Keycloak admin API (`KEYCLOAK_ADMIN_CLIENT_ID`), compared without letter
case. The key
`asked_in_web_console` of `proposed_effect` is internal
(`INTERNAL_EFFECT_KEYS` in `operations/_preview.py`): no approval response
returns it. The dispatcher
marks the audit row of a call that runs at once with `rights_change_run: admin_in_web_console`
(an `audit_*` context key), and sets its `policy_rule` column to
`default:rights_change.admin_in_web_console` (`CONSOLE_RUN_POLICY_RULE`),
so the main columns show that no second person approved. The policy evaluator has the same rule
(`admin_console_rights_change`, mark `rights_change`, lookups
`rights_change_refusal` and `rights_change_waits`).

`policy.apply` and `policy.classify` check again in the handler: a run
without an approval, or a run by the person who asked (also a re-run of a
failed approved request), writes only for an admin in the web console.

**Who sets the mark.**

- The park step (`create_pending_request`) sets it for a group or role
  change in MEHO's own login realm (`grant_facts`, see above). Outside
  the login realm, such a change carries the mark when its operation is
  also registered with `register_rights_change_op` (every Keycloak one
  is, see the next point).
- An operation registered with `register_rights_change_op` is a rights
  change on every request. `policy.classify` (a change of a category,
  labels or the "always wait" mark, see
  [policy-classification.md](policy-classification.md)) is one. The
  Keycloak connector registers every write that is a change of rights this
  way, also the group, role and login writes above (`GRANT_OPS` in
  `connectors/keycloak/ops_write_grant.py`), so they carry the mark in
  every realm. The login-realm checks above come on top.
- Any other caller passes `rights_change=True` to `create_pending_request`.

**No grant and no run approval covers a registered change of rights.**
`register_grant_op` and `register_rights_change_op` also mark the
operation with `declare_per_call_approval`. So making a standing grant for
it is refused, a stored grant row does not clear a call, and a run approval
list that names it is refused. This is one rule in the core: a connector
that registers a change of rights needs no second mark. The Keycloak
writes that are changes of rights are listed in
[connectors-keycloak.md](connectors-keycloak.md), "Writes that are
changes of rights".

A request parked before the column existed has no stored mark. It counts
through the same `is_rights_change` check, so an upgrade weakens nothing.

**Where the person sees it.** `ApprovalRequestView.rights_change` on REST.
`meho approvals show` prints `Rights change: approve this change of rights in the web console`,
and the request window in the web interface shows the same sign
(`data-approval-rights-change`).

**A tenant with one admin (#6641).** There, nobody can approve that
admin's change of rights. MEHO says so; it builds no exception.

- `auth/tenant_admins.py` counts the admins of a tenant with MEHO's own
  Keycloak admin client: the enabled members of every group (at any level)
  whose attributes give `tenant_admin` in the tenant (`tenant_role` with
  `tenant_id`, or `meho_tenant` `<tenant>:<name>:Admins`) or
  `platform_admin`. A child group takes a missing attribute from its
  parent, as the token does. A service account (a username that starts
  with `JWT_SERVICE_ACCOUNT_USERNAME_PREFIX`) is not counted: only a
  person may approve. The answer is kept 60 seconds per tenant.
- The count is "not known" (`None`) when the admin client is not set up,
  a read fails or is refused, platform admins come from a realm role
  (`JWT_PLATFORM_ADMIN_ROLE_NAME`), or no enabled admin is found. "Not
  known" never gives a warning.
- At request time, `rights_change_note` in `operations/approval_queue.py`
  runs after the request and its approval step event are saved (the
  dispatcher and the composite step), outside the `try` that answers
  "denied". A failed read of the note gives no note; the call still waits
  for approval. When the requester is the only admin, the answer gets
  `extras.approval_note`: "This change needs a second admin to approve it.
  This tenant has only one admin." and a link to the approvals guide. The
  note is added to `error` too. The request is still stored, so an admin
  added later can approve it. `meho operation call` prints the note under
  the parked hint. The web console shows it in the waiting answer of the
  Keycloak pages, the operation run window, and the classify page.
- `/api/v1/health` and `meho_status` report the count in
  `approvals.rights_change_admins`. `meho doctor` shows the line "second
  admin": a warning with the same sentence for one admin, ok for two or
  more, and no line when the count is not known or the backplane is older.

## The "may I approve this?" answer (#3329)

People see, for each request, whether they may approve it and why not.
MEHO works the answer out on the server; the command line and the web
interface only show it.

- **Where it is worked out.** `approve_right(operator, request)` in
  `operations/approval_rights.py`. It runs the checks of
  `approve_request` in the same order: a machine login never approves
  (`not_a_person`), a `read_only` person without the `approver` flag
  cannot decide (`no_approval_access`), only a `pending` request can be
  approved (`not_pending`), the group rule of #3328 (`rights_change`,
  `own_change`, `wrong_group`), and the role floor of the operation
  (`role_too_low`).
  Otherwise the answer is `ok`. For an own change that a
  MEHO-Admin (or break-glass) may approve, the answer is yes with
  `own_change`. So is a web click on an own `caution` change that
  waited only because it came with a token (#3980): the web interface
  says yes, the command line and REST say no. It does not check what can change before the click: the
  sign-in age, the deadline, the params and the preview binding. The
  approve still checks those. The read writes nothing and logs no
  break-glass warning (`_check_self_approval(..., log_break_glass=False)`).
- **REST.** `GET /api/v1/approvals` and `GET /api/v1/approvals/{id}`
  carry `may_approve` and `may_approve_reason` for the caller. The
  withdraw answer leaves both `null`. `POST …/decide` answers with
  `self_approved` and `approval_rule` after an approve: the same values
  as the `approval.decision` audit row.
- **Command line.** `meho approvals list` has an `APPROVE` column
  (`yes`, `yes:own_change`, `no:<reason>`, `-` when not pending).
  `meho approvals show` has a `May approve` line. `meho approvals
  approve` prints a self-approval line after a self-approval (also with
  several ids and with `--in-order`). On a 403 it prints the server's
  text as it is, then says the web interface is the normal place to
  approve.
- **Web interface.** The request window shows the answer for a pending
  request, and the Approve button is disabled when the answer is no.
  This is not a new gate: the approve POST runs `approve_request`,
  which checks again (see the module docstring of
  `ui/routes/approvals/routes.py`). After a self-approval the outcome
  card says so. A refused approve (403) shows the server's refusal text
  as it is in its banner, then the same hint as the command line
  (`WEB_APPROVAL_HINT`: the web interface is the normal place to
  approve). A refused reject carries no hint. The routes find a refused
  own-change approve by the `SelfApprovalForbiddenError` cause, because
  since #3328 the detail is the plain refusal text, not the
  `self_approval_forbidden` token.
- **"Approve group in order" window** (`ui/routes/approvals/in_order.py`).
  Each pending step shows the same answer. When a step the run has not
  reached says no, the window says the run will stop there; the steps
  before it can still go. A step the run approved as a self-approval
  says so on its row, and the done banner counts them. A refused step
  shows the refusal text and the hint. Each step's approval checks
  again.

### Shadow mode of "may approve"

With the `POLICY_ENGINE_SHADOW` switch on (off by default), every "may I
approve this?" answer (`approve_right`) and every approve also run the
policy evaluator's `may_approve` (`policy/approve.py`). When the two
answers differ, MEHO logs `policy_shadow_mismatch` with `kind=approval`
and counts the metric `policy_shadow_mismatch_total`. Today's answer still
decides. Run approvals are not compared. See
[policy-evaluator.md](policy-evaluator.md).

### The policy rule on the request (#6044)

With the same switch on, a new request also stores the policy rule that
the evaluator matched for the call: `approval_request.policy_rule`
(`{"source", "rule_id", "text", "decided"}`) and `policy_version`
(migration `0168`). Its `approval.request` audit row has the same rule in
`audit_log.policy_rule`. The `approval.decision` rows name no rule, also
an expiry that a retry writes for an old overdue request. `decided` is
`false` in shadow mode: today's
checks decided. The console shows a "Policy rule" line in the request
details, with "(shadow: watched only, today's checks decided)". With the
switch off both columns stay empty and the line is not shown.

### Approver routing (`POLICY_APPROVER_ROUTING`)

Off by default (Helm `config.policy.approverRouting`). With it on, a
customer can send approvals to the right team: for example only
MEHO-Operators and MEHO-Admins approve network changes. The rules are
the `routing` section of the policy file ([policy.md](policy.md)). The
code is `policy/routing.py`.

**When a request starts to wait** (`create_pending_request`, used by the
dispatcher and by a step of a multi-step operation), `stamp_approver_set`
works out who may approve it and stores it on the request:

- `approver_roles` (JSON): the login group kinds that may approve, for
  example `["Operators", "Admins"]`. Group kinds only, never people.
  MEHO stores no members, so the requester is never in it. Whether the
  requester may approve their own change is today's rule (below).
- `approver_rule` (text): the routing entries that set them, the
  install's first (`install:routing[2]`, `tenant:routing[0]`,
  `install:routing[0], tenant:routing[1]`), or `default`.
- `approver_category` (text): the category the tenant's entries used (the
  category the classification gave the change).
- `approver_install_category` (text): the category that the install's
  policy alone gives the change. The install's entries used it, and the
  click routes the request again with it (below). A tenant can move an
  operation to another category for its own rules; the install's routing
  still applies with this one.
- Both categories are the empty text on a request that had stored kinds
  before migration `0173`: MEHO does not know them.
- Migration `0167` adds the first two columns, migration `0173` the other
  two. All are nullable.

How the kinds are found:

1. The install's routing is a floor. The first install entry whose
   `match` fits counts, then the first tenant entry whose `match` fits.
   An install entry matches on the install's category, a tenant entry on
   the tenant's.
   The kinds are the kinds that both name, so a tenant entry can only
   narrow the install's ([policy.md](policy.md), "The install's routing is
   a floor"). A `match` can name a `category`, a `connector` pattern and a
   `risk` level; all that it names must fit.
2. The kinds are cut to the kinds that may approve the change
   today (`today_kinds`, the same rule as `approver_groups` in
   `policy/evaluate.py`): a `dangerous` or `destructive` change only
   Admins; a lower one also Operators; an
   operation that needs the `tenant_admin` role only Admins. So routing
   only narrows. It never lets a group approve more than today.
3. No entry fits: today's kinds, rule `default`. MEHO without a policy file
   therefore asks the same people as today.
4. A change of rights is not routed (both columns stay NULL). Only another
   admin approves it, as today.
5. An old kind (`SA-Operators`, `Approvers`) in an entry stays as written.
   It is narrower than the kind it is read as (`covers` and `narrow` in
   `policy/routing.py`): `[Operators, Admins]` and `[SA-Operators, Admins]`
   give `[SA-Operators, Admins]`. `Approvers` is read as Readers, who never
   approve, so it is never stored. At the click (`in_kinds`), an old kind
   matches only a person whose token names that old login group for the
   current tenant (`Operator.old_group`: the `meho_tenants` entry, or the
   `groups` claim group `JWT_SELF_APPROVER_GROUP` / `MEHO-Approvers`; a
   sub-group does not count), never every Operator. A request that waits
   keeps the kinds it was stored with, read by the same rule.

The risk level comes from `proposed_effect` (`safety_level`). The
category is the one the classification gave the looked-up operation
(`EndpointDescriptor.policy_category`, set by `apply_effective_policy` and
`apply_step_policy` while `POLICY_CLASSIFICATION` is on). The
install's category is `EndpointDescriptor.policy_install_category`: the
category from the install's classification entries and overrides alone.
A step of a multi-step operation reads the override rows of its operation
too (`apply_step_policy` passes them to `classify_step`), so a
step and a direct call of the same operation get the same categories and
the same approvers.
When those entries give the operation two categories, it is the empty
text: MEHO does not know it (an operation registered after the install's
import can match two such entries). Then no group may approve the change
when an install entry that matches on a `category` could fit it (rule
`category_not_stored`); the click refuses it and explain shows no group.
The dispatcher and the step gate pass both to `create_pending_request`. A request
without a category fits only entries without `category`.

**At the click** (`_load_pending_for_approval`) today's checks run first,
in their order: a person only, the read-only check, still pending, the
deadline, the rights-change and group rules (also the rules on own
changes), the role floor. Then `_check_install_floor`
(`InstallFloor.refusal` in `policy/routing_floor.py`): MEHO routes the
request again with the install's file as it is now, the stored risk level
and the stored `approver_install_category`. A request with stored kinds
can have no stored install category (NULL) in two cases: it started to
wait while `POLICY_CLASSIFICATION` was off (no change has a category
then), or a build of main between 15 November 2026 and pull request #6542
wrote it (those builds did not store the install category). In both
cases the category counts as "not known" while `POLICY_CLASSIFICATION` is
on, like the empty text, so the click is refused when an install entry
that matches on a `category` could fit. With that switch off, a
`category` entry fits nothing, as when the request started to wait. A
request without stored
kinds (it started to wait while the switch was off) is not checked here:
it keeps today's rule until it is decided or expires. When an install
entry fits and the
person's kind is not in it, `ApprovalRoutingError` with one sentence, for
example "The install's routing (`install:routing[0]`) lets only
MEHO-Operators or MEHO-Admins approve this change. You are in
MEHO-Operators. A tenant's routing can only narrow it." An install routing
that is not valid refuses the click. A request whose category is the
empty text (stored before migration `0173`, or the install's entries
gave the change two categories) is refused when an install entry that
matches on a `category` could fit it: MEHO cannot check who may approve
it, so it must be rejected. When no such
entry could fit, the category does not change the answer. A change of
rights is not checked here. `meho policy explain --request` uses the same
install routing (`install_floor`), so it names the groups the click
allows now. Then `_check_routed_approver`: the person's
group kind (`kind_of`: Admins, Operators or
Readers) must be in the stored kinds. `kind_of` reads the role and the
`approver` flag: a `read_only` person with the flag counts as Operators,
because they approve what an Operator approves. No role gives the flag
today; roles that customers define come later. A binding of the policy
file sets the role ([policy-bindings.md](policy-bindings.md)). So a
binding can set the kind
that the routing checks: a person whose group is bound to `Operators`
approves a change routed to Operators. If not, `ApprovalRoutingError` (a
subclass of `ApprovalGroupForbiddenError`, so every approve route answers
403) with one sentence that names who may approve, for example: "Only
MEHO-Operators or MEHO-Admins may approve this change (routing rule
`install:routing[0]`). You are in MEHO-Operators." The fresh sign-in, the
params check and the destructive preview binding still run after it.
Routing also narrows an own click: an Operator whose own network
change is routed to Admins only cannot approve it.

`approve_right` gives the same answer with the reason `not_routed`. Its
callers load the install's routing as it is now once
(`load_install_floor` or `current_install_floor`) and pass it as
`floor=`, so `approve_right` checks it first, as the click does. So the
console sign and the approval card, the REST fields `may_approve` /
`may_approve_reason`, the "waiting for you" order, the bell count and the
push recipients (`approve_right_any_status`) all follow the routing. The shadow mode of
"may approve" compares the evaluator with today's answer before routing.

**When the routing changes.** A waiting request keeps the group kinds it
stored when it started to wait. A tenant file that is imported later
applies only to new requests, not to the ones already waiting. The
install's file is different: the click always checks the install's
current routing too, so a narrower install file counts at once for the
requests that already wait. `approve_right` (the console sign, the
approval card, the push recipients) reads the install's current routing
too, so the sign says what the click decides. The "Who can approve" text
still names the stored kinds. Run approvals are different: they store no kinds, so they use the policy that
is stored at the moment of the click.

**Audit rows.** When a request has stored kinds, its audit rows carry
`approver_rule` and `approver_roles`: the `approval.request` row and the
`approval.decision` row of the approve. When an install entry counted at
the click, the `approval.decision` row of the approve also carries
`approver_floor`: the place of that install entry (for example
`install:routing[0]`), from the install's file as it was at the click. The
approve audit row of a run
approval carries `approver_routing`: the rule and the kinds of each routed
entry, also when the list is refused. When the stored routing in the
policy file is not valid, the request does not wait. One
`approval.request` audit row records the refusal (`result_status`
`routing_invalid`, `approver_rule` `invalid`, `routing_error`), written in
its own transaction.

**Routing on, classification off.** Then no change has a category, so a
`category` entry fits nothing. The import and the check of a policy file
warn for each such entry, and `/ready` shows the same sentence under
`features.policy.notes`.

**Old requests and the switch.** A request without stored kinds (made while
the switch was off, or before migration `0167`) keeps today's rule until it
is decided or expires: the click does not check the install's routing for
it. A request with stored kinds from before migration `0173` has the empty
text as category (see the click above). With
the switch off, MEHO stores nothing and checks no stored kinds, so turning
the switch off brings back today's rule for every waiting request too. A
stored value that is not a list of kinds lets nobody approve (fail
closed).

**Console.** With the switch on:

- the request window and the bell list show "Who can approve: Members of
  MEHO-Operators or MEHO-Admins." when the request has stored kinds
  (`who_can_approve`);
- the approvals page has a **For you** tab: the pending requests the
  signed-in person may approve (the `approve_right` answer). When MEHO
  cannot check the person, the tab answers 503 instead of an empty list;
- the bell counts only the requests the person may approve.

With the switch off, none of this shows, and `tab=for_you` is an unknown
tab (422).

**Run approvals** use the same routing for each entry of the list, with
the policy that is stored at the moment a person approves it
([run-approvals.md](run-approvals.md)).

Tests: `tests/test_policy_routing.py`, `tests/test_policy_routing_classified.py`,
`tests/test_run_approvals_routing.py`, `tests/test_ui_approvals_routing.py`,
`tests/test_web_push_send.py`,
`tests/test_policy_routing_floor.py`, `tests/test_policy_routing_step_floor.py`,
`tests/migrations/test_migration_0167_add_approval_request_approver_roles.py`,
`tests/migrations/test_migration_0173_add_approval_request_approver_category.py`.

**Not yet:** a team is a group kind. Bindings that give a login group
categories come later. The "separated duties" file
(`deploy/policy-profiles/separated-duties.yaml`) shows the routing:
identity and secret changes only Admins, network and storage changes only
Admins.

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

## The sender withdraws its own request (#4987)

The sender of a request can take it back while it still waits for
approval. This is **not** a decision. Before #4987 the only way was to
reject your own request, which looked like an approver said no, needed a
person, and was a decision an agent must never make.

- **Who:** only the sender (`operator.sub == request.principal_sub`).
  Any kind of principal may do it: a person, an agent, or a service
  account. Anyone else gets 403 `not_request_sender`, an approver
  included. The route uses `verify_jwt_and_bind`, not the
  approvals-plane gate and not `require_human_principal`.
- **When:** only while the request is `pending`. A decided, expired or
  already withdrawn request gets 409 `approval_request_already_<status>`.
  The owner check runs first, so a non-sender never learns the state.
- **What changes:** `withdraw_request` (`approval_queue.py`) takes the
  same row lock as approve and reject, sets `status = 'withdrawn'`,
  `withdrawn_at` and `withdraw_reason` (columns added by migration
  `0119`), and drops a stored credential handoff (#3537).
  `reviewed_by` and `decided_at` stay NULL.
- **Audit:** one row, `path = 'approval.withdraw'`,
  `event_class = 'approval_decision'`, `operator_sub` = the sender. The
  payload has `withdrawn_by` and the optional `reason`, and no `decision`
  or `reviewed_by` key. The audit query shows its result as `withdrawn`,
  never `ok`, and a filter for operation runs
  (`event_class = operation_execution`) does not return it: the
  operation never ran (#5717). The class is the one of the other rows
  that close a request (approve, reject, expiry). Rows written before
  #5717 keep the class `operation_execution`; audit rows never change. A paired
  add-on gets the `approval.withdrawn` step event (#3027).
- **Nothing runs:** approve, reject and decide on a withdrawn request
  see a closed row and answer 409 `approval_request_already_withdrawn`.
  An agent run that waits in process for the decision stops at once
  with `error_code = "approval_withdrawn"`.
- **Surfaces:** `POST /api/v1/approvals/{id}/withdraw` with an optional
  `reason` (answers with the updated request view), and
  `meho approvals withdraw <id> [--reason TEXT]`. List, show and the
  console show `withdrawn` with the time and the reason. The console
  has a "Withdrawn" tab, and its request page says "Withdrawn by the
  sender" and shows no approve or deny buttons.
- **MCP:** there is no withdraw tool. Adding one to the agent tool list
  is a separate team decision.

## Single-operator tenants: make the person an Admin

The self-approval guard enforces requester != approver on the stable
`sub` claim. On a tenant with **one** human operator, the operator's
change waits, and the only person who could approve it is the operator.
The answer is to make the person an **Admin**:

1. Put the operator in MEHO-Admins (the login group,
   [keycloak-realm.md](../../docs-site/install/keycloak-realm.md)).
2. The operator's change waits as before. The wait keeps the preview
   binding, the blast-radius review and the approval record.
3. The operator opens the request in the web interface and approves it
   with a click. The `approval.decision` row records
   `self_approved: true` and `approval_rule: sa_operator_self_approval`
   (the stored value keeps its name).
   On the command line the same works with a fresh approval sign-in (see
   [The approval groups and how a person approves](#the-approval-groups-and-how-a-person-approves-3325)).

This works at every risk level, also for `dangerous` and `destructive`
changes, because `Operator.may_self_approve` is true for the
`tenant_admin` role. MEHO reads the old group MEHO-SA-Operators as
Operators: it gives no self-approval. A site that wants a second person
for an admin's risky changes keeps the admins out of risky work and gives
the people who run changes the MEHO-Operators group.

The break-glass switch `APPROVAL_ALLOW_SELF_APPROVAL` is still an
**emergency** escape, not the everyday answer. Flipping it posture-wide
re-opens, for every `safe` and `caution` op, the single-account
*request + grant* hole #1401 was created to close, and it never reaches
`dangerous` or `destructive`. Whether a deploy has it set is surfaced on
`GET /ready` → `features.approval_queue.effective_posture`
(`"single_operator_break_glass"` vs the default
`"four_eyes_enforced"`, #2087).

### Who is the requester of an agent run

The request row records who asked (see [Subject + actor attribution on
the request row](#subject--actor-attribution-on-the-request-row-1481)
above). The two agent-run entry points in
`backend/src/meho_backplane/agent/invocation.py` record it differently:

- **Scheduled run** — `AgentInvoker.run_scheduled` signs the agent in
  with its own `client_credentials` grant and binds no
  `actor_delegation`. The row gets `principal_sub=<agent-sub>` and
  `principal_act=NULL`. This is how writes that run on a schedule wait
  for approval ([`scheduler.md`](scheduler.md)).
- **Run started by a person** — `AgentInvoker.run` binds the agent as
  the RFC 8693 **actor** with `actor_delegation(...)`. The person stays
  the subject: the row gets `principal_sub=<person>` and
  `principal_act=<agent-sub>`, so the rules for the person's own change
  apply.

### The people behind a request

A request made by an agent, a service login (an add-on) or a runner
counts as a request of the people behind that login. The code is
`backend/src/meho_backplane/operations/people_behind.py`.

**Who counts.** `find_people_behind` looks in the login's tenant when the
request is made (`create_pending_request`):

| Login | People behind it (reason code) |
|---|---|
| Agent | who registered it (`registered`) or owns it (`owns`), from `agent_principal` by client id; who gave one of its grants in force (`gave_grant`, `agent_permission`, key = token `sub` or client id, not expired); who wrote (`agent_definition`) or last changed (`changed_agent_definition`) an agent definition with `identity_ref` = the client id; who made a schedule of those definitions, active or not (`made_schedule`); when one of those schedules waits for events, who made an event source of the tenant (`made_event_source`) |
| Service login | who paired (`registered`) or owns (`owns`) its add-on (`addon_pairing` by `service_account_sub` or client id); who gave one of its standing grants in force (`gave_grant`, not revoked, not replaced, not expired) |
| Runner | who registered (`registered`) or owns (`owns`) it (`runner_principal` by the token's `runner_id`, or by client id) |
| Every machine login | who started (`started_run`) or last handed over (`handed_over_run`) a runbook run assigned to it that has not ended |
| A person, or a delegated call | nobody: the person is the requester |

**A machine login never counts as a person behind another login.**
`machine_subs` (`backend/src/meho_backplane/auth/machine_logins.py`)
leaves out every id that MEHO knows as an agent, service or runner login:
a row of the `machine_login` table, or the login of an agent definition,
an agent, an add-on or a runner of the tenant. Each REST call
(`verify_jwt_and_bind`) and each MCP call (`verify_mcp_jwt_and_bind`) of
a machine login adds its id to `machine_login` (`note_machine_login`,
safe to run twice). So does the sign-in of a hosted agent run that a
schedule or an event starts (`_authenticate_scheduled_agent`). When MEHO finds only machine logins, the list is
empty and only an admin may approve.

The checks investigator runs as an agent (`run_scheduled`), so its
requests follow the agent row. A sensor never makes a request.

**What MEHO stores.** Migration `0178` adds four columns and a table:

- `approval_request.people_behind` — JSON list of
  `{"sub", "name", "why"}`, one entry per person and reason. `NULL` for a
  person's request. An empty list: MEHO knows nobody behind the login.
  The name is the newest `principal_name` of the person in the tenant's
  audit log, or `null`. A step of a multi-step operation that waits
  again keeps the list of the first request (`ResumeRequester`).
- `agent_definition.updated_by_sub` — set by every edit of a person
  (`AgentDefinitionService.update`). An edit by a machine login keeps the
  person who was there. The migration fills it from the newest successful
  `agent.edit` audit row of the definition by a login that is not a
  machine login, else from `created_by_sub`. A machine login here is a
  row of `machine_login`, or the login of an agent definition, an agent,
  an add-on pairing (client id or service login) or a runner of the
  tenant: the same records `machine_subs` reads. It reads only audit rows from the time the definition
  was created, so an edit of a deleted definition with the same name does
  not count.
- `runbook_runs.reassigned_by` — set by `RunbookRunService.reassign_run`
  when a person hands the run over. A handover by a machine login keeps
  the person who was there.
- `run_approval.principal_client_id` — the client id of the add-on login
  that asked for a run approval list. At approve time MEHO finds the
  add-on pairing by the login id and by this client id. `NULL` on a list
  from before the upgrade: then MEHO uses the login id alone.
- `machine_login` — one row per tenant and login id that MEHO has seen
  as an agent, service or runner login. The migration fills it from
  the requests of such logins (`approval_request.principal_kind`) and
  from the `client` block of the audit rows.

**The rule.** One shared check, `_check_approver_group`, used by every
approve door (`approve_request`, so REST approve and decide, the CLI and
the web console) and by the "may I approve?" answer (`approve_right`,
`approve_right_any_status`: the button, the lists, the waiting count and
web push):

1. A change of rights: `Operator.may_approve_rights_change(requester,
   behind)` refuses the requester and every person behind it, also an
   admin.
2. A machine login asked and MEHO knows nobody behind it:
   `_check_requester_known` lets only an admin approve
   (`UnknownRequesterApprovalError`, 403). An unknown origin never gets
   less control than a known one.
3. `_check_self_approval` treats a person behind the login like the
   requester: they may approve only where they may approve their own
   change (`Operator.may_self_approve`, the web click on a `caution`
   change that waited only for its token, the emergency switch for
   `safe` and `caution`). Else `SelfApprovalForbiddenError` with
   `behind=True`; its text says the change counts as the person's own
   change, and who may approve it.

Run approval lists (`RunApprovalService.approve`) look up the people
behind the service login at approve time (`_check_people_behind`), so a
list that waited across the upgrade follows the rule too. A person behind
it may approve the list only when `may_self_approve` is true; nobody
known: only an admin; a change of rights: never a person behind it. The
policy evaluator (`policy/approve.py`) gets the same facts on
`ApprovalAsk` (`people_behind`, `requester_unknown`), so shadow mode
reports no difference. `tests/test_people_behind_doors_guard.py` lists
every door and fails when a new function writes an approval without the
check.

**Audit.** When a person behind the login approves, the
`approval.decision` audit row carries `self_approved: true`,
`approver_behind_requester: true` and `approver_behind_why` (the reason
codes). The `approval.approved` step event of an add-on carries only
`self_approved: true`.

**Where people see it.** REST `ApprovalRequestView.people_behind`
(`sub`, `name`, `why`, `why_text`) and `people_behind_known`, only
when a person calls the API: for a person's request, and for an agent,
service or runner login that reads the request, the answer leaves both
keys out; the web
console request card ("Set up by Ann. Grant by Bob.") and the request
modal ("Behind it"); `meho approvals show` ("Behind it:"). When MEHO
knows nobody, each says "Set up by: not known".

### Troubleshooting — `self_approval_forbidden`

| Symptom | Cause | Fix |
|---|---|---|
| `approve` returns `self_approval_forbidden` | The request is your own change, and you are not in MEHO-Admins. | Ask a person in one of the groups the refusal names to approve it on the Approvals page. On a one-operator tenant, make yourself an **Admin** (MEHO-Admins; the change applies at your next login), then approve with a click in the web interface. |
| `approve` returns `self_approval_forbidden` and the text says you set up, changed or started the login that asked | You are one of the people behind the agent, service or runner login (see [The people behind a request](#the-people-behind-a-request)). | Ask another person the text names. |
| `approve` refuses with "MEHO does not know who set up the login" | A machine login asked and `people_behind` is empty. | An admin approves it, or give the login a grant in MEHO so its next requests have a known person behind them. |

### Where operators see this

- **Docs-site guide** — `docs-site/guides/approvals-and-break-glass.md`
  ("Do real work → Approvals and break-glass"). The emergency switch is
  documented there, in the part for deployment admins only.
- **Console** — the approvals modal (`ui/templates/approvals/_modal.html`)
  shows the refusal text when the Approve button is disabled for an own
  change and after a refused self-approval (403). The text says who may
  approve and where to ask.
- **Refusal texts** — the console, the REST `403` (the command line
  prints it) and the "may I approve this?" answer name no setting, no
  emergency switch and no other way to approve an own change.
  `test_ui_approvals.py`, `test_ui_approvals_rights.py`,
  `test_approval_rights.py` and `test_api_v1_approvals.py` check it.
- **Chart value** — `config.approvalAllowSelfApproval` (rendered into
  `APPROVAL_ALLOW_SELF_APPROVAL` by `configmap.yaml`) is a
  schema-documented value (`values.schema.json`,
  `enum: ["true","false"]`, default `"false"`).

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

**No `run_id` gate any more (#2293).** At first, `/decide` ran the op
only when `run_id IS NULL` (a direct operator op). It skipped an
**agent-run** request (`run_id` set), because the waiting agent runs that
one itself. That skip was removed. When the agent had stopped waiting
(wait timeout, pod restart, run cancelled), the approval committed but
nothing ran.

Now `/decide` tries to run the op on **every** approval, for both kinds of
request. The one-resumer claim (`claim_resume`, inside
`resume_dispatch_after_approval`) decides who runs it. Only the caller
that wins the claim runs the op:

- `/decide` wins: it runs the op, or refuses it before it runs. A
  waiting agent then runs nothing. It reads the request again and
  returns the stored result of that run: the refusal, or the result of
  the run (`_lost_claim_envelope` in `agent/approval_wait.py`). When no
  result is stored yet, it returns an `already_resumed` answer that says
  the outcome is not known yet; it never says the operation ran.
- The waiting agent wins: `/decide` runs nothing and returns
  `dispatch_status="already_resumed"`.
- No agent is waiting any more: `/decide` wins and runs the op.

So the op runs at most once.

**Double-execution prevention (terminal-state guard).**
`approve_request` flips the row to `approved` (terminal) and commits
*before* the re-dispatch runs. A concurrent second `/decide`/approve on
the same row hits the already-decided guard (409 / `not_pending`), so the
stored params drive exactly one execution.

**No params to run with.** A surface that sends no params (`/decide`,
the console, MCP by-id approve) runs the params stored on the row. When
the row holds none MEHO can run with (a row parked before migration 0036
has `params IS NULL`; params whose secret values MEHO already hid), the
operation does not run. `resume_dispatch_after_approval` records the
refusal like the release and target refusals (`_refuse_resume`, #6064):
the claim is taken, one audit row is written
(`result_status = "approval_resume_params_unavailable"`), and the refusal
is stored as the run result. The request shows `result_state = "failed"`
with that reason on every surface. The requester can then re-run it and
send the params (`POST /api/v1/approvals/{id}/rerun`).

## Run an approved request again after its run failed (#5417)

A person approved one exact call, and its run failed. For example, a file
write ran before the folder it needed existed. The requester can now run
the same call again with no new approval, as long as nothing changed.
Before this, the request stayed `approved` with a failed result, and nothing
could run it again: the run claim (`resumed_at`) is set once and never
cleared, and a second `/decide` answers 409.

**Route.** `POST /api/v1/approvals/{id}/rerun`, body `{"params": {...}}`
(optional). Code: `operations/approval_rerun.py`
(`rerun_failed_request`) and the route in `api/v1/approvals.py`. It is not
a decision, so it is not on the approvals gate and not human-only. A
person, an agent or a service principal can re-run **its own** request.
There is no MCP tool for it; the agent working surface is unchanged.

**When it runs.** All of these must hold, checked in this order:

| Rule | Refusal |
|---|---|
| The caller made the request (`sub == principal_sub`). | 403 `not_request_owner` |
| `result_state` is `failed`. Not `executed`, not `uncertain` (MEHO does not know if the change happened, so it never runs it again), not `started` (still running), not a request that is not `approved`. | 409 `approval_rerun_not_allowed` |
| The request is not for a step of a multi-step operation (`resume_parent` is not set). A re-run would send the steps after the approved step again, so the requester starts a new run of the operation. | 409 `approval_rerun_needs_new_approval` (`multi_step_run`) |
| MEHO has params to run with: the caller sent them, or the request stored them. | 422 `approval_rerun_params_required` |
| The `expires_at` deadline has not passed. | 409 `approval_rerun_needs_new_approval` (`deadline_passed`) |
| MEHO was not updated since the request was made (same check as `refuse_resume_if_release_changed`). | same (`release_changed`) |
| The work reference (`Meho-Work-Ref` header) is the approved one. | same (`work_ref_changed`) |
| The params match `params_hash`. | same (`params_changed`) |
| The saved target still resolves. | same (`target_gone`) |
| The saved target has the connection settings it had when the request was made (`target_hash`, same check as every approved run). | same (`target_changed`) |
| A `destructive` request: a new server-side preview has the approved `preview_hash`. | same (`preview_changed`) |
| A `destructive` request: the live card builds, and the object it names is still the one on the approval card, or is gone (`approval_object_check.py`, same check as every approved run). | same (`object_changed`) |
| An operation whose card shows the object as it is now: that state is still the one on the approval card (`approval_state_pin.py`, same check as every approved run). | same (`object_changed`) |

The operation, connector and target always come from the request, so the
caller cannot change them. A refused re-run runs nothing.

**Credential writes.** MEHO keeps their params only in the one-time
encrypted custody (`execution_handle`), and the first run deletes it. A
re-run of a credential write must send the params again. They must match
`params_hash`. The requester's reveal choice (`reveal_secret: true` of the
original call) is not in that custody: it is on the request row
(`approval_request.reveal_secret`). A request with that choice never runs
again (`state_secret_reveal`): it runs once, when the person who asked
fetches the value. A `reveal_secret` flag sent with the params of another
re-run comes off before the hash check and does not change the stored
choice.

**Runs once.** The re-run has its own claim, because `resumed_at` stays
the one-way latch of the first run. The claim is one conditional `UPDATE`:
it sets `resume_result` to NULL and adds one to `rerun_count` (migration
`0129`), only while the stored result is a failure and `rerun_count` is
still the value the caller read. Two re-runs sent at the same time: one
wins, the other gets 409. That also holds when the first re-run already
ran and failed again before the second one claims: the second one read the
old `rerun_count`, so its claim does not match and it must read the
request again. While the re-run runs, `result_state` reads `started`.

**Who runs it.** The run uses the requester's identity, like the agent's
own resume after approval. The approval is the authorization
(`dispatch(..., _approved=True)`), through the same
`_dispatch_resume_with_bound_context` the approve paths use. So the work
reference, the session link and the result-handle owner are bound the same
way.

**Result.** The new result replaces `resume_result`, so `/result`,
`result_state` and `resume_outcome` show the latest attempt. When the run
raises instead of returning a result, MEHO stores a failed result
(`rerun_failed: ...`), so the request can be run again later.

**Audit.** Every re-run writes an `approval.rerun` row (`policy_decision`,
parent: the request row), in the same transaction as the claim:
`status_code` `202`, `payload.result_status` `accepted` and
`payload.attempt`. The first run after the approval is attempt 1, so the
first re-run is attempt 2. The run's own row also carries
`approval_rerun_attempt` next to `approval_request_id`. A refused re-run
writes an `approval.rerun` row with `result_status` `refused`, the reason
in `refusal`, and `status_code` `409` or `422`.

**Response.** `RerunResponseBody`: `approval_request_id`, `attempt`,
`dispatch_status`, `dispatch_op_id`, `dispatch_result`, `dispatch_error`.

**Pinned by tests:** `tests/test_approval_rerun.py`,
`tests/migrations/test_migration_0129_add_approval_request_rerun_count.py`,
the credential-write re-run tests in `tests/test_secret_values_hidden.py`
and `tests/migrations/test_migration_0131_add_approval_request_reveal_secret.py`.

## An approved secret reveal runs when the person who asked fetches it

Every request with `reveal_secret: true` that waits for a second
person's approval follows one rule: a read that shows secret values (for
example `vault.kv.read`), and an operation that creates a credential (for
example a token or a secret id). The approval is a one-time permission
for the person who asked, not a run. The code is in
`operations/approved_reveal.py`.

- **Approving runs nothing.** Every approve surface (REST `/approve` and
  `/decide`, the console approve card, the in-order page) goes through
  `resume_dispatch_after_approval`. For such a request it answers with
  `status: "waiting_for_requester"` and claims nothing. The answer to the
  approving person holds no value. An `async` approve starts no
  background run. The CLI `meho approvals approve` prints the same.
- **The request view says so too.** `ApprovalRequestView.waits_for_requester`
  is `true` for such a request (`is_secret_reveal`). When the answer to
  `meho approvals approve --wait` is lost on the network, the CLI reads
  the request again. For such a request it does not wait for a run, and
  prints how the person who asked gets the value.
- **An approve in order stops at a change that has not happened.**
  `ApprovalRequestView.reveal_changes_something` is `true` when such a
  request may change something when it runs (`is_write_reveal`). Only
  an op of class `read` or `credential_read` counts as a read. Every
  other class counts as a change: a credential write, a token create,
  and also class `other` (for example an apply, a scale or a reboot).
  The console in-order page and `meho approvals approve --in-order` stop
  at such a step, so the steps after it do not run before it. After a
  read they go on: a read changes nothing. On a later in-order run, a
  reveal approved earlier and not fetched yet still stops the run, with
  a message that it waits for the person who asked.
- **The audit class of the fetch.** The HTTP record of `POST
  /api/v1/approvals/{id}/reveal` has class `credential_read`, never a
  plain `read`: its answer holds a secret value, and the run may change
  something.
- **The person who asked fetches the value.** `POST
  /api/v1/approvals/{id}/reveal` (`api/v1/approval_reveal.py`), the
  console's "Show value" button (`POST /ui/vault/read/reveal`), `meho
  secret read --approval <id>` and, with `MCP_REVEAL_FETCH` on,
  `call_operation` with `approval_request_id` on MCP
  ([mcp.md](mcp.md#getting-an-approved-secret-value-on-mcp-mcp_reveal_fetch-off-by-default))
  call `fetch_approved_reveal`. It runs the
  operation through `resume_dispatch_after_approval(..., requester_fetch=True)`
  and gives the value only in that one answer.
- **No cache keeps the answer with the value.** An answer that holds the
  value says `Cache-Control: no-store` and `Pragma: no-cache`
  (`no_store.py`). The dispatcher marks the answer when it puts the
  values in (`_protect_secret_values` calls
  `no_store.mark_answer_holds_secret`). `fetch_approved_reveal` also
  marks every approved reveal that ran with `status: ok`, so the answer
  with a new credential (for example a new AppRole secret ID) is marked
  too. Each route opens
  `no_store.answer_scope` around the run and sets the headers only when
  the answer was marked: `POST /api/v1/approvals/{id}/reveal`, `POST
  /api/v1/operations/call`, MCP, and the console's operation page, Vault
  read page and "Show value" button. So these keep their usual headers: a
  refused fetch, a run that was refused (for example the target changed)
  or ended with an error, the stored run result
  (`GET /api/v1/approvals/{id}/result`), the request view, and the same
  call without `reveal_secret`. A person's `reveal_secret: true` on a
  plain read runs at once, with no approval, and says no-store too.
  Tests: `tests/test_approval_reveal_no_store.py`, `tests/test_ui_vault.py`,
  `tests/test_ui_operations.py`, `tests/test_mcp_reveal_fetch.py`.
- **A reveal call cannot run in the background.** `reveal_secret: true`
  together with `async: true` is refused before any run starts: HTTP 422
  `reveal_secret_with_async` on `POST /api/v1/operations/call`, and bad
  input (reason `reveal_secret_with_async`) on MCP `call_operation`. The
  call runs at once, or through an approval.
- **Once, and only for a short time.** The fetch must win the one-resumer
  claim, so a second fetch is refused (`reveal_already_used`). It must
  come within `APPROVAL_REVEAL_FETCH_WINDOW_SECONDS` (default 900, 15
  minutes) of the approval (`reveal_window_passed`). After that, the
  person sends the request again. Such a request also never runs again
  with "run again" (`state_secret_reveal`), also when its one run failed.
- **Tied to the request.** Only the person who sent the request, in the
  same tenant, may fetch (403 / 404). The stored params must still match
  the stored hash (`reveal_params_changed`), and the usual checks of an
  approved run hold (same release, same target, same preview).
- **MEHO keeps no value.** The run result stored on the request
  (`resume_result`) has no answer (`result: null`, `handle: null`,
  `extras.secret_value_withheld: true`). The result cache keeps nothing
  either: for a `reveal_secret: true` run the dispatcher sets
  `secret_reveal` in the reducer context, and the reducer returns the
  answer whole, with no handle, also when it is over the JSONFlux limit.
  The audit row of the run keeps
  fingerprints only, as for every reveal. Each fetch attempt writes one
  `approval.reveal` audit row with its outcome (`run` or the refusal
  code).
- A refused fetch answers 409 with `detail` `reveal_<code>: <message>`.
  Codes: `not_a_reveal`, `not_approved`, `already_used`,
  `window_passed`, `params_changed`.

### Reading a long script, page by page

The park-time preview of `linux.script.run` holds page 1 of the script (64 KiB). The
full script is in the encrypted hand-off store until the approved call runs
(`consume` deletes it). While the request waits, the approver reads the other
pages with `GET /api/v1/approvals/{id}/script?page=N` (same access as
`GET /api/v1/approvals/{id}`; 410 with a plain sentence after the decision; 409
when the stored script cannot be decrypted; 404 for a request with no script or
a page that does not exist), with `meho
approvals show <id> --script-page N`, and with the page links of the console
block (`GET /ui/approvals/{id}/script?page=N`). The read does not delete the stored
script (`approval_handoff.read`). After the decision the console block has no
page links and says that only page 1 is shown. Details and limits:
[`connectors-linux.md`](connectors-linux.md).

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
starts a second bring-up). So a paired add-on's resume-in-place engine
had to **re-park** non-idempotent ops instead of resuming them.

The fix is a durable capture + a principal-scoped read, both composing with
the existing exactly-one-resumer substrate rather than duplicating it:

1. **Capture at the winning resume path.** `resume_dispatch_after_approval`
   — the single execute-after-approve entry point shared by REST `/approve`
   \+ `/decide`, the console approve, and the async approve background task
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

The same function also binds a hosted agent's connector list again. The
request stores it when the call starts to wait (`allowed_connectors`, JSON,
migration `0175`, NULL for a caller without a list). The run and each step
of a multi-step operation then stay on targets of those connectors
(`operations/_target_connector_limit.py`), also when a person approves
after the agent stopped waiting.

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

**Relation to the #3027 add-on step event.** `record_approval_step_event`
already durably records the *decision* (`approval.approved` / `.rejected`)
for the paired add-on that requested the approval
(`AddonStepEventService.record_if_owned_committed`). That answers "was it
approved?"; the `/result` surface answers "what did the approved re-dispatch
produce?" — complementary, not a duplicate.

On MCP, with `MCP_OWN_REQUEST_STATUS` on (off by default), the owner makes
the same read with `call_operation(connector_id, op_id, approval_request_id)`.
The owner is the same login. The read checks no agent grant.
It uses `read_approval_result` too, so the owner check and the
`approval.result` audit row are the same. Another person's request and an
unknown id both answer `approval_request_not_found`
([mcp.md](mcp.md#reading-the-state-of-your-own-approval-request-mcp_own_request_status-off-by-default)).

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
- **Asked the same way.** A request counts for the key only when it was
  asked the same way as this call (`_Asker` in `approval_queue.py`): the
  same kind of login (`principal_kind`), the same people behind the login
  (`people_behind`, compared by the set of `sub` values, casefolded: the
  compare of the self-approval rule, `behind_subs`) and the same
  `reveal_secret` choice. A request asked another way is not returned, and
  the call gets its own request, which waits for a person. The first
  request stays as it is. The new request cannot keep the key in its own
  column while the first one holds it there (the unique index below), so
  MEHO keeps the key for it in `approval_request_key`. A later retry asked
  the same way as the new request gets the new request back.
- **Same key, different call.** "Identical" means the same `op_id`,
  `connector_id`, target, `params_hash`, `preview_hash`, and `work_ref`. If
  any of them differs, the call is refused with `status="denied"` and
  `extras.error_code="idempotency_key_conflict"`, and
  `extras.approval_request_id` names the request that holds the key. Nothing
  is parked. The refusal writes a `denied` audit row. This holds for every
  live request the key gets back, in its own column or in
  `approval_request_key`, also for one asked another way
  (`_refuse_key_held_for_another_call`). MEHO checks all of them before it
  gives a request back, so a key that gets back requests of two calls
  refuses a retry of each. A `pending` request past its deadline does not
  count, but only after MEHO locked it and read it again
  (`_holds_key_after_lock`): an approve that started before the deadline
  holds the row lock, and the request it approves keeps the key.
- **When the key is free again.** A `rejected` or `expired` request no
  longer holds its key, so the same key parks a new request. An `approved`
  request keeps its key even after it ran: a retry then returns the request
  that already ran, so the op is never run twice.
  The answer keeps its `status` and `extras`, but its text is
  "awaiting_approval: the approval request `<id>` is already approved". This
  text is the same for every approved request, also for one that shows a
  secret value. A retry of a waiting request keeps the text
  "requires approval before execution".
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
- **Who can match.** The key is scoped to `(tenant_id, principal_sub)` and
  to the way the call was asked (see "Asked the same way"). Two requesters,
  two logins with one `principal_sub` but other people behind them, or two
  runs with different `work_ref` values, never share one request. With the switch `APPROVAL_SAME_CALL_REUSE` off (the default),
  a call with no key makes a new request every time. With it on, see
  "The same call without the same key" below.

### The same call without the same key (`APPROVAL_SAME_CALL_REUSE`, off by default)

Two agent sessions can ask for the same change with no key, or with keys
that differ. Without the switch, each call makes its own request and the
approver sees the same change twice. With the switch on,
`create_pending_request` looks for a waiting request before it builds a
new row (`_find_same_call_pending_request`). It reuses the request when all
of this is the same:

- the tenant and the login (`principal_sub`), so nobody learns about the
  request of another person;
- `op_id`, `connector_id`, target, `params_hash`, `preview_hash`,
  `work_ref` and the `reveal_secret` choice.
- the kind of login (`principal_kind`: `user`, `agent`, `service` or `runner`).
  The kind, the people behind the login and the `reveal_secret` choice are
  the same compare as for a retry key (`_Asker`).
  The approval checks read the kind stored on the request. So a call of
  the same login with another kind gets its own request with its own
  kind.
- the people behind the login (`people_behind`, compared by the set of
  `sub` values, casefolded). The self-approval check reads only the list
  stored on the request, and the people behind an agent, service or
  runner login can change between two calls. So a call whose people
  behind differ gets its own request with its own list. A person's own
  call has no people behind on either side.

The answer has the same `approval_request_id` and
`extras.approval_request_reused: true`. No new request row is made, and no
second `approval.pending` event is sent. The call writes one audit row of
its own in the same transaction (`_write_reuse_audit_row`). It is not a
second `approval.request` row:

- `path` is `approval.reuse`, `status_code` is 202 (waits for approval),
  and `event_class` is `policy_decision`.
- `payload.approval_request_id` names the reused request.
- `agent_session_id` is the session of this call, not the session of the
  first call. `parent_audit_id` is the first call's `approval.request` row.
- The answer's `audit_id` is the id of this new row.

A retry with the same key writes no such row: it gets the first call's
`approval.request` row as its `audit_id`, as before. A key that is different from the key of the waiting request does
not matter. MEHO keeps that other key in the table `approval_request_key`
(one row per key, linked to the request; migration `0183`). The key lookup
(`_find_idempotent_request`) reads it too, so a later retry with that key
gets the same request back, also after the request was approved. The table
keeps no tenant or login: they come from the request row.

- Only a `pending` request inside its deadline is reused. An approved
  request may have run, so it is never returned. A rejected, withdrawn,
  expired or overdue request gives a new one. The lookup expires nothing.
- Only a direct call is checked. A call inside an agent run, a step of a
  multi-step operation (both kinds: one sent through `dispatch_child`, and
  one saved to restart after approval), or a re-run for an earlier
  requester always gets its own request. A step sent through
  `dispatch_child` is found by its depth (`composite_depth_var` is not the
  top level).
- Only a stored direct request is reused. A stored request of an agent run
  (`run_id` set) or of a step of a multi-step operation (`resume_parent`
  set, or `composite_step` not false) is never returned to a direct call:
  approving it would run that run or that step, not the direct call. Every
  new request stores `composite_step` (migration `0183`): true for a step
  sent through `dispatch_child`, false for any other call. A request made
  before the migration keeps NULL, because it may be a step that was
  already waiting. The lookup returns only a request marked false, so such
  an older request is never returned to another call. Its retry with the
  same key still gets it back.
- A retry with the same key still gets its request back, also for a step.
- A late retry with key A can expire its overdue request A and then get a
  waiting request B of the same call. The answer is B with
  `approval_request_reused: true`. After commit the dispatcher still
  publishes `approval.expired` for A, and no second `approval.pending` for
  B.
- Two calls that arrive at the same moment each get their own request;
  there is no database lock for this case.

**Races.** A partial unique index,
`approval_request_idempotency_key_idx` on `(tenant_id, principal_sub,
idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN
('pending', 'approved')`, makes sure two racing calls cannot both insert.
The losing insert runs in a savepoint (`_insert_idempotent_request`). When
the index refuses it, the savepoint rolls back and the winner's row is
returned the same way as a normal retry. When the index refuses it
because a request asked another way holds the key, MEHO stores the new
request with the key in `approval_request_key`.

Before it looks a key up, every call with a key takes the lock of that key
for its login (`_take_idempotency_key_lock`, `pg_advisory_xact_lock` on the
tenant, the login and the key; PostgreSQL frees it at commit or rollback).
So two calls with the same key never look it up and store a request at the
same moment. The second one looks the key up only after the first one
committed, and finds the request the first one stored or linked. This holds
also when the request that held the key is rejected or expires in between.
SQLite (the unit tests) has no such lock.

Two late retries can also race to expire the same overdue request. The key
lock makes the second one wait, and it then finds the replacement. An
approve or reject takes no key lock, so a row can still change under a
retry: the retry and the approve both want the row lock, and only one gets
it first. When the retry's locked re-read finds no `pending` row,
the row changed under it: another retry expired it and parked a replacement,
or a sweep, reject or approve got there first. `_resolve_idempotent_request`
then looks the key up again and returns what holds it now, for example the
replacement as a reused request. It never returns the expired row it read
first. That second lookup refreshes the row the session already holds
(`populate_existing`). This matters when an approve commits while the late
retry waits for the lock: the row is now `approved`, and the retry returns
it. With the cached `pending` state it would try to expire the row again,
miss again, and never return.
`tests/integration/test_approval_idempotent_retry_pg.py` runs these races
(two late retries; a late retry against an approve; a late retry for
another call, asked another way, against an approve; two retries beside a
holder asked another way; the same two retries with the holder rejected in
between) on a real Postgres.

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

`status` has five values (`pending` / `approved` / `rejected` /
`expired` / `withdrawn`); migration `0119` added `withdrawn` to the `CHECK`
constraint. Next to it, the list and show views carry
`result_state`, worked out from the row by `approval_result_state` (it is
not stored):

| `result_state` | Meaning |
|---|---|
| `null` | `status` is not `approved`. |
| `not_started` | Approved. No resumer has claimed the run yet (`resumed_at IS NULL`). |
| `started` | A resumer claimed the run (`resumed_at` is set), but no result is stored yet. The op may still be running. |
| `executed` | The stored result (`resume_result`) has `status == "ok"`. |
| `failed` | A result is stored, its `status` is not `ok`, and it does not say the outcome is unknown. |
| `uncertain` | A result is stored, its `status` is not `ok`, and `extras.execution_outcome` is `uncertain` (#5025). MEHO does not know if the change happened. |

So a client that lists or shows requests can tell "approved, not run yet"
from "approved and run" without polling the owner-only `/result` read. The
result body itself stays on `/result` only; the views show the phase, not
the payload.

Both resume paths store the result (`persist_resume_result`): the operator
surface (`resume_dispatch_after_approval`) and the in-process agent waiter
(`_resume_approved_or_already_resumed` in `agent/approval_wait.py`). So a
request the agent resumed also reaches `executed`, `failed` or `uncertain`.
Limits of `started`: the result write is best-effort, and a re-dispatch that
raised instead of returning an envelope stores nothing. In those cases the
row stays `started` until the next start of MEHO, when the restart recovery
below ends it as `uncertain`.

#### `uncertain`: MEHO does not know if the change happened (#5025)

One rule for every view: `result_state_of_envelope` in
`operations/approval_queue.py`. REST, MCP, the CLI (`UNCERTAIN`) and the
console all read it. Every view gives the same advice: do not run it again,
check the target first. No view says "failed" or "did not succeed" for it.

Eight cases end as `uncertain`:

| Case | Where | Stored result |
|---|---|---|
| The audit record could not be saved after the change ran. | `result_audit_commit_failed` | `error_code` `audit_commit_failed` |
| A change was sent and the answer was lost: a read timeout, a write timeout, or a dropped connection after the request was written. | `result_connector_timeout(..., changes_target=True)` | `error_code` `connector_timeout`, `retry: false` |
| The same on a connection that is not HTTP: an SSH command that timed out or lost its connection, or the same fault of the Kubernetes client, of the Vault client (`requests`) or of a database driver. | the general error arm of the dispatcher (`connection_lost_after_send`) | `error_code` `connector_error`, `retry: false` |
| A step of a multi-step operation ended `uncertain`, and the operation then failed (it handed back the step's result, returned its own error, or raised). | `_composite_steps.mark_when_a_step_is_unknown`, on the error audit path of the dispatcher | the operation's own error, `retry: false`, and `steps`: each step's `op_id` and `state` (`done`, `failed` or `unknown`) |
| An approved re-run stopped with such a timeout or lost connection. | `rerun_failed_request` (`connection_lost_after_send`) | `error` `rerun_failed: ...`, `retry: false` |
| The product ended the login session after MEHO sent a change, and MEHO did not send it again. | `result_connector_auth_failed(..., changes_target=True)` | `error_code` `connector_auth_failed`, `retry: false`, advice `LOGIN_ENDED_ADVICE` |
| MEHO stopped in the middle of an approved run. | `recover_interrupted_approvals` | `error_code` `interrupted_by_restart`, `retry: false` |
| An async run of a change lost its worker. | `operation_run_reaper` | the run row is `uncertain` (see `async-governed-dispatch.md`) |

The rule holds for every kind of connection. On HTTP, a connect timeout, a
pool timeout, a refused connection and a refused login
(`connector_auth_failed` at stage `establish`) stay plain errors, because
nothing was sent. An approved change always starts with a fresh login
(`drop_login_before_approved_change`, see `backend.md`), so a login that ended
while the change waited does not end the run. On the other connections, the
types are listed once, in `operations/uncertain_outcome.py`:

- uncertain: `TimeoutError` (what `asyncio.wait_for` raises around an SSH
  command), `ConnectionError` (reset, aborted, broken pipe),
  `asyncssh.ConnectionLost`, aiohttp's `ServerDisconnectedError`,
  `ClientOSError` and `ClientPayloadError`, the `ReadTimeout`,
  `ConnectionError` and `ChunkedEncodingError` of `requests` (the Vault
  client), pymongo's `AutoReconnect`, asyncpg's
  `ConnectionDoesNotExistError`, and pytds's `TimeoutError` and
  `ClosedConnectionError`;
- a plain error, because nothing was sent: `ConnectionRefusedError`,
  aiohttp's `ClientConnectorError` and `ConnectionTimeoutError`, the
  `ConnectTimeout` of `requests`, pymongo's `ServerSelectionTimeoutError`,
  and the credential errors of MEHO (`VaultClientError`, a failed Vault
  login; `CredentialsReadError`; `TargetSecretNotFoundError`;
  `TargetSecretReadFailedError`).

The not-sent list is checked first, because some of its types are
subclasses of types on the other list.

A connector may wrap a fault in its own error (`raise RuntimeError(...) from
exc`, as the VPN user create of the pfSense connector does). So
`connection_lost_after_send` reads the `__cause__` chain from the outside in,
up to eight links. The first link whose type is on a list decides. A wrapped
httpx fault counts the same way: `ReadTimeout`, `WriteTimeout`, `ReadError`,
`WriteError` and `RemoteProtocolError` are uncertain, `ConnectError`,
`ConnectTimeout` and `PoolTimeout` are plain. A wrapper that cuts the chain
(`from None`) is judged by its own type only.

Errors of the steps before the call stay plain errors too: the target or
secret lookup, the input checks and a refused probe. The shared loader of
the target's secret (`_resolve_and_load` in `connectors/_shared/vault_creds.py`)
turns a `requests` fault of the read into `TargetSecretReadFailedError`, so
a secret read that times out can run again, while the same `requests` fault
during a Vault change is uncertain. A timeout while an SSH connection opens
has the same type as a timeout of the command, so it counts as uncertain: in
doubt, MEHO does not run the change again.

A multi-step run keeps a log of its steps while it runs
(`track_composite_steps` around the run in `_execute_and_audit`). Its
`dispatch_child` writes each step into the log (`recording_steps`). The log
carries the run's `audit_id`, so the error result of a step itself, which
passes the same audit path, is never marked by the log of its parent. Only
steps that run through `dispatch_child` are in the log. A call that a
multi-step operation sends on its own connection is not a step of the log:
when its fault reaches the dispatcher, the general error arm judges it like
any other error.

A timeout of a read operation (`safety_level` `safe`) stays a plain error.
Any other level counts as a change (`changes_target`).

MEHO never runs an uncertain change again. `retry` is `false`, an approved
request starts only once (`resumed_at`), and the re-run (#5417) refuses a
request whose state is not `failed`, so it refuses `uncertain` too. The
claim of the re-run (`_claim_rerun`) also leaves out a stored result whose
`extras.execution_outcome` is `uncertain`, in its SQL.

The summary of such a run (`resume_outcome`, see below) carries the advice
as its `guidance`, because a timeout, a failed audit write and a restart
store no guidance stamp. `POST /decide` answers
`dispatch_execution_outcome: "uncertain"` next to `dispatch_status` and
`dispatch_error`, so a client that reads only the decide answer (`meho
approvals approve --in-order`) can tell this run from a plain failure
without a second read.

**Restart recovery.** At start-up (`main.py`, after the release check),
`recover_interrupted_approvals` gives every `approved` request that has
`resumed_at` set and no result a stored `uncertain` result and one audit row
(`path` `approval.recovery`). It runs nothing. It is safe on a one-machine
install because the old pod is stopped first and its running work drained,
so a request that is `started` with no result cannot still be running. An
install with several pods needs a lease (a time-limited claim that shows a
run is alive) before it may use this step.

**What the approver can check.** The approver gets the request itself (the
operation, the target and the params the approval named), the audit row of
a recovery, and the advice to check the target. MEHO keeps no vendor id
(for example a job id) from a lost or unauditable answer: the vendor answer
is dropped on purpose when no audit record can be written, and nothing may
leave MEHO without one. A timeout has no vendor answer at all.

**Paths that start a change.** The approved path has a lasting record before
the vendor call (the `resumed_at` claim). An async run has a lasting
`operation_run` row. A change that runs at once with no approval (a standing
grant for a service principal, or a safe or auto-approved call) has no
record before the vendor call: its first record is the audit row written
after the call. That gap is open. A restart in the middle of such a call
leaves no trace, and the caller sees the dropped connection.

#### The "outcome unknown" list and "Mark as checked" (#5026)

Someone must look at an approved change whose outcome MEHO does not know.
The "outcome unknown" list shows these changes, and a person records what
they found on the target. Code: `operations/approval_outcome_check.py`.

**Who is on the list.** An `approved` request where either:

- its stored result is `uncertain` (`reason` `uncertain`), or
- its latest run started more than
  `APPROVAL_OUTCOME_UNKNOWN_AFTER_MINUTES` minutes ago (default 15, from 1
  to 1440) and stored no result (`reason` `started_no_result`). A run that
  started less than that ago is not listed: it may still be running.

and no check covers it (see below).

**When the latest run started.** The first run starts at `resumed_at`. A
re-run (#5417) clears the stored result and sets `rerun_started_at` in the
same claim (migration `0142`). So the minutes count from
`rerun_started_at`, or from `resumed_at` when there was no re-run. A re-run
that just started is not listed, even when the first run was long ago.

A request that never ran, a run with a known result (`executed` or
`failed`), a request that is not `approved`, and a request of another
tenant are never listed. A long operation that is still running after the
set minutes is listed too. Marking such a run does not stop it.

**What a check covers.** A check records why the request was on the list
(`outcome_check_reason`) and when (`outcome_checked_at`). It keeps the
request off the list only while both are still true: the reason is the
same, and the check is not older than the start of the latest run. So the
request comes back on the list when:

- a person checked a run with no result, and that run later stores an
  `uncertain` result, or
- a person checked a run, that run later failed, and a re-run then has no
  result after the set minutes (or ends `uncertain`).

`list_outcome_unknown` is the one list rule (one SQL clause, with
`outcome_unknown` as its Python twin for one row). Every surface reads it,
so they show the same rows:

| Surface | How |
|---|---|
| REST | `GET /api/v1/approvals?outcome_unknown=true` (`status` is ignored) |
| MCP | `meho_approvals_list` with `outcome_unknown: true` (`status` is ignored) |
| CLI | `meho approvals list --outcome-unknown` (not with `--status`) |
| Console | the "Outcome unknown" tab of `/ui/approvals` |

Each row says what ran (`op_id`, `connector_id`), on which target
(`target_id`), when the latest run started (`outcome_unknown.run_started_at`:
the latest re-run, else the first run; the minutes count from this time),
who asked (`principal_sub`), who approved (`reviewed_by`) and what to check.
The REST and MCP views carry `outcome_unknown: {reason, advice,
run_started_at}` on every listed request (and on show), `null` on all
others. `resume_outcome.resumed_at` stays the start of the first run. The
console row carries a second line with the target (its name in this tenant,
else its id), the start of the latest run and the advice; the console
modal's "Run started" is the start of the latest run too
(`run_started_at` in `ui/routes/approvals/render.py`). The advice of an
`uncertain` run is the same advice as above. A run with no result gets: "The
run started and stored no result. MEHO does not know if the change
happened. Do not run it again. Check the target first."

**Mark as checked.** `POST /api/v1/approvals/{id}/outcome-check` with
`{"verdict": "happened" | "did_not_happen", "note": "..."}`, `meho
approvals mark-checked <id> --outcome happened|did-not-happen --note TEXT`,
or the form in the console modal (`POST /ui/approvals/{id}/outcome-check`).
`mark_outcome_checked`:

- needs a person who may decide approvals (the same rule as approve and
  reject: `_check_reviewer_role`, and the REST route is human-only). No
  fresh sign-in is needed: nothing runs.
- needs a note (1 to 2000 characters, not only spaces).
- locks the row and refuses a request whose outcome is known (409
  `outcome_not_unknown`) or that a check already covers (409
  `outcome_already_checked`).
- sets six columns on the request (`outcome_checked_at`,
  `outcome_checked_by`, `outcome_checked_by_name`, `outcome_check_verdict`,
  `outcome_check_note`, `outcome_check_reason`, migration `0142`). A new
  check replaces an older one that no longer covers the request; the audit
  row of the older check stays. It writes one audit row: `path`
  `approval.outcome_check`, class `approval_decision`, status 200, with
  `verdict`, `note`, `outcome_unknown_reason`, `resumed_at`, `rerun_count`
  and `run_started_at` in the payload.
- keeps the stored result (`resume_result`) and `result_state` as they
  were, and never runs anything. An approved request starts only once, so
  the check cannot start it again.

REST and MCP show the check as `outcome_check: {verdict, note, checked_by,
checked_by_name, checked_at}`. `meho approvals show` prints `Checked:`,
`Check note:`, `Checked by:` and `Checked at:`. The console shows a
"checked: it happened" or "checked: it did not happen" badge in the list
and the check in the modal. A checked request leaves the list until the
check no longer covers it (see "What a check covers").

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
| `execution_outcome` | `uncertain` when MEHO does not know if the change happened (#5025). `null` for any other run. |
| `exit_code` | The exit code of a finished command or script, when the `ok` result body has an integer `exit_code` (for example `linux.script.run`). `result_status` stays `ok` because MEHO ran the script, so read this field for the script's own result. A non-zero code shows in `meho approvals show` ("the script ran, but it failed"), in the console card after Approve and in the dialog. `null` when the result has no exit code. The MCP tools `meho_approvals_list` and `meho_approvals_get` send this field only with the switch `MCP_APPROVAL_EXIT_CODE` on (off by default); with it off their answer is the same as before. |
| `guidance` | The next step a failed multi-step operation names (#5377), for example "The VM waits on question _vmx1: show it to the user and answer it with vmware.vm.question.answer." For an `uncertain` run without such a stamp, MEHO's fixed advice: "MEHO does not know if the change happened. Do not run it again. Check the target first." `null` for any other run. |

`resume_outcome` is `null` for a request that is not approved, and before
the run starts. The result body and the free-text `error` stay on the
owner-only `/result` read: an odd or missing field reads as `null`, never
raises.

**Where `guidance` comes from (#5377).** A multi-step operation can end with
its own error answer (#3809): a `result` body with an `error` issue, whose
`status` word (for example `result_unknown`) the dispatcher copies into
`extras.error_code`. So `error_code` already carries that word. When the
approval store writes the result on the row (`persist_resume_result`), it
stamps the body's `guidance` text into `extras.approval_guidance`
(`with_approval_guidance` in `operations/approval_outcome.py`). The summary
reads only that stamp, never the body. The stamp is written only when all
of these hold:

- the run did not end `ok`;
- the body's `status` equals `error_code`, so the body is MEHO's own error
  answer, not a vendor answer;
- neither the run's op nor the parked op is in a secret class
  (`credential_read`, `credential_mint`, `credential_write`).

So no param and no vendor text of an operation that sends or reads a secret
reaches the summary (the rule of #4854). For other operations the text is
MEHO's own words: a multi-step operation keeps a vendor error in its
`issues`, never in its `guidance`, because every approver reads the
guidance. Like every error result, the body has already passed the
dispatcher's secret cleanup (`redact_error_result`). The text is cut at 500
characters. For
`vmware.composite.vm.device.cdrom`, the text names the running task ids and
the question id; the question text and any read error stay in the body
(`vm_question`, `tasks_error`).

A result stored before the stamp existed has no `extras.approval_guidance`,
so its summary has no `guidance`. This matters after an upgrade: an older
connector could copy vendor error text into the body's `guidance` (the VM
resize step did), and that text must not reach the list and show views.
It stays on the owner-only `/result` read. The card after an approve in
the console stamps the fresh result the same way before it reads the
summary.

Where it shows:

- **REST** `GET /api/v1/approvals` and `GET /api/v1/approvals/{id}`: the
  `resume_outcome` field.
- **MCP** `meho_approvals_list` / `meho_approvals_get`: `result_state` and
  `resume_outcome`, same shape.
- **CLI** `meho approvals list`: a `RUN` column (`not-run`, `started`,
  `ok`, `FAILED`, or `-` when the request is not approved). `meho approvals
  show` and `approve`: `Run:`, `Run started:`, `Run result:`, `Error code:`,
  `Next step:` (the `guidance`, when set; for an `UNCERTAIN` run one
  `Warning:` line with the advice instead) and `Run audit:` lines, with the
  `meho audit show <id>` command for the audit row. `approve` reads the
  request again after the decision, so it prints the same summary as
  `show`.
- **Console** `/ui/approvals`: a run badge next to the `approved` pill in
  the history list (`ran OK`, `run failed`, `outcome unknown`, ...). The
  detail modal of an uncertain run shows a warning banner with the advice
  "Do not run it again. Check the target first.", and its card after an
  approve says "Approved, but the outcome is unknown". The detail modal of a
  failed run shows a red banner with the error code and a link to the audit
  row (`/ui/audit?audit_id=<id>`). The card after an approve says "Approved,
  but the run failed" when the run returned an error. Both show a "Next
  step" line with the `guidance`, when set. When another resumer
  (usually the waiting requester) won the run claim (`already_resumed`),
  the card reads the request again and shows its stored result: the same
  refused or failed card, or the normal "Request approved" card when the
  run landed. No stored result yet gives an "Approved, the run is still
  going" card that names the approvals history as the place where the
  result will show. The card never says the operation was re-dispatched
  when another caller may have refused it.

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
MCP, the `guidance` stamp rules, a credential operation with no vendor
text, and a result stored before the stamp that shows no guidance),
`tests/test_uncertain_outcome.py` (the shared rule, the timeout rule and the
restart recovery), `tests/test_uncertain_outcome_any_connection.py` (an SSH
timeout or lost connection of a change, the re-run refusal and its SQL, a
failed secret lookup, a read that times out),
`tests/test_uncertain_outcome_vault_and_steps.py` (a Vault change that loses
its answer, a secret read that times out, a wrapped timeout and the cause
chain, and a failed multi-step run with an uncertain step), `tests/test_ui_approvals.py` (run badge, failed-run modal,
uncertain-run badge, modal and card, failed approve card, the "Next step" line, and no "Next step" for an older stored result),
`tests/test_connectors_vmware_rest_typed_vm_question.py` (a CD-ROM change
that times out on a VM question names the question id in the summary) and
`cli/internal/cmd/approvals/approvals_test.go`.

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
  `register_preview_builder_switch(op_id, enabled)` (core only, not in
  the connector kit) puts the builder behind a switch for the caller's
  side only: with `enabled()` false, the preview the caller gets back
  (`preview_operation`, `PreviewContext.agent_preview` true) is the one of an
  op with no builder. The approval request a person reads always uses the
  builder. A new card for an op that agents already see goes behind a
  switch this way.
  `register_person_only_preview_keys(op_id, enabled, keys)` (core only)
  names the keys a builder adds for the person only. The MCP tools
  `meho_approvals_get` and `meho_approvals_list` read the stored request
  through `proposed_effect_for_agent`: with the switch off, an op behind
  a builder switch shows the generic params echo of its stored params,
  and an op with person-only keys shows its preview without them.
  `register_agent_preview_filter(op_id, filter)` gives a caller the
  stored preview as the filter returns it, for fields deeper than the
  top-level keys; it runs after the person-only keys are gone, so an op
  can have both. So the caller gets the same answer there as from
  `preview_operation`. The console shows the stored card, and so does the
  REST approvals API for a person. For an agent, service or runner login
  the REST API leaves out the top-level person-only keys (for example
  `credential_wait`, the line that says why a credential call waits), and
  for `linux.script.run` and `k8s.apply` it gives the same view as the MCP
  tools (`api/v1/approvals.py::_proposed_effect_for`).
- `register_preview_last_step(op_id, step)` runs `step(ctx, preview)` on
  every preview the builder of `op_id` returns, whatever module
  registered the builder and when. A step that raises makes the preview
  unavailable, the same as a builder that raises.
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
   bespoke builder collapses to the identifier-only default. For an
   ingested op, the class stored on its row by a connector floor counts
   too (`resolve_op_class`): a stored `credential_read` hides the
   parameters, and the request and its audit row carry that class
   ([spec-ingestion.md](spec-ingestion.md#a-floor-sees-the-answer-and-sets-the-op-class)). A **bespoke
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
reviewer reads. The builder also adds `manifest` (the applied manifest,
one dict per document) and `subject` (the objects it touches), so the
approver sees what the apply sets, not only which objects it touches
(#3877). Secret `data` / `stringData` values, every `env` value, every
`httpHeaders` value of a probe or lifecycle hook, every `command` / `args`
list (only its size in bytes stays) and the
`kubectl.kubernetes.io/last-applied-configuration` annotation are hidden
first, at any depth (`connectors/kubernetes/ops_write_preview.py`), then
the echo goes through the same two-pass redaction as the generic params
echo. The echo keeps at most 16 KB in total, counting every entry: a
document that does not fit in full keeps only its identity, and when not
even that fits, one closing entry counts the documents left out. When the
API server rejects the dry-run (for example an
admission webhook denial), the handler raises `ApplyRejectedError` and the
row gets the `preview_unavailable` marker with the server's message in
`preview_error`, so the reviewer sees that the apply will be rejected
(#3876). The ArgoCD write ops register their own builders the same
way (#1452): `argocd.app.set` / `argocd.appproject.update` populate
`{before_spec, after_spec}` and `argocd.app.delete` populates
`{cascade_resources}` from read-only GETs against the live
Application / AppProject — wired in
`connectors/argocd/ops_write_preview.py`. The `secret.move` broker op
populates a ref-only `{action, source, sink, merge}` summary with no store
I/O (#1580, `connectors/secret/move_preview.py`). `source` and `sink` are
`{kind, ref}`; a `vault-target` side also has `target`, the name of the
Vault target whose role does that read or write. `merge` is `true` when
the sink keeps the other fields of the secret. The 8 vmware write
composites register builders in
`connectors/vmware_rest/composites/_write_preview.py` (#1608): the
fan-out composites (`vm.power.bulk`, `host.evacuate`,
`host.detach_from_vds`, `cluster.patch`) resolve their entity set via
the same read-only listing helpers the handlers use — `{..., resolved,
total_resolved}` with the list capped — and the single-entity
composites echo their params; see
`connectors-vmware-rest.md` (in the private connectors repository), section "Park-time
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

For a person the `k8s.apply` preview also has `run_commands`: one entry
per container (init, main and ephemeral, at any depth, and every other
object with a `command` or `args` list, under any field, such as the
`container`, `script` or `steps` of a custom kind) with the object,
the container name, the image, the `command` and `args`, and in
`exec_commands` the `exec` command of each lifecycle hook (`postStart`,
`preStop`) and exec probe (`startupProbe`, `livenessProbe`,
`readinessProbe`), with secret values hidden by the rules of the Job
preview. The list keeps at most 16 KB and always keeps room for the
name of every later container: an entry that is too large keeps
the object, the container and the image, the start of what it runs in
`command_cut`, and a `cut` note. The `manifest` echo above shows only the
size of each command line. When the dry run fails, the stored request has the
`preview_unavailable` marker and, for a person, a top-level `run_commands`
built from the manifest text (`register_preview_failure_keys` in
`operations/_preview.py`). The console card shows that list in a "What
each container runs" block under the warning that the preview failed
(`approvals/_modal.html`). An agent never reads `run_commands`: its own
preview has no such key, and the stored request it reads (MCP tools and
the REST API) leaves it out (`PERSON_ONLY_EFFECT_KEYS`).

## Credential writes: what the approver sees (#4850)

A credential write is an op that `classify_op` (`broadcast/events.py`)
calls `credential_write`; a connector declares that class in its own
`op_classes.py`. When it waits for
approval, the generic echo of params is turned off (invariant 3). So the
approver sees only what a bespoke preview builder shows. Without one,
the approver sees the op id, the target and the reason
`credential_write_redacted`, and does not know which account, secret or
host changes.

Every credential write that can wait for approval now has a builder.
Each builder shows what changes and never a secret value. These nine
got one in #4850. `k8s.secret.update` and `secret.handoff.import` came
later, each with its own builder
(`connectors/kubernetes/ops_secret_write_preview.py`,
`connectors/secret/handoff_preview.py`). So did `keycloak.realm.update`
(`connectors/keycloak/ops_write_realm_smtp.py`):

| Op | Who waits for approval | The approver sees | Never shown |
|---|---|---|---|
| `keycloak.user.reset_password` | every call | user (`username` / `id`), realm, Vault path of the new password (`password_secret_ref`, mount, field), `temporary` | the password |
| `keycloak.realm.update` | every call | realm, the requested realm fields (`representation`), the reference of the SMTP password (`smtp_password_secret_ref`) | any secret-named value; an SMTP password given inline is refused before the wait |
| `vault.auth.userpass.update_password` | every call | mount, username, `sets_password` | the password |
| `vault.auth.userpass.write` | every call | mount, username, `sets_password`, `token_policies` and whether they are replaced or kept | the password |
| `k8s.secret.create` | every call | name, namespace, type, all key names, inline key names, the `labels` and `annotations` maps, the `*_secret_ref` store paths per key, the `*_secret_mount` store mounts | any value |
| `k8s.secret.update` | every call | name, namespace, `mode`, the key names that are set, inline key names, the `*_secret_ref` store paths per key, the `*_secret_mount` store mounts, the key names the Secret has now, in `replace` mode the key names that are removed | any value |
| `k8s.job.create` | every call | name, namespace and the whole Job `spec` as it will be created: Job settings (`backoffLimit`, ...), node choice (`nodeName`, `nodeSelector`, `tolerations`, `affinity`), `hostPID` / `hostNetwork` / `hostIPC`, service account, pod and container `securityContext`, image, `command`, `args`, `volumes`, `volumeMounts`, env var names, `envFrom` and `valueFrom` references (Secret or ConfigMap name and key) | every inline `env` value and probe `httpHeaders` value (the name stays); on `command` and `args`, read as one command line: the value of a `NAME=VALUE` or `--name=value` whose name holds a secret word (`PGPASSWORD=...`, `--password=...`, any length), the argument after a secret flag (`--password x`, `-p x`; also across the end of `command`), the password in `user:password` after `-u` / `--user`, the password in a URL, tokens with a known vendor prefix (`ghp_...`). A list item is one argument of the program, so such a value runs to the end of the item, spaces and shell operators included (`PGPASSWORD=x;tail` is hidden whole); only in the item after `-c` (alone or in a short option group such as `-ec` or `-lc`), a shell command string, is the value one shell word, quotes and escapes included, and an unquoted shell operator such as `;` or `&&` ends it, so the commands after it stay visible; secret-shaped text anywhere in the spec (JWTs, bearer tokens); the value of a field with a secret-shaped name (a `secret` volume, `imagePullSecrets`). A secret on a command line with no known shape is shown. |
| `linux.script.run` | every call | target, interpreter, the script text (page 1 of the pages, 64 KiB each, with the page count) with its size and SHA-256, the reviewed copy (`script_ref`) and whether the script matches `expected_sha256`, argument byte size, each `env` name with its value and a line that secrets belong in `secret_env` (both for a person only: reads by an agent or a token get the `env` names), the `secret_env` references (`path#field`), working directory, `use_sudo`, timeout ([`connectors-linux.md`](connectors-linux.md)) | the argument values and the `secret_env` values; in the script text, values that look like secrets, with the command-line rules of `k8s.job.create` and two changes: `-p` hides its value only for `sshpass`, `mysql*`, `mariadb*`, `mongo*` (`mongosh`, `mongodump`) and a command with the word `login` (`docker login`, `podman login`, `helm registry login`), also glued (`mysql -pVALUE`), so `mkdir -p`, `cp -p`, `ssh -p 22` and `docker run -p 8080:80` stay as they are; and a password glued to `-u` (`curl -uuser:password`) is hidden too (then the SHA-256 is left out too) |
| `sddc.host.validate` | an agent's call | host count; per host: `fqdn`, `username`, `storageType`, `networkPoolId` | the ESXi password and any other spec field |
| `sddc.host.commission` | every call | the same as `sddc.host.validate` | the same |
| `holodeck.router.patch` | every call | HoloRouter version, whether the call only checks (`verify_only`, a 9.1 router), the patch ids and the module file each one edits | nothing secret exists; the op takes no params |
| `secret.handoff.import` | every call | the store target name (the other instance's Vault), the sink `{kind, ref}`, `expect_fingerprint`, the reason | the one-time handoff token (`connectors/secret/handoff_preview.py`) |
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
`holodeck/router_patch_preview.py`,
`vmware_rest/composites/_guest_file_read_preview.py` and
`secret/handoff_preview.py`.

**CI keeps it that way.** `tests/test_credential_write_preview_conformance.py`
runs every typed-op registrar. It takes each registered op that
classifies `credential_write` and can wait for approval (`requires_approval`,
or a `safety_level` above `safe`), and fails when the op has no builder.
An op that must stay without a preview goes into
`_NO_PREVIEW_CREDENTIAL_WRITES` in that test, with the reason. The list
is empty today. Two credential writes have no builder and are not in the
sweep: `vmware.composite.vm.guest.env.read` and
`vmware.composite.vm.guest.process.list`. They are `safe`, so no call of
theirs waits for approval by default.

## Operator overrides and the fresh sign-in check (#4711)

The `safety_level` and `requires_approval` a dispatch sees are the
*effective* values: the classification in code or in the API
description, made stricter by operator overrides
(`operation_policy_override`; see `spec-ingestion.md`, "Operator
overrides of risk level and approval"). `lookup_descriptor` applies the
overrides, so the approval gate, the preview and the parked request all
use the same values. A destructive operation always waits for approval,
whatever the overrides say. The one exception is a test install in
sandbox mode (see "Sandbox mode" below).

Turning approval off for an operation is the one loosening edit. It
also ends the per-call wait of an operation that a connector marked with
`declare_per_call_approval`. After it, a person in the web console runs a
`caution` operation at once, a `dangerous` operation still waits because of its
risk level, and a standing grant or a run approval still does not cover the
operation. The edit is refused for a destructive operation (`approval_off_destructive`) and for a change of rights (`approval_off_rights_change`).
An operation that writes or makes a credential, for example `harbor.robot.create`
or `pfsense.openvpn.user.create`, keeps its approval: the edit does not end its
wait. It
needs `platform_admin`, a written reason, and a person. This holds for
every edit that ends approval, also removing an override that required
it (`meho connector remove-override`, `DELETE
/api/v1/connectors/{id}/operation-overrides/{op_id}`), and also when
approval ends only for a tenant's own copy of a built-in operation that
an install-wide override reached: in the console
the web session is the person (the same rule as an approval click); on
REST it is the **fresh sign-in check**,
`auth/fresh_sign_in.py:has_fresh_sign_in`:

* the caller is a human principal (`PrincipalKind.USER`); agents,
  service principals and runners always fail;
* the token's `auth_time` claim (now on `Operator.auth_time`) is at
  most `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS` old (default `300`, from 30
  to 3600; `get_settings()` reads the environment variable, and the same
  value sets the check and the `max_age` in the challenge). This is the
  limit for an action that needs a fresh sign-in each time (#5515). The
  approval window `FRESH_SIGN_IN_MAX_AGE_SECONDS` never covers it. A
  token refresh keeps `auth_time` (RFC 9470 section 6.1), so a
  long-lived refreshed CLI token does not pass; a token without
  `auth_time` fails.

A failed check answers `401` with `WWW-Authenticate: Bearer
error="insufficient_user_authentication", max_age=<N>`
(`fresh_sign_in_challenge`, RFC 9470 section 3). The CLI prints the
message and says to run `meho login` again. The MCP edit tool never
loosens, so this check has no MCP path. The check is shared: the
approval decision on CLI and REST (#3979) reuses it with the approval
window as its limit (`has_fresh_sign_in(..., max_age_seconds=...)`). Two edits of
the same operation cannot slip past the check together: edits and
removals of one operation run one at a time, across all scopes, under
a per-operation lock (see `spec-ingestion.md`, "Edits of one operation
run one at a time").

## The passkey touch level in Keycloak (#6213)

A later step lets MEHO ask for a passkey touch before a `dangerous` or
`destructive` approval (setting `APPROVAL_PRESENCE_TOUCH`, off by
default). This section is the Keycloak part. It is inert until MEHO
asks for the touch level; nothing in the backplane reads it yet.

* **Realm file** (`deploy/charts/meho/charts/keycloak/files/meho-realm.json`):
  the realm attribute `acr.loa.map` maps `meho-touch` to level 3 (level 2
  is `meho-approve`, the approval sign-in level of #6281; a higher level
  also counts where a lower one is asked for). The
  login flow `meho approval touch` is the browser flow of `meho-web`
  only. Its level 1 branch has the steps of Keycloak's own browser
  flow (cookie, company login redirect, password form that also takes
  a passkey, one-time code unless a passkey was used), so the normal
  console login does not change. Its level 3 branch is
  `conditional-level-of-authentication` (level 3, maximum age 0) plus
  `webauthn-authenticator-passwordless`. The passkey step's setting has
  `default.reference.value=hwk` and `default.reference.maxAge=120`;
  without the maximum age Keycloak leaves `amr` empty. `meho-web` has
  the `amr` mapper (`oidc-amr-mapper`).
* **How MEHO asks:** `acr_values=meho-touch` and a `nonce` on the
  console's authorization request. Not `max_age=0`: that asks for the
  password again. The ID token then has `acr=meho-touch`, the same
  `nonce` and a fresh `auth_time`. MEHO must check `acr`, `nonce` and
  `auth_time`, never `amr` alone: `amr` can still name the passkey on a
  plain login shortly after a touch.
* **Running realms:** the chart's realm stage
  (`core/keycloak_flows.py`) does not make a flow whose steps need
  settings, and binds no client to it. A running realm gets the flow
  through `keycloak.authflow.ensure_approval_touch`
  (`connectors/keycloak/ops_authflow_touch.py`, risk level `caution`,
  waits for approval). A customer's own Keycloak gets it from
  `meho admin keycloak bootstrap-clients`
  (`cli/internal/cmd/admin/keycloak/touchflow.go`). The three keep the
  same flow; tests compare each with the realm file. The operation and
  the CLI bind the flow only when the realm's passkey policy asks for
  user verification (`webAuthnPolicyPasswordlessUserVerificationRequirement`
  is `required`): otherwise a touch could pass without a PIN or a
  fingerprint. They also turn on the token settings of an `amr` mapper
  that leaves the claim out of the ID token. They do not bind it when
  the realm's browser flow is not `browser` or `browser with passkeys`,
  when `browser with passkeys` has other steps or settings than
  `browser` plus the passkey step, or when `meho-web` has a browser flow
  of its own: level 1 is a copy of the built-in login, so binding would
  drop that login's steps. The operation binds it anyway with
  `replace_custom_login: true`. A client bound to the touch flow
  already stays bound on later runs.
* **Company login:** the realm stage sets the redirect default of the
  company login on the realm's browser flow and on `meho approval
  touch` (`core/company_login.py`), so the console still goes straight
  to the company login when `autoRedirect` is on. The operation
  `keycloak.authflow.ensure_approval_touch` and `meho admin keycloak
  bootstrap-clients` copy that default from the
  realm's browser flow onto the touch flow before they bind `meho-web`,
  so a running realm keeps the redirect before the next upgrade too.
  When the realm's flow no longer has that default, they remove it from
  the touch flow too.
* **Tests:** `tests/test_core_keycloak_realm.py` (the file),
  `tests/test_connectors_keycloak_authflow_touch.py` (the operation),
  `tests/integration/test_core_keycloak_touch_flow.py` (a real
  Keycloak with a software passkey: the touch, the `nonce`, a new touch
  each time, `max_age=0` against `acr_values`, `amr` after a touch and
  without a maximum age).

## Passkey touch: the challenge row and the binding hash (#6214)

A `dangerous` or `destructive` approval can need a passkey touch (Face
ID, Touch ID, Windows Hello or a hardware key). Keycloak checks the
touch. MEHO binds the touch to the request with a hash of what the
request will run. This part ships the row and the hash. Nothing uses
them while the `APPROVAL_PRESENCE_TOUCH` switch is off (the default).

Code: `operations/approval_presence.py`. Table:
`approval_presence_challenge` (migration `0157`).

**The row.** When a person clicks Approve, `start_touch(...)` makes one
row:

| Column | What it holds |
|---|---|
| `tenant_id`, `approver_sub` | The tenant and the person. Only this person, in this tenant, can use the row. |
| `request_ids` | The requests the touch approves, in the order the person saw them (a JSON list). |
| `card_hash` | SHA-256 over the text of the approval card. |
| `server_random` | 32 random bytes (hex), made by MEHO for this row. |
| `binding_hash` | SHA-256 over the binding encoding (below). MEHO sends it to Keycloak as the `nonce`. |
| `match_code` | 6 characters from the binding hash. |
| `created_at`, `expires_at` | `expires_at` = `created_at` + `APPROVAL_PRESENCE_TOUCH_VALID_SECONDS` (default 120, from 30 to 600). |
| `used_at` | When an approve used the row. Set once. |
| `channel` | `web`, `cli_link` or `rest_link`. |
| `session_kind` | What kind of session started the touch. |

Each request must be `pending` and in the person's tenant, or
`start_touch` refuses (`ApprovalNotFoundError`,
`ApprovalRequestAlreadyDecidedError`). Two touches for one request get
different binding hashes, because each row has its own random bytes.

**The card.** `approval_card_text(requests)` builds the card from the
stored request rows at decision time, never from what the caller sent:
for each request the operation (`connector_id`, `op_id`), the target
id, the risk level (`proposed_effect.safety_level`) and the effect
(`proposed_effect`, which holds the parameters as the card shows them).
The text is canonical JSON (sorted keys, no spaces).

**The binding hash.** `compute_binding_hash(...)` is SHA-256 over one
canonical JSON text with: the algorithm name
`sha256/meho-touch-binding-v1`, the tenant id, for each request (in
order) its `id`, `params_hash`, `preview_hash`, `target_hash` and
`release_id`, the `card_hash`, and the `server_random`. A change of the
encoding gets a new algorithm name.

**The match code.** The first 30 bits of the binding hash, as 6
characters of `23456789ABCDEFGHJKLMNPQRSTUVWXYZ` (32 characters, no 0,
1, I or O, so no two look alike). The person sees it in MEHO before the
touch.

**Using the row.** When the person comes back, `use_touch(session,
challenge_id, operator=..., nonce=...)` checks, in this order, and
refuses with `TouchChallengeError` (a `reason` and one plain sentence):

1. `not_found`: no row, or the row of another person or another tenant.
2. `used`: the row was used before.
3. `expired`: the valid time has passed.
4. `nonce_mismatch`: the `nonce` of the touch's ID token is not the
   binding hash of the row.
5. `request_changed`: MEHO builds the card hash and the binding hash
   again from the requests as they are now, with the row's random
   bytes. When any hash differs, or a request is gone: "The request
   changed after you started the passkey touch. Click Approve again to
   see it as it is now."

Then it marks the row used. Two approves can use one row at the same
moment, on two replicas. `use_touch` first takes the transaction-level
advisory lock of the row through `take_approval_lock` in
`approval_queue.py`, the same helper that guards the reuse of a sign-in.
The mark itself is a conditional `UPDATE ... WHERE used_at IS NULL`, so
exactly one approve wins also on a database without the lock. The caller
commits; a rollback leaves the row unused.

**The audit row.** `ApprovalAuth` has two optional fields:
`presence` (`TouchPresence`: `acr`, `amr`, `auth_time` from the touch's
ID token) and `binding` (`TouchBinding`: `algorithm`, `hash`,
`request_ids`, `card_hash`, `challenge_id`; `touch_binding(row)` builds
it). `audit_payload` writes `"presence"` and `"binding"` only when they
are set, so an approve without a touch writes the same block as before.

**Clean-up.** A loop that the app starts (`start_presence_challenge_cleanup`)
first runs one minute after start (`FIRST_RUN_DELAY_SECONDS`), then once a
day. So a deployment that restarts often still cleans the table. Each run
deletes rows that were never used and are older than 14 days. Used rows
stay: the audit row names them by `challenge_id`. The table has no foreign
key to the tenant, so rows of a deleted tenant stay until this clean-up
removes them (used rows stay for good). The rows hold only hashes.

## Catalog `safety_level` on every envelope (#1855)

The per-op preview hook is opt-in, so before #1855 a parked op with no
registered builder carried only `{op_id, connector_id, target_id}` — a
reviewer could not tell a `dangerous` op (e.g. `keycloak.realm.create`)
from a `caution` op (e.g. `keycloak.realm.update`) on the row alone.

`dispatcher._build_proposed_effect` now stamps the catalog
`descriptor.safety_level` onto **every** parked op's `proposed_effect`,
alongside `op_class` / `preview` (and `permission_preflight` when that
hook fired). The value (`safe` / `caution` / `dangerous` /
`destructive`) is read
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

**Category, labels and classification rule (#6041).** While the switch
`POLICY_CLASSIFICATION` is on, the same seam also stamps the operation's
classification (`approval_classification` in `operations/op_policy.py`):

- `category` — for example `network`; `null` when two classification
  rules disagree;
- `labels` — a map of free `key=value` text;
- `classification_rule` — the classification entry that set them, for
  example `tenant:classifications[2]` or `taxonomy.default`.

The values come from the looked-up row (`policy_category`,
`policy_labels`, `policy_rule_ref`, set by `apply_effective_policy`, and
by `apply_step_policy` for a step of a multi-step operation).
So an approver sees at once what kind of change waits. The console
modal shows the category as a badge next to the risk level, and the
labels and rule below it (`data-testid="approval-classification"`).
`meho approvals show` prints `Category:`, `Labels:` and `Rule:` lines
after `Operation:`. With the switch off, none of the three keys is on
the request, and both surfaces show nothing new. The values are stored when
the request is made, so a later edit of the classification does not
change the card of an older request.

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
(`dispatch(..., _approved=True)`) skips the policy gate. Right before the
run, every resume path builds the preview again and compares its hash
(`refuse_resume_if_preview_changed`, see
[An approval covers one target and one preview](#an-approval-covers-one-target-and-one-preview)).

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
not this task). That path still refuses a destructive step on its own: it
calls `_destructive_binding_refusal` with no preview hash, so the step is
refused and no approval row is written. A person's approval of the whole
multi-step run does not cover a destructive step
(`_covered_by_approved_run`).

## First governed delete: `vmware.composite.vm.destroy` (#3198)

Requirement 1 of
[`docs/decisions/governed-delete-operations.md`](../decisions/governed-delete-operations.md)
— one real delete family modeled into the tier end-to-end. The op is
`vmware.composite.vm.destroy` (`safety_level="destructive"`,
`requires_approval=True`; the dual-arm handler + blast-radius preview
builder are documented in
`connectors-vmware-rest.md`, in the private connectors repository). Wiring it in
surfaced — and this task closed — three gaps in the tier's *human*-side
enforcement that #3196/#3197 left, plus one machinery limitation:

- **A composite/typed op is now previewable when it is `destructive`
  (#3198) or `requires_approval` (#3312).**
  `preview_dispatch` returns `unavailable` for non-ingested ops (they have
  no single literal HTTP request), which would make the requirement-2 hash
  binding impossible on the composite surface. The gate
  (`_is_previewable` in `operations/_request_preview.py`, called from
  `_gate_and_validate`) now lets a governed-tier non-ingested op
  through to `build_composite_preview` (`operations/_composite_preview.py`),
  which binds the *logical request tuple*: the `redacted_body` slot holds a
  safe view of the params (`safe_params_view`), and a keyed digest of the
  full params rides a `params_digest` slot that `compute_preview_hash` adds
  to the hash when present. So the hash changes with every param value,
  while a `COMPOSITE` sentinel `method` + the `op_id` as
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
  is a safe view of the params (`safe_params_view`), not a place to carry a
  written secret at all — the same reason `build_proposed_effect` suppresses
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
  for every tier. The break-glass switch now never reaches a `dangerous`
  **or** `destructive` row (`_HIGH_RISK_TIERS`, decision requirement 1).
  #3198 scoped this to `destructive` only, but a delete-class op can
  legitimately register on the reversible `dangerous` tier (e.g. a KV
  secret-version soft-delete) while still being the four-eyes-critical
  action the requirement protects, so #3290 widened the refusal to cover
  it; `caution` / `safe` still honour break-glass. Since #3328 the
  group rules decide the rest: a MEHO-Admin may approve their own
  `dangerous` or `destructive` change (`approval_rule`
  `sa_operator_self_approval`), and another person's change at these
  levels needs a tenant admin (see
  [Who may approve, by group](#the-approval-groups-and-how-a-person-approves-3325)).
  A single-operator tenant makes its operator an Admin (MEHO-Admins).
- **Registration fail-fast.** `_register_in_session` (`operations/typed_register.py`)
  now rejects a `destructive` descriptor declared `requires_approval=False`
  at registration time — an inconsistent row can't exist. Belt-and-suspenders
  with the runtime park guarantee above.

Conformance for the op — an agent's request waits for a person like any
other destructive request (agent verdict `NEEDS_APPROVAL`, #3980), no standing
grant (`ServicePrincipalGrant` refuses), no break-glass self-approval
(only a MEHO-Admin may approve their own), no satellite mint
(`MintRefusalCode.OP_NOT_SAFE`), dispatch
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
`.sync` / `.rollback` / `.refresh` — are declared `write` by **exact op-id**
(today in `argocd/op_classes.py`) rather than added to the global write-suffix set. A `.set` /
`.sync` / `.rollback` suffix would classify the dotted argocd op-ids
correctly, but `_MEHO_FLAT_WRITE_SUFFIXES` is auto-derived from
`_WRITE_SUFFIXES` (`.` → `_`), so it would also relabel `meho_`-prefixed
MCP-tool ops such as `meho_broadcast_overrides_set` from `other` to
`write`; a `.refresh` suffix would likewise relabel the read-class
`topology.refresh` at render time. Exact-pinning the dotted argocd op-ids
avoids both collisions. See [`events.md`](events.md).

## Check before the wait for approval

A call that waits for approval is stored with its params as they are, so
a reviewer can read them and the approved call can run with them. Some
values must never be stored there, for example a secret that the caller
gave as a plain value. Name-based redaction does not know every such
field, so an op can turn on its own check.

- **Turn it on.** `_park_check.register_park_check(op_id, check)`. The
  check gets the same `PreviewContext` as a preview builder. It answers
  with the reason it refuses the call, or `None`. The reason is shown to
  the caller, so it must name no param value.
- **Where it runs.** `_park_check.park_check_refusal` runs it at every
  point where a call would wait for approval, before anything else:
  - the dispatcher, first in `_handle_needs_approval`;
  - the gate of a step of a multi-step operation
    (`composite.enforce_subop_policy`);
  - `preview_operation` (`_request_preview.preview_dispatch`), when the
    params pass the schema and the policy gate says the call would wait.
    So the preview gives the same answer as the call.
- **A refusal.** The call answers `status="denied"` with
  `error_code="refused_before_approval"` and the reason under
  `extras["reason"]`. The reason goes through the free-text redaction. MEHO
  stores no approval request and runs no handler, so nothing goes to the
  vendor. The call writes one `denied` audit row through
  `_audit_denied_and_return`, like the other refusals; the row has the
  params hash, not the params. The preview returns the same `error` and
  `extras` in a `status="error"` envelope and writes nothing.
- **A call that does not wait** (it runs at once) is not checked: the
  check guards only what is stored for approval.
- **Fail-closed.** A check that raises refuses the call. The reason and
  the log line name only the exception class: a traceback could show a
  param value, a login header or a vendor text.
- **Expired login.** The check runs before the dispatcher's
  session-recovery step. So `run_park_check` takes the same step: when the
  vendor answers an auth-class status and the connector has the
  `invalidate_session(target, rejected=...)` hook, the login is dropped and
  the check runs once more. A second rejection is the fail-closed refusal.
  A connector without the hook is not retried.
- **The connector resolves first.** A connector that does not resolve
  reaches the check as `connector_instance=None`; the check decides what
  that means.

Tests: `backend/tests/test_operations_park_check.py`.

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
policy, `connector-vault-policy.md#6-kv-v2-write-surface`). A preflight of
another vendor sets its own next step (`remediation`) and `doc_ref` in its
result; the text then names them instead of the Vault write policy. So no person
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

`target_warnings` is a list of plain lines about a setting of the target
that is allowed but risky, for example a Keycloak target that manages the
realm MEHO itself signs in with
(`targets/keycloak_realm_warnings.py`). The dispatcher stamps it onto the
card when there is at least one line, and the console modal shows it as a
"Check the target" box. For `targets.register` and `targets.update` the lines
come from the target as it will be after the write: an op registers a
builder with `register_card_warnings` (`operations/_preview.py`), and the
dispatcher adds its lines (`build_card_warnings`, fail-soft). So the approver
sees the warning before approving.

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

The audit row of the approved call names the reviewing operator in
`secret_read_as` (the login the secret store accepted), and `operator_sub` is
the reviewer too. The person who asked stays on the approval request row.

`k8s.secret.read_to_ref` (#3844) also registers a preflight. The op reads
a Kubernetes Secret and writes the value to Vault through the
`vault.kv.put` handler, so it gets the same `vault.kv.put` check on the
path it writes to: `secret/data/tenants/<tenant_id>/<register_as>`. Its
preview stays hidden (it is a credential op), but the check still runs,
so a write Vault would refuse is refused at park time instead of after
approval. See
[`docs/codebase/connectors-kubernetes.md`](connectors-kubernetes.md).

### The requester's policy on a second target (#3898)

Some ops write to a second target that the caller names in the params.
`vcfa.provider.api_token.create` is the first: it writes the minted token
through `vault.kv.patch` on the caller-named `vault_target`. The handler
calls the Vault handler directly, so the dispatcher's policy gate never
sees that write. One shared module checks it:
`operations/secondary_target_policy.py`.

**Execution runs as the reviewer. The requester's policy on the second
target is still enforced.** The operator approval surfaces (REST
`/approve` and `/decide`, the console approve) run the approved call as
the reviewer. Without this module, only the reviewer's policy would be
checked, and an agent's `deny` on the Vault target would be skipped.

How it works:

1. **At park time** the op's permission preflight calls
   `requester_policy_preflight`. It runs the policy gate for the requester
   on the second target and adds three keys to
   `proposed_effect["permission_preflight"]`:
   - `requester_denied` — `true` when the requester's policy says `deny`.
   - `requester_reason` — the `policy_denied: …` reason, or `null`.
   - `requester` — who asked: `sub`, `principal_kind`, `client_id`,
     `tenant_role`, `act`. The resume uses this to check them again.

   `needs-approval` counts as allowed: the approval being asked for
   satisfies it. A `requester_denied: true` does **not** refuse the park.
   The reviewer sees it on the card and should reject.
2. **On a reviewer's resume** `_dispatch_resume_with_bound_context` binds
   the requester for the run (`bind_approved_requester`). Who asked comes
   from the approval row (`principal_sub` / `principal_act`). The card's
   `requester` record supplies the rest (kind, role, client id), and only
   when its `sub` matches the row.
3. **In the handler** `secondary_target_refusal` checks the caller (the
   reviewer) as before, then the bound requester, **live**, with
   `decide_only=True` (no standing grant is marked used). A requester
   `deny` refuses before any appliance call:
   `policy_denied: requester '<sub>': <op> on <target label> '<name>': …`.
   A deny added while the request waited still stops it.

The agent's own resume (`call_operation_with_approval`) runs as the agent,
so the agent is the caller and the requester check adds nothing. Its audit row names the agent's own login id in
`secret_read_as`. When a
reviewer runs an opted-in op and the card has no `requester` record, or
its `sub` does not match the row, the run is refused before any appliance
call: `policy_denied: the requester's policy … cannot be checked … -- ask
again`. This covers a park whose preflight failed and a request parked
before #3898. Ops that do not opt in never read the binding.

**To opt an op in:** describe its second-target write as a
`SecondaryTargetWrite(connector_id, op_ids, target_label)`. Call
`requester_policy_preflight` from its registered permission preflight and
`secondary_target_refusal` from its handler, with the same value.

## Transports

### REST (`backend/src/meho_backplane/api/v1/approvals.py`)

**Access model — approve-only, decoupled from dispatch (#3243).** Every
row below except `/result` is gated by `require_approvals_access`
(`auth/rbac.py`), which admits a principal that is **either**
`operator`-or-higher on the linear tenant-role lattice **or** carries the
orthogonal `approver` capability (`Operator.approver`; MEHO does not read an
`approver` claim). MEHO keeps the flag in the code. No role gives it today;
roles that customers define come later. A `read_only` principal with
`approver=true` would clear a four-eyes gate here yet is denied
`POST /api/v1/operations/call` (still `require_role(TenantRole.OPERATOR)`,
and `read_only` ranks below `operator`), so "approve but never operate"
stays a rule of the role layer. The gate has a
service-layer twin (`_check_reviewer_role`) applying the same rule for
direct queue callers, and the `sub`-keyed self-approval refusal (#1401) is
unchanged — an approver cannot approve a request it parked. A refused
attempt is 403 `insufficient_role` + an `insufficient_approvals_access` log
line. The `/result` read is deliberately **not** on this gate: it is
principal-scoped on `principal_sub`, so an approver deciding a request it
did not park gains no result-read rights.

**Human-only on every transport.** `require_approvals_access`
gates on *role/capability*, not on *what authenticated the request* — and an
agent principal is minted with the `operator` role and the REST audience
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
on `require_approvals_access` alone — so a `service` principal (for
example an add-on's approval bridge) still lists the tenant queue. The gate removes
only machine kinds: the `approver` capability model (#3243) and the seniority
self-approval rules are preserved unchanged for **human** principals.

**Approve needs a fresh approval sign-in (#3979, #5515).** An approval is
a person's decision. On every surface the sign-in behind the approve must
be recent and must have asked for the credentials. On REST (and so on the
CLI) the token must prove it:

- `POST /approve`, and `POST /decide` with `decision=approved`, run
  `check_fresh_approval_sign_in` (`auth/approval_sign_in.py`; on
  `/approve` through the `require_fresh_approval_sign_in` dependency in
  `auth/rbac.py`). It runs after `require_human_principal`, so a machine
  principal (agent, service, runner) still gets its 403 first.
- **Only the approval client.** The token's `azp` claim
  (`Operator.azp`) must be the approval client,
  `KEYCLOAK_CLI_APPROVE_CLIENT_ID` (default `meho-cli-approve`, chart
  `config.keycloakCliApproveClientId`; the built-in realm has it), and
  the principal must be a person (`is_approval_sign_in`). Every other
  token gets the same 401 as an old sign-in, whatever its `auth_time`:
  a stored `meho login` token (`meho-cli`), a person's MCP token
  (`meho-mcp`), or any token without `azp`. An empty setting means no
  token approves; only the console can.
- **The approval window.** The token's `auth_time` claim
  (`Operator.auth_time`) must be no older than
  `FRESH_SIGN_IN_MAX_AGE_SECONDS` (default 900 = 15 minutes, chart
  `config.freshSignInMaxAgeSeconds`). It uses the shared
  `has_fresh_sign_in` in `auth/fresh_sign_in.py` (see
  [Operator overrides and the fresh sign-in check](#operator-overrides-and-the-fresh-sign-in-check-4711)).
  The window counts from the sign-in: a refreshed token keeps the time of
  the first sign-in (RFC 9470 §6.1), and approving does not move it. A
  token without `auth_time` is refused. Each risk level has its own
  limit (`approval_sign_in_max_age` in `auth/approval_sign_in.py`): the
  window for every request but a `destructive` one, which uses
  `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS` instead (see below). Neither
  limit caps the other: with a window of 120 seconds and the
  destructive limit at 300, a 180-second-old sign-in approves a
  destructive request and not a plain one. The route's dependency
  (`require_fresh_approval_sign_in`) runs before the request is read,
  so it refuses only a sign-in older than both limits; `approve_request`
  applies the limit of the request's risk level
  (`ApprovalSignInTooOldError`, mapped to 401 `fresh_sign_in_required:`
  with that limit as `max_age`).
- **A sign-in that asked for credentials.** The token's `acr` claim
  (`Operator.acr`) must be set and must not be `"0"`
  (`asked_for_credentials`). With no level-of-authentication mapping,
  Keycloak 26 gives `"1"` to a sign-in that asked for the password and
  `"0"` to one that did not (`AcrProtocolMapper`, from the client
  session; a refreshed token keeps it). The approval client's own login
  flow `meho approval sign-in level` has only the level step, the
  password form and the conditional one-time code step, so its tokens
  carry `meho-approve`. A token without `acr` is refused.
- **A passkey sign-in counts (#6006).** A passkey (WebAuthn) sign-in is
  not made from the session cookie, so Keycloak gives it `acr` `"1"` and
  a new `auth_time`, like a password sign-in. The checks need no passkey
  case: they never read `amr`. The password form of both the browser
  flow and `meho approval sign-in level` offers the passkey when the realm has
  passkeys on. Only the browser flow has Keycloak's step that skips the
  one-time code after a passkey, so on the command line a person with a
  code still types it. `tests/test_approval_passkey_sign_in.py` pins a
  passkey token on REST and in the console.
- **One sign-in, several approvals.** Inside the window one sign-in may
  approve several requests. Each approval is still bound to one request
  id, so to its stored operation, parameters, target and version; the
  parameter hash check (when `params` are sent) and the destructive
  preview binding stay.
- **A destructive request needs a sign-in of its own.** For a request
  whose `proposed_effect.safety_level` is `destructive`,
  `approve_request` runs `_check_sign_in_use` (`operations/approval_queue.py`):
  - the sign-in must be at most `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS` old
    (default 300), whatever the window is; else
    `ApprovalSignInTooOldError`, mapped to 401 `fresh_sign_in_required:`
    with `max_age=300`;
  - the sign-in must not have approved anything else; else
    `ApprovalSignInReusedError`, mapped to 401
    `fresh_sign_in_already_used:` with `max_age=300`;
  - after it, that sign-in approves nothing more: its audit row carries
    `"single_use": true`, and a later approve with the same sign-in gets
    the same `fresh_sign_in_already_used` 401.

  "The same sign-in" means an earlier `approval.decision` row of the same
  person, written since that sign-in, with the same `sign_in_key`: a hash
  of `sub`, `sid` and `auth_time`. The audit row keeps the hash, not the
  session id. The scan starts 60 seconds (`CLOCK_SKEW_SECONDS`, the
  allowed clock drift of the fresh sign-in check) before `auth_time`. On
  PostgreSQL the check first takes a transaction-level advisory lock on
  the sign-in key, so two approves with one sign-in at the same moment
  run one after the other
  (`tests/integration/test_approval_sign_in_once_pg.py`).
- A token whose `acr` is in `APPROVAL_ONE_TIME_CODE_ACR_VALUES` (empty =
  off) is recorded as a one-time code approval. It needs the same checks:
  an `acr` alone is not enough.
- The refusal is 401 `fresh_sign_in_required: <text>` with
  `WWW-Authenticate: Bearer error="insufficient_user_authentication",
  max_age=<N>` (the shared `fresh_sign_in_challenge`, RFC 9470 §3). The
  request stays pending.
- **In the web interface the console session's own sign-in is the
  proof.** `POST /ui/approvals/{id}/approve` runs
  `check_fresh_web_approval_sign_in` (`auth/approval_sign_in.py`) on the
  operator the console builds from the session's access token: the same
  `acr` check and the same age limits as REST, without the client check
  (the console got the token from its own `meho-web` login, and the
  browser never holds it). `approve_request` then applies the limit of
  the request's risk level and the destructive one-use rule, exactly as
  for REST (`ApprovalSignInTooOldError` / `ApprovalSignInReusedError`,
  mapped to 401 in `ui/routes/approvals/routes.py`). A refusal re-renders
  the detail modal with the banner "Sign in again to approve" and a link
  to `/ui/auth/login?return_to=/ui/approvals&fresh_sign_in=1`. That login
  carries `prompt=login` and `max_age=0` (`build_authorization_request`
  in `ui/auth/flow.py`), so Keycloak asks for the credentials also in a
  remembered browser; the new console session's token then has a fresh
  `auth_time` and `acr` `"1"`. When the session's sign-in is already too
  old for any approval, the modal shows "Sign in to approve" (the same
  link) in place of the Approve button (`approve_needs_sign_in` in
  `_render_detail_modal`); the POST checks again either way. Deny needs
  no new sign-in. The audit row carries `"channel": "web"`. The console
  session itself is unchanged: it ends after its own limits, and a plain
  console login still sends no `prompt`.
- Every approve writes `payload["approval_auth"]` on its
  `approval.decision` row: `{"channel": "rest" | "web", "method":
  "fresh_sign_in" | "one_time_code", "auth_time": <int>, "acr":
  <str|null>, "sign_in_key": <hash>}` plus `"single_use": true` for a
  destructive approve. Rows written before this rule carry `"method":
  "web_click"` with no `sign_in_key`. An approve with a passkey touch
  also has `"presence"` and `"binding"` (see "Passkey touch: the
  challenge row and the binding hash"). Without a touch the block is
  exactly as above.
- The window never covers turning approval off or lowering a risk level
  (it uses `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS`, see above), and the
  login and permission changes do not read it.
- Reject and `/decide` with `decision=rejected` need no fresh sign-in: a
  reject never runs anything.
- `/ready` shows the settings under `features.approval_queue`
  (`fresh_sign_in_max_age_seconds`, `per_action_sign_in_max_age_seconds`,
  `one_time_code_acr_values`). `GET /api/v1/auth-config` names the
  approval client as `cli_approve_client_id`.

MEHO does not read an `approver` claim from the token, so the realm needs
no claim recipe for approval.

| Verb | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/approvals` | operator **or** approver | List, newest first, filtered by `status` (default: `pending`; `all` = every state, #5129). Optional `limit` (1..500, no limit when omitted) and `offset`. Optional `order=oldest\|newest` (default `newest`, #3596): `oldest` lists `created_at ASC`. |
| `GET` | `/api/v1/approvals/{id}` | operator **or** approver | **Inspect one request** (T5). 404 on cross-tenant. |
| `GET` | `/api/v1/approvals/{id}/result` | **request owner** (any role) | **Read the approved dispatch's result** (#3209). Principal-scoped: only `principal_sub` reads it; any other principal gets 403 `not_request_owner`. Writes an `approval.result` audit row. |
| `POST` | `/api/v1/approvals/{id}/approve` | **human** operator **or** approver, **fresh approval sign-in** | Approve. 401 `fresh_sign_in_required` / `fresh_sign_in_already_used` without a fresh approval sign-in, or for a destructive request without a sign-in of its own (#3979, #5515). Requires `params` (hash-verified). Re-hydrates the target by id, then re-dispatches with `dispatch(..., _approved=True)`. 403 `self_approval_forbidden` when the approver is the requester and no self-approval rule allows it: only an Admin may approve their own change at every risk level, break-glass covers only `safe` and `caution` (G11.7-T1, #3328); 403 `approval_group_required` for another person's `dangerous` or `destructive` change when the approver is not an Admin; 403 `human_principal_required` for a machine `principal_kind`. |
| `POST` | `/api/v1/approvals/{id}/decide` | **human** operator **or** approver; **fresh sign-in** for `approved` | Decide (`approved` / `rejected`) by id alone. `approved` needs a fresh approval sign-in like `/approve` (#3979, #5515). On every approval it runs the op with the **stored** params (#1503) and returns the outcome in `dispatch_*`. This is the same for a direct op and an agent-run request (#2293): `claim_resume` lets only one caller run it, so when the waiting agent ran it first, `dispatch_status` is `already_resumed`. 403 `human_principal_required` for a machine `principal_kind`. |
| `POST` | `/api/v1/approvals/{id}/reject` | **human** operator **or** approver | Reject. The op never executes. 403 `human_principal_required` for a machine `principal_kind`. |
| `POST` | `/api/v1/approvals/{id}/withdraw` | **request sender** (any role, any principal kind) | The sender takes back its own pending request (#4987). Not a decision. 403 `not_request_sender` for anyone else; 409 on a closed request. Writes an `approval.withdraw` audit row. |
| `POST` | `/api/v1/approvals/{id}/outcome-check` | **human** operator **or** approver | Record what a person found on the target of an approved run whose outcome MEHO does not know (#5026): `verdict` `happened` / `did_not_happen` and a `note`. Runs nothing; writes an `approval.outcome_check` audit row. 409 `outcome_not_unknown` / `outcome_already_checked`. `GET /api/v1/approvals?outcome_unknown=true` lists these requests. |

### MCP (`backend/src/meho_backplane/mcp/tools/approvals.py`)

Two **read** `meho_approvals_*` tools (both `TenantRole.OPERATOR`-gated):

- `meho_approvals_list` — `status` filter (`pending` default), `limit`/`offset`,
  `work_ref`, and `outcome_unknown` (#5026: approved changes whose outcome
  MEHO does not know).
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
verbs that hit the REST surface via the generated typed client.
`meho approvals withdraw <id> [--reason]` lets the sender take back its own
pending request (#4987). `meho approvals list --outcome-unknown` and
`meho approvals mark-checked <id>` are the outcome-unknown list and its
check (#5026, see above). `meho approvals result <id> [--json]` lets the
sender read the output of its approved run from the owner-only `/result`
read (#5313). It prints the stored result the way `meho operation call`
does (with the `meho operation result-query` command for a result
handle), says in plain words when there is no result yet, and shows the
403 `not_request_owner` answer as `insufficient_role` (exit 5). `show`
adds a `Run output:` line naming that command once the run is done, and
the parked hint of every call that waits for approval names it too.

`approve` uses the approval sign-in (#3979, #5515,
`approval_signin.go`). The stored `meho login` token never approves, so
the CLI does not try it. Before the first approve of a run it asks
`GET /api/v1/auth-config` for `cli_approve_client_id` (a MEHO that does
not send the field gets the client of the stored login; an empty value
stops with "approve in the web interface"). It then runs
`auth.RunFreshSignIn` (`cli/internal/auth/freshsignin.go`): OIDC
authorization code + PKCE S256 against the issuer of the stored login and
that client, scope `openid` only (no `offline_access`), with a loopback
redirect on `127.0.0.1` and a random port (RFC 8252 §7.3), and
`prompt=login` + `max_age=0` so Keycloak asks for the credentials again.
The token goes on `/decide` through a per-request editor. It stays in
memory for this one run (`approvalSignIn`): it is never written to the
token store, a file, an environment variable or the output.
The approval client gives no refresh token, and its access token lasts
900 seconds, the default approval window. When the token runs out,
`auth.FreshSignInTokenSource` returns `auth.ErrFreshSignInExpired`;
`approvalSignIn.bearer` then prints "The approval sign-in has run out.
Sign in again in the browser." (several-ids and in-order runs) and opens
a new sign-in. Where an identity provider does give a refresh token, the
source renews the token in memory with it; the renewal keeps
`auth_time`. One run reuses the
sign-in for several approves. Before a destructive request whose sign-in
was already used, and after a destructive approve, it opens a new
sign-in. When `/decide` still answers 401 with
`error="insufficient_user_authentication"`, it opens one new sign-in and
tries once more. When no browser can be opened (no `DISPLAY` /
`WAYLAND_DISPLAY` on Linux, or the opener fails to start or ends with an
error within 3 seconds) it prints the sign-in link and says to approve in
the web interface, and exits non-zero (auth_expired).

`approve <id> <id> ...` approves several requests with one sign-in
(`approve_many.go`). For each id it shows the request on stderr (what it
does, operation, connector with its version, target, risk level,
requester, and the parameters and preview from `proposed_effect`) and
asks for `yes`; anything else skips that request. A request that is not
pending is skipped. It stops at the first refusal. `--json` prints one
`{id, outcome, detail, request}` entry per id. One id alone asks no
question, as before.

The Keycloak side — the public client `meho-cli-approve` with standard
flow, PKCE and `http://127.0.0.1:*` only, no device flow, no
`offline_access`, no refresh token (`use.refresh.tokens` `false`) and an
access token that lasts 900 seconds (`access.token.lifespan`), the claim
mappers of `meho-cli` without the MCP
audience, and its own login flow `meho approval sign-in level`
(`authenticationFlowBindingOverrides.browser`) — is set by `meho admin keycloak bootstrap-clients`, the
bundled realm `deploy/charts/meho/charts/keycloak/files/meho-realm.json`
(new installs; the realm stage adds the flow to a running realm when it
may change login flows, else `keycloak.authflow.ensure_approval_flow`
does), and `docs-site/install/keycloak-realm.md` Step 2. The flow holds
also when the realm's login goes straight to a company login: a
command-line approval always signs in through its own flow
(`tests/integration/test_core_company_login_keycloak.py`). `list`
has a `RUN` column and `show` / `approve` print how the run of an approved
request ended (#3543, see "Approved, but the run failed" above).
`list` shows only pending requests unless `--status` is set; `--status all`
shows every state. An empty list names the state it filtered on and points
to `--status all` (#5129). `--limit` / `--offset` go to the server as
`limit` / `offset`. When `--offset` skips every match, the empty message
says the page is empty and points to a smaller `--offset`, so it does not
claim the tenant has no requests. When `--filter-work-ref` matches nothing, the
empty message names the change ticket and points to removing the filter,
for the same reason. `--order oldest|newest` picks the sort (#3596). It
defaults to newest first, but to **oldest first when `--filter-work-ref` is
set**, so the steps of one change ticket read in the order they were sent.
`approve --filter-work-ref REF --in-order [--reason]` approves the requests of one
change ticket oldest first and stops at the first step that fails (#3597,
see "Approve a change ticket's requests in order" below).

`approve <id>` answers at once (#5110, `approve_wait.go`). It sends
`/decide` with `"async": true`: MEHO saves the decision, starts the
operation in the background and answers HTTP 202 with the run id. The CLI
then reads the request once and prints its state, the `Run:` line
(`started` while it runs) and "Approved. The operation is running. Check
it with `meho approvals show <id>`." With `--wait` it reads
`GET /api/v1/approvals/{id}` every 3 seconds until `result_state` is
`executed`, `failed` or `uncertain`, and then prints the run lines as
before. The "outcome unknown" list does not end the wait: a run longer
than that age limit is still waited for. When the CLI cannot check the
run, it prints once that the run takes longer than usual and that Ctrl-C
stops the wait (the run goes on). Each read is a
short call, so a proxy time limit cannot cut the wait; up to 5 failed
reads in a row are tried again. When the person may read runs (operator
role), each read also checks the run (`GET /api/v1/operations/runs/{id}`):
a run that ended without storing a result on the request ends the wait
with "The run ended, but MEHO stored no result." A run that only found
that another caller runs the operation (`succeeded` with result status
`already_resumed`) does not end the wait: the CLI stops reading that run
and waits on the request alone. `--json` prints the
request view (`ApprovalRequestView`) after the approve, or with `--wait`
after the run ended. When the answer to `/decide` is lost on the network
(a transport error, not an HTTP answer), the CLI reads the request again:
when it is no longer pending it prints that state after "The answer to
this call was lost on the network.", else the `unreachable` error as
before. A MEHO from before #5110 refuses the unknown `async` field with
422 before it saves anything; the CLI then approves again without it.
`reject`, `approve <id> <id> ...` and `--in-order` do not send `async`:
`--in-order` needs each run's result before the next step.

### Operator console (`backend/src/meho_backplane/ui/routes/approvals/`)

A session-BFF surface (cookie session + CSRF double-submit, not the
Bearer REST routes — a browser carrying only the BFF cookie cannot auth
those). Every read + decision derives `tenant_id` from the validated
`UISessionContext` only, and calls the `approval_queue` service
in-process (same in-process-audit binding the REST routes get).

**Access model — operator-or-approver flag, parity with REST (#3243 / #3270 /
#3282).** The console decide path carries the **same** access model the
Bearer REST plane adopted in #3243/#3270, reached through the *same*
shared service gate rather than a console-local role check. The BFF
reconstructs the full `Operator` via `_resolve_operator` →
`verify_access_token_with_refresh` → `_operator_from_claims` (which lifts
the orthogonal `approver` flag, `Operator.approver`; MEHO does not read an
`approver` claim, and no role gives the flag today), then calls
`approve_request` / `reject_request`; their `_check_reviewer_role` admits
a principal that is `operator`-or-higher **or** carries `approver`, and
refuses only a `read_only` principal *without* the flag (403, re-rendered
in the modal banner naming both levers). So a `read_only` principal
with `approver=true` would clear a four-eyes gate through the console
exactly as over REST/CLI. MEHO keeps the flag in the code; no role gives
it today, and roles that customers define come later. The read
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
| `GET` | `/ui/approvals` | Content-negotiated. A normal navigation (no `HX-Request`) → the **full-page console**: status tabs (Pending / Approved / Rejected / Expired / All, plus an "Outcome unknown" tab, #5026), a `work_ref` filter, an oldest/newest toggle (`order`, #3596), and the decision-history list. The bell's `hx-get` (`HX-Request: true`) → the pending **panel** modal fragment, oldest first with step numbers when a change ticket has two or more pending requests (#3596). (G10.8-T #1827) |
| `GET` | `/ui/approvals/list` | Decision-history partial — the HTMX swap target for the status tabs / `work_ref` filter / oldest-newest toggle / "Load more" offset pager. Reuses `list_pending(status=…, work_ref=…, offset=…, order=…)`; `tab=all` passes `status=None`. `order` is `oldest`, `newest`, or empty (the default rule in "Chains of requests under one change ticket" below); any other value is a 422. Pages with a real offset, not the badge's 50-row glance cap. (G10.8-T #1827) |
| `GET` | `/ui/approvals/{id}` | Request-detail modal. A pending row offers Approve/Deny; a **decided** row renders read-only with a decision banner ("Approved/Rejected by X at T"). An approved row also shows how its run ended; a failed run turns the banner red (#3543). The `proposed_effect` envelope renders **structurally** (#2447), not as raw JSON (below). |
| `POST` | `/ui/approvals/{id}/approve` | Approve in-process + start the parked op in the background + the fail-open `approval.approved` add-on step event. Answers within about a second, however long the op runs (#5110, below). Needs a recent console sign-in that asked for the credentials (the fresh approval sign-in rule above); otherwise the modal re-renders with the sign-in link. |
| `GET` | `/ui/approvals/{id}/run` | The run part of the outcome card, as it is now (#5110). Asks again every 3 seconds while the run goes on. |
| `GET` | `/ui/approvals/{id}/row` | One row of the history list, as it is now (#5110). Asks again every 3 seconds while the run goes on. |
| `GET` | `/ui/runs/notices?run_id=` | The "the run has ended" notices for up to 20 runs this person started (#5110, below). |
| `POST` | `/ui/approvals/{id}/reject` | Reject in-process + the `approval.rejected` add-on step event; the op never runs. |
| `GET` | `/ui/approvals/in-order?work_ref=` | The "Approve group in order" modal: the change ticket's requests oldest first and a Start button (#3597). |
| `POST` | `/ui/approvals/in-order` | Runs the steps in order (form: `work_ref`, `reason`, `confirm_id`, one `step_id` per step shown); renders what happened to each step (#3597). Approves nothing when a pending request of the ticket is not among the posted `step_id`s. |
| `POST` | `/ui/approvals/{id}/outcome-check` | "Mark as checked" in the modal of a request on the "Outcome unknown" tab (#5026): form `verdict` and `note`. Runs nothing; the modal shows the recorded check. |

**Approve answers at once; the page follows the run (#5110).** Approving
used to run the whole operation inside the web request, so the dialog
stayed open, without progress, until the operation ended (a content
library deploy can take hours), and a proxy could cut the request.
`run_watch.start_approved_run` now starts the run with
`OperationRunService.submit_approval_resume` — the same run claim, policy
bypass and audit as before, so the operation still runs exactly once and
writes its audit row — and waits at most `ANSWER_WAIT_SECONDS` (1 second).
A quick run shows its result on the card at once. A longer one shows
"Approved. The operation is running." with a spinner and says that closing
the window is safe: closing never stops the run.

`run_watch.live_run(request, run)` says where the run is. It reads the
stored request and its latest background run (`operation_run` rows with
`approval_request_id`), so it works on every MEHO server process:

- A result stored on the request wins: `executed`, `failed` or `uncertain`,
  with the summary and the audit link.
- Else the background run decides: still going is `running`; ended with a
  result is that result; ended without one (a crash, a server restart that
  the reaper turned into `uncertain`, a cancel) is `ended_no_result` ("no
  result: check it"). So nothing shows "running" forever.
- When another caller (usually the waiting requester) runs the operation,
  the request is `started` until its result is stored. Once the run is
  older than the "outcome unknown" time
  (`approval_outcome_unknown_after_minutes`), the asking stops and the
  state is `started_no_result` ("Approved, but MEHO has no result", with
  the advice to check the target). Only the age counts, so a request that
  someone marked as checked does not start asking again.
- A re-run (#5417) clears the stored result but makes no background run.
  So a background run made before `rerun_started_at` belongs to an earlier
  attempt and is left out: the re-run shows `started`, not the old
  attempt's result.

The card part (`_run_state.html`) and each list row (`_history_row.html`)
carry `hx-get` + `hx-trigger="every 3s"` + `hx-swap="outerHTML"` while
`polling` is true and drop them at a final state, the pattern of the
connector import job card.

**The "the run has ended" notice (#5110).** Not tied to approvals, so the
operations page can use it too. A page marks a background run whose state
is not final yet (`polling`) with `data-meho-watch-run="<run id>"`, also
when another caller runs the operation.
`static/src/app/run-notices.js` keeps those ids for the browser tab
(sessionStorage, so they survive a move to another page) and, on the
5-second `meho:approval-badge-poll` timer of `approvals-bell.js` (only
while the page is visible), asks `GET /ui/runs/notices?run_id=...`
(`ui/routes/run_notices.py`). The answer has one notice
(`runs/_ended_notices.html`) per ended run, added to `#meho-run-notices`
in the corner of every page (`base.html`). Each notice says how the run
ended and, for an approval run, has a "View request" button that opens the
request. The route reads all asked runs in one query. Only the person who started a run gets its notice: a run of
another person or tenant, or an unknown id, gets a hidden marker, and the
script stops asking for it. A final state shown in an open dialog carries
`data-meho-run-seen`, so the notice does not repeat it. The Close button
is handled in the script, not in the template.

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

### Chains of requests under one change ticket: oldest first and step numbers (#3596)

Approving runs the operation at once. When an agent sends several requests
that depend on each other (step 2 needs step 1 to have run), approving them
newest first runs them in the wrong order, and the later steps fail. The
requests of such a chain share one change ticket (`work_ref`). MEHO now
shows that order on the surfaces an approver uses. Nothing here changes what
"approve" runs, and nothing stores an order on the row.

- **Step numbers.** `approval_queue.pending_chain_positions(session,
  tenant_id=…, work_ref=None)` reads the pending requests that have a
  `work_ref`, groups them by `work_ref`, and numbers each group oldest
  first (`created_at`, then `id`). It returns a `ChainPosition(work_ref,
  step, total)` per request, only for groups with **two or more** pending
  requests. The numbers count pending requests only: once step 1 is
  decided, the old step 2 becomes step 1 of the rest.
- **Sort order.** `list_pending(order="oldest"|"newest")` (default
  `newest`) and `GET /api/v1/approvals?order=` (default `newest`, so a
  client that does not send it gets the same answer as before). `oldest`
  sorts on `created_at ASC, id ASC`, the same order the step numbers use.
- **Bell panel.** When any change ticket has two or more pending requests,
  the panel lists oldest first and shows "Step k of N in change ticket
  `<work_ref>`" on each grouped row. Otherwise it stays newest first. The
  approver does not need to type a filter.
- **Full-page console.** The Pending tab uses the same rule when no
  `order` is given (the rule looks only at the `work_ref` filter when one
  is set). Every other tab is newest first by default. The "Oldest first" /
  "Newest first" toggle sets `order` explicitly; the live refresh and the
  `work_ref` box keep that choice, a tab switch drops it, and "Load more"
  carries the order the first page used. Pending rows in a group show the
  "Step k of N" badge.
- **Detail modal.** A pending request in a group shows "Step k of N
  waiting in this change ticket" and a link to the Pending tab filtered to
  that ticket, oldest first. When k > 1, an earlier step still waits, so
  the consequence banner says so and the Approve armer opens **one more
  confirm** first (`data-approval-confirm="out-of-order"`): "You are
  approving step k of N before the earlier steps. This runs at once and
  cannot be un-approved." Only after "Approve step k anyway" does the
  normal approve confirm appear. The route computes the step for every way
  the modal opens (bell, history row, or a deep link from a "waiting for
  approval" result), so the link used cannot skip it. This is a console
  prompt, not a server rule: REST and CLI approve a later step without
  asking.
- **"Waiting for approval" result in the console.** When an operator runs
  an operation (`operations/_result.html`) or a Vault write
  (`vault/_write_result.html`) and it waits for approval, the result
  fragment calls `render.parked_request_chain(tenant_id, envelope)`. It
  loads the parked request named by `extras.approval_request_id` and
  numbers it among the pending requests of its `work_ref`. When the ticket
  has two or more pending requests, the fragment says "Step k of N waiting
  in change ticket `<work_ref>`" and its first link opens the Pending tab
  filtered to that ticket, oldest first; the per-request modal link stays
  as the second link. A lone request, a request with no `work_ref`, or an
  id outside the tenant keeps the plain per-request link.
- **Step event for the requesting add-on.** The `approval.pending` add-on
  step event (`record_approval_step_event`) adds `chain_step` and
  `chain_total` to its payload when the new request is one of two or more
  pending requests under its `work_ref`. A read error only drops the two
  fields (fail-open, like the event itself).
- **CLI.** `meho approvals list --order oldest|newest`; oldest is the
  default when `--filter-work-ref` is set (see the CLI section above).

### Approve a change ticket's requests in order (#3597)

Step numbers (#3596) show the order, but approving still runs one step at a
time, and nothing stops an approver from approving step 3 after step 2's run
failed. The in-order action approves the steps for the approver, oldest
first, and stops at the first failure. It exists on the CLI and in the
console. Each step is still an ordinary single approval: its own
`approval.decision` audit row, its own self-approval (#1401), role and
approver (#3243) checks. There is no bulk-approve endpoint and no new
permission.

For each request of the `work_ref`, oldest first (`status=all`,
`order=oldest`):

| The request is | The run |
|---|---|
| `approved` and its run is `executed` (ran OK) | skips it; this makes a second run after a stop safe |
| `approved` and its run is `started` (claimed, no stored result yet) | stops: the outcome is not known yet; run the action again when the step has ended |
| `approved` and its run is `uncertain` (MEHO does not know if the change happened) | stops: do not run it again, check the target first (#5025) |
| `approved` and its run is `failed` or `not_started` | stops: an approval is used once, so the step cannot run again from here; the requester must send it again |
| `rejected`, `expired` or `withdrawn` | stops: the steps after it may need it |
| `pending`, risk level `dangerous` or `destructive` | waits for a yes for this one step, then approves it |
| `pending`, any other risk level | approves it (the CLI waits for a yes here too, #5515) |

After an approval the run reads how the run of the operation ended. It
goes on for `ok`. `already_resumed` means the waiting requester (the
in-process agent waiter) won the claim, not that its run ended, so the
action then reads the request until the stored result
(`persist_resume_result`, read as `result_state`) is there: `executed`
goes on, `failed` stops. No stored result within the wait (30 s in the
console, 2 min on the CLI) stops the run too, because the outcome is not
known and the next step may depend on it. Any other dispatch status, a
refusal (403 self-approval or role, 409 already decided or expired, 422),
or an answer that is not a finished 200 stops it. The steps after a stop
stay pending. The run is not one transaction: if it stops or is killed,
the steps it already approved stay approved.

Before it starts, the run warns when the requests come from more than one
`principal_sub`, or when two requests have the same `created_at`. Oldest
first is the dependency order only for one chain sent in order by one
requester.

- **CLI** (`cli/internal/cmd/approvals/approve_in_order.go`). Lists the
  group through `GET /api/v1/approvals` (no `limit`, so the whole group),
  then posts `/decide` per step and reads
  `DecideResponseBody.dispatch_status` / `dispatch_error` /
  `dispatch_execution_outcome` (the single-id `approve` only checks the HTTP
  status). A step whose own run ends `uncertain` stops the run with the
  target-check advice, never with "send it again". After `already_resumed` it polls
  `GET /api/v1/approvals/{id}` for `result_state`. At every pending step
  (#5515) it prints what the step does (`reviewer_context` and
  `proposed_effect` from `GET /api/v1/approvals/{id}`: operation,
  connector with its version, target, risk level, parameters) on stderr
  and reads a line from stdin; only `yes` goes on. All steps share one
  approval sign-in; a destructive step gets a sign-in of its own (see
  "Approve needs a fresh approval sign-in"). On a stop it exits non-zero and names the step and the
  steps it did not run. A refusal returns the same error and exit code the
  single-id `approve` returns. `--json` prints `{work_ref, completed,
  stopped_at, warnings, steps[]}`.
- **Console** (`ui/routes/approvals/in_order.py`, `_in_order.html`). The
  Pending tab shows **Approve group in order** when the `work_ref` filter
  names a ticket with two or more pending requests. The route reads the
  group page by page (`list_pending`, 500 rows a page) until a short
  page, so a ticket with more than 500 requests is complete. The steps
  run on the server, through the same `_commit_decision` +
  `resume_dispatch_after_approval` calls the single Approve button uses;
  after `already_resumed` it re-reads the row until `resume_result` is
  stored. Every step is an approval, so the sign-in behind the console
  session must pass the same checks as a single approve (see "Approve
  needs a fresh approval sign-in"): a `destructive` step needs a sign-in
  that approved nothing else, and after it that sign-in approves no
  further step. When a step's approval is refused for its sign-in
  (`fresh_sign_in_required` / `fresh_sign_in_already_used`), the run
  stops there and the modal shows **Sign in again**: the console login
  with `fresh_sign_in=1`, which comes back to the Pending tab filtered to
  the ticket. The operator then starts the run again; the steps that ran
  OK are skipped. A
  server-side loop was picked over re-opening the detail modal for each
  step: the server reads each step's run result before it goes on, with no
  round trip per step. At a dangerous or destructive step the POST returns
  the modal with that step's context and an **Approve step k and go on**
  button. That button posts `confirm_id` = the id of that one request, so a
  yes never covers another step. Every form also carries one hidden
  `step_id` for each step it lists. Before anything is approved, the POST
  reads the ticket again: when a pending request is not among the posted
  ids (it joined the ticket after the modal was rendered), the run
  approves nothing and shows the list again with a banner that names the
  new steps and marks them "new". The operator starts again from that
  list. So a run covers only the requests the operator saw. Every form in the modal carries a CSRF
  token minted for that render, and the response re-sets the cookie. The
  response fires `meho:approval-bump`, not `meho:approval-decided`, so the
  bell and the history refresh but the modal stays open.

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

### The approvals page on a phone (#6000)

The page works on a phone screen (about 390 px wide) and still looks the
same on a laptop. It uses the existing Tailwind and DaisyUI classes, with
the `sm:` width (640 px) as the switch point.

- **Waiting for you first.** On the Pending tab, the requests the
  signed-in person may approve come first, each with a "Waiting for you"
  badge (`data-waiting-for-me` on the row). The check is
  `approval_rights.approve_right`, the same one the modal and the approve
  use. `ui/routes/approvals/waiting.py` (`WaitingFirstPager`) does the
  split; each group keeps the order the database returned (a change
  ticket's steps stay oldest first). The split runs over all pending
  requests before the page is cut (`routes._list_pending_waiting_first`),
  so a request waiting for you is on page one even behind hundreds of
  newer ones, and "Load more" goes on in the same order. The route reads
  the queue in batches of 500 (`_WAITING_SCAN_BATCH`), and the pager keeps
  only the rows the page can need, so a long queue does not fill memory.
  Each batch starts right after the last row of the batch before
  (`list_pending(after=…)`, sorted by time and then request id), not at a
  counted offset. So a request that someone decides during the scan does
  not move the rest of the queue, and the scan skips no row. The other tabs do not check the person and keep their
  order.
- **The person check can fail.** The list pages are open with the session
  alone. To know who may approve, the Pending tab also checks the
  session's sign-in (`routes._operator_or_none`, the same check the modal
  makes). When that check fails for any reason (a session that ended, a
  tenant switched in another tab, the sign-in service not reachable), the
  list shows its plain order with no badge, and logs
  `ui_approvals_waiting_order_skipped`. It never shows the badge without a
  real check.
- **Rows.** Below `sm` a row stacks: the text wraps instead of being cut
  (`[overflow-wrap:anywhere] sm:truncate`), so the person sees who asks
  for what, and the Review / View button fills the row as a large touch
  target. The status tabs wrap to a second line. The bell panel rows do
  the same.
- **Decision window.** The modal, the bell panel, the "decision recorded"
  window and the in-order window open as a sheet from the bottom on a
  phone (`modal-bottom sm:modal-middle`). In the request modal the
  decision bar (Deny / Approve and their confirm steps,
  `data-approval-decision-bar`) sticks to the bottom of the window while
  the detail scrolls, so the buttons are in reach without scrolling, also
  on a laptop for a long request. The reason field sits above the bar;
  the confirm forms still read it by id (`hx-include`). On a phone the
  two buttons are larger and share the row.

The console has no browser test harness. `test_ui_approvals_phone_layout.py`
pins the order and the markup; the look was checked in a real browser at
390 px and 1366 px wide (no sideways scroll, decision buttons in view).

## Ordered and dependent approvals (#3598)

This section is for a sender (an agent, an add-on or a script) that sends
several requests where a later step needs an earlier step to have run.
For example: write a secret, then run a build that reads it.

### What approve does

- **Approve runs the operation at once.** This is true on every human
  surface: REST `/approve` and `/decide`, the CLI, and the console. There
  is no "approve now, run later".
- **Each request stands alone.** The queue has no idea that one request
  depends on another. No order is stored on the row. Nothing stops a
  person from approving step 3 before step 1. Step 3 then runs first and
  fails, for example because its secret or folder does not exist yet.
- **A failed run stays failed.** When the run after an approval fails, the
  request stays `approved` (`result_state` is `failed`). Approving it again
  returns `409`. MEHO does not try the run again by itself.

The oldest-first views and step numbers (#3596, "Chains of requests under
one change ticket" above) help the approver see the order. They do not
enforce it. So the **sender** must make the order safe. Use one of the two
patterns below.

### Pattern 1 (preferred): send the next step only after the last one ran

Send step N+1 only after step N was approved **and** ran without error.
Then the queue never holds two steps of the chain at the same time. Nobody
can approve them in the wrong order, because the later step does not exist
yet. This is the strongest guarantee.

How to wait for step N:

1. Poll `GET /api/v1/approvals/{id}/result`. Only the sender of the
   request can read it ("Surfacing the approved dispatch's result" above).
2. Wait until `status` is `approved` **and** `result` is not `null`.
   `result` stays `null` until the run has stored its result.
3. Check `result.status`. Only `ok` means the step worked. A stored result
   can also carry a failure (`error`, `denied`, ...). Then stop the chain.
4. Stop the chain also when `status` is `rejected`, `expired` or
   `withdrawn`.

Do not treat the `approval.approved` step event as proof that the step
worked. It only says that a person approved. Use it to know when to read
`/result`, then check the result.

An agent run does **not** give this guarantee by itself. It waits at each
approval for the decision, not for the run. Only one caller may run an
approved request (`claim_resume`, #2293). When the waiting agent wins, it
runs the step itself and gets the result. When an operator surface such as
`/decide` wins, the agent gets `already_resumed` at once
(`agent/approval_wait.py`), while the operator's run may still be going.
So an agent that sees `already_resumed` must still poll `/result` and
check `result.status` is `ok` before it sends the next step.

### Pattern 2: one change ticket, sent in order, approved with the ordered group action

Send all steps of the chain with the **same `work_ref`** (the change
ticket; the `work_ref` argument of `call_operation`, or `--work-ref` on
`meho operation call`). Send them **in dependency order**, one chain per
`work_ref`. Then "oldest first" is the same as "dependency order", and
the step numbers ("step k of N") are right.

Then tell the approver to use the ordered group action. It ships with
#3597: the CLI verb `meho approvals approve --filter-work-ref <ref> --in-order`
(`--filter-work-ref` picks the group; the global `--work-ref` flag only
tags new requests with a change ticket)
and an "Approve group in order" action in the console. It approves the
group oldest first, one request at a time. It stops at the first step that
fails, is refused, or was rejected or expired. Each request still gets its
own decision, its own audit row and its own checks; it is not a bulk
approval. #3597 also warns when oldest first may not be the right order:
when the group has requests from more than one requester, or when two
requests have the same creation time (MEHO then cannot tell which comes
first).

Rules for pattern 2:

- One chain per `work_ref`. Two chains in one ticket mix their steps.
- Send the steps in dependency order. If you send them in another order,
  "oldest first" is the wrong order.
- Never hand the approver an unordered set of requests whose order lives
  only in a chat message. A surface with a different sort order defeats it.

### When a step fails after approval

Pattern 1 never sent the later steps, so there is nothing to undo. With
pattern 2 the ordered group action stops at the failed step and leaves the
later steps pending.

The failed request itself cannot run again: approving it again returns
`409`, and its one run is used up. Fix the cause, then send a new request
for that step. It needs a new approval. Running an approved request again
after a failed run is tracked in #5417.

## Approval step events (no Broadcast events)

Approvals publish **no Broadcast events** any more (#3758). The old
`approval_queue.publish_approval_event` is gone. Waiters, the approvals
bell and the console read the stored approval row instead.

What is left is one **add-on step event** per lifecycle step (#3027). It
goes only to the paired add-on that sent the request: the owner is the
request's `principal_sub`. `approval_queue.record_approval_step_event`
writes it. It is fail-open (a failed write never blocks the decision), and
every call site writes it **after** its transaction commits, so an event
never outlives a failed transaction. When the requester is not a paired
add-on, nothing is written. The add-on reads its events from `GET
/api/v1/addons/pairings/{name}/events?after=<seq>` (see
[addon-step-events.md](addon-step-events.md)).

| Stage | Event kind | Call site |
|---|---|---|
| Create | `approval.pending` | `operations/dispatcher.py::_handle_needs_approval`; `operations/composite.py::enforce_subop_policy` (a step of a multi-step operation waits for approval) |
| Approve | `approval.approved` | `api/v1/approvals.py` (`/approve`, and `/decide` through `_capture_operator_decision`); `ui/routes/approvals/routes.py` (console) |
| Reject | `approval.rejected` | `api/v1/approvals.py` (`/reject`, `/decide`); `ui/routes/approvals/routes.py` (console) |
| Expire | `approval.expired` | `operations/approval_expiry.py::_sweep_one_tenant` (the periodic sweeper) and `::expire_approvals_from_older_release`; `operations/dispatcher.py::_handle_needs_approval` (a late retry with the same idempotency key finds its request still `pending` past the deadline) |
| Withdraw | `approval.withdrawn` | `api/v1/approvals.py` (the sender takes the request back, #4987) |

Payload: `approval_request_id`, `decision`, `connector_id`,
`approval_op_id`, `work_ref`. An `approval.pending` event of a request
that shares its `work_ref` with other pending requests also carries
`chain_step` / `chain_total` (#3596). The event's `audit_id` is the
decision row's primary key (FK to `audit_log.id`).

`approval.approved` says that a person approved. It does **not** say that
the run worked. To learn that, read `result_state` / `resume_outcome` on
the request, or the result on `/result` (see "Ordered and dependent
approvals" above).

## Notifications

When a request waits for approval, MEHO sends a push message to the phones
and laptops of the people who may approve it. When the request is decided,
a second message with the result replaces the first. A person switches the
messages on per browser on the account page
([ui.md, "Approval notifications"](ui.md#approval-notifications)). The
whole feature is off until an admin sets `WEB_PUSH_ENABLED`.

**When.** `record_approval_step_event` calls
`web_push.send.notify_approval_step`. Every caller runs that function only
after the transaction with the audit row has committed (table in
[Approval step events](#approval-step-events-no-broadcast-events)). So a
message never goes out for a request whose audit row did not commit. The
call only starts a background task and returns at once: the request never
waits for a push service, and a failed send never changes the request.

| Step | Message |
|---|---|
| `pending` | "An approval waits" |
| `approved` / `rejected` / `expired` / `withdrawn` | "Approval approved" (and so on), with the same tag, so it replaces the first message on the device |

**Who gets it.** The login groups are in the login token, not in the
database. So each subscription keeps the person's login facts in its tenant:
`tenant_role`, `approver`, `self_approver`, `platform_admin`, and when they
were read (`facts_read_at`). MEHO reads them from the login when the person
switches the messages on, at each console sign-in (also when they pick a
tenant), and each time they open the account page. No default group sets
`approver` or `self_approver`: a login in MEHO-Approvers or
MEHO-SA-Operators stores both as false. MEHO does not read the `approver`
claim of the token. No role gives the flag today; roles that customers
define come later. Migration `0177` set
both flags to false on the rows that existed at the upgrade; the next
sign-in stores the person's current flags.

For each request, `web_push/recipients.py` builds a person from each row's
facts and asks `approve_right_any_status` (`operations/approval_rights.py`)
whether that person may approve the request. That is the same rule as the
approve: the group rule for `dangerous` and `destructive` changes, the
own-change rules, the rule for a change of rights (only an admin, in the web
console), and
the role an operation needs from its approver (for example `tenant_admin`
for `targets.register`). It only leaves out the "still pending" check, so
the decision message goes to the same people as the first message.
A row stores no login group, so with approver routing on, a request whose
stored kinds name an old kind (`SA-Operators`) sends no message to the
members of that old group; the other stored kinds still get it.

Only subscriptions of the request's tenant count. A row whose facts are
more than 7 days old gets no message, because the person's groups may have
changed since. Rows made before the facts columns (migration `0161`) get no
message until the person signs in again. This is only the list of who gets
a message: the approve itself checks the person again, as always.

**What the message holds.** A JSON object with `title`, `body` (the tenant
name and the request id), `tag` (`approval-<request id>`) and `url`
(`/ui/approvals`). Never the operation, the parameters or the target. It is
encrypted for each browser (RFC 8291, `aes128gcm`; `web_push/encryption.py`),
so the push service cannot read it. The request carries a token signed with
the push key (RFC 8292, `Authorization: vapid t=..., k=...`), whose `sub` is
`BACKPLANE_URL` when that is an `https://` address. Other headers: `TTL`
(one day), `Urgency: high`, and `Topic` (the request id), so the result
also replaces a first message that the push service has not delivered yet.
The service worker shows the notification; a click opens the approvals page
of this MEHO, never another address.

**Rate limit.** A person gets at most one "An approval waits" message per
tenant per 30 seconds (`RATE_LIMIT_SECONDS`). The messages held back in that
time become one summary at its end, sent only to the person's browsers of
that tenant: "N more approval messages. Open the
approvals page." (tag `approval-summary`). A decision goes out at once only
to a person who got the "An approval waits" message of that request, because
it replaces that message. For everyone else a decision counts against the
same limit, so a burst of decisions (for example many requests expiring at
once) gives one message and one summary, not one message per request. MEHO
remembers up to 1,000 such requests per person and tenant (the oldest are
forgotten first). The limit is kept in memory, per process.

**Order.** The steps of one request go out in the order they happened. Each
step waits until the step before it of the same request was sent. So a
first message that was slow can never arrive after the result and replace
it. The order is kept in memory, per process. So with `replicaCount`
above 1 the rate limit and the order hold only inside one copy; the
user guide (`docs-site/guides/phone-and-laptop.md`) and
`docs/deploying.md` (`webPush.*`) say so where the feature is turned on.

**Network.** MEHO needs outbound HTTPS to the hosts of the allow-list
(`web_push/endpoints.py`). The install docs list them for firewall teams:
`docs-site/install/index.md`, "Push services for approval notifications".
Why web push, and what leaves the network:
[the decision record](../decisions/2026-11-15-approval-notifications-through-web-push.md).

**Where messages go.** Only to the browser makers' push services: the
endpoint is checked again before each send, not only when it was stored.
Redirects are not followed, and each send has a 10 second timeout. A push
service that answers 404 or 410 (the browser dropped the subscription)
gets the row deleted. When that delete fails (the database is down),
MEHO logs `web_push_gone_not_removed` and the other sends of the step go
on; the row goes at the next 404 or 410. Other errors are logged and the
row stays. Logs name
the row id, the person, the push service and the status or the error type.
Never the endpoint or a key.

Tests: `tests/test_web_push_send.py` (a fake push service that decrypts
each message), `tests/test_web_push_service_worker.py`.

## Audit rows

Two rows per request lifecycle (the synchronous-audit invariant). A
withdrawn request has a withdraw row instead of a decision row:

| Row | `path` | `status_code` | Written by |
|---|---|---|---|
| Request | `approval.request` | `202` | `create_pending_request` |
| Decision | `approval.decision` | `200` (approved) / `403` (rejected) / `410` (expired) | `approve_request` / `reject_request` / `expire_stale_requests` |
| Withdraw | `approval.withdraw` | `200` | `withdraw_request` (#4987) |

### Who the payload names

`_write_audit_row` writes the people of a row in clear pairs. Each id
stands next to the name of the same person:

| Payload keys | Person |
|---|---|
| `principal_sub` / `principal_name` | The person who acted on this row. The same person as the row's `operator_sub` column: the requester on a request, withdraw or result row; the reviewer on an approve or reject row; the expiry login on an expiry row. |
| `requester_sub` / `requester_name` | The person who asked for the operation (`approval_request.principal_sub` / `principal_name`). `requester_name` is left out when the request has no name. |
| `reviewed_by` / `reviewed_by_name` | Only on an approve or reject row: the reviewer. `reviewed_by_name` is left out when the reviewer has no name. |

The other rows that `_write_audit_row` writes (refused resume, re-run,
outcome check, reveal fetch, restart recovery) follow the same rule. The
standing-grant row (`service_grants._record_grant_use`) and the run
approval row (`run_approval_gate`) name only the caller, in
`principal_sub`.

**Rows in the older form.** Older rows have no `requester_sub`: their
`payload.principal_sub` is the requester, while `payload.principal_name`
is the person who acted. The audit log is append-only, so these rows stay
as they are. The readers show them through
`audit_query/approval_payload.py:approval_payload_view`: every such row
with an `approval_request_id` shows the requester's id in
`requester_sub`, and `principal_sub` shows the row's `operator_sub`. This
holds also when the requester acted on the row (a request, withdraw or
result row, or a self-approval), so every reader finds the requester in
`requester_sub`. Two readers use it: `audit_query/query.py:_build_audit_entry`
(the audit REST API, `meho audit query` / `show` / `replay`, the MCP audit
tools and the session replay) and the console's audit row drawer
(`ui/routes/audit/routes.py:_build_drawer_context`).

### Session-replay lineage (#2086)

The park → decide → execute chain reconstructs as **one subtree** on the
G8.2 replay surface (`GET /api/v1/audit/sessions/{id}/replay`), which
anchors on `audit_log.agent_session_id` and walks `parent_audit_id`.
Before #2086 every row in the chain carried NULLs for both, so an
approval-gated dispatch was invisible to replay (`{root: [],
row_count: 0}`) even with `MCP_REQUIRE_SESSION_ID=true` — the
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

- **Operator surfaces** (REST `/approve` + `/decide`, UI) —
  `resume_dispatch_after_approval` →
  `_dispatch_resume_with_bound_context` re-binds `agent_session_id_var`
  \+ `parent_audit_id_var` (token-scoped, alongside `work_ref_var`)
  around the `dispatch(..., _approved=True)`, because the approver runs
  on a fresh task whose contextvars are unset.
- **In-process agent-run resume** (#2323, completing #2086) — the
  agent waiter
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

#### The actual contract: one resume completes the composite (#3351 review B1, #3980)

The resume does **not** re-park "one gate at a time". A person approved
the run, so its remaining internal steps go ahead under that approval
(`_covered_by_approved_run` in `composite.py`). A run counts as approved
while the dispatcher runs it with `_approved=True` (it binds
`approved_run_var`) or while `composite_resume_scope_var` is set (the
re-entry of an approved step). Then a step whose gate says
`NEEDS_APPROVAL` goes ahead, unless the step is `destructive`, marked
`requires_approval`, or reads, writes or makes a credential
(`service_grants.credential_call_waits`). A `DENY` stays a deny. The same
holds for a child operation the composite runs through `dispatch_child`:
the child's own `dispatch()` reads `approved_run_var` right after its
policy gate and goes ahead on `NEEDS_APPROVAL` under the same three
exceptions. A credential step or child waits for its own approval, also
when the rights of the person who approved the run would let it run: the
step's gate and the child's `dispatch()` turn an `AUTO_EXECUTE` into a wait
(`credential_step_waits_after_approval`). This holds whoever runs the
resume and however the approval was given: a web click, or the command
line with a fresh sign-in (#3979). It matters since #3980: a reviewer's
token, and a `dangerous` step in the web interface, would otherwise wait
again. So a multi-step composite (e.g. `vm.create` with
`power_on_after_create=True`, which parks at create then powers on)
completes in a **single** resume, each step running exactly once. The
invariant is explicit: **every governed sub-op of a composite that must
finish in one resume is not `destructive` and not `requires_approval`**.

A later step that is `destructive` or marked `requires_approval` waits for
its own approval, also during a resume. The steps the run already did are
stored with that new request, so its resume sends none of them again. See
the next section. A child call that waits is its own request: its approval
runs only the child, and the multi-step run does not go on.

#### How a multi-step run goes on after an approval

MEHO never sends a step of a multi-step operation twice. The approval of
a step runs the operation again from its first step, so MEHO keeps a record
of the steps each run went through (`operations/composite_steps.py`):

- **Recording.** `dispatch_composite` binds one `StepJournal` for each run
  of a composite handler (`composite_step_journal`). Each step gets an
  entry with its connector, op id, target and params hash. The target is
  kept as its id (`step_target_key`), or its name when it has no id.
  Its status:
  - a `dispatch_child` call: `started` before it runs, then `done` with
    the `OperationResult` the composite got back;
  - a `run_governed_subop(..., call=...)` call: `started` after the gate
    lets it go ahead, then `done` with what `call` returned;
  - a step that only `enforce_subop_policy` checks: `started`. The seam
    does not see the handler's own call, so this step never has a stored
    end.
  A result is kept only when the step's operation does not read, write or
  make a credential (its class from `classify_op` is not
  `credential_read`, `credential_mint` or `credential_write`, and its
  answer does not hide its values, `hides_secret_values`), and the result
  is JSON and holds no secret-named value, also no number under a secret
  name (`scrub_secret_fields` with `hide_numbers=True`); otherwise the
  entry stays `started`. A `call`
  that raises leaves its entry `started` too.
- **Credential steps.** MEHO never stores the result of a credential step,
  and never a credential value. The entry holds only the connector, op id,
  target and the keyed params hash. The rule:
  - A step that **writes or makes** a credential (`credential_write`,
    `credential_mint`) and ended well becomes `done_no_result`. "Ended
    well": the `call` of `run_governed_subop` returned, or the
    `dispatch_child` result is `ok` and not `execution_outcome:
    "uncertain"`. The entry says only that the step is finished.
  - A step that **reads** a credential (`credential_read`, or an answer
    that hides its values) is `run_again` only when both hold:
    - the read is on the short list of reads that only read, and change and
      store nothing on any system (`_READS_SAFE_TO_SEND_AGAIN`:
      `vault.kv.read`, `vault.kv.list`, `vault.kv.compare`);
    - its policy let it go ahead with no person: the verdict was "go
      ahead", and no person's approval cleared it (its own, or the approval
      of the run). For a `dispatch_child` read the dispatcher tells the
      record when the approval of the run let it go ahead
      (`mark_child_cleared_by_person`).
  - Every other credential read stays `started`, like a change with no
    stored result: a read that is not on the list (some reads also store a
    value, for example `k8s.secret.read_to_ref` writes the value it reads
    to Vault), and a read that a person approved. Its result is not stored
    either. A resume does not send it again.
  - A credential write whose end MEHO does not know (the call raised, a
    timeout after it was sent, an error or `uncertain` result) stays
    `started`, like any other step.
- **Storing.** When a step waits, the record goes on the new request as
  `resume_parent["steps"]` (left out when it is empty). Like the rest of
  `resume_parent` it is internal and never shown. For a credential write
  it is inside the encrypted copy of the run input.
- **Replaying.** `composite_resume_binding` hands the stored steps to the
  first run of that composite (`composite_resume_steps`). For each step,
  `StepJournal.take` looks for the first entry in the rest of the record
  with the same connector, op id, target and params hash. A step with such
  an entry is not sent again. The step gets a copy of the stored entry,
  so a handler that changes the result does not change what a later wait
  stores:
  - `done`: it returns the stored result (`run_governed_subop` gives
    `SubopStep(value=<stored>, replayed=True)`, `dispatch_child` gives the
    stored `OperationResult`);
  - `done_no_result`: the step is not sent again and gives back no result
    (`run_governed_subop` gives `SubopStep(value=None, replayed=True)`,
    `dispatch_child` gives an `ok` `OperationResult` with `result=None`
    and `extras["result_kept"] = False`). A later step does not get the
    credential step's answer on a resume;
  - `run_again`: `run_governed_subop` and `enforce_subop_policy` first
    check the read's policy again (`apply_step_policy` and `policy_gate`,
    the same check as a first send). Only when the answer is "go ahead"
    with no person is the read sent again, with the same target and params.
    An approval of the run does not count here. Any other answer (it must
    wait, or it is denied) stops the run with
    `composite_resume_read_not_sent`, which names the read, and the read is
    not sent. A `dispatch_child` read runs through the dispatcher again,
    with its normal checks, but the approval of the run does not cover it
    (`child_read_sent_again`): when its policy now needs a person, the
    dispatcher refuses it with a `denied` result that names
    `composite_resume_read_not_sent`. The new answer goes to the handler
    and is not stored. A stored `run_again` entry whose operation is not on
    the list is taken as `started`;
  - `started`: the run stops with `composite_resume_step_outcome_unknown`
    (`execution_outcome: "uncertain"`, `retry: false`). MEHO does not know
    if that change happened. For a credential read a person approved, the
    run stops with `composite_resume_read_not_sent` (`retry: false`), which
    names the read: MEHO does not send it again under another approval.

  **What the approver's card shows when a read runs again.** The card of
  the waiting step shows that step and its params. The read is
  not on the card. On the run after the approval MEHO reads again, and the
  approved step goes ahead only when its params are exactly the ones on the
  card (the params hash). When the new answer of the read changes those
  params, the approved step does not match: the run stops with
  `composite_resume_repeats_write` and the step is not sent. So the
  approved step runs only with the values on its card. A later step of the
  run, after the approved one, may use the new answer of the read.
- **Before the approved step.** Until the approved step has run, every step
  must come from the record. A governed step that is not in it, and is not
  `after_main_effect`, is refused before it runs
  (`composite_resume_repeats_write`). So is a `dispatch_child` step, unless
  the approved step is `after_main_effect` (that composite continues there
  on its own and may read on the way).
- **A wait after a step with no stored end.** When a step would wait and
  the record already holds a step with no stored end (`started`), the run
  after that approval could only stop at that step. So MEHO asks no person
  and stops at once with `composite_resume_step_outcome_unknown`. Not for an
  `after_main_effect` step: its run after the approval continues at that
  step and does not reach the earlier one. A finished credential write
  (`done_no_result`) and a listed credential read that needed no person
  (`run_again`) do not stop a later wait; a credential write with no known
  end does. Every other credential read (not on the list, or a person
  approved it) also stops a later wait at once, with
  `composite_resume_read_not_sent`: the error names the read and the step
  that would wait, and no approval request is made. So such a read reaches
  the target once in the whole run.
- **A second wait.** A step that needs its own approval during a resume
  waits again. Its request stores the whole record so far: the replayed
  steps, the approved step and the steps after it. Two, three or more
  waits in one run each send every change once.
- **A rejected approval** runs nothing: no resume starts.
- **No re-run.** A request of a step of a multi-step operation
  (`resume_parent` set) never runs again through
  `POST /api/v1/approvals/{id}/rerun`: it is refused with
  `multi_step_run`, and the requester starts a new run of the operation.

What a composite author does: run each change of a composite that can go on
after an approval through `run_governed_subop`, and use the `value` it
gives back (it is the stored result on a resume). A composite that keeps
`enforce_subop_policy` plus its own call still never sends a step twice,
but a later step of it that needs approval stops the run at once, with no
approval request.

The same holds on every resume path: the person's approval in the console,
REST, MCP or CLI, the agent that waited (`agent/approval_wait.py`), and a
scheduled run (its request is approved and run on the person's path). Tests:
`test_operations_composite_steps_resume.py` and
`test_operations_composite_credential_steps_resume.py` (through the real
dispatcher, counting each request that reached the target).

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

**Waiting again.** Such a step may wait for approval during a re-run, like
any other step (see "How a multi-step run goes on after an approval"). Its
own re-run continues at that step.

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
- During a re-run, until the approved sub-op has cleared, any other
  governed step that is not a stored step of the earlier run and not itself
  `after_main_effect` is refused before it runs, whatever the policy
  verdict (`composite_resume_repeats_write`).

An earlier step that is itself marked `after_main_effect` (for example a
second removal in the same cleanup) and is in the stored record is not sent
again. One that is not in the record may run, so only mark steps that are
safe to run again. Reads a composite makes on its own connector session are
not checked.

Steps that use the switch today: in `vm.deploy_from_library` the rename,
the power-on, the cleanup VM delete and the vApp destroy; in `vm.create`
the rollback delete; in `vm.import_from_library` the power-on. Tests:
`test_operations_subop_policy_gate.py` (the seam checks, on their own) and
the `#4217` tests in `test_connectors_vmware_rest_composites_write_e2e.py`
(through the real dispatcher, including three approvals in a row where
each write runs once).

A direct (non-composite) park is unchanged — `resume_parent` is NULL, so
the resume keeps the generic re-dispatch of the stored `op_id`.

#### The approved run acts on what the approver saw (#5202)

A park stores the request's parameters (hash-pinned) and the preview its
builder computed (`proposed_effect.preview`). The re-run gets only the
parameters. When a parameter is a *name* that the preview resolved to an
id, a re-run that looks the name up again may act on another object than
the one the approver saw (a rename, or a deleted and re-created item).

So `composite_resume_binding` also binds, for every park (direct or
sub-op) and for the whole re-run, `approved_preview_var`
(`operations/_preview.py`) with the parked op id and the stored computed
preview (`{}` when the park stored none). A handler reads it with
`approved_preview(op_id)`:

- `None`: an ordinary dispatch, or the re-run of a different op (a sub-op
  park re-runs its parent composite, which must not read the sub-op's
  preview as its own). The handler resolves live.
- a dict: the approved run of this op. The handler must act on what the
  preview shows, and refuse (send nothing) when the preview shows no id,
  because the approver then saw no object at all.

First user: `vmware.composite.vm.cdrom.mount_library_iso`, whose mount by
item name reads the pinned `library_item` from its preview and answers
`item_not_pinned` without it. Tests:
`test_resume_binding_hands_the_approved_preview_to_the_parked_op` in
`test_operations_subop_policy_gate.py` and the `after_approval` tests in
`test_connectors_vmware_rest_composites_cdrom_library_iso.py`.

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
  the target name from the agent's original call. This holds for a composite
  sub-op and for a direct request. While the request waited,
  that name may have moved to another target: the target was renamed, or
  deleted and a new one created with the same name. Loading by id means the
  approved write can only run against the original target. If the saved
  target no longer resolves (soft-deleted or revoked), or its connection
  settings changed since the request was made (`target_changed_since_approval`
  or `target_gone_since_approval`,
  see [An approval covers one target and one preview](#an-approval-covers-one-target-and-one-preview)),
  the waiter returns a `denied` envelope and runs nothing. The refusal
  takes the one-resumer claim, deletes the stored run input, writes one
  audit row and is stored as the run result, like the release refusal.
  When another caller (usually the console) took the claim first, the
  waiter reads the request again and returns the stored result of that
  caller's run: its refusal, or the result of the run. No stored result
  yet gives an `already_resumed` envelope that says the outcome is not
  known; the waiter never answers that the operation ran.
  Covered by `test_composite_resume_uses_saved_target_when_name_was_reassigned`
  and `test_composite_resume_fails_closed_when_saved_target_deleted_and_name_reused`
  (unit) and the two `test_agent_waiter_composite_resume_*` tests (end to
  end).
- **Per-call `fqdn`.** The agent's call may name a `fqdn` in its `target`
  dict: the virtual host the connector sends as the HTTP Host header and
  the TLS server name. The park ran with it. After the saved target passed
  the checks above, the waiter applies the same value to it again
  (`apply_target_fqdn_override` in `operations/meta_tools.py`, the same
  helper the name lookup of every call uses), in memory only, before the
  preview is built again and before the run. So the approved call reaches
  the same virtual host as the call that waited. A bare target name has no
  override, so the stored `fqdn` is used. This holds for a direct request
  and for a composite sub-op. Covered by
  `test_direct_park_resume_keeps_the_per_call_fqdn`. The operator surfaces
  run with the stored `fqdn`: the request row keeps no per-call value.
- **Direct request (no `resume_parent`).** It runs the agent's original
  `call_arguments` again with two changes: the target is the one the row
  saved by id (the target name is not looked up again), and the
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
it waits for it, with the steps already done stored on the new request. If
the agent has no grant for it, that sub-op is denied.

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
decision UI; until that lands, the operator approves or rejects in the
web interface or with `meho approvals approve|reject`. There are no MCP
approve or reject tools (`mcp/human_only.py`).

## Agent-runtime resume on `approval.{approved,rejected}` (T9 #1117)

The operator/agent split, keyed on whether the parked request belongs to
an **agent run** (`run_id` set) or a **direct operator op** (`run_id IS
NULL`):

| Path | Decision capture | Re-dispatch (agent run) | Re-dispatch (direct op) |
|---|---|---|---|
| REST `/approve` with `params` | inline | inline (`_approved=True`, the human-driven express lane) | inline (caller params, hash-verified) |
| REST `/decide`, CLI, console | durable row | **whoever wins `claim_resume`** (#2293): the waiting agent (the wrapped `call_operation` in `meho_backplane/agent/toolset.py` + `meho_backplane/agent/run.py` reads the row through `meho_backplane.agent.approval_wait.wait_for_approval_decision` and re-invokes the dispatcher via `call_operation_with_approval`), or the decide surface itself through `resume_dispatch_after_approval` with the **stored** params. The loser runs nothing (`already_resumed`). | **inline** (#1503): the decision drives `resume_dispatch_after_approval` with the **stored** params. |

For an agent-run request, the waiting agent and the decide surface both
try to run the approved op. Only the winner of `claim_resume` runs it, so
it runs once. If the agent is no longer waiting, the decide surface runs
it. On a rejection, the agent tells the model. For a direct operator op
(#1503), the decide surface runs it inline with the stored params. See
[agent-runtime.md § Awaiting-approval resume](agent-runtime.md#awaiting-approval-resume-t9-1117)
for the agent-run side, including the wait timeout
(`Settings.agent_approval_wait_timeout_seconds`, default 30 min). The wait
reads the stored row every few seconds; it no longer listens to Broadcast
(#3758).

## An add-on proposes a governed destroy (#3478)

A paired add-on signs in as a **service principal** (client credentials,
`principal_kind=service`). It **can** drive a destructive op. It cannot run
the op alone: a human must approve it. It needs no standing grant. This is
the supported way for an add-on to do two-person deletes without a person
typing each delete on the CLI.

Since #3980 the destructive tier waits for a person's approval for every
principal kind, agents included. What differs is who may approve: an agent
never approves. The rule is recorded in
[`governed-delete-operations.md`](../decisions/governed-delete-operations.md)
("Who may propose").

| Principal kind | Destructive op | Code |
|---|---|---|
| `agent` | Waits for a person's approval. It needs a matching preview hash first. No grant lets it run without a person: the grant limit is `needs-approval`. With no matching grant, it waits while `AGENT_GRANT_REQUIRED` is `off` (the default) or `observe`, and is refused (`agent_no_grant`) with `on`. Agents never approve. | `auth/permissions.py:134` `_SAFETY_DEFAULT` + `:150` `_SAFETY_CEILING`; agent branch of `operations/_validate.py:825` `policy_gate` |
| `service` | Waits for approval. No grant can approve it in advance, except a delete grant on an instance in sandbox mode (see [service-principal-grants.md](service-principal-grants.md)). | `operations/_validate.py` `_non_agent_park_reason` (the `destructive` branch, called by `_non_agent_verdict`); `operations/service_grants.py:1272` `consult_and_record_grant` refuses the tier before any grant lookup |
| `user` | Waits for approval. | `operations/_validate.py:724` `_non_agent_verdict` |

### The round trip

The add-on needs the `operator` role to call `POST /api/v1/operations/call`
and `/preview`. Paths are under `backend/src/meho_backplane/`.

1. **Preview.** The add-on calls `preview_operation` (REST
   `POST /api/v1/operations/preview`) with the exact
   `(connector_id, op_id, target, params)`. The reply carries a
   `preview_hash`. The reply does **not** carry the blast-radius statement.
2. **Call with the hash.** The add-on calls `call_operation` with the same
   values plus `preview_hash` (`operations/meta_tools.py`
   `CallOperationBody.preview_hash`, passed on to `dispatch`).
3. **Park.** The policy gate returns `NEEDS_APPROVAL`, so the dispatcher
   reaches `_handle_needs_approval` (`operations/dispatcher.py`). For a
   destructive op it runs `_destructive_binding_refusal` before it writes
   any row. That check recomputes the preview hash
   on the server and needs a valid blast-radius block on `proposed_effect`.
   A missing or wrong hash, or a missing block, is refused, and no row is
   written. On success the row stores `preview_hash`, and the blast radius
   lives on the row's `proposed_effect`. The add-on gets
   `status="awaiting_approval"` and `extras.approval_request_id`. It should
   store that id.
4. **A human approves.** A MEHO-Admin may approve their own delete.
   Everyone else needs a tenant admin to approve it,
   and break-glass never covers this tier
   (`operations/approval_queue.py` `_check_self_approval`,
   `_check_approver_group`, #3328). The approve
   step checks the preview hash again
   (`_check_destructive_preview_binding` in `operations/approval_queue.py`). The decision verbs are human-only
   on every surface, so the add-on cannot approve its own request.
5. **The op runs under the approver.**
   `resume_dispatch_after_approval` (`operations/approval_queue.py`)
   re-runs the dispatch with the **approver's** `Operator`. So the approver's
   role is what counts when the op runs, and the audit row for the delete
   names the approver. The result is saved on the row
   (`persist_resume_result`). A large result goes into a result
   handle that the add-on owns (see "Who owns a result handle" above).
6. **The add-on reads the result.** The add-on polls
   `GET /api/v1/approvals/{id}/result` (`api/v1/approvals.py`,
   `read_approval_result` in `operations/approval_queue.py`). Only the
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
- `tests/test_service_principal_grant_gate.py::test_service_principal_destructive_refused_and_grant_never_satisfies`
  — a destructive step without a preview hash is refused for a service
  principal (`preview_binding_required`), even with a live matching grant,
  and no grant-use row is written.
- `tests/test_connectors_harbor_robot_delete.py::test_agent_principal_is_denied`
  — the same op is refused for an agent principal.

## Service-principal standing grants + safety_level gate (#3151 / #3152)

The non-agent gate (`_non_agent_verdict`, `operations/_validate.py`) gained
two paired behaviours for **service** principals (`principal_kind =
service`). A person's call follows its own rule (`_person_park_reason`,
#3980; see the overview at the top of this page):

- **`safety_level` is now consulted** (#3152, resolved as option 1): a
  mutating `caution` op — and any `dangerous` op — parks even without
  `requires_approval`. Previously only `requires_approval` parked a
  non-agent op, so a `caution` mutation (`POST:/vcenter/vm/{vm}/hardware/disk`)
  executed ungated for a service principal.
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
covers a delete, satellites get no delete, and self-approval follows the
group rules (only a MEHO-Admin approves their own delete).

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
  unchanged. The preview has already looked for the grant, so its reason
  says either that a named delete grant covers the call (with the
  preview hash) or that none does and the call waits (#5492, see
  [`dispatch-request-preview.md`](dispatch-request-preview.md)).

Details: [`service-principal-grants.md`](service-principal-grants.md#sandbox-mode-4622).

## References

- `backend/src/meho_backplane/operations/approval_queue.py` — queue lifecycle (T4) + read helpers + add-on step events (`record_approval_step_event`) + park-time `expires_at` stamping and `expire_stale_requests` coalesce (#2322).
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
- `backend/src/meho_backplane/agent/approval_wait.py` — agent-runtime resume substrate (T9 #1117): reads the stored row until it is decided + re-dispatch on approval.
- `backend/src/meho_backplane/operations/meta_tools.py` — `call_operation_with_approval` (the gate-bypass re-dispatch entry point).
