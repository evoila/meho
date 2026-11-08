# Reflex advisory (dispatch-time)

## Overview

The **third** in-band `extras` fragment on successful dispatch responses
(#3133, Initiative #3128), beside the #2550 `target_activity_advisory`
and the #2718 `checks_alert_advisory`. It makes the backplane's
coordination levers a *default reflex* rather than an on-request
behavior: it nudges an agent session toward the discipline the backplane
exposes but does not otherwise enforce — read the feed before acting,
announce before mutating.

The advisory (default ON, advice only) is a `build_reflex_advisory(...)`
fragment on the shared `dispatch()` path, merged into the same `extras`
dict. It returns `{"reflex_advisory": "<one line>"}` or `{}`. It copies
the #2550 / #2718 advisory mould: fail-open, bounded, Valkey `SET NX EX`
dedupe.

**No write waits for an announcement (#3762).** #3133 also shipped an
opt-in per-tenant announce gate that refused a caution-or-higher write
without an active claim. Only trusted agents can publish an announcement
(#3736), so the gate could refuse a person's write for an announcement
the person could never make. #3762 removed it: the gate module, the
`announce_required` refusal, the `tenant.announce_gate_enabled` flag in
the model, and the `policies.announce_gate_enabled` key of the settings
file. The database column stays (migrations only add, see
[migrations.md](migrations.md)); no code reads it. See
[the decision record](../decisions/2026-11-08-broadcast-people-read-agents-publish.md).

## Key types

- `meho_backplane.broadcast.reflex.build_reflex_advisory(operator, *, op_class, target_name)`
  — the advisory builder; returns `{"reflex_advisory": "<line>"}` or `{}`.
- `REFLEX_ADVISORY_EXTRAS_KEY = "reflex_advisory"` — the extras key,
  sibling of `ADVISORY_EXTRAS_KEY` (`target_activity_advisory`) and
  `CHECKS_ADVISORY_EXTRAS_KEY` (`checks_alert_advisory`).
- `settings.reflex_advisory_window_minutes` — dedupe window, default
  `30`, `ge=0`; `0` disables the advisory before any I/O.
- `announce_claim_gap(operator, *, op_class, target_name)`
  (`broadcast/announce_claim_lookup.py`) — the active-claim lookup. Returns
  `None` when an active claim of the caller covers the op, else the
  reason (`ClaimGap`): `no_claim`, `expired`, `other_target` or
  `other_op_class`. The advisory uses the reason in its message.

## Control flow

### Advisory (success path, after the audit commit)

```text
dispatcher._reduce_and_audit_success(...)   # after the audit commit
  → build_target_activity_advisory(...)     # #2550 fragment (write-gated)
  → build_checks_alert_advisory(...)        # #2718 fragment (all classes)
  → build_reflex_advisory(operator, op_class, target_name)   # this fragment
      [gate — returns {} without any I/O when:]
        · settings.reflex_advisory_window_minutes == 0
        · resolve_agent_session_id() is None   # no MCP session (CLI etc.)
      [otherwise, heuristics in priority order, first winner returns:]
        · read-before-act:  no audit_log row with
            agent_session_id == session AND
            path == "/mcp/tools/call/meho_broadcast_recent"
          → claim (session, "read_before_act") via SET NX EX → _READ_NUDGE
        · announce-before-mutate:  op_class is write-class AND
            gap = announce_claim_gap(...) is not None
          → claim (session, "announce_before_mutate") → _ANNOUNCE_NUDGES[gap]
  → wrap_ok_result(..., extras={**activity, **checks, **reflex})
```

The fragment is a single line. When more than one heuristic qualifies the
builder returns the first (read, then announce) whose per-`(session,
heuristic)` dedupe claim it wins, and leaves the other heuristic's claim
untouched so a later response can still carry it — a deduped read on one
response falls through to announce on the next.

## Design contract

- **Advice only.** The advisory never blocks; it rides an
  already-successful response, built after the synchronous audit commit —
  never on a denied / error / awaiting-approval envelope.
- **Session-scoped advisory.** The nudge targets an agent session, keyed
  on `resolve_agent_session_id()`. A dispatch with no session id — an
  operator CLI / REST call, a system sweep — is not an agent session and
  gets no nudge (returns `{}` before any I/O). This is a data-driven
  no-op on the shared dispatch path, not a surface branch: an MCP
  `call_operation` carries a session and an operator `meho operation
  call` does not, so the same code nudges the former and stays silent for
  the latter.
- **Deduped advisory.** At most one nudge per `(agent_session_id,
  heuristic)` per window, via an atomic Valkey `SET NX EX` — the #2718
  dedupe primitive, one key per heuristic.
- **Fail-open throughout.** Any advisory error (DB / Valkey teardown,
  parse bug) is swallowed, warn-logged `reflex_advisory_failed`, and
  yields `{}`.
- **Bounded.** The read heuristic is one indexed `EXISTS` probe on
  `audit_log` (the `audit_log_agent_session_id_idx` b-tree). The announce
  heuristic is one archive query (`announce_claim_gap`):
  the caller's own `agent_announcement` rows of the last 1440 minutes,
  newest first, at most 500 rows. The index
  `agent_announcement_tenant_principal_created_at_idx` on `(tenant_id,
  principal_sub, created_at DESC)` (migration `0135`, #5005) matches this
  query, so it reads only the caller's rows, already in order.
- **`0` disables the advisory.** `REFLEX_ADVISORY_WINDOW_MINUTES` (default
  30) short-circuits before session resolution or I/O when `0`.

## Which claims count (#4091)

The claim lookup reads the `agent_announcement` archive, the same record
`meho_broadcast_recent` reads. So a claim that `meho_broadcast_recent`
shows as active (`filter.active_only=true`) also silences the advisory.

Before #4091 the lookup read the newest 100 entries of the tenant's
Valkey stream. Those entries belong to every principal of the tenant (and
before #3758 to every audited operation, sensor runs included). On a busy
tenant the caller's claim dropped out of those 100 entries within
minutes, and the advisory told the agent to announce again although its
claim was still active. The stream is now only a wake-up signal for live
watchers, and its write is allowed to fail, so it cannot be the record.

Only trusted claims count (#3762): rows without a `claim_id` and
`owner_sub` are history from before trusted admission. Each claim is
judged by its newest step (`broadcast/claim_state.py`); a claim whose
newest step is the `completion` is finished and counts as no claim. A
claim covers the op when it was published for the caller's principal,
its finite TTL has not run out (a step without a TTL counts as expired),
it declared the op's class (or no class), and its newest step names the
op's target in `target` or `targets` (or names no target). Matching runs
on the stored values, not on the untrusted-text wrap the read tools add.

When no claim covers the op, the advisory says why, so the agent knows
what to fix. If several reasons apply, the one closest to a covering
claim wins:

| Reason | What the agent has | What the message asks |
|---|---|---|
| `expired` | a claim for this op whose TTL ran out | announce again |
| `other_op_class` | an active claim for this target, another `planned_op_class` | announce with the op's class |
| `other_target` | active claims, none naming this target | announce with this target |
| `no_claim` | no active claim at all | announce first |

## Known issues / limitations

- **`meho_broadcast_watch` not counted.** The read-before-act heuristic
  keys on `meho_broadcast_recent` only (the discipline's read step the
  issue names). A session that only tailed the feed via
  `meho_broadcast_watch` still gets the read nudge.
- **Coarse claim coverage (v1).** A claim covers an op when its declared
  `planned_op_class` matches the op's class (or the claim declared no
  class) and its target attribution covers the op (or either is
  target-agnostic). The op's class is the class its audit row stores
  (`descriptor_op_class`, see [broadcast.md](broadcast.md)). A claim declared for a different write-class (e.g.
  `write` vs `credential_write`) does not cover — a deliberate, documented
  v1 strictness.
- **Bounded claim lookup.** `announce_claim_gap` reads at most the
  caller's newest 500 announcements of the last 1440 minutes. Other
  principals' announcements never count toward that cap. Only a principal
  that announces close to the rate limit all day can miss a claim it made
  more than 500 announcements ago.
- **Session identity.** Both the dedupe key and the read query use
  `resolve_agent_session_id()`, which prefers the agent-run id and falls
  back to the MCP header session. A direct MCP agent session (the primary
  target) correlates cleanly; a nested agent-run-over-MCP may correlate
  imperfectly. The header fallback is the shape a standalone MCP client
  exercises — it has no in-process agent loop, so `agent_session_id_var`
  is unset and the whole nudge hinges on the `Mcp-Session-Id` header
  being present (see "When the advisory does *not* fire" below).

## When the advisory does *not* fire (qualifying shape)

The fragment is present on a response only when **all** of these hold.
Any one absent is a silent, correct `{}` — never a defect:

1. **The window is non-zero.** `REFLEX_ADVISORY_WINDOW_MINUTES=0`
   short-circuits to `{}` before any session resolution or I/O.
2. **The dispatch has an agent session.** `resolve_agent_session_id()`
   is non-`None` — an agent-run id (`agent_session_id_var`) *or* a
   direct MCP session's `Mcp-Session-Id` header. An operator CLI / REST
   call, a system sweep, **and a header-less MCP client that POSTed
   `tools/call` without completing `initialize`** all resolve to `None`
   and get no nudge (`audit_log.agent_session_id` lands NULL for that
   same header-less call — see `mcp_session_id_capture_mode`).
3. **The dispatch succeeded.** The fragment rides an already-ok
   response, built after the audit commit — never a denied / error /
   awaiting-approval envelope.
4. **A heuristic both qualifies and wins its dedupe claim.** Read: no
   prior `meho_broadcast_recent` row for the session. Announce:
   write-class op with no covering active claim. And the
   `(session, heuristic)` key must be unclaimed this window — a second
   `call_operation` in the same session+window is silent by design.

## Operator reach (JSON only today)

Same as the #2718 fragment: the backend sets `extras` on the response for
every front, but the human CLI render prints `extras` only on non-ok
statuses. An agent (MCP `call_operation`) sees `reflex_advisory`
unprompted; an operator sees it through `meho operation call --json`.

## References

- Task #3133, Initiative #3128 (parent goal #2661).
- Precedents: `docs/codebase/broadcast.md` "Read path (dispatch-time
  target-activity advisory)" (#2550); `docs/codebase/checks-advisory.md`
  (#2718, per-caller Valkey dedupe).
- Data read: #2544 (structured announce claims: `planned_op_class`, TTL,
  `run_id`), `audit_log.agent_session_id` (`db/models.py`, MCP-session
  correlation, migration `0014`). Boundary: #2546 (announce rate limit).
- Code: `backend/src/meho_backplane/broadcast/reflex.py`,
  `announce_claim_gap` in
  `backend/src/meho_backplane/broadcast/announce_claim_lookup.py`
  (moved out of `broadcast/history.py` in #5005),
  the claim rules in `backend/src/meho_backplane/broadcast/claim_state.py`
  (#3762), the claim lookup's index (migration `0135`), the merge seam in
  `backend/src/meho_backplane/operations/dispatcher.py`.
- Tests: `backend/tests/test_reflex_advisory.py`,
  `backend/tests/test_broadcast_human_writes_not_gated.py` (a person's
  write runs without any claim, #3762),
  `backend/tests/test_announce_claim_lookup.py` (the archive claim lookup,
  #4091),
  `backend/tests/integration/test_announce_claim_lookup_pg.py` (on a real
  PostgreSQL, `EXPLAIN` shows the lookup uses its index, #5005).
