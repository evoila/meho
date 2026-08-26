# Reflex advisory + opt-in announce gate (dispatch-time)

## Overview

The **third** in-band `extras` fragment on successful dispatch responses
(#3133, Initiative #3128), beside the #2550 `target_activity_advisory`
and the #2718 `checks_alert_advisory`. It makes the backplane's
coordination levers a *default reflex* rather than an on-request
behavior: it nudges an agent session toward the discipline the backplane
exposes but does not otherwise enforce — read the feed before acting,
announce before mutating.

`#3133` ships two pieces on the shared `dispatch()` path, so an agent
`call_operation` and an operator CLI dispatch exercise the same code:

1. **Reflex advisory** (default ON, advisory-only) — a
   `build_reflex_advisory(...)` fragment merged into the same `extras`
   dict. Returns `{"reflex_advisory": "<one line>"}` or `{}`.
2. **Announce gate** (default OFF, opt-in per tenant) — a pre-execution
   dispatch gate that *rejects* a caution-or-higher write-class op
   dispatched without an active announce claim, in a tenant that opted
   in.

The advisory *nudges*; the gate *enforces*. Both copy the established
#2550 / #2718 advisory mould: fail-open, bounded, Valkey `SET NX EX`
dedupe where a dedupe applies.

## Key types

- `meho_backplane.broadcast.reflex.build_reflex_advisory(operator, *, op_id, target_name)`
  — the advisory builder; returns `{"reflex_advisory": "<line>"}` or `{}`.
- `REFLEX_ADVISORY_EXTRAS_KEY = "reflex_advisory"` — the extras key,
  sibling of `ADVISORY_EXTRAS_KEY` (`target_activity_advisory`) and
  `CHECKS_ADVISORY_EXTRAS_KEY` (`checks_alert_advisory`).
- `settings.reflex_advisory_window_minutes` — dedupe window, default
  `30`, `ge=0`; `0` disables the advisory before any I/O.
- `meho_backplane.broadcast.announce_gate.announce_gate_blocks(operator, *, op_id, safety_level, target_name)`
  — the gate decision; returns a remediation string (block) or `None`.
- `announce_gate_enabled(tenant_id) -> bool` — the cache-aware, fail-open
  per-tenant enablement resolver.
- `tenant.announce_gate_enabled` — the structured per-tenant policy flag
  (Boolean, default `False`), migration `0077`.
- `caller_has_active_announce_claim(operator, *, op_id, target_name)`
  (`broadcast/history.py`) — the shared active-claim scanner used by both
  the advisory's announce heuristic and the gate.
- `result_announce_required(op_id, remediation, duration_ms)`
  (`operations/_errors.py`) — the `denied` envelope the gate returns,
  `extras.error_code = "announce_required"`.

## Control flow

### Advisory (success path, after the audit commit)

```text
dispatcher._reduce_and_audit_success(...)   # after audit + broadcast
  → build_target_activity_advisory(...)     # #2550 fragment (write-gated)
  → build_checks_alert_advisory(...)        # #2718 fragment (all classes)
  → build_reflex_advisory(operator, op_id, target_name)   # this fragment
      [gate — returns {} without any I/O when:]
        · settings.reflex_advisory_window_minutes == 0
        · resolve_agent_session_id() is None   # no MCP session (CLI etc.)
      [otherwise, heuristics in priority order, first winner returns:]
        · read-before-act:  no audit_log row with
            agent_session_id == session AND
            path == "/mcp/tools/call/meho_broadcast_recent"
          → claim (session, "read_before_act") via SET NX EX → _READ_NUDGE
        · announce-before-mutate:  classify_op(op_id) is write-class AND
            not caller_has_active_announce_claim(...)
          → claim (session, "announce_before_mutate") → _ANNOUNCE_NUDGE
  → wrap_ok_result(..., extras={**activity, **checks, **reflex})
```

The fragment is a single line. When more than one heuristic qualifies the
builder returns the first (read, then announce) whose per-`(session,
heuristic)` dedupe claim it wins, and leaves the other heuristic's claim
untouched so a later response can still carry it — a deduped read on one
response falls through to announce on the next.

### Gate (pre-execution, after the policy gate's AUTO_EXECUTE)

```text
dispatch(...)   # Step 4, verdict == AUTO_EXECUTE, not the _approved resume
  → announce_gate_blocks(operator, op_id, safety_level, target_name)
      [returns None — no block — in order, cheapest first:]
        · classify_op(op_id) not in WRITE_OP_CLASSES
        · _SAFETY_RANK[safety_level] < _SAFETY_RANK["caution"]
        · not announce_gate_enabled(tenant_id)        # cached, fail-open
        · caller_has_active_announce_claim(...)         # active claim covers it
      [otherwise:]
        → remediation string naming meho_broadcast_announce
  → if blocked: write a "denied" audit row + return result_announce_required(...)
```

## Design contract

- **Advisory only / gate is a plain denial.** The advisory never blocks;
  it rides an already-successful response, built after the synchronous
  audit commit — never on a denied / error / awaiting-approval envelope.
  The gate is a policy rejection (`status="denied"`), **not** a
  NEEDS_APPROVAL path: no durable approval row, no four-eyes queue. The
  caller self-remediates by announcing intent and retrying.
- **Session-scoped advisory.** The nudge targets an agent session, keyed
  on `resolve_agent_session_id()`. A dispatch with no session id — an
  operator CLI / REST call, a system sweep — is not an agent session and
  gets no nudge (returns `{}` before any I/O). This is a data-driven
  no-op on the shared dispatch path, not a surface branch: an MCP
  `call_operation` carries a session and an operator `meho operation
  call` does not, so the same code nudges the former and stays silent for
  the latter.
- **Surface-agnostic gate.** The gate keys on announce-*state*, not the
  surface, so an enabled-tenant caution write with no claim is rejected
  identically over MCP and CLI. It composes with #2546 (which
  rate-limits the announce *call*): announce once, then the write passes.
- **Deduped advisory.** At most one nudge per `(agent_session_id,
  heuristic)` per window, via an atomic Valkey `SET NX EX` — the #2718
  dedupe primitive, one key per heuristic.
- **Fail-open throughout.** Any advisory error (DB / Valkey teardown,
  parse bug) is swallowed, warn-logged `reflex_advisory_failed`, and
  yields `{}`. Any gate error — an unreadable enablement policy
  (`announce_gate_enablement_read_failed`), an unreachable claim scan
  (`announce_gate_check_failed`) — resolves to *no block*. The gate only
  ever *adds* a rejection when it can positively determine the tenant
  opted in AND the claim is absent.
- **Bounded.** The read heuristic is one indexed `EXISTS` probe on
  `audit_log` (the `audit_log_agent_session_id_idx` b-tree). The announce
  heuristic and the gate share one bounded newest-first `XREVRANGE`
  (`caller_has_active_announce_claim`), capped at 100 entries over a
  1440-minute window. The enablement flag is read through a 60s per-tenant
  TTL cache. A read-class dispatch or a disabled tenant pays nothing on
  the gate.
- **`0` disables the advisory.** `REFLEX_ADVISORY_WINDOW_MINUTES` (default
  30) short-circuits before session resolution or I/O when `0`. The gate
  has no global knob — it is OFF unless a tenant opts in.

## Enablement store — why a `tenant` column

The gate's per-tenant enablement is a **structured per-tenant policy
field** — `tenant.announce_gate_enabled` (Boolean, default `False`) —
deliberately **not** `tenant_conventions` (free-form Markdown, unsuitable
for a machine-read boolean). A typed column on the canonical per-tenant
row is the minimum structured store for a single boolean, read through a
cache-aware fail-open resolver that mirrors the `broadcast_override`
resolver pattern (`broadcast/overrides.py`: 60s per-tenant TTL cache,
fail-open-to-default). A dedicated rules table + CRUD/REST/MCP/UI surface
(the full `broadcast_override` shape) is more than a one-flag policy
warrants; opt-in tenants set the flag through seeding / tenant
administration, and a management verb is a clean follow-up.

## Known issues / limitations

- **`meho_broadcast_watch` not counted.** The read-before-act heuristic
  keys on `meho_broadcast_recent` only (the discipline's read step the
  issue names). A session that only tailed the feed via
  `meho_broadcast_watch` still gets the read nudge.
- **Coarse claim coverage (v1).** A claim covers an op when its declared
  `planned_op_class` matches the op's `classify_op` class (or the claim
  declared no class) and its target attribution covers the op (or either
  is target-agnostic). A claim declared for a different write-class (e.g.
  `write` vs `credential_write`) does not cover — a deliberate, documented
  v1 strictness.
- **Bounded claim scan.** `caller_has_active_announce_claim` reads the
  newest 100 stream entries in the 1440-minute window; a caller who
  announced more than 100 events ago in a very busy tenant may fall off
  the tail (an acceptable false-negative for an awareness nudge, and
  unlikely for the announce-then-write flow the gate targets).
- **Session identity.** Both the dedupe key and the read query use
  `resolve_agent_session_id()`, which prefers the agent-run id and falls
  back to the MCP header session. A direct MCP agent session (the primary
  target) correlates cleanly; a nested agent-run-over-MCP may correlate
  imperfectly.

## Operator reach (JSON only today)

Same as the #2718 fragment: the backend sets `extras` on the response for
every front, but the human CLI render prints `extras` only on non-ok
statuses. An agent (MCP `call_operation`) sees `reflex_advisory`
unprompted; an operator sees it through `meho operation call --json`. A
gate rejection is a non-ok (`denied`) status, so its remediation *does*
surface on the human render.

## References

- Task #3133, Initiative #3128 (parent goal #2661).
- Precedents: `docs/codebase/broadcast.md` "Read path (dispatch-time
  target-activity advisory)" (#2550); `docs/codebase/checks-advisory.md`
  (#2718, per-caller Valkey dedupe).
- Data read: #2544 (structured announce claims: `planned_op_class`, TTL,
  `run_id`), `audit_log.agent_session_id` (`db/models.py`, MCP-session
  correlation, migration `0014`). Boundary: #2546 (announce rate limit).
- Code: `backend/src/meho_backplane/broadcast/reflex.py`,
  `backend/src/meho_backplane/broadcast/announce_gate.py`,
  `caller_has_active_announce_claim` in
  `backend/src/meho_backplane/broadcast/history.py`,
  the merge seam + gate in
  `backend/src/meho_backplane/operations/dispatcher.py`,
  `result_announce_required` in
  `backend/src/meho_backplane/operations/_errors.py`,
  `tenant.announce_gate_enabled` (migration `0077`).
- Tests: `backend/tests/test_reflex_advisory.py`,
  `backend/tests/test_announce_gate.py`.
