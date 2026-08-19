# Tiered-triage investigator wiring (check dashboards)

## Overview

When a check Dashboard's five-state rollup crosses from green into a
non-green state, `meho_backplane.checks.investigate` fires a **diagnose-only**
agent investigation: it correlates the affected non-green Sensors through the
topology blast-radius graph so one underlying cause produces exactly one
investigation, checks tenant memory for a known-noise policy that suppresses
re-escalation, and — only for novel, non-suppressed groups — runs a real,
durable, budget-gated agent via
`AgentInvoker.run_scheduled`. The investigator writes a structured finding
plus an advisory suggested action; it **never executes a change op**.

This productises the `examples/r1-tiered-triage` mould against the
deterministic check layer (#2503–#2506): the *cheap tier* is the runner +
rollup (no LLM), and the *deep tier* is the scheduled agent run.

## Key types

- `investigate_on_transition(*, sensor_id, tenant_id, now=None)` — the public
  hook the check-runner (#2505) calls immediately after every
  `record_sensor_result`. Never raises. Does the in-band memo work and spawns
  the expensive investigation as a fire-and-forget task.
- `run_investigation(*, tenant_id, dashboard, members)` — the awaitable
  investigation: correlate → per cause-group (suppress / dedupe / invoke /
  persist).
- `ChecksFinding` — the structured investigation output parsed from the run's
  terminal answer (`verdict`, `re_escalate`, `summary`, `evidence`,
  `recommended_action`). Mirrors the r1 `PolicyDecision` minus the
  model-chosen `alert_class` (suppression keys on the deterministic topology
  group key instead).

## Control flow

1. **Transition detection** (in-band, `investigate_on_transition` →
   `_claim_dashboard_transition`). For each Dashboard containing the
   just-evaluated Sensor, the claim runs one atomic, per-`(tenant, dashboard)`
   transaction: take a transaction-scoped Postgres advisory lock
   (`pg_advisory_xact_lock`, auto-released at commit; a no-op on the SQLite test
   path), read the members **under the lock**, fold them through #2506's pure
   rollup against `now`, and **compare-and-swap** the persisted
   `check_dashboards.last_rollup_state` memo (NULL treated as `ok`) in a single
   conditional `UPDATE` guarded on the memo still equalling the observed value.
   #2506 shipped that memo column **unwritten** and reserved for this hook —
   this is its only writer. Only a *worsening* transition (`ok`/`skip` →
   `degraded`/`critical`, or `degraded` → `critical`) with at least one
   actively-failing member schedules an investigation, **and only for the caller
   whose swap won** (`rowcount == 1`); improving/unchanged/`unknown`/`skip`
   states just maintain the memo. The compare-and-swap is the atomic claim that
   coalesces two Sensor outcomes landing together on one Dashboard into exactly
   one investigation over the settled member set (#2575) — replica-safe because
   the claim is DB-enforced, not a per-process guard. Every evaluation is
   processed (not only state changes) because a `for:` hold expiring flips a
   Dashboard non-green with no sensor-state change; the memo-equality check is
   the cheap exit.

   Since #2719 the claim is returned for **both** edge directions and the
   worsening filter sits one level up, at the single routing site in
   `_process_transition` — unchanged in meaning for the investigator. The
   improving edge is returned because the email notifier
   (`docs/codebase/checks-notifications.md`) consumes it, not discarded
   because the investigator does not. The two consumers are independent:
   the notifier applies its own per-Dashboard `notify_min_state` floor and
   never reaches this module's correlation, suppression, or agent path.

   **What counts as an edge, since #2799:** on a sensor with
   `retry_times > 0`, an unconfirmed (pending soft-state) evaluation
   never changes the sensor's `last_state`, so the fold this hook
   recomputes cannot see it — the memo CAS is a cheap no-op and no edge
   is claimed. Only a *confirmed* sensor transition (or a `for:` hold
   expiring / stale derivation, which are fold-side) can move the
   rollup, so the investigator fires on confirmed edges only. The hook
   still runs on every persist; confirmation just changes which
   persists can produce an edge (`docs/codebase/sensor.md` §Result
   recording).
2. **Correlation** (`_correlate`). Each non-green member maps to its topology
   anchor via its registered target's name (`kind="target"`), and
   `topology.query.find_dependencies` returns the forward closure. Members
   whose closures intersect are unioned into one cause group. The group key is
   the deepest shared closure node (maximal minimum depth, ties broken by
   `(kind, name)`), slugified — the deterministic key that is both the
   `work_ref` group segment and the memory-slug suffix. A member whose anchor
   is unresolvable — no `name` in the target dict, or an untracked / ambiguous
   node (`NodeNotFoundError` / `AmbiguousNodeError`, the expected state for
   every non-k8s target today, since only the Kubernetes connector overrides
   `discover_topology`) — forms a singleton keyed on its own Sensor slug. So
   correlation degrades gracefully to per-Sensor investigations when topology
   has no data.
3. **Suppression** (`_is_suppressed`). Read the `checks-noise-<group-key>`
   tenant memory entry; suppress iff its metadata `re_escalate` is falsey. A
   missing entry (novel red) or `re_escalate=true` (still actionable) does not
   suppress. Decided on metadata only — no LLM call to decide.
4. **In-flight dedupe** (`_has_in_flight_run`). A non-terminal `agent_run`
   with the same `(tenant_id, work_ref)` suppresses a duplicate fire (rides
   `agent_run_tenant_work_ref_idx`).
5. **Invoke** (`_fire_investigation`). Resolve `(client_id, secret)` from the
   definition's `identity_ref` (Vault-first, `resolve_agent_credentials`), then
   `AgentInvoker.run_scheduled(name, briefing, work_ref=…)`. The budget gate +
   kill switch are enforced before any row is created; a refusal
   (`BudgetExceededError`), an unconfigured/disabled agent, unresolved
   credentials, or a token failure is caught, logged, and skipped — never a
   crash-loop.
6. **Closed loop** (`_await_finding` → `_persist_finding`). A run terminal on
   return is parsed immediately; one that converted to async is polled up to
   `checks_investigation_poll_timeout_seconds`. A valid `ChecksFinding` is
   written back to tenant memory as `checks-noise-<group-key>` (upsert on
   `(scope, slug)`) with metadata `source` / `verdict` / `re_escalate` /
   `run_id`, so a subsequent red for the same group with `re_escalate=false`
   is suppressed without an LLM call.
7. **Emailed finding** (#2721, `_finding_notice` →
   `notify.schedule_finding_notification`). When the Dashboard carries a
   `notify_email`, the persisted finding is mailed to it: verdict, summary,
   `run_id`, evidence, and — for an `actionable` verdict only — the
   recommended action. Ordered strictly after the memory write-back, because
   the noise-suppression policy is the load-bearing durable effect and the
   mail is the operator courtesy; scheduled rather than awaited, so a 30 s
   SMTP session does not sit in front of the next cause group's diagnosis.
   `notify_finding` never raises (`checks_finding_email_failed` on any
   failure), so a broken MTA cannot cost the tenant its policy write.

## Operator-supplied briefing context (#2721)

`check_dashboards.investigator_prompt` (migration `0069`, `text` NULL, capped
at 4096 characters by `DashboardCreate`) is operator prose about what a
Dashboard means and where to look first. `_build_briefing` renders it in a
clearly delimited section **between the transition snapshot and the
`## Output` contract**:

```
## Dashboard              <- server-built, deterministic
## Correlated sensors     <- server-built, deterministic
## Operator instructions for this dashboard
<<<OPERATOR-PROMPT
…operator text…
OPERATOR-PROMPT>>>
## Output                 <- server-built, the answer contract
```

Three properties are load-bearing, in this order:

- **Appended, never substituted.** The deterministic transition facts always
  lead, so operator text cannot suppress what the Dashboard actually
  reported. A `NULL` — or blank, or whitespace-only — prompt yields a briefing
  byte-identical to the pre-#2721 one (pinned by a literal-comparison
  regression test); the gate strips before testing, so an all-blank prompt
  renders no heading and no empty fence.
- **Delimited and attributed.** The block states the text's provenance before
  quoting it, so the model reads "this is operator configuration, context not
  instruction" first. This is OWASP LLM01's "separate and clearly denote
  untrusted content" mitigation, applied to a mixed-provenance prompt.
- **`## Output` stays last.** The answer contract is server-built too, and
  keeping it after the operator block means prompt text cannot displace the
  JSON shape `_parse_finding` depends on. `_fence_safe` neutralises either
  fence token appearing inside the prompt, so operator text cannot close its
  own block early and appear to speak as MEHO.

The threat model is *careless*, not adversarial: the column is writable only
by a `tenant_admin` through the create path, which is why a fixed fence is
sufficient and a per-briefing nonce is not warranted.

## Agent-name convention (opt-in per tenant)

The wiring is **off** until a tenant creates an *enabled* agent definition
whose name matches `Settings.checks_investigator_agent` (default
`checks-investigator`). An absent or disabled definition logs
`checks_investigator_unconfigured` and skips — the `enabled` flag is the
on/off switch.

Example create payload (derived from
`examples/r1-tiered-triage/agent.deep-tier-investigator.json`; the read-only
toolset + no write grants is the diagnose-only posture):

```json
{
  "name": "checks-investigator",
  "identity_ref": "agent:checks-investigator",
  "model_tier": "deep",
  "system_prompt": "You investigate check-dashboard non-green transitions. You receive one briefing per correlated cause group: the Dashboard, its transition, and the non-green member Sensors that share a common topology cause. Investigate the ONE underlying cause read-only, then answer with a single JSON object matching ChecksFinding: {verdict: benign|acknowledged|actionable, re_escalate: bool, summary: str, evidence: [str], recommended_action: str|null}. Set re_escalate=false for benign/acknowledged so this correlated red is suppressed until it recurs. recommended_action is advisory text only; any change op you attempt parks for operator approval and is never executed by this wiring.",
  "toolset": {
    "meta_tools": ["list_operation_groups", "search_operations", "call_operation"]
  },
  "turn_budget": 25,
  "enabled": true
}
```

Grant this principal `*.read` / `*.list` auto-execute and any `*.write`
**needs-approval** (the `examples/r1-tiered-triage/permissions.json` mould).

## Diagnose-only contract (the CRUX)

This wiring never executes a change op:

- `investigate.py` imports **no** operations dispatcher and calls no execution
  seam (a unit test asserts `operations.dispatcher` is absent from the module
  source).
- **The wrapper sends the finding email, not the agent (#2721).** The
  investigator is given no mail tool and no agent toolset exposes one (a unit
  test asserts `mail` is absent from every `agent/toolset*.py`). Mail is a
  side effect of a *deterministic* code path reading a *persisted* finding —
  not something the model can choose to do, address, or word. An agent that
  should send ad-hoc mail does so in its own run through `call_operation` on
  the `mail.*` connector under normal policy; that is a different seam,
  outside this module, and is exactly why #2717 made the transport a
  connector operation rather than a private helper.
- `recommended_action` is persisted as memory text only.
- Any write op the **agent** attempts is not executed by this wiring: the
  policy gate parks it as a durable `ApprovalRequest`
  (`operations/approval_queue.py`, #817) because the agent principal holds
  `*.write` grants as needs-approval. Approval + execution is a separate,
  operator-driven step (R2 territory), not this hook.

The topology reads and memory read/write run under a synthetic per-tenant
`Operator` confined to the Sensor's own tenant — no cross-tenant reach, the
#2505 runner precedent. Its role is `TENANT_ADMIN` because the closed-loop
noise-suppression policy is a **tenant-shared** memory write (the memory RBAC
matrix restricts `TENANT`-scope writes to `tenant_admin`); that operator never
dispatches an op, and the agent run authenticates independently as its own
principal, so no execution path inherits the role.

## Dependencies

- `meho_backplane.checks.rollup` (#2506) — the pure five-state fold reused for
  transition detection; `meho_backplane.checks.dashboard_repository` —
  `members_by_dashboard`.
- `meho_backplane.agent.invocation` — `AgentInvoker.run_scheduled` / `poll`,
  the budget gate + kill switch, `work_ref` stamping.
- `meho_backplane.scheduler.credentials` — Vault-first
  `resolve_agent_credentials` (the same seam the cron scheduler uses).
- `meho_backplane.topology.query.find_dependencies` — forward blast-radius
  closure (PG-only recursive CTE; correlation degrades to singletons where
  topology has no data).
- `meho_backplane.memory.service.MemoryService` — the closed-loop suppression
  channel (`remember` upsert on `(scope, slug)`).
- `meho_backplane.checks.notify` — the finding mail (#2721). The import is
  strictly one-directional (`investigate` → `notify`); the notifier projects
  both notice kinds into its own dataclasses rather than importing
  `ChecksFinding`, which would be a cycle.
- Migrations: `0069` adds `investigator_prompt` (#2721). The
  `last_rollup_state` memo column is #2506's DDL and `notify_email` /
  `notify_min_state` are `0068`'s (#2719); this hook is the memo's only
  writer and reads the other three.

## Known issues / boundaries

- **No sensor→graph-node link for non-k8s targets.** Only the Kubernetes
  connector discovers topology, so most Sensors fall to the singleton
  (per-Sensor) path today; correlation grows automatically as more connectors
  discover topology. Anchor resolution is exact-name only (no alias
  resolution) — an alias-referencing target does not correlate.
- **Concurrency.** Two members of one Dashboard evaluated near-simultaneously
  are coalesced into a single investigation over the settled member set: each
  transition is claimed under a transaction-scoped per-`(tenant, dashboard)`
  advisory lock, and the memo transition is an atomic compare-and-swap whose
  `rowcount` is the claim token, so exactly one caller fires even across
  replicas (#2575). The in-flight `work_ref` dedupe (`_has_in_flight_run`) and
  the noise-suppression memory layer remain as the cross-edge / cross-tick
  backstops. The residual timing gap is intrinsic to the persist-hook seam: a
  member that has not yet committed when the winning claim reads the membership
  is not in that investigation's correlated set — it re-escalates on its own
  next worsening edge (all runs are diagnose-only, so a follow-up run is
  harmless).
- **Serial cause-group fan-out (intentional, #2576).** `run_investigation`
  investigates a Dashboard's cause groups **serially** — `for group in groups:
  await _investigate_group(...)` — awaiting each to a terminal outcome before
  the next fires. This is a deliberate design choice weighed against the tail
  latency it causes (group *N* waits on the runtime/timeout of groups
  1..*N*-1), **not** an un-optimised loop.
  - *Why serial is load-bearing.* `_investigate_group` runs the pre-fire budget
    gate + kill switch (inside `run_scheduled`) before firing its agent run. That
    gate reads **already-recorded** consumption
    (`operations/budget_enforcement.py`): tokens/cost are charged against
    committed state *after* a run finishes, and a reservation ("reserve this
    run's cost before it starts") is explicitly deferred there. Awaiting each
    group to completion is what makes the cap correct — group *N*+1's gate
    observes group *N*'s spend already recorded. Fanning the groups out under a
    concurrency cap would open a check-then-fire race where several groups read
    the same pre-spend state, all pass the gate, and **over-fire beyond budget**.
  - *Why the latency cost is acceptable.* The whole investigation coroutine runs
    off the runner's persist path as a fire-and-forget task
    (`_schedule_investigation` → `asyncio.create_task`). The serialization
    therefore delays only *later* cause-groups' **diagnosis on the same
    Dashboard** — never the runner cadence and never another Dashboard's
    investigation (each transitioning Dashboard gets its own task). Topology
    correlation already collapses a shared cause into one group, so a Dashboard
    typically resolves to few groups. The 600 s per-group ceiling
    (`checks_investigation_poll_timeout_seconds`) is only reached when an
    investigation genuinely never terminates; the common case terminates far
    sooner.
  - *When to revisit.* If real deployments show the multi-independent-cause tail
    latency is an operational problem, the correct fix is **not** a naive
    concurrency cap but an **atomic budget reserve/decrement** seam so a group
    reserves budget before firing (replica-safe if the budget is shared across
    processes) — i.e. the reservation protocol `budget_enforcement.py` defers.
    That is a separate, larger change; until it exists, serial-to-completion is
    the only fan-out shape that keeps the cap honest.
- **Trigger seam.** The runner-persist hook is the v1 seam because the
  `kind=event` matcher (#2878) dispatches subscribed agent triggers, not the
  diagnose-only investigator, and #2506's rollup is read-path-only. Migrating
  the investigator onto an event trigger is a separate task.
- **Remote Sensors.** Investigation of remote / isolated-network Sensors
  (#2415 gateway workload) is out of scope here (soft link).

## References

- Initiative #2416 (binding design), Task #2507, parent goal #221. Builds on
  Sensor #2503, assertion evaluator #2504, runner #2505, dashboard/rollup
  #2506. Serial-fan-out-vs-budget-gating decision: #2576 (follow-up from the
  #2507 review). Operator prompt + emailed finding: Initiative #2716, Task
  #2721 (on #2717's transport and #2719's `notify_email`).
- Prompt-injection posture for the appended operator section: OWASP LLM Top 10
  for LLM Applications, LLM01 Prompt Injection —
  <https://genai.owasp.org/llmrisk/llm01-prompt-injection/> ("separate and
  clearly denote untrusted content to limit its influence on user prompts").
- Mould: `examples/r1-tiered-triage/workflow.py` (harness-persists rationale,
  briefing builder, render/persist shape), `agent.deep-tier-investigator.json`
  (definition payload), `permissions.json` (`*.write` needs-approval).
- Seams: `agent/invocation.py` (`run_scheduled`), `scheduler/loop.py`
  (step 4 caller), `scheduler/credentials.py`, `operations/budget_enforcement.py`,
  `operations/approval_queue.py` (#817), `topology/query.py`.
