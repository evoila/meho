<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# R1 — Tiered-triage: the operator recipe

> Step-by-step setup for the [R1 reference pattern](./README.md):
> a cheap-tier classifier on a 15-minute schedule that escalates
> interesting events to a deep-tier investigator, with the deep
> tier's verdicts persisted back to memory as the policy the cheap
> tier reads on every firing.

This guide is **composition only** — it wires existing MEHO
primitives. No new server-side endpoint is introduced.

## What you'll have at the end

- Two hosted agent definitions: `r1-cheap-tier-classifier`
  (`model_tier=fast`) and `r1-deep-tier-investigator`
  (`model_tier=deep`).
- A `*/15 * * * *` cron trigger that fires the cheap tier under its
  own Keycloak identity.
- An RBAC posture where the cheap tier can read broadcast events +
  escalate to the deep tier (and nothing else), and the deep tier
  is read-only across discovery connectors (write ops route through
  the R2 approval gate, [PR #1243](https://github.com/evoila/meho/pull/1243)).
- Per-identity daily budget caps that **degrade** the deep tier to
  the fast tier at 80% of the daily cost cap, and **refuse** every
  run at 100% — both per the [G11.5-T6 #1080](https://github.com/evoila/meho/issues/1080)
  pre-execution gate.
- A closed-loop policy memory: every deep-tier investigation
  produces a `r1-policy-<alert_class>` entry that the cheap tier
  reads on its next firing to skip re-triage of the same event
  class.

## Prereqs

You have, in this order:

1. **A running MEHO backplane** on v0.2 or later at some hostname
   (we'll use `https://meho.example.com`). Verify with
   `curl https://meho.example.com/healthz`.
2. **A Keycloak realm** wired per
   [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).
3. **The MEHO CLI installed and authenticated as a tenant admin:**
   ```bash
   meho version
   meho login https://meho.example.com
   meho status --json | jq '.operator'    # confirms login + shows sub/email
   ```
   To verify you're authenticated as a tenant_admin and capture your
   `tenant_id`, decode the access token's payload — the JWT carries
   both claims and the CLI stores it at the default
   `~/.config/meho/credentials.json` (see
   [`cli/internal/auth/store.go`](../../cli/internal/auth/store.go)).
   ```bash
   # Pull the access_token, base64-decode the payload, then read the claims.
   JWT_PAYLOAD="$(jq -r .access_token ~/.config/meho/credentials.json \
       | cut -d. -f2 | base64 -d 2>/dev/null)"
   echo "$JWT_PAYLOAD" | jq '{tenant_id, sub, realm_access: .realm_access.roles}'
   # expect realm_access.roles to contain "tenant_admin"
   TENANT_ID="$(echo "$JWT_PAYLOAD" | jq -r .tenant_id)"
   ```
   `meho status` (and its `--json` form) renders the operator's
   sub/email/name and backplane health but does not surface the
   tenant id or the realm roles — the JWT is the source of truth for
   both (per
   [`backend/src/meho_backplane/auth/jwt.py:585`](../../backend/src/meho_backplane/auth/jwt.py)).
4. **Two agent principals registered**, one per tier
   ([G11.2-T1 #815](https://github.com/evoila/meho/issues/815)):
   ```bash
   meho agent-principal register r1-cheap-tier-classifier --json | jq -r .keycloak_client_id
   meho agent-principal register r1-deep-tier-investigator --json | jq -r .keycloak_client_id
   # expect: agent:r1-cheap-tier-classifier
   #         agent:r1-deep-tier-investigator
   ```
   The `keycloak_client_id` field is the principal's OIDC `sub` —
   the registry sets it to `agent:<name>` so it matches the
   `identity_ref` already on each `agent.*.json`. There is no
   per-principal `show` verb in v0.2; `register` is the place the
   sub is surfaced. Re-run `meho agent-principal list --json | jq
   '.principals[] | {name, keycloak_client_id}'` to enumerate them
   later.

## Step 1 — Create the agent definitions

The cheap-tier and deep-tier `AgentDefinitionCreate` payloads ship
in this directory. Apply each via the CLI:

```bash
# Source of truth for the fields is the two agent.*.json files;
# the commands below mirror them. Re-run after editing either file.
# --toolset / --output-schema each expect a JSON object — the CLI
# reads the WHOLE file as that object (it does NOT extract a named
# subkey). Pipe the relevant subkey through stdin via `@-` so the
# command sees just the object the backend wants.

jq .toolset examples/r1-tiered-triage/agent.cheap-tier-classifier.json | \
meho agent create r1-cheap-tier-classifier \
  --identity-ref "agent:r1-cheap-tier-classifier" \
  --model-tier fast \
  --turn-budget 12 \
  --system-prompt "$(jq -r .system_prompt examples/r1-tiered-triage/agent.cheap-tier-classifier.json)" \
  --toolset @-

jq .toolset examples/r1-tiered-triage/agent.deep-tier-investigator.json | \
meho agent create r1-deep-tier-investigator \
  --identity-ref "agent:r1-deep-tier-investigator" \
  --model-tier deep \
  --turn-budget 25 \
  --system-prompt "$(jq -r .system_prompt examples/r1-tiered-triage/agent.deep-tier-investigator.json)" \
  --toolset @- \
  --output-schema @<(jq .output_schema examples/r1-tiered-triage/agent.deep-tier-investigator.json)
```

> Note on `--toolset @<path>` / `--output-schema @<path>`: each flag
> uses `loadJSONObjectFlag` (see
> [`cli/internal/cmd/agent/agent.go:442`](../../cli/internal/cmd/agent/agent.go)),
> which reads the **entire** file as the JSON object — it does NOT
> extract a named subkey. Passing the full `agent.*.json` file would
> upload the wrapper (`name`, `system_prompt`, `enabled`, …) as the
> toolset/output_schema object and the backend rejects it. The
> commands above extract the subobjects via `jq` and stream them in
> via `@-` (stdin) or process substitution; a consumer repo may
> prefer to ship pre-extracted `toolset.json` / `output_schema.json`
> files instead and pass them with `@<path>`.

Verify:

```bash
meho agent list | grep r1-
# expect:
# r1-cheap-tier-classifier   model=fast  enabled=true  toolset.meta_tools=[call_operation]
# r1-deep-tier-investigator  model=deep  enabled=true  output_schema=set
```

Capture each agent's `id` (`meho agent show r1-cheap-tier-classifier | jq -r .id`)
— you'll need them for the scheduler payload.

## Step 2 — Wire the schedule

The [`scheduler.cron.json`](./scheduler.cron.json) payload fires
the cheap tier every 15 minutes. Substitute the cheap-tier agent
id you captured above:

`meho scheduler create` takes the trigger fields as flags
(no `--stdin` exists — see
[`cli/internal/cmd/scheduler/create.go:100-123`](../../cli/internal/cmd/scheduler/create.go));
the `--inputs` flag accepts inline JSON, `@<path>`, or `@-` for
stdin. The trigger discriminator (`--cron-expr` vs `--fire-at`
vs `--event-filter`) is checked client-side and the backend's
`ScheduledTriggerCreate` validator is the ultimate gate.

```bash
CHEAP_ID="$(meho agent show r1-cheap-tier-classifier --json | jq -r .id)"

meho scheduler create \
  --kind cron \
  --agent-definition "$CHEAP_ID" \
  --cron-expr "*/15 * * * *" \
  --timezone UTC \
  --identity-sub "agent:r1-cheap-tier-classifier" \
  --in-flight-policy fail_into_audit \
  --inputs "@examples/r1-tiered-triage/scheduler.inputs.json"
```

The `--inputs` payload (the cheap tier's per-firing prompt
context) lives in
[`scheduler.cron.json`](./scheduler.cron.json) under the `inputs`
key; extract it once with
`jq .inputs examples/r1-tiered-triage/scheduler.cron.json >
examples/r1-tiered-triage/scheduler.inputs.json` and pass that
file via `@<path>`, or extract inline via process substitution:
`--inputs "@<(jq .inputs examples/r1-tiered-triage/scheduler.cron.json)"`.

Verify:

```bash
meho scheduler list --json | jq '.[] | select(.identity_sub=="agent:r1-cheap-tier-classifier")'
# expect: kind=cron cron_expr=*/15 * * * * identity_sub=agent:r1-cheap-tier-classifier in_flight_policy=fail_into_audit
```

> The cron `*/15 * * * *` is deliberate. A per-minute schedule
> against a noisy broadcast feed runs up against the per-identity
> budget within a couple of hours; the 15-minute cadence gives the
> cheap tier a realistic batch to triage on each firing while
> staying well under the daily cost cap seeded in Step 4 (96
> firings/day at *$2/day* leaves ~2¢ per firing — comfortable for a
> Haiku-class model). Tighten only after you've watched a week of
> broadcast volume against the budget. See
> [`docs/codebase/agent-runtime.md`](../../docs/codebase/agent-runtime.md)
> for the runtime cost properties that motivate cadence choices.

## Step 3 — Apply the permission grants

The [`permissions.json`](./permissions.json) file documents five
`AgentPermission` rows that wire the RBAC posture:

- Cheap tier: read `meho.broadcast.recent`; escalate to `r1-deep-tier-investigator`.
- Deep tier: read across `*.read` / `*.list` op patterns;
  `*.write` patterns route through the operator-approval gate
  (the R2 approval-gate pattern in [PR #1243](https://github.com/evoila/meho/pull/1243) covers that flow in detail).

The grants surface in v0.2 is `meho agent grant` (see
[`cli/internal/cmd/agent/grant.go`](../../cli/internal/cmd/agent/grant.go)) —
there is no batch `apply`/`resolve` verb. Loop over the rows in
`permissions.json` and post each one with `meho agent grant create`:

```bash
# Principal subs match the keycloak_client_id values surfaced by
# `meho agent-principal register` in the prereqs. Both follow the
# `agent:<name>` convention recorded as identity_ref on each agent.
CHEAP_SUB="agent:r1-cheap-tier-classifier"
DEEP_SUB="agent:r1-deep-tier-investigator"

# Substitute the placeholders, strip the doc-only `_*` keys, and
# emit one `meho agent grant create` invocation per row.
jq -r \
  --arg cs "$CHEAP_SUB" --arg ds "$DEEP_SUB" \
  '.permissions
   | map(if .principal_sub == "<cheap-principal-sub>" then .principal_sub = $cs
         elif .principal_sub == "<deep-principal-sub>" then .principal_sub = $ds
         else . end)
   | .[]
   | "meho agent grant create --principal \(.principal_sub) "
     + "--op \(.op_pattern) --target \(.target_scope) "
     + "--verdict \(.verdict)"' \
  examples/r1-tiered-triage/permissions.json | \
while IFS= read -r cmd; do
  echo "+ $cmd"
  eval "$cmd"
done
```

Each `meho agent grant create` call posts to
`POST /api/v1/agents/grants` (see
[`cli/internal/cmd/agent/grant.go:265`](../../cli/internal/cmd/agent/grant.go))
with `--principal`, `--op` (the op-pattern), `--target` (the
target-scope, `*` for "any target"), and `--verdict` (one of
`auto-execute | needs-approval | deny`). Omit `--expires` for
a permanent grant — the five rows above are baseline RBAC posture,
not a time-bounded elevation.

Verify the grants landed by listing them for the cheap tier:

```bash
meho agent grant list --principal "$CHEAP_SUB" --json | \
  jq '.grants[] | {op_pattern, target_scope, verdict}'
# expect:
#   {op_pattern: "meho.broadcast.recent", target_scope: "*", verdict: "auto-execute"}
#   {op_pattern: "meho.invoke_agent",     target_scope: "agent:r1-deep-tier-investigator",
#    verdict: "auto-execute"}
```

The escalate edge (`meho.invoke_agent` to
`agent:r1-deep-tier-investigator`) is what makes the composition
work — without it the cheap tier's `invoke_agent` tool call lands
as a permission denial at child-resolver time and the loop
surfaces a `ModelRetry` it cannot recover from. The same
verification for the deep tier should show the three `*.read`,
`*.list`, and `*.write` rows.

## Step 4 — Seed the per-identity daily budgets

The cheap tier's per-minute spending against a long-tail event
feed can creep up over a 24-hour cycle. The
[`identity_budget_seed.py`](./identity_budget_seed.py) script
writes a single daily `identity_budget` row per principal via
[`set_limits`](../../backend/src/meho_backplane/operations/identity_budget.py).

Run it once per agent principal (or pass both subs as the script
does). The `$TENANT_ID` was captured in the prereqs step from the
JWT payload of `credentials.json`:

```bash
cd backend
uv run python ../examples/r1-tiered-triage/identity_budget_seed.py \
  --tenant-id "$TENANT_ID" \
  --cheap-sub "$CHEAP_SUB" \
  --deep-sub "$DEEP_SUB"
# expect: seeded daily budgets: cheap=... (cost_limit=$2.00, requests=200), deep=... (cost_limit=$10.00, requests=100)
```

The seed values are deliberately small (`$2/day` cheap, `$10/day`
deep) so a real-world consumer copying this example has to think
about the numbers rather than land on a multi-thousand-dollar cap
by accident. Tune for your tenant's expected event throughput +
your provider's published rates
([`MODEL_PRICING`](../../backend/src/meho_backplane/operations/identity_budget.py)).

### Degrade behaviour

The pre-execution gate ([G11.5-T6 #1080](https://github.com/evoila/meho/issues/1080))
reads these rows on every invocation. When `cost_consumed` crosses
`AGENT_BUDGET_DEGRADE_THRESHOLD` (default `0.8`) of `cost_limit`:

- The **cheap tier**'s requested tier is already `fast`; degrade is
  a no-op (the resolver doesn't drop below `fast`).
- The **deep tier**'s requested tier is `deep`; degrade routes the
  next run through the `fast` backend. The deep tier's prompt still
  asks for a `PolicyDecision`, but the structured output is
  produced by the cheaper model.

When `cost_consumed >= cost_limit`, the gate raises
[`BudgetExceededError`](../../backend/src/meho_backplane/agent/run.py)
**before any model call** and the cheap tier's `invoke_agent` to
the deep tier surfaces as a `ModelRetry` the cheap tier reasons
about ("the deep tier is over budget, defer to the operator").

To exercise the refuse path **without** a long warm-up:

v0.2 ships no operator CLI verb for setting `cost_consumed`
directly — `set_limits`
(`backend/src/meho_backplane/operations/identity_budget.py`)
writes only the limit columns, not consumption. The deterministic
path that exercises the refuse arm is the harness's in-process
test
([`backend/tests/test_examples_r1_tiered_triage.py`](../../backend/tests/test_examples_r1_tiered_triage.py)) —
the `BudgetExceededError` branch is covered there against a
seeded row with `cost_consumed >= cost_limit`. For a smoke check
against your live deploy, either let normal cheap-tier firings
accumulate against the seeded `$2/day` cap (a few hours of busy
broadcast volume), or use your DB toolchain to update the
`identity_budget.cost_consumed` column directly under the same
session as the rest of your ops — there is no public CLI surface
for that operation today.

## Step 5 — Drive a firing and watch the closed loop

The cheap tier's normal input is the live broadcast feed (sourced
from real operator activity). v0.2 ships no operator CLI verb for
publishing a broadcast event by hand — the `meho broadcast`
command tree exposes only `overrides {list, set, remove}` for
detail-resolver rules (see
[`cli/internal/cmd/broadcast/broadcast.go`](../../cli/internal/cmd/broadcast/broadcast.go)).
Two practical options for driving the closed loop:

1. **Wait for real broadcast traffic.** With the cron set to
   `*/15 * * * *`, the next firing picks up whatever the
   broadcast resolver surfaced in the last interval. This is
   the production path and the right way to validate that the
   composition runs end to end against actual operator activity.
2. **Drive the cheap tier manually with a synthetic briefing.**
   `meho agent run` (see
   [`cli/internal/cmd/agent/run.go:56`](../../cli/internal/cmd/agent/run.go))
   runs the named agent against a one-shot `--input` string and
   blocks for the result; no scheduler tick is involved. This is
   the right way to smoke-test the cheap → deep → memory-write
   cycle deterministically:

```bash
meho agent run r1-cheap-tier-classifier --input "$(cat <<'BRIEF'
## Known policy

(none -- fresh-tenant smoke test)

## Recent events

{"event_id":"smoke-001","alert_class":"kc-realm-export",
 "timestamp":"2026-05-27T20:00:00Z",
 "symptom":"Keycloak realm export failed",
 "signals":["signal_failure"]}
BRIEF
)"
```

The two-section input shape (`## Known policy` + `## Recent
events`) is the cheap tier's prompt contract — it's what
`build_cheap_tier_input` in
[`workflow.py`](./workflow.py) emits during a scheduled firing.
Driving `meho agent run` with the same shape exercises the loop
without an in-process harness.

Watch the audit lineage. `meho audit query` (see
[`cli/internal/cmd/audit/query.go:87`](../../cli/internal/cmd/audit/query.go))
filters on principal/op-id/parent-audit-id and lacks an
`--include-children` shortcut — walk it as two steps:

```bash
# 1. Find the latest audit row written under the cheap tier's principal.
PARENT_ID="$(meho audit query \
  --principal "$CHEAP_SUB" \
  --limit 1 \
  --json | jq -r '.rows[0].audit_id')"

# 2. Walk the children of that audit row (the deep-tier dispatch).
meho audit query --parent-audit-id "$PARENT_ID" --json | \
  jq '.rows[] | {audit_id, op_id, principal_sub, result_status}'
```

You should see:

- A parent audit row written under `$CHEAP_SUB` for the cheap
  tier's last op (e.g. an `invoke_agent` dispatch).
- One or more child rows under `--parent-audit-id` covering the
  deep tier's investigation tool calls — the `parent_audit_id`
  column is what threads the cascade together (G0.6-T7).
- A `r1-policy-<alert-class>` memory entry persisted by the
  harness (verified in Step 6).

> The audit-row walk shows the **operation** lineage (each
> `call_operation` dispatch the cheap and deep tiers made). The
> `agent_run` durable rows (G11.1-T6 #813) are a separate
> persistence layer the runtime writes; the in-process closed-
> loop test
> ([`backend/tests/test_examples_r1_tiered_triage.py`](../../backend/tests/test_examples_r1_tiered_triage.py))
> exercises the `parent_run_id` lineage on those rows
> deterministically.

## Step 6 — Verify the closed loop

Read the policy memory entry the deep tier produced. The memory
verbs are top-level (no `meho memory` namespace; see
[`cli/internal/cmd/memory/list.go:60`](../../cli/internal/cmd/memory/list.go)
and
[`cli/internal/cmd/memory/recall.go:61`](../../cli/internal/cmd/memory/recall.go)):

```bash
meho list --scope tenant --slug-pattern r1-policy-
# expect at least one row: SCOPE=tenant SLUG=r1-policy-kc-realm-export
meho recall tenant/r1-policy-kc-realm-export
# expect a body with verdict / re_escalate / Summary / Evidence sections
```

Drive the cheap tier a second time against the **same** alert
class without restating it in `## Recent events`, and observe that
the loaded policy short-circuits re-triage:

```bash
meho agent run r1-cheap-tier-classifier --input "$(cat <<'BRIEF'
## Known policy

- slug: r1-policy-kc-realm-export
  verdict: acknowledged
  re_escalate: false
  ## Summary
  Investigated; not actionable on re-occurrence.

## Recent events

{"event_id":"smoke-002","alert_class":"kc-realm-export",
 "timestamp":"2026-05-27T20:30:00Z",
 "symptom":"Keycloak realm export failed (repeat)",
 "signals":["signal_failure"]}
BRIEF
)"
```

Walk the audit again (Step 5's two-step query):

```bash
PARENT_ID="$(meho audit query --principal "$CHEAP_SUB" --limit 1 --json | jq -r '.rows[0].audit_id')"
meho audit query --parent-audit-id "$PARENT_ID" --json | jq '.rows | length'
# expect: 0 (no deep-tier dispatch — the cheap tier saw the policy and skipped)
```

You should see **no** child deep-tier dispatch — the cheap tier
read the `r1-policy-kc-realm-export` entry from memory (via the
input's `## Known policy` section, which a production scheduler
firing builds from `load_known_policy` per
[`workflow.py`](./workflow.py)) and skipped re-triage. That is the
closed loop firing.

## Step 7 — Wire the example into your CI

The CI exercise
[`backend/tests/test_examples_r1_tiered_triage.py`](../../backend/tests/test_examples_r1_tiered_triage.py)
runs on every PR against `evoila/meho` via
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml). For
your consumer repo, the equivalent is:

```yaml
- name: validate R1 example
  run: |
    cd backend
    uv run pytest tests/examples/test_r1_tiered_triage_consumer.py
```

The consumer-side test should at minimum validate the four JSON
payloads against the live schemas (the same shape the CI exercise
takes) and walk the local markdown links. The harness exercise
(end-to-end closed loop) requires a Pydantic AI model and the
memory substrate; the unit-lane shape with a `FunctionModel` is
the recommended consumer-side variant.

## Troubleshooting

### The cheap tier never escalates

Likely causes:

- The broadcast feed has no events that meet the interestingness
  rules. Stage one manually (Step 5).
- The cheap tier doesn't carry the `meho.invoke_agent` grant for
  the deep tier's name. Re-check Step 3's resolve verification.
- The cheap tier's structured input doesn't match the prompt's
  expected section headings (`## Known policy` / `## Recent events`).
  The harness builds these correctly; a custom consumer-side
  driver may need updating.

### The deep tier runs but no memory entry appears

- The deep tier's output didn't validate against `PolicyDecision`.
  Check the deep-tier audit row's `output` field — a non-matching
  shape means the harness's finalizer skipped the write
  (see [`workflow.py`](./workflow.py) `_finalize`).
- The harness's `MemoryService` write was rejected by the RBAC
  matrix. Cheap tier's role must satisfy the `tenant`-scope write
  permission (typically `operator` or `tenant_admin` for the
  agent's principal). Re-check the permission grants for the
  cheap tier.

### Cost crept up despite the daily cap

- The seed script only writes the **daily** window. Weekly /
  monthly buckets need their own `set_limits` calls if you want
  them. v0.2's enforcement gate respects whichever buckets exist.
- The pre-execution gate fires *before* the run starts — but
  consumption is applied *after* the run finishes. A run that
  starts at 99% of cap will complete and may push consumption past
  100%. The next run will refuse. This is the documented "one
  overshoot" property; if you need a hard pre-run reservation, the
  consumer's option is to set `cost_limit` lower than the actual
  budget by one run's worth.
