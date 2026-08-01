# Checks-alert advisory (in-band, dispatch-time)

## Overview

The second in-band `extras` fragment on successful dispatch responses
(#2718, Initiative #2716 scope item 4): when the caller's tenant has a
Dashboard whose `check_dashboards.last_rollup_state` memo is `degraded`
or `critical`, the next successful dispatch — agent `call_operation` and
operator CLI alike, **read ops included** — carries one compact
`extras["checks_alert_advisory"]` entry naming it, once per
`(caller, dashboard, state)` window. Ambient awareness for anyone
actively working through the backplane; delivery to people *not*
dispatching is the sibling email path (#2719), not this fragment.

"Carries" is about the response envelope, which is the same on both
fronts. What each front then *shows* differs, and only the agent front
shows it unprompted today — see
[Operator reach](#operator-reach-json-only-today).

## Key types

- `meho_backplane.checks.advisory.build_checks_alert_advisory(operator)`
  — the builder; returns
  `{"checks_alert_advisory": [{"dashboard_id", "name", "state"}, ...]}`
  or `{}`.
- `CHECKS_ADVISORY_EXTRAS_KEY = "checks_alert_advisory"` — the extras
  key, sibling of `broadcast.history.ADVISORY_EXTRAS_KEY`
  (`target_activity_advisory`, #2550).
- `settings.checks_alert_advisory_window_minutes` — dedupe window,
  default `30`, `ge=0`; `0` disables the feature before any I/O.

## Control flow

```text
dispatcher._reduce_and_audit_success(...)   # after audit + broadcast
  → build_target_activity_advisory(...)     # 2550 fragment (write-gated)
  → build_checks_alert_advisory(operator)   # this fragment (all classes)
      [gate — returns {} without any I/O when:]
        · settings.checks_alert_advisory_window_minutes == 0
      [otherwise:]
        → SELECT id, name, last_rollup_state FROM check_dashboards
          WHERE tenant_id = :t AND last_rollup_state IN
          ('degraded','critical')            # check_dashboard_tenant_idx
          ORDER BY name LIMIT 10             # _ADVISORY_MAX_DASHBOARDS
        → one MULTI/EXEC batch, staging per hit: SET meho:checks:
          advisory:{tenant}:{principal_sub}:{actor_sub|-}:{dashboard_id}:
          {state} 1 NX EX <window-seconds>   # ONE awaited round-trip
        → include the dashboard ONLY when its SET claimed the key
  → wrap_ok_result(..., extras={**activity, **checks})
```

## Design contract

- **All op classes.** Deliberate divergence from the write-gated #2550
  precedent: that gate exists because the target-activity advisory is
  mutation-overlap-specific. A non-green Dashboard concerns every active
  caller; the per-caller NX dedupe is what keeps that from becoming
  spam.
- **Bounded per dispatch — two round-trips, flat.** The claims are
  staged into a single pipelined `MULTI`/`EXEC` batch, so the Valkey
  cost is one awaited round-trip however many Dashboards are red. That
  matters because the NX dedupe *cannot* help here: a claim has to be
  issued before anyone can know whether it will succeed, so an
  unpipelined loop would pay a round-trip per row on **every**
  successful dispatch, read ops included, for as long as the tenant is
  red — worst exactly during the incidents the feature exists to
  announce.
- **`_ADVISORY_MAX_DASHBOARDS = 10`** caps the SELECT, and with it the
  `SET`s staged and the bytes the fragment adds. It is sized as an
  awareness nudge, not an audit — the rationale of the #2550
  precedent's `_ADVISORY_MAX_ENTRIES = 5`, the constant it plays the
  role of. (Its sibling `_ADVISORY_SCAN_LIMIT = 100` bounds a single
  `XREVRANGE` — one round-trip whatever the count — so its *value*
  carries no argument for a cap that also decides what the agent
  reads.) What the value buys here is bytes of unsolicited agent
  context, and `extras` is attached after the dispatcher's JSONFlux
  reduction, so nothing downstream trims it. One query is both the read
  and the entry source, so claims and entries share the cap — which is
  also what makes a claimed key mean "this caller was told".
  `ORDER BY name` makes the truncation deterministic.
- **The Valkey TTL key IS the delivery state.** Nothing durable is
  persisted; a Valkey flush re-reminds each caller once. A state change
  mints a new key, so escalation re-announces immediately; window expiry
  re-reminds on unchanged state.
- **Caller identity** is the `(principal_sub, actor_sub)` pair — the
  same pair #2550 uses for its self-drop — so a delegated agent and its
  human principal are distinct callers.
- **Memo-backed states only.** The advisory reads the transition memo
  (maintained exactly-once-per-transition by `checks/investigate.py`'s
  compare-and-swap), never the on-read rollup — so a staleness-derived
  `unknown` is not reflected, and a `NULL` memo (never-transitioned
  Dashboard) yields nothing. Documented limitation, acceptable for an
  awareness nudge.
- **Fail-open, advisory-only.** A broad guard swallows any DB/Valkey
  error, warn-logs `checks_alert_advisory_failed`, and returns `{}`.
  Never gates, blocks, or fails a dispatch.
- **Server-derived fields only** — no free-form sensor evidence enters
  the op response (Initiative #2543 untrusted-prose discipline).

## Dependencies

- `check_dashboards.last_rollup_state` (#2506's column, #2507's CAS
  writer) — read-only here; this module never writes the memo.
- `broadcast.client.get_broadcast_client()` — the shared fast Valkey
  client (5 s socket timeout bounds the worst-case added latency).
- redis-py 8.x `set(name, value, nx=True, ex=seconds)` — atomic
  set-if-absent-with-TTL; returns `True` when claimed, `None` when the
  key exists (`parse_set_result`).
- redis-py 8.x `client.pipeline(transaction=True)` — async context
  manager; `pipe.set(...)` stages the command and returns the pipeline
  synchronously, `await pipe.execute()` is the one round-trip and
  replies with a result per staged command, through the *same*
  response-callback table as an unbuffered call. Same idiom as
  `broadcast/rate_limit.py`.

## Operator reach (`--json`-only today)

The fragment is on the wire for every front — the backend sets it on
the `OperationResult` envelope, so the HTTP response is identical
whether an agent or `meho operation call` issued it. What the **Go
CLI's default human render** does with it is the gap:

```go
// cli/internal/cmd/operation/call.go — printCallResult
if r.Status == "ok" {
    ... print r.Result ...
    return                      // ← returns before the extras block
}
...
if len(r.Extras) > 0 && ... {   // ← non-ok path only
```

`printCallResult` prints `extras` only on the **non-ok** branch, and
this advisory only ever rides a *successful* dispatch. So today:

| Front | Sees the fragment? |
|---|---|
| Agent / MCP `call_operation` | yes — it reads the JSON envelope |
| `meho operation call --json` | yes — full envelope, verbatim |
| `meho operation call` (human render) | **no** — extras dropped on `ok` |

That is a deliberate scope line, not an oversight left unrecorded:
teaching the human render to print `extras` on success is a CLI-wide
UX change (it would also start printing #2550's
`target_activity_advisory`, and the vendor verbs render through a
different path — `dispatch.Render` — so a one-line edit here would
leave the two fronts inconsistent). #2718 is backend-only; the CLI
render decision wants its own Task.

`cli/internal/cmd/operation/client_test.go`
(`TestCallJSONCarriesChecksAlertAdvisoryOnOK`,
`TestCallHumanRenderOmitsExtrasOnOK`) pins both halves of that table so
the gap cannot close or widen silently.

## Known issues / boundaries

- The two free-form key segments (`principal_sub`, `actor_sub`) share
  the `:` delimiter, so two distinct caller pairs could in principle
  alias one dedupe key; the aliasing window is one reminder and the
  payload is advisory-only — deliberately not escaped. No reachable
  pair aliases today: an IdP-issued `sub` is a Keycloak UUID and agent
  principals are pinned to `^[A-Za-z0-9_\-\.]+$`
  (`auth/agent_principals.py`), neither of which admits a `:`.
- The default human CLI render does not show the fragment — see
  [Operator reach](#operator-reach-json-only-today).
- Only the first 10 non-green Dashboards by name ride a fragment
  (`_ADVISORY_MAX_DASHBOARDS`); the tail is permanently invisible to
  this surface. The checks surface and the #2719 email path own
  completeness. A tenant that far into the red has bigger problems than
  advisory coverage. The fragment carries no truncation marker today —
  an agent cannot tell a capped list from a complete one.

## References

- Task #2718, Initiative #2716 (parent goal #221).
- Precedent: `docs/codebase/broadcast.md` § "Read path (dispatch-time
  target-activity advisory)" (#2550).
- Memo + CAS: `backend/src/meho_backplane/db/models.py`
  (`CheckDashboard`), `backend/src/meho_backplane/checks/investigate.py`
  (`_claim_rollup_transition`).
- Tests: `backend/tests/test_checks_advisory.py`;
  `cli/internal/cmd/operation/client_test.go` (the CLI-path pair).
