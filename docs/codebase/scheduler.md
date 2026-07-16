# Scheduler — cron + one-off agent triggers (G11.3 P2)

## Overview

The scheduler fires P1 agent runs on durable cron + one-off triggers. It is
the floor of MEHO's 24/7 operation: an operator creates a trigger row, the
scheduler loop scans for due rows on a cadence, claims them
replica-safely, and invokes the referenced agent definition through the
G11.1-T4 invocation surface.

Two of the three G11.3 trigger shapes ship in T2 (this task, issue #823):

- **Cron** — fires repeatedly on a 5-field cron expression evaluated in
  the trigger's persisted timezone.
- **One-off** — fires once at a stored instant, then transitions to a
  terminal `fired` state.

The third shape (event-subscription, T3 #824) lives in a sibling
substrate (`event_outbox` table + drain loop in
`backend/src/meho_backplane/events/`) because events arrive
asynchronously rather than on a clock boundary. See [events.md](events.md)
for that surface.

T1 (#822) is an in-flight spike that may settle the durable-execution
layer as DBOS Transact. T2 ships on the codebase's twice-stated
"roll-our-own + `pg_try_advisory_lock` + minimal deps" posture
([topology/scheduler.py](../../backend/src/meho_backplane/topology/scheduler.py),
[memory/expiry.py](../../backend/src/meho_backplane/memory/expiry.py)).
The `scheduled_trigger` row shape is substrate-neutral so a future
DBOS rebase swaps only the loop module.

## Key types

- `ScheduledTrigger` ([db/models.py](../../backend/src/meho_backplane/db/models.py))
  — the durable row. One per trigger. Columns:
  - `id`, `tenant_id` (real FK), `agent_definition_id` (real FK to
    `agent_definition` with `ON DELETE CASCADE` — migration `0035`,
    #1480 — so deleting a definition cascade-deletes its trigger rows,
    including a cancelled one retained for audit; before 0035 the
    default `NO ACTION` made a once-scheduled definition undeletable via
    the API), `identity_sub`, `created_by_sub`.
  - `kind` (closed enum: `cron`/`one_off`), `cron_expr` (NULL for
    one-off), `fire_at` (one-off scheduled instant, NULL for cron),
    `timezone` (IANA name, default `UTC`).
  - `next_fire_at` (the hot column the loop scans, populated for
    both cron and one-off), `last_fired_at`, `status` (closed enum:
    `active`/`paused`/`cancelled`/`fired`).
  - `inputs` (JSON-shaped, nullable; `_coerce_inputs` renders it to the
    run's user-prompt input string — `"prompt"` key when present, else the
    dict as JSON, else `""` for a `NULL`/no-`inputs` trigger). For
    `kind=cron`/`one_off` a payload that renders no usable prompt (no
    `inputs`, `inputs: {}`, or a whitespace-only `"prompt"`) is **rejected
    at create** with a 422 (payload-only check, see the no-input guard
    below); `kind=event` is exempt. A `""` result that slips through (an
    event trigger, or a row inserted around the wire schema) is still
    refused typed at fire time.
  - `work_ref` (nullable Text, migration `0043`, #1663) — the opaque
    external change-ticket reference (`"gh:evoila/meho#13"`, a Jira key,
    a CR id) the trigger works under. Set at create time (triggers have
    no UPDATE path); inherited end-to-end by every dispatched run — see
    "work_ref inheritance" under Dispatch. Indexed
    `(tenant_id, work_ref)` for the list `--work-ref` exact-match filter.
- `ScheduledTriggerKind`, `ScheduledTriggerStatus` — closed StrEnums
  kept in lock-step with the DB-layer `CHECK (... IN (...))` constraints
  via migrations `0020` (T1 substrate) and `0025` (T2 dispatcher
  columns + widened `status` CHECK).
- Scheduler package
  ([scheduler/](../../backend/src/meho_backplane/scheduler)):
  - `scheduler.cron` — `next_fire_after()` (croniter wrapper),
    `is_valid_cron_expr()`, `InvalidCronExpressionError`,
    `InvalidTimezoneError`.
  - `scheduler.repository` — `create_cron_trigger()`,
    `create_one_off_trigger()`, `claim_due_triggers()` (PG `SELECT ...
    FOR UPDATE SKIP LOCKED`), `advance_cron_trigger()`,
    `mark_one_off_fired()`.
  - `scheduler.loop` — `run_one_tick()`, `start_scheduler()`,
    `stop_scheduler()`. The forever loop the FastAPI lifespan owns.

## Control flow

```
                                    +-----------------------------+
   FastAPI lifespan startup    --->  |  start_scheduler()          |
                                    |  asyncio.create_task(...)   |
                                    +-----------------------------+
                                                 |
                                                 v
                              +------------------------------------------+
                              | _scheduler_loop()                        |
                              | while True:                              |
                              |   await sleep(tick_interval_seconds)     |
                              |   await run_one_tick()                   |
                              +------------------------------------------+
                                                 |
                                                 v
                  +------------------------------------------+
                  | run_one_tick()                           |
                  | 1. pg_try_advisory_lock(MEHOSCHD)        |
                  |    (no-op on SQLite test path)           |
                  | 2. claim_due_triggers(now=now(UTC),      |
                  |       limit=50)                          |
                  |    -- SELECT ... FOR UPDATE SKIP LOCKED  |
                  | 3. for each row:                         |
                  |      kind==cron: advance + invoke        |
                  |      kind==one_off: mark fired + invoke  |
                  | 4. pg_advisory_unlock                    |
                  +------------------------------------------+
                                                 |
                                                 v
                              +------------------------------------------+
                              | resolve_agent_credentials(identity_ref)  |
                              |   -> (client_id, client_secret)          |
                              | AgentInvoker.run_scheduled(name, inputs, |
                              |     agent_client_id=..,                  |
                              |     agent_client_secret=..)              |
                              +------------------------------------------+
```

### Autonomous-agent credentials

The scheduler is operator-less (no Keycloak JWT to forward to Vault's
JWT/OIDC auth method), so it sources the `client_credentials` secret
**Vault-first** under its own static service token, falling back to a
pod env var only when Vault yields nothing (#1478). The lookup chain:

1. The trigger's `agent_definition_id` resolves to an
   `AgentDefinition.identity_ref` (e.g. `agent:reporter`). The
   `identity_ref` verbatim is the `client_id` passed to `run_scheduled`
   (Keycloak's namespace tolerates the `:` separator).
2. `resolve_agent_credentials(identity_ref)` (in
   [scheduler/credentials.py](../../backend/src/meho_backplane/scheduler/credentials.py))
   sanitises the ref (non-alphanumeric chars to `_`, upper-case) and
   resolves the secret:
   - **Vault (first).**
     [`read_agent_secret`](../../backend/src/meho_backplane/scheduler/vault_credentials.py)
     reads the secret from `SCHEDULER_AGENT_VAULT_PATH_PATTERN`
     (default `secret/data/agents/{client_id}/credentials`) under
     `VAULT_SCHEDULER_TOKEN`. The `{client_id}` token is **not** the raw
     `identity_ref` — `vault_path_for_client_id` substitutes the
     **sanitised, UPPER-CASED** form (non-alphanumeric chars to `_`,
     then `.upper()`), the same shape the env-var key uses below. For
     `agent:ops-writer` the resolved path is
     `secret/data/agents/AGENT_OPS_WRITER/credentials` (not a raw
     `agent:ops-writer` key). Both the read here and the write below call
     this one helper, so the two paths cannot diverge — an operator
     hand-provisioning the Vault secret or policy must target the
     sanitised path. The raw KV-v2 API path is split into hvac's
     `(mount_point, logical_path)` form by `split_kv_v2_api_path`.
     This is the path registration writes to (see below), so an agent
     registered + defined purely over the API is schedulable with **no
     pod env var and no redeploy**. A missing path / unset token / read
     error falls through to the env var.
   - **Env var (fallback / break-glass).** When Vault yields nothing,
     the secret is read from the env var derived from
     `SCHEDULER_AGENT_SECRET_ENV_PATTERN` (default
     `MEHO_AGENT_SECRET_{client_id}`). For `agent:reporter` the
     resolved env var is `MEHO_AGENT_SECRET_AGENT_REPORTER`. Operators
     wire it the same way `ANTHROPIC_API_KEY` is wired when Vault is
     unavailable.
3. When **neither** source yields a secret,
   `AgentCredentialsUnresolvedError` is raised; the loop logs
   `scheduler_credentials_unresolved` and skips the fire. The trigger
   stays `active` (fire state untouched) so a subsequent tick retries
   once the secret is available. Since #2327 the skip is recorded on the
   row (`last_skip_reason='credentials_unresolved'`, `skip_count++`) and,
   after `_PARK_AFTER_CONSECUTIVE_SKIPS` consecutive skips, the trigger is
   parked (`status='paused'`) so a permanently-unresolvable secret stops
   the silent every-tick loop — see "Skip-state projection + park-after-N".

The write side: registering an agent principal
([`AgentPrincipalService.register`](../../backend/src/meho_backplane/auth/agent_principals.py))
captures the Keycloak-generated client secret (`get_client_secret`) and
persists it to Vault at `SCHEDULER_AGENT_VAULT_PATH_PATTERN`
([`write_agent_secret`](../../backend/src/meho_backplane/scheduler/vault_credentials.py)),
under the same scheduler service token. The write resolves the path
through the **same** `vault_path_for_client_id` helper as the read, so it
lands on the sanitised, UPPER-CASED path (`agent:ops-writer` →
`secret/data/agents/AGENT_OPS_WRITER/credentials`) — write and read can
never target different keys. A Vault-write failure rolls back the
just-created Keycloak client so registration never produces an
unschedulable agent.

`VAULT_SCHEDULER_TOKEN` is a static token bound to a narrow read/write
policy on the agent-credentials path — the lowest-friction
operator-less Vault identity (it reuses hvac's `Client(token=…)`
primitive with no AppRole `secret_id` bootstrap). Operators preferring
AppRole run a Vault Agent sidecar that renews a token into the env var:
additive, no code change.

The documented mint is a **periodic** token (`-period=768h`), which
expires `period` after its last renewal. To keep it alive the broker
fires a best-effort `auth/token/renew-self` after every successful
read/write (`_maybe_renew_scheduler_token`), renewing the token at
tick frequency so it never ages out while the pod runs; failures are
logged and swallowed. `verify_scheduler_token` runs
`auth/token/lookup-self` at scheduler startup and hourly (driven from
`loop.py`) and logs a dead/unreachable token loudly, cutting
time-to-notice from weeks to minutes. The token is resolved from its
live source per use (`_current_scheduler_token`): setting
`VAULT_SCHEDULER_TOKEN_FILE` points at a sidecar's token sink that is
re-read on every read/write, so a re-mint is picked up without a pod
restart (#2328).

### Precondition gate vs invoke-time failure

The two fire paths follow the same lifecycle shape:

1. **Prepare** (`_prepare_invocation`) — look up the agent definition
   (FK; real-FK lookup-by-primary-key) and resolve the agent's
   `client_credentials` pair Vault-first (env-var fallback). Returns a
   `_PreconditionSkip` (carrying a machine-tag `reason`; skip without
   advancing fire state) when any precondition fails:
   - the agent definition was removed since trigger creation
     (`definition_missing`), or
   - the definition is disabled (`definition_disabled`), or
   - the agent's secret is in neither Vault nor the fallback env var
     (`credentials_unresolved`).

   The caller projects that reason onto the trigger row rather than
   skipping silently — see "Skip-state projection + park-after-N"
   below (#2327).
2. **Advance / mark-fired** — only when the prepare step succeeded.
   The conditional `UPDATE` (status / next_fire_at guard) commits
   the row's state transition.
3. **Dispatch** (`_dispatch_invocation`) — call
   `AgentInvoker.run_scheduled` (G11.2-T2 #1096). Invocation-time
   failures (Keycloak grant timeout, identity-binding refusal) are
   logged and swallowed under the at-most-once contract — the
   state transition has already committed; the missed fire is
   visible in audit and an operator can re-create / re-fire via
   the admin surface (T5 #826).

This split is load-bearing for one-off triggers: without the
precondition gate, an unresolved agent secret would be committed as
`status='fired'` before `_dispatch_invocation` could reject the
fire, silently consuming the one-off. The gate keeps the
at-most-once contract honest for invoke-time failures (where it was
always the right behaviour) without dropping work for the
precondition cases (where it was always recoverable by the
operator).

### Skip-state projection + park-after-N (#2327)

A precondition skip leaves the trigger's fire state untouched so a
*transient* miss self-heals on the next tick once the operator fixes the
cause. But before #2327 the skip was invisible on the row: a **permanent**
miss (revoked scheduler Vault token, deleted-but-still-referenced
definition, never-persisted agent secret) produced an infinite silent
loop whose only trace was a WARN pair in the pod log every tick.
`scheduler list` showed a healthy-looking `active` trigger; a real deploy
lost ~360 hourly fires over 15 days before anyone noticed.

The fix projects the cumulative skip state onto the row and parks a
permanently-broken trigger:

- **Three columns on `scheduled_trigger`** (migration `0057`):
  - `last_skip_reason` (`text`, nullable) — the machine tag of the most
    recent skip cause (`definition_missing` / `definition_disabled` /
    `credentials_unresolved`; a park path also stamps `invalid_cron_expr`
    / `unknown_kind`).
  - `last_skipped_at` (`timestamptz`, nullable) — UTC time of the most
    recent skip.
  - `skip_count` (`integer` NOT NULL, default 0) — **consecutive** skips
    since the last successful fire.
- **On each skip** the loop calls `_record_skip`: increment `skip_count`,
  stamp `last_skip_reason` / `last_skipped_at`. The row's `next_fire_at`
  (cron) / `status='active'` (one-off) is still untouched, so the
  at-most-once contract and the transient-retry behaviour are unchanged —
  the columns are additive visibility only.
- **Park at the cap**: once `skip_count` reaches
  `_PARK_AFTER_CONSECUTIVE_SKIPS` (a module constant, 10, matching the
  `_CLAIM_BATCH_LIMIT` "dumb fixed loop-bound" posture — no per-deployment
  tunable) the same `_record_skip` transitions the row to
  `status='paused'`. The state machine itself now says "broken, stopped
  trying" instead of re-tripping every tick forever. At the default 30 s
  tick that's ~5 min of an unresolvable trigger — past any normal
  credential-rotation window.
- **Reset on recovery**: a successful `_prepare_invocation` breaks the
  streak. When `skip_count > 0` the loop calls `_clear_skip_state` before
  the advance / mark-fired step, resetting `skip_count` to 0 and clearing
  the reason / timestamp. The healthy hot path (a well-behaved trigger's
  every fire) issues no extra `UPDATE` — the reset only fires when there
  was a streak to clear.
- **Parks carry a reason too**: the corrupt-cron and unknown-kind park
  paths (`_park_trigger`) also stamp `last_skip_reason` / `last_skipped_at`
  now, so every paused-by-the-loop row explains itself on the read
  surfaces.

The state is surfaced everywhere the row is read: `ScheduledTriggerRead`
carries the three fields, so `GET /api/v1/scheduler/triggers`,
`meho.scheduler.list` / `.show` (MCP), `meho scheduler list` (a `SKIPS`
column), and the operator console (a warning badge on the list row + a
skip block on the detail page) all agree with the pod-log WARNs. This is
the same read-surface projection pattern `last_fired_at` / `next_fire_at`
already use.

### work_ref inheritance (#1663)

A scheduled trigger's `work_ref` is inherited by every run it
dispatches, end-to-end:

1. `_prepare_invocation` copies `row.work_ref` onto
   `_PreparedInvocation.work_ref`.
2. `_dispatch_invocation` forwards it as the `work_ref=` argument to
   `AgentInvoker.run_scheduled`.
3. `run_scheduled` binds the value onto the shared `work_ref_var`
   ContextVar for the duration of the call. `_create_run_row` reads
   that ContextVar at run-create time, so the dispatched
   `agent_run.work_ref` lands the trigger's ref; the background loop
   task snapshots the ContextVar at `asyncio.create_task` time (in
   `_launch_run`), so every per-tool-call `audit_log` row the run
   produces inherits it too.

This is the trigger → dispatched-run seam the work_ref Initiative
(#1654) widened: before #1663 the dispatch carried only name + inputs
and `agent_run` had no trigger linkage, so a dispatched run could not
inherit the trigger's ref. `work_ref` is set-at-create-only and binds
nothing when the trigger carries no ticket (the run lands `NULL`).

### Cron fire path (`_fire_cron`)

1. `_prepare_invocation(row)` → `_PreparedInvocation` or
   `_PreconditionSkip`. On a skip the loop records the skip state
   (`_record_skip`) and the trigger's `next_fire_at` stays unchanged so
   the next tick re-claims and re-tries; on success it clears any prior
   skip state before advancing.
2. `advance_cron_trigger(row, fire_instant=now)`:
   - Compute the next cron match via `croniter` from `now` in the
     trigger's timezone.
   - Conditional `UPDATE` (`WHERE id=:id AND status='active' AND
     next_fire_at=:previous_next`) sets `next_fire_at=new_next` and
     `last_fired_at=now`.
   - Returns `None` when zero rows match (another claimer beat this
     replica to it) — skip the fire.
3. Commit the advance.
4. `_dispatch_invocation(row, prepared, invoker)` calls
   `AgentInvoker.run_scheduled` (G11.2-T2 #1096) which obtains a
   Keycloak token, verifies it, and kicks off the agent run with
   `AgentRunTrigger.SCHEDULED` provenance.

### One-off fire path (`_fire_one_off`)

1. `_prepare_invocation(row)` → `_PreparedInvocation` or
   `_PreconditionSkip`. On a skip the loop records the skip state
   (`_record_skip`) and the trigger stays `status='active'` (the row is
   **not** consumed) so the next tick re-claims and re-tries once the
   operator fixes the underlying issue.
2. `mark_one_off_fired(row, fire_instant=now)`:
   - Conditional `UPDATE` (`WHERE id=:id AND status='active' AND
     next_fire_at=:previous_next`) sets `status='fired'`,
     `last_fired_at=now`.
   - Returns `None` when another claimer beat this replica to it.
3. Commit, then dispatch the invocation.

### Replica-safety property

Two backplane pods sharing one Postgres are coordinated by two layers:

1. **Process-wide advisory lock** (`pg_try_advisory_lock(MEHOSCHD)`):
   only one replica's loop runs the tick body at a time. The losing
   replica's `try_advisory_lock` returns `False` and the tick is skipped.
   Non-blocking.
2. **Per-row claim under `FOR UPDATE SKIP LOCKED`** plus the conditional
   `UPDATE` in the advance/mark-fired step. Belt-and-braces — even if the
   advisory lock were removed, single-fire would still hold across
   concurrent claimers because the conditional `UPDATE` matches zero rows
   on the loser side.

On SQLite (the unit-test path) both layers no-op. The
two-concurrent-ticks test
([tests/test_scheduler.py](../../backend/tests/test_scheduler.py))
launches two `run_one_tick` coroutines on the same DB; the
conditional-`UPDATE` discipline is what enforces single-fire and
exercises the same guard that runs on PG when advisory-lock acquisition
ordering creates a brief window of double-claim possibility.

### Restart durability

State lives in the `scheduled_trigger` row. On pod restart:

- A cron trigger whose `next_fire_at` has already passed fires once on
  the next tick, advances to the next cron match, resumes normal cadence.
  No catch-up storm — a 24-hour outage on `*/5 * * * *` fires exactly
  once on resume rather than 288 times.
- A one-off trigger whose `next_fire_at` has already passed fires once
  on the next tick and transitions to `fired`.

The "compute next then fire" discipline (advance BEFORE invoking the
agent) is what guarantees single-fire: `next_fire_at` is already
persisted at the next scheduled instant by the time the agent run
starts, so a missed/duplicated claim cannot replay it. The wait on
the agent loop itself is separately bounded inside `run_scheduled`
(`AGENT_SYNC_TIMEOUT_SECONDS`, default 30 s) so a hung or
approval-gated run cannot stall later ticks or strand the advisory
lock — see "Known issues / limitations" (#1502).

## Dependencies

- **`croniter` 6.x** — pure-Python, ~1.5 kLoC, single-purpose, MIT
  licensed. The only new runtime dependency this task introduces.
  Confined to `scheduler.cron`; the rest of the codebase depends on the
  module-level `next_fire_after()` / `is_valid_cron_expr()` seam.
- **`AgentInvoker.run(..., trigger=AgentRunTrigger.SCHEDULED)`** (G11.1-T4)
  — the invocation surface grew a `trigger` keyword alongside this task
  so the durable `agent_run` row's provenance column reflects the
  scheduler vs. a direct REST/MCP/CLI invocation.
- **`pg_try_advisory_lock` / `pg_advisory_unlock`** — Postgres
  session-level advisory locks. No-op on SQLite via dialect gate.
- **`SELECT ... FOR UPDATE SKIP LOCKED`** — SQLAlchemy 2.0
  `with_for_update(skip_locked=True)`; emitted only on PG.

## Settings

| Setting (env var) | Default | Notes |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | Lifespan skips starting the loop when `false`. Operators with an external orchestrator can opt out. |
| `SCHEDULER_TICK_INTERVAL_SECONDS` | `30` | Cadence of the scan-for-due loop. Floor 1 s, ceiling 3600 s. 30 s is the consumer-doc-accepted granularity (cron's finest field is a minute). |

## Fire-time latency contract (#2245)

`fire_at` / `next_fire_at` are a **floor, not an exact dispatch time**. The
loop scans on a fixed grid every `SCHEDULER_TICK_INTERVAL_SECONDS` (default
30 s) and claims rows whose `next_fire_at <=` the tick instant, so a trigger
fires on the **first tick at or after** its requested time and dispatch can
trail that time by **up to one whole tick interval** (worst case).
`last_fired_at` is stamped with the *claiming tick instant*, not with
`fire_at`/`next_fire_at`, so two consecutive fires read back exactly
tick-aligned. That quantization — not failure fallout — is what produced the
reported "~28 s post-failure delay" (#2245): a `fire_at` that lands just after
a tick waits nearly a full interval for the next one. The window is designed
and bounded; there is **no backoff constant**, and per-fire failure isolation
already exists (PR #1509, v0.11.0), so a failed fire does not shift the grid
for the next trigger.

The window is now part of the **API contract**, not just this internal doc:
`ScheduledTriggerCreate.fire_at` and
`ScheduledTriggerRead.{fire_at,next_fire_at,last_fired_at}` carry it in their
OpenAPI field `description` (`scheduler/schemas.py`), so consumers can plan
SLAs against it directly from the generated client/spec.

**Tuning knob.** An SLA-sensitive deployment that needs tighter fire
resolution lowers `SCHEDULER_TICK_INTERVAL_SECONDS` per deployment (floor
1 s, e.g. via the chart's `extraEnv`); worst-case latency drops to the chosen
interval at the cost of one extra scan query per elapsed tick.

## Known issues / limitations

- **One-off resolution is "to the second"** — `next_fire_at <= now`
  semantics fire as soon as the tick after the scheduled instant runs.
  With a 30 s tick, a one-off scheduled for `12:00:00` fires somewhere in
  `[12:00:00, 12:00:30]`. Cron has the same semantics: `0 12 * * *`
  fires in `[12:00:00, 12:00:30]`. Tightening this needs a smaller tick;
  the loop is bounded by `_CLAIM_BATCH_LIMIT=50` rows per tick to keep
  per-tick wall-clock cost low. The `[fire_at, fire_at + tick]` window is
  the API-level **fire-time latency contract** above (#2245) — now carried
  on the schema field descriptions, not only here.
- **Catch-up policy is "one fire on resume"** — a long outage does not
  replay every missed cron instant. The consumer doc accepts this; an
  operator who needs "fire-every-N-runs" semantics writes that into the
  agent definition itself.
- **`AgentRunTrigger.SCHEDULED` provenance** — passed through to
  `AgentInvoker.run`'s new `trigger` kwarg. Audit queries that filter by
  trigger see scheduled runs distinctly from direct invocations.
- **A blocking run is abandoned, not reclaimed, by this loop** (#1502) —
  `run_scheduled` bounds its wait by `AGENT_SYNC_TIMEOUT_SECONDS`
  (default 30 s) and, on timeout, returns the still-running handle
  (`converted_to_async`) while the agent loop keeps running in the
  background. This is what keeps the serial tick non-blocking and frees
  the advisory lock each tick even when a run hangs or blocks on a
  `requires_approval` wait (up to `AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS`,
  default 30 min). The lock is therefore held at most one bounded wait
  per blocking run per tick, not for the run's whole lifetime. Driving
  the abandoned background run to a terminal state (lease/heartbeat
  reaper) is a separate concern (T1 #1501), not this loop's job.
- **No-usable-prompt cron/one_off rejected at create; fire-time guard is
  defense-in-depth** (#1505 fire-time, #2244 create-time) — a `cron` or
  `one_off` trigger whose `inputs` render no usable prompt (no `inputs`,
  `inputs: {}`, or a whitespace-only `"prompt"`) is **rejected at create**
  with a 422. The check lives in `ScheduledTriggerCreate`'s
  discriminated-union validator (`_payload_yields_prompt`) and is
  **payload-only** — it loads no agent definition, so it sidesteps the
  layering objection that originally kept this fire-time-only: a cron that
  fires every tick and a one_off that burns its single fire with no user
  turn are deterministic failures the payload alone reveals. It also closes
  the `inputs: {}` edge, which `_coerce_inputs` renders to the literal
  `"{}"` — non-whitespace, so it slips past the fire-time guard and reaches
  the model as a meaningless `"{}"` turn. `kind=event` is **exempt**: its
  future payload-dispatch junction (`events/drain.py`, still a no-op at
  HEAD) may legitimately derive the prompt from the matched event, so an
  input-less event trigger stays creatable.

  The fire-time guard is retained as defense-in-depth for the paths the
  create check does not cover — an `event` trigger, or a row inserted
  directly around the wire schema. At fire time `run_scheduled` detects the
  empty prompt (`prompt_is_effectively_empty`) **before** the model call and
  finalises the run `failed` with a `scheduled_run_no_input`-tagged `error`
  (`SCHEDULED_RUN_NO_INPUT_CLASS`), rather than letting it reach the
  provider as a system-prompt-only request with an empty `messages` array
  (every supported backend 400s on that). The scheduler logs
  `scheduler_fired_run_failed` (not `scheduler_fired`) so the
  misconfiguration is visible at fire time. The fire still counts (a
  one-off is consumed, a cron has advanced) — the fix is operator-side
  (add `inputs`), not a scheduler retry. MEHO deliberately does **not**
  inject a synthetic user turn (it would misrepresent operator intent); a
  genuine no-user-turn autonomous run shape would be a distinct feature.
- **Pause / resume not exposed yet** — the T5 admin surface ships
  create / list / cancel; pause-then-resume of an active trigger is
  not in v0.2. `ScheduledTriggerStatus.PAUSED` exists in the enum and
  the cancel path admits paused→cancelled transitions, but no public
  verb writes paused. Operators that need a temporary disable today
  cancel and re-create.

## Admin surface (T5 #826)

The T5 task lands the create / list / cancel verbs across all three
transports. The single code path is
`backend/src/meho_backplane/scheduler/service.py`'s
`SchedulerAdminService`; the REST routes, MCP tools, and CLI verbs
each translate transport-shaped arguments into service calls.

### Verbs

| Verb | REST | MCP | CLI | UI | Role |
|---|---|---|---|---|---|
| Create | `POST /api/v1/scheduler/triggers` | `meho.scheduler.create` | `meho scheduler create` | `POST /ui/scheduler/create` | `tenant_admin` |
| List | `GET /api/v1/scheduler/triggers` | `meho.scheduler.list` | `meho scheduler list` | `GET /ui/scheduler` | `operator` |
| Detail | `GET /api/v1/scheduler/triggers` (filtered) | — | — | `GET /ui/scheduler/{id}` | `operator` |
| Cancel | `DELETE /api/v1/scheduler/triggers/{id}` | `meho.scheduler.cancel` | `meho scheduler cancel <id>` | `POST /ui/scheduler/{id}/cancel` | `tenant_admin` |

The discriminated-union validator on `ScheduledTriggerCreate` enforces
exactly one of `cron_expr` / `fire_at` / `event_filter` per kind. An
invalid cron expression surfaces as `invalid_arguments` at the
boundary; an unknown `agent_definition_id` surfaces as
`agent_definition_not_found` (422 / MCP invalid-params).

### `kind=event` is refused at create until #826 (#2325)

`kind=event` trigger creation is **refused** with a structured 422
`event_triggers_not_implemented` (MCP invalid-params with the same
code; UI modal banner naming #826) on every transport. The refusal
lives in one place — `SchedulerAdminService.create` raises
`EventTriggersNotImplementedError` before any DB write — so the wire
schema still models the `event` kind but no `event` row is ever
persisted.

Why: the event-subscription matcher in
`backend/src/meho_backplane/events/drain.py` is still the documented
T5 no-op (it stamps `processed_at` on drained rows without consulting
`scheduled_trigger`). Real producers already emit onto the outbox —
`backend/src/meho_backplane/operations/agent_run.py` publishes
agent-run terminal-transition events — so those events land in
`event_outbox` and are silently swallowed. Accepting a trigger that
reports `status=active` but can never fire is dishonest to the
operator; refusing at create is the honest shape until #826 wires the
matcher. The guard is a single create-site check (not a feature-flag
system) and is removed in the same change that lands #826, at which
point the `create_event_trigger` branch below the guard dispatches for
real.

**Pre-existing rows.** Event triggers created `active` before this
refusal landed are parked to `paused` by a one-shot startup reconcile
(`reconcile_active_event_triggers` in `scheduler/loop.py`, run once at
the top of `_scheduler_loop` before the first tick). Because an event
trigger carries no `next_fire_at`, the tick loop's `claim_due_triggers`
scan never sees it, so an in-loop park path would never fire — the
reconcile is the deliberate cleanup. The park reason is logged under
`scheduler_event_triggers_parked` (`reason=event_triggers_not_implemented:826`),
mirroring the `_park_trigger` "reason is logged for audit" precedent
since the row has no reason column; the parked state is visible via
`GET /api/v1/scheduler/triggers?kind=event&status=paused` and the UI
list. The reconcile is idempotent and removed alongside the guard when
#826 lands.

### Cross-tenant admin

`tenant_admin` callers may target another tenant by passing
`tenant_id` in the create body or `tenant_filter` in the list /
cancel query (REST) or `tenant_id` in the MCP create arguments. The
MCP create handler rejects `tenant_id` from `operator` callers with
`tenant_id_requires_tenant_admin` (it does *not* silently drop the
field; review M1 on PR #1128). The REST list / cancel routes use the
same role gate. Audit rows carry
`audit_tenant_scope=other|self` so cross-tenant activity is
greppable.

### Cancel idempotency + 404 / 409 contract

Cancel is **idempotent**: a second cancel against an already-cancelled
trigger returns 204. A cancel against a row that hit terminal
`fired` returns 409 `trigger_already_fired` — the lifecycle is
`fired → end`, not `fired → cancelled`. A cancel against an absent
or cross-tenant id returns 404 `trigger_not_found` (the existence of
the trigger is **not** leaked across the tenant boundary via a
403 / 404 differential).

Concurrent cancels race safely via a read-after-update pattern: the
pre-flight SELECT classifies obviously-already-terminal states; the
conditional UPDATE matches the active / paused set; on rowcount==0
the service re-reads the row and treats `cancelled` as success
(idempotent), `fired` as 409, and absence as 404. This closes the
phantom-409 TOCTOU window flagged in review B1 on PR #1128.

### CLI input safety

`meho scheduler create --event-filter @<path>` / `--inputs @<path>` /
`--event-filter @-` reads from a file or stdin with a 256 KiB cap
enforced via `io.LimitReader` (review M4 on PR #1128 — an unbounded
read could OOM the CLI on an adversarial JSON file). The same helper
rejects JSON `null` explicitly (review M3 on PR #1128 — `json.Unmarshal`
of `null` into `map[string]any` sets the map to nil without error,
which would silently forward an empty body field that the backend
cannot distinguish from "omitted").

### CLI transport (G0.12-T13 #1271)

`cli/internal/cmd/scheduler/` drives the generated
`api.ClientWithResponses` surface directly: `api.NewAuthedClient`
wires the bearer + lazy 401-refresh editor onto the embedded
`ClientWithResponses`, and the verbs call the typed `*WithResponse`
methods (`ListTriggersApiV1SchedulerTriggersGetWithResponse`,
`CreateTriggerApiV1SchedulerTriggersPostWithResponse`,
`CancelTriggerApiV1SchedulerTriggersTriggerIdDeleteWithResponse`).
Consumer-side struct duplicates of the backend pydantic models
(previously `Trigger`, `ListResponse`) are gone — every wire shape
is now sourced from `cli/internal/api/client.gen.go`
(`api.ScheduledTriggerRead`, `api.ScheduledTriggerListResponse`,
`api.ScheduledTriggerCreate`) so a schema drift between the backend
and the CLI surfaces at `go build` time rather than as the kind of
#1069-class silent loss the freshness gate at
`.github/workflows/cli-api-snapshot.yml` is designed to catch.

A 1 MiB response-body cap is installed at the transport layer via
an inline `capRoundTripper` threaded through
`api.AuthedClientOptions.HTTPClient` (mirroring the T12 retrieval
sibling on PR #1286). The wrapper re-binds every response body to
`http.MaxBytesReader` so the generated `Parse*Response` helpers
(which `io.ReadAll(rsp.Body)` into the typed envelope) can't be
pinned by an adversarial / runaway backplane response.

### Operator console (`/ui/scheduler`, G10.8-T6 #1826)

The fourth transport. `backend/src/meho_backplane/ui/routes/scheduler/`
adds the operator-console surface for the same verbs. Like every other
write-bearing `/ui/*` surface it is a **session BFF**: the browser
carries only the BFF session cookie + the CSRF double-submit token (it
cannot authenticate the Bearer REST routes), so the UI routes are
`require_ui_session` + CSRF-gated and call `SchedulerAdminService`
**in-process** — the same `list_` / `get` / `create` / `cancel` the REST
/ MCP / CLI surfaces share. The in-process call keeps the
synchronous-audit binding (each handler binds `audit_op_id` =
`scheduler.list` / `scheduler.create` / `scheduler.cancel` so the chassis
audit + broadcast hooks classify the row, mirroring the REST route).

Surface map:

* `GET /ui/scheduler` — list (operator). Dual full-page / HTMX-fragment
  table with kind + status + work_ref filters; soft-hides the
  create / cancel affordances from non-`tenant_admin` sessions.
* `GET /ui/scheduler/{id}` — detail (operator). Full trigger row +
  governance fields; cancel button hidden on a terminal trigger.
* `GET`/`POST /ui/scheduler/create` — create modal + submit
  (`tenant_admin`). Alpine kind-switch; the `cron` branch live-validates
  via `POST /ui/scheduler/validate-cron`, which reuses
  `is_valid_cron_expr` + `next_fire_after` to render a `next_fire_at`
  preview before submit — no free-text cron with no feedback.
* `GET`/`POST /ui/scheduler/{id}/cancel` — terminal-confirm modal +
  submit (`tenant_admin`). The confirm dialog spells out that cancel is
  permanent (no un-cancel); the submit maps the service's 404
  (`trigger_not_found`) and 409 (`trigger_already_fired`) edges the same
  way the REST `DELETE` route does.

The UI does **not** expose pause / resume (the service has no UPDATE
path — only the dispatcher sets `paused`) or edit (triggers are
immutable; "edit" is cancel + recreate); cross-tenant `tenant_filter` is
platform_admin-only and waits on the tenant selector (T4 #865).

## References

- Issue #823 (G11.3-T2 cron + one-off triggers)
- Initiative #804 (G11.3 Scheduler)
- Goal #800 (G11 Agentic ops runtime)
- Sibling tasks: #822 (T1 substrate-decision spike), #824 (T3 event
  trigger), #825 (T4 in-flight resume); #826 (T5 admin surface) lands
  with this PR.
- Precedent loops:
  [topology/scheduler.py](../../backend/src/meho_backplane/topology/scheduler.py),
  [memory/expiry.py](../../backend/src/meho_backplane/memory/expiry.py)
- Migrations:
  - `alembic/versions/0020_create_scheduled_trigger.py` — T1 #1064
    storage substrate (table, indexes, kind / status / in_flight_policy
    CHECKs, discriminated-union kind-fields CHECK).
  - `alembic/versions/0025_scheduled_trigger_dispatcher_columns.py` —
    T2 this PR (`identity_sub`, `inputs`, `timezone` columns;
    `status` CHECK widened to admit `fired`).
- Tests:
  [tests/test_scheduler.py](../../backend/tests/test_scheduler.py),
  [tests/test_scheduler_credentials.py](../../backend/tests/test_scheduler_credentials.py),
  [tests/migrations/test_migration_0020_scheduled_trigger.py](../../backend/tests/migrations/test_migration_0020_scheduled_trigger.py),
  [tests/migrations/test_migration_0025_scheduled_trigger.py](../../backend/tests/migrations/test_migration_0025_scheduled_trigger.py),
  [tests/test_db_scheduled_trigger.py](../../backend/tests/test_db_scheduled_trigger.py)
