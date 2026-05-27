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
   meho whoami    # > role=tenant_admin tenant=<your tenant uuid>
   ```
4. **Two agent principals registered**, one per tier
   ([G11.2-T1 #815](https://github.com/evoila/meho/issues/815)):
   ```bash
   meho agent-principal register r1-cheap-tier-classifier
   meho agent-principal register r1-deep-tier-investigator
   ```
   Capture each principal's `sub` claim — you'll substitute them
   into [`permissions.json`](./permissions.json) below. The `sub`
   convention `agent:<agent-name>` matches the `identity_ref`
   already on each `agent.*.json`.

## Step 1 — Create the agent definitions

The cheap-tier and deep-tier `AgentDefinitionCreate` payloads ship
in this directory. Apply each via the CLI:

```bash
# Source of truth for the fields is the two agent.*.json files;
# the commands below mirror them. Re-run after editing either file.
meho agent create r1-cheap-tier-classifier \
  --identity-ref "agent:r1-cheap-tier-classifier" \
  --model-tier fast \
  --turn-budget 12 \
  --system-prompt "$(jq -r .system_prompt examples/r1-tiered-triage/agent.cheap-tier-classifier.json)" \
  --toolset "@examples/r1-tiered-triage/agent.cheap-tier-classifier.json"

meho agent create r1-deep-tier-investigator \
  --identity-ref "agent:r1-deep-tier-investigator" \
  --model-tier deep \
  --turn-budget 25 \
  --system-prompt "$(jq -r .system_prompt examples/r1-tiered-triage/agent.deep-tier-investigator.json)" \
  --toolset "@examples/r1-tiered-triage/agent.deep-tier-investigator.json" \
  --output-schema "@examples/r1-tiered-triage/agent.deep-tier-investigator.json"
```

> Note on `--toolset @<path>` / `--output-schema @<path>`: the CLI
> reads the whole file and uses the named subkey. Passing the
> agent-definition JSON works because the file's `toolset` /
> `output_schema` keys are the relevant subsets — see the
> implementation in
> [`cli/internal/cmd/agent/create.go`](../../cli/internal/cmd/agent/create.go).
> For a clean separation, extract the subobjects into their own
> files in your consumer repo.

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

```bash
jq --arg id "$(meho agent show r1-cheap-tier-classifier | jq -r .id)" \
   '.agent_definition_id = $id' \
   examples/r1-tiered-triage/scheduler.cron.json | \
  meho scheduler create --stdin
```

Verify:

```bash
meho scheduler list | grep r1
# expect: kind=cron cron_expr=*/15 * * * * agent=r1-cheap-tier-classifier identity_sub=agent:r1-cheap-tier-classifier in_flight_policy=fail_into_audit
```

> The cron `*/15 * * * *` is deliberate. A per-minute schedule
> against a noisy broadcast feed runs up against the per-identity
> budget within a couple of hours; 15-minute cadence is the
> consumer-doc baseline (`agent-runtime-for-ops-spec.md` §
> *"Tier-1 cadence"*) and gives the cheap tier a realistic batch
> to triage. Tighten only after you've watched a week of broadcast
> volume against the daily cost cap.

## Step 3 — Apply the permission grants

The [`permissions.json`](./permissions.json) file documents five
`AgentPermission` rows that wire the RBAC posture:

- Cheap tier: read `meho.broadcast.recent`; escalate to `r1-deep-tier-investigator`.
- Deep tier: read across `*.read` / `*.list` op patterns;
  `*.write` patterns route through the operator-approval gate
  (the R2 approval-gate pattern in [PR #1243](https://github.com/evoila/meho/pull/1243) covers that flow in detail).

Substitute the two `<*-principal-sub>` placeholders with the
`sub` claims you captured in the prereqs step, strip the doc-only
`_*` keys, and apply:

```bash
# Replace placeholders. Adjust the seds to your shell of choice.
CHEAP_SUB="$(meho agent-principal show r1-cheap-tier-classifier | jq -r .sub)"
DEEP_SUB="$(meho agent-principal show r1-deep-tier-investigator | jq -r .sub)"
jq \
  --arg cs "$CHEAP_SUB" --arg ds "$DEEP_SUB" \
  '.permissions
   | map(if .principal_sub == "<cheap-principal-sub>" then .principal_sub = $cs
         elif .principal_sub == "<deep-principal-sub>" then .principal_sub = $ds
         else . end)
   | map(del(._purpose))
   | { permissions: . }' \
  examples/r1-tiered-triage/permissions.json | \
  meho agent-permissions apply --stdin
```

Verify the grant resolution for the change-class escalate edge:

```bash
meho agent-permissions resolve \
  --principal "$CHEAP_SUB" \
  --op meho.invoke_agent \
  --target agent:r1-deep-tier-investigator
# expect: verdict=auto-execute  matched=<row 2 from permissions.json>
```

## Step 4 — Seed the per-identity daily budgets

The cheap tier's per-minute spending against a long-tail event
feed can creep up over a 24-hour cycle. The
[`identity_budget_seed.py`](./identity_budget_seed.py) script
writes a single daily `identity_budget` row per principal via
[`set_limits`](../../backend/src/meho_backplane/operations/identity_budget.py).

Run it once per agent principal (or pass both subs as the script
does):

```bash
cd backend
uv run python ../examples/r1-tiered-triage/identity_budget_seed.py \
  --tenant-id "$(meho whoami | jq -r .tenant_id)" \
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

To exercise the refuse path manually:

```bash
# Set the deep tier's cost_consumed to its cost_limit, then trigger.
meho agent-budget set \
  --principal "$DEEP_SUB" \
  --window daily \
  --cost-consumed 10.00     # if your CLI supports the override; otherwise edit the row directly
meho agent fire r1-cheap-tier-classifier  # one-shot test trigger
# expect cheap-tier audit row showing: invoke_agent -> ModelRetry: "child agent failed: budget_exceeded"
```

## Step 5 — Stage an event and watch the closed loop

The cheap tier's input is the live broadcast feed. To drive the
example end-to-end without waiting for the next real event, stage
one yourself:

```bash
meho broadcast publish --event-class "kc-realm-export" \
  --status error \
  --target "vc-prod-a/kc:realm-export" \
  --tags env=prod \
  --signal "operation failed: status_code=503"
```

Either wait up to 15 minutes for the next cron firing or trigger
manually:

```bash
meho agent fire r1-cheap-tier-classifier
```

Watch the audit lineage:

```bash
# The cheap tier's run row, with its child deep-tier row linked via parent_run_id.
meho audit query \
  --agent r1-cheap-tier-classifier \
  --limit 1 \
  --include-children
```

You should see:

- One parent `agent_run` row for the cheap tier (trigger=`scheduled` or `manual`).
- One child `agent_run` row for the deep tier (trigger=`agent_invoked`, parent_run_id = cheap row).
- A memory write row for `r1-policy-<alert-class>` (in this case,
  `r1-policy-kc-realm-export`).

## Step 6 — Verify the closed loop

Read the policy memory entry the deep tier produced:

```bash
meho memory list --scope tenant --slug-pattern r1-policy-
# expect at least one row: slug=r1-policy-kc-realm-export
meho memory show --scope tenant --slug r1-policy-kc-realm-export
# expect a body with verdict / re_escalate / Summary / Evidence sections
```

Fire the cheap tier a second time without staging a new event of
the same class:

```bash
meho agent fire r1-cheap-tier-classifier
```

Watch the audit:

```bash
meho audit query \
  --agent r1-cheap-tier-classifier \
  --limit 1 \
  --include-children
```

You should see **no** child deep-tier run — the cheap tier read the
`r1-policy-kc-realm-export` entry from memory and skipped re-triage.
That is the closed loop firing.

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
