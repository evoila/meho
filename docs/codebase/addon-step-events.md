# Add-on step-event push contract

The durable outbound event subscription a paired add-on rides for its own
orchestration needs (Initiative #2900, Task #3027). It gives a paired
add-on a **durable, resumable** stream of the step events that belong to
its own work — approval outcomes and dispatch completions — replacing the
at-most-once, count-trimmed Valkey SSE feed (`api/v1/feed.py`) that a
restart silently loses events across.

Two acceptance properties define it, and both are structural rather than
best-effort:

- **Durable delivery with resume.** Every recorded event carries a
  monotonic `seq`. An add-on persists the last `seq` it consumed,
  reconnects, and reads strictly forward. Nothing recorded is lost across
  the add-on's own restarts.
- **Scoping to the paired principal's lineage.** A step event is attributed
  to a pairing at **write** time by identity, so a pairing's log only ever
  holds that pairing's events. An event outside the paired principal's
  lineage is never written into another pairing's log and therefore can
  never be delivered — even when two add-ons' events share a `work_ref`.

## Why a dedicated durable log, not the broadcast feed

Two substrates already carry these events, and neither is a durable
per-add-on subscription:

- **Broadcast** (`BroadcastEvent` on a Valkey stream, `api/v1/feed.py`) is
  at-most-once and count-trimmed. It is the operator-awareness feed; a
  subscriber that reconnects after the trim window has passed silently
  misses events. Resume is `Last-Event-Id` against the live stream, not a
  durable log.
- **`event_outbox`** (`events/outbox.py`) is durable but single-consumer:
  the drain claims rows and stamps `processed_at`. It carries
  `agent_run.completed`, not approval outcomes, and its cursor is the
  drain's, not a per-subscriber one.

Approval outcomes and dispatch completions are the events the add-on needs,
they carry the principal and `work_ref` in their lineage, and the durable
gap is exactly that they are at-most-once on the wire. So the contract adds
a small, cohesive **durable projection**: one append-only row per step
event attributed to a paired add-on, with its own `BIGSERIAL` cursor. This
is the transactional-outbox discipline (`docs/codebase/events.md`) applied
to outbound add-on delivery.

## The identity join

The load-bearing problem is attribution: a produced row (an
`approval_request`, an `agent_run`) carries the responsible principal's
Keycloak service-account **`sub`** (a UUID), while the pairing row stores
the OAuth `clientId` (`addon:<name>`). They do not join.

`AddonPairing.service_account_sub` (migration `0082`) closes the gap. At
pair time the backplane already provisions the add-on's confidential
Keycloak client; it now also fetches that client's service-account **user
id** (`KeycloakAdminClient.get_service_account_user_id` →
`GET /clients/{id}/service-account-user`) and persists it. That value is
exactly the `sub` the add-on's `client_credentials` tokens carry, so:

- a **produced row** joins to its pairing by
  `service_account_sub == approval_request.principal_sub` /
  `== agent_run.identity_sub`;
- a **subscription request** binds to its pairing by
  `service_account_sub == operator.sub` (the caller's own token subject).

Capturing it at provisioning time — the one point the backplane controls
the identity — means attribution is never inferred from an unverified
token. A pairing created before `0082` has `service_account_sub = NULL`; a
`NULL` never matches a real `sub`, so it fails closed until the add-on
re-pairs.

## Key types

- `meho_backplane.db.models.AddonStepEvent` — table `addon_step_event`
  (migration `0082`). `seq` (`BIGSERIAL` cursor), `id` (stable UUID event
  id), `tenant_id` FK, `pairing_id` FK (`ON DELETE CASCADE`), `event_kind`,
  `work_ref`, `audit_id` (convention-only reference to `audit_log.id`, same
  as `BroadcastEvent.audit_id`), `payload`, `created_at`. Index
  `(pairing_id, seq)` drives the resume read.
- `meho_backplane.operations.addon_step_events.AddonStepEventService` — the
  cohesive record + read service:
  - `record_if_owned(session, ...)` — append in the caller's transaction
    (flush, no commit) iff the owner `sub` matches a pairing. A cheap no-op
    (one indexed lookup, no write) when the producer is not a paired
    add-on, so the overwhelmingly common producer path costs almost
    nothing.
  - `record_if_owned_committed(...)` — the same, owning its own committed,
    fail-open transaction, for producer sites without an ambient
    transaction (the post-commit approval notification).
  - `resolve_pairing_for_sub(...)` — the subscription bind (token `sub` →
    pairing).
  - `list_for_pairing(pairing_id, after_seq, limit)` — the durable resume
    read; returns `StepEventListResponse` (`{items, next_cursor}`).
- `meho_backplane.api.v1.addon_pairing.list_step_events` —
  `GET /api/v1/addons/pairings/{name}/events?after=<seq>&limit=N`.

## Control flow

**Record (producer side).** Two producers call the recorder:

1. **Dispatch completions** — `operations/agent_run.py`'s terminal
   transition, in the **same transaction** as the run's status write (a
   run-transition rollback discards the step event too). Owner =
   `agent_run.identity_sub`; kind `agent_run.completed`.
2. **Approval outcomes** — `operations/approval_queue.publish_approval_event`,
   the single choke point for `pending` / `approved` / `rejected` /
   `expired`. Owner = `approval_request.principal_sub` (the requester —
   constant across the request's lifecycle, so every decision routes to the
   same add-on regardless of who decided it); kind `approval.<decision>`.
   Recorded via the fail-open committed variant, matching the surrounding
   approval-broadcast posture: a step-event write never blocks the durable
   decision, and the add-on re-reads forward by `seq`.

**Read (subscription side).** `GET /api/v1/addons/pairings/{name}/events`:

1. Require a **service** principal (a non-service principal is 403).
2. Bind the caller to its pairing by `operator.sub` → `service_account_sub`.
3. A caller whose `sub` binds to no pairing, or whose bound pairing is not
   `{name}`, gets a uniform 404 — never another add-on's log, never a
   name-existence oracle to a principal that does not own it.
4. Return the pairing's events with `seq > after`, `seq`-ordered, capped at
   `limit`; `next_cursor` is the last `seq` for the next poll.

## Scoping proof

The acceptance test (`tests/test_addon_step_events.py`) records events for
two paired add-ons in the same tenant that carry the **same** `work_ref`,
then asserts each pairing's subscription returns only its own event. The
isolation is total because attribution happened at write time by identity:
the cross-pairing containment is not a read-time filter that could be
bypassed but a property of which rows exist in which pairing's log.

## Durability posture

- The **dispatch-completion** record is in the producer's transaction —
  fully durable with the state change (transactional outbox).
- The **approval-outcome** record is a self-contained committed write at
  the fail-open notification choke point. A backplane crash in the narrow
  window between the decision commit and the step-event commit is the same
  window the existing approval broadcast already accepts; the acceptance
  criterion — no missed events across the **add-on's** restarts — holds for
  every committed row, which is what the `seq` cursor resumes over.

## Known issues / follow-ups

- **SSE / long-poll transport.** The durable substrate + resumable cursor
  read is the load-bearing contract. A push transport (SSE or long-poll
  over the durable table, cursor = `seq`) is a thin layer on top and a
  natural follow-up; the durability and resume live in the table, not the
  transport.
- **Direct `call_operation` completions.** Dispatch-completion recording is
  wired to the `agent_run` terminal transition (and is a reusable seam for
  the async governed-dispatch run handle, #3079). A direct, non-agent-run
  governed dispatch that completes synchronously is audited but not yet
  projected into the step-event log; adding that seam is additive.
- **Heartbeat binding.** `service_account_sub` also enables the finer
  heartbeat principal binding flagged in `addon-pairing.md` (verify
  `operator.sub == pairing.service_account_sub`); that hardening is not
  changed here.
- **Retention.** The log grows append-only; a retention sweep (mirroring
  the announcement-retention job) is a future operational refinement.

## References

- Initiative #2900 (add-on pairing contract), Task #3027 (this contract),
  Task #3025 (the pairing foundation, `addon-pairing.md`).
- `docs/codebase/events.md` — the transactional-outbox substrate this
  discipline mirrors; `docs/codebase/broadcast.md` — the at-most-once feed
  it complements; `docs/codebase/approvals.md` — the approval lifecycle
  producer.
