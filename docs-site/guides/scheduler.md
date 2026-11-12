# Schedule unattended work

The scheduler is the floor of MEHO's 24/7 operation: it fires **agent
runs** on durable triggers — a cron expression, a one-off instant, or an
event — with no operator at the keyboard. An operator (or agent author)
creates a trigger row; a background loop scans for what is due and
launches the bound agent definition when its time comes. This is what
runs your nightly reconciliation, your scheduled remediation, and the
autonomous writes behind the [agent-requester approval
pattern](approvals-and-break-glass.md#option-2-the-agent-requester-pattern).

The hard part of unattended execution is not the timer — it is *credentials*:
a scheduled run days later still needs a live identity to reach Vault.
This guide covers creating triggers and the durable-credential behavior
that keeps them firing.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and an **agent definition** to fire
      (`meho agent create …`).
    - Creating and cancelling triggers needs **tenant_admin**; listing
      needs **operator**.
    - The scheduler is **Beta** — see the
      [feature-maturity index](../reference/maturity.md#scheduler).

## The surface at a glance

| Action | MCP tool | CLI |
|---|---|---|
| List triggers | `meho_scheduler_list` | `meho scheduler list` |
| Create a trigger (tenant_admin) | `meho_scheduler_create` | `meho scheduler create …` |
| Cancel a trigger (tenant_admin) | `meho_scheduler_cancel` | `meho scheduler cancel <trigger_id>` |

## Create a trigger

Every trigger binds one `--agent-definition` and one `--kind`.
`--agent-definition` takes the UUID of the agent definition, not its
name. Find it on the `id:` line of `meho agent show <name>`. A
**cron** trigger fires repeatedly on a 5-field expression evaluated in
its persisted `--timezone`:

```bash
meho scheduler create --kind cron \
  --agent-definition <definition-uuid> \
  --cron-expr "0 2 * * *" --timezone Europe/Vienna \
  --inputs '{"prompt": "Reconcile the prod scope."}' \
  --work-ref gh:evoila/meho#520
```

A **one-off** trigger fires once at a stored instant, then transitions
to a terminal state:

```bash
meho scheduler create --kind one_off \
  --agent-definition <definition-uuid> --fire-at 2026-08-10T22:00:00Z \
  --inputs '{"prompt": "Drain and patch esx-3."}'
```

A third kind, **event**, fires an agent run in response to a backplane
event (`--event-filter`) rather than a clock. The knobs worth knowing:

- `--inputs` — a JSON object rendered into the agent's prompt at fire
  time. A `cron` or `one_off` trigger needs it, and it must make a
  non-empty prompt (for example `{"prompt": "…"}`). MEHO refuses an
  empty `{}` with 422. An `event` trigger may leave it out.
- `--identity-sub` — the identity the scheduler impersonates at fire
  time (default `__scheduler__`); an override you rarely set. What makes
  the agent, not you, the requester of a scheduled write is the **bound
  agent definition's own subject** on an autonomous run, not this flag —
  see the [approvals guide](approvals-and-break-glass.md).
- `--in-flight-policy` — `fail_into_audit` (default) or `resume`: what
  happens to a run the scheduler was killed in the middle of.
  `fail_into_audit` records the interrupted run as failed on the audit
  ledger; `resume` picks it back up on restart.
- `--work-ref` — a change-ticket reference stamped onto the run's audit
  and broadcast lineage.

The external webhook sources that feed these events — Alertmanager,
Grafana, VCF Operations, Harbor, and generic JSON senders — and how to
write an `--event-filter` against them are covered in
[Event ingestion](event-ingestion.md).

List what's scheduled and its state:

```bash
meho scheduler list
```

Each `ScheduledTrigger` row carries `kind`, `cron_expr` / `fire_at`,
`timezone`, the hot `next_fire_at` column the loop scans, `last_fired_at`,
`status`, and — when a fire was skipped — a `last_skip_reason` and a
`skip_count`.

## Durable credentials: surviving unattended

A scheduled run has **no operator logged in**, so there is no Keycloak
JWT to forward to Vault's OIDC auth method. The scheduler instead sources
the agent's `client_credentials` secret **Vault-first**, under its own
static service token (`VAULT_SCHEDULER_TOKEN`), from
`SCHEDULER_AGENT_VAULT_PATH_PATTERN` (default
`secret/data/agents/{tenant_id}_{client_id}/credentials`), falling back to a pod env
var only when Vault yields nothing.

That static token is the failure point unattended operation used to hit:
a periodic Vault token **dies in the field** — it ages out, or an
operator forgets to re-mint it — and every credential read then `403`s,
so the scheduler **silently skips its fires**. A job that ran for weeks
just stops, quietly. MEHO closes that hole three ways:

- **Self-heal instead of skip.** On a `lookup-self`-confirmed dead
  token, the scheduler mints a **fresh** Vault token by `jwt_login` as
  the runner principal (runner JWT + `VAULT_CHECK_RUNNER_ROLE`, falling
  back to `VAULT_OIDC_ROLE`) and retries the failed read once — no
  operator, no sidecar in the loop. If the re-mint itself fails it falls
  back to the existing **loud** failure, never a silent skip.
- **Renewal on a timer, not on traffic.** Token renewal now fires on a
  dedicated tick cadence rather than only when agent-secret traffic
  flows — so an *idle* scheduler no longer ages its token out between
  jobs.
- **A loud pre-death guard.** Startup and an hourly `lookup-self` log a
  loud `ERROR` (`scheduler_vault_token_will_expire`) when the token is
  non-renewable or carries an explicit max TTL — it will die despite
  renewal, so it must be minted `-period=768h`.

When credentials genuinely cannot be resolved (no self-heal path
provisioned), the loop does the honest thing: it logs
`scheduler_credentials_unresolved`, **skips** the fire, and records
`last_skip_reason='credentials_unresolved'` with an incremented
`skip_count` on the trigger row. `meho scheduler list` shows the count
in the `SKIPS` column; `meho scheduler list --json` also shows
`last_skip_reason`. After 10 skips in a row, MEHO sets the trigger to
`paused` and stops trying. Fix the cause, cancel the trigger, and create
it again.

!!! note "Provisioning the headless mint"

    The headless `client_credentials` → runner-JWT → Vault `jwt_login`
    mint (the identity the self-heal logs in as) is provisioned once,
    deploy-side. The recipe lives in
    [`docs/cross-repo/vault-provisioning.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md);
    the dedicated `VAULT_CHECK_RUNNER_ROLE` lets you bound exactly what
    that background identity can read.

## Start an agent when a Sensor or Dashboard changes

A trigger can start an agent when a [Sensor](sensors-quickstart.md) or a
Dashboard changes state. So the simple model is: an agent, plus a trigger.
The trigger is a schedule, a webhook, or a Sensor or Dashboard change.

### Step 0: turn on the switch

MEHO publishes these events only when `CHECKS_AGENT_EVENTS` is `true`. It is
off by default. While it is off, a trigger that waits for a Sensor or a
Dashboard never starts.

The Helm chart has no value of its own for it. Set it with `extraEnv` in your
values file, then upgrade the release:

```yaml
extraEnv:
  - name: CHECKS_AGENT_EVENTS
    value: "true"
```

### The four choices

| Start when | CLI flag | Console ("Start on") |
|---|---|---|
| One Sensor changes | `--on-sensor <name or id>` | Start on Sensor, then pick the Sensor |
| One Dashboard changes | `--on-dashboard <name or id>` | Start on Dashboard, then pick the Dashboard |
| Any Sensor changes | `--on-any-sensor` | Start on Sensor, then "Any sensor" |
| Any Dashboard changes | `--on-any-dashboard` | Start on Dashboard, then "Any dashboard" |

Add `--only-worse` (console: "Only when it gets worse") to start only when the
state gets worse (for example from green to degraded). Without it, a recovery starts the agent too.

Use one of these flags with `--kind event`. Do not use them together with
`--event-filter`. `meho_scheduler_create` (the admin MCP tool) takes the same
choices. The console form also has a "Custom filter" choice, for people who
write the filter themselves.

MEHO stores the Sensor or Dashboard by its id, so a rename does not break the
trigger. A name that does not exist in your tenant is refused with 422.

### What the event says

If you write your own `--event-filter`, these are the fields you can match.
A Sensor event is `check.sensor.changed`. A Dashboard event is
`check.dashboard.changed`.

| Field | Meaning |
|---|---|
| `event` | The event name, as above. |
| `source` | `{kind, id, name}`: whether it is a `sensor` or a `dashboard`, and which one. |
| `previous_state`, `state` | The state before and after the change. |
| `worsening` | `true` when the state got worse. |
| `severity` | Sensor events only: the Sensor's severity. |
| `target`, `connector_id`, `op_id` | Sensor events only: the target name and the operation the Sensor runs. |
| `cause_group` | Dashboard events only: `{key, members}`, the failing Sensors that share one cause. It is `null` for a recovery. |
| `known_noise` | Dashboard events only: `true` when an earlier finding said this cause is known noise. |
| `coalesce_key` | Names the cause. It is the key for "one run per cause", below. |

An event has no raw results from the Sensor. The agent reads those itself,
with its own grants.

An example filter that starts only on a worse Dashboard change that is not
known noise:

```json
{"event": "check.dashboard.changed", "worsening": true, "known_noise": false}
```

!!! warning "An empty filter matches every event"

    An event trigger with the filter `{}` matches every event. Once the switch
    is on, it also starts on every Sensor and Dashboard change. Never leave
    the filter empty. Pick one of the `--on-*` flags, or write a filter with
    at least an `event` field.

### One run per cause, budgets and the kill switch

- **One run per cause.** Each event carries a `coalesce_key` that names its
  cause. While a run for that cause is still working (`pending`, `running` or
  `awaiting_approval`), a new event for the same cause is skipped. When the
  run is done, the next event starts a new run. So a Sensor that flaps does
  not start ten agents.
- **Budgets.** A run takes the agent's own login. A spend cap on that login
  (`meho agent budget set agent:<name> ...`, see
  [Hosted agents](hosted-agents.md)) stops a new run with `budget_exceeded`
  when the cap is reached. The agent's `--turn-budget` limits each run.
- **Kill switch.** To stop all runs of one agent, run
  `meho agent edit <name> --disabled`. To stop only one trigger, cancel it
  with `meho scheduler cancel <trigger_id>`.

### Worked example: a Sensor starts my agent

You need the `tenant_admin` role. The example uses a test system, so you can
make a Sensor fail on purpose. Names such as `lab-rke2` and `k8s-1.x` are only
examples: use your own target and connector.

**1. Make a Sensor.** It turns non-green when a pod is not running. The full
explanation is in the [Sensors quickstart](sensors-quickstart.md).

```bash
meho sensor create \
  --name pods-not-running \
  --connector-id k8s-1.x \
  --op-id k8s.pod.list \
  --target '{"name": "lab-rke2"}' \
  --params '{"all_namespaces": true, "field_selector": "status.phase!=Running"}' \
  --assertion '{"select": {"path": "$.total"},
                "compare": {"type": "threshold", "op": "gt", "degraded": 0, "critical": 2}}' \
  --cadence-kind interval --interval-seconds 60 \
  --severity critical
```

**2. Make a small agent that can only read.** First give it a login. Then
create it, and give it two grants: deny everything, then allow the pod list.

```bash
meho agent-principal register pod-reader

cat > pod-reader-toolset.json <<'JSON'
{
  "meta_tools": ["list_operation_groups", "search_operations", "call_operation"],
  "connectors": ["k8s-1.x"]
}
JSON

meho agent create pod-reader \
  --identity-ref agent:pod-reader \
  --model-tier standard \
  --turn-budget 20 \
  --system-prompt "You are pod-reader. You only read. You never change anything. A Sensor started you. Use the connector k8s-1.x. Find the operation with search_operations, then run it with call_operation. Say in short plain sentences which pods are not running." \
  --toolset @pod-reader-toolset.json

meho agent grant create --principal agent:pod-reader --op '*' --verdict deny
meho agent grant create --principal agent:pod-reader \
  --op k8s.pod.list --target '*' --verdict auto-execute
```

Hosted agents are described in [Hosted agents](hosted-agents.md). You do not
need that page for this example.

**3. Make the trigger.** `--agent-definition` takes the id of the agent. Read
it from the `id:` line of `meho agent show pod-reader`.

```bash
meho agent show pod-reader        # copy the id: line

meho scheduler create --kind event \
  --agent-definition <agent-id> \
  --on-sensor pods-not-running \
  --only-worse
```

If `CHECKS_AGENT_EVENTS` is still off, the answer carries a notice that the
trigger will not start until you turn the switch on.

**4. Make the Sensor fail.** Point one deployment at an image that does not
exist, so a pod gets stuck. Wait for the next Sensor check. The state changes
from `ok` to `degraded`, and MEHO starts the agent.

```bash
meho sensor list     # LAST_STATE is now degraded
```

**5. Find the run and read its answer.**

```bash
meho agent run-list --agent-name pod-reader
meho agent run-status <run-id>
```

`run-status` shows the `status`. When the run has `succeeded`, it shows the
`output`: the agent's answer. If the status stays `running`, the agent may be
waiting for a person to approve a step: check `meho approvals list`.

**6. Clean up.** Cancel the trigger with `meho scheduler cancel <trigger_id>`
(`meho scheduler list` shows the id), and fix the pod.

### Run the checks investigator from a trigger

The checks investigator is an agent that finds the cause of a worse Dashboard
and answers with a short finding. MEHO stores the finding and mails it to the
Dashboard's mail address. You can run it from a normal event trigger.

The folder
[`examples/checks-investigator/`](https://github.com/evoila/meho/tree/main/examples/checks-investigator)
has everything: the agent (`agent.json`), its grants (`permissions.json`) and
the trigger (`trigger.json`). Its README lists the commands. In short:

1. Turn on `CHECKS_AGENT_EVENTS`.
2. Register the login `agent:checks-investigator`, then create the agent and
   its grants from the files.
3. Create the trigger with the filter from `trigger.json`. It matches
   `check.dashboard.changed` events that are worse and are not known noise.

While an active event trigger for the investigator agent exists, the older
built-in investigator path steps aside. So one Dashboard change never starts
the agent twice. To go back to the old path, pause or cancel the trigger.

## Scheduler and the checks runner

The scheduler fires **agent runs**. It is not the same background worker
as the **checks/sensors runner**, which evaluates deterministic
[sensors](sensors-quickstart.md) on their own cadence — but both are
"background work with no operator present", and both depend on the same
durable-credential story. The sensor side of it (the `checkRunner.*`
chart block and `VAULT_CHECK_RUNNER_ROLE`) is covered in
[Sensors and your credential backend](sensors-quickstart.md#sensors-and-your-credential-backend);
this page is its agent-run twin.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| A trigger shows `last_skip_reason=credentials_unresolved` and a rising `skip_count`, or is `paused` after 10 skips | The scheduler could not resolve the agent's credentials and skipped the fire (loudly, not silently). | Stage the agent's `client_credentials` secret at `secret/data/agents/{tenant_id}_{client_id}/credentials`, or provision the self-heal mint (`vault-provisioning.md`). Create a `paused` trigger again. |
| Startup logs `scheduler_vault_token_will_expire` (ERROR) | `VAULT_SCHEDULER_TOKEN` is non-renewable or has an `explicit_max_ttl` — it will die despite renewal. | Re-mint it as a periodic token: `vault token create -period=768h …`. |
| `403 insufficient_role` on `scheduler create` / `cancel` | Creating and cancelling triggers need **tenant_admin**; listing is operator. | Use a tenant_admin session to author triggers. |
| `scheduler create` answers 422 | MEHO checks the trigger when you create it: a cron expression that does not parse, an unknown `--timezone`, or missing `--inputs` on a `cron` / `one_off` trigger. No trigger is made. | Fix the 5-field expression, the IANA timezone or the inputs, and create it again. |
| A Sensor trigger never starts the agent | `CHECKS_AGENT_EVENTS` is off, or the trigger's filter does not match. | Turn the switch on (see [Start an agent when a Sensor or Dashboard changes](#start-an-agent-when-a-sensor-or-dashboard-changes)). Check the filter with `meho scheduler list --json`. |
| A scheduled write is stuck `awaiting_approval` | Working as designed — every agent write above `safe` waits for a person, under the agent's subject. | Approve it as yourself; because the requester is the agent, it is not your own change. A `dangerous` or `destructive` write needs a MEHO-SA-Operator or an admin ([approvals](approvals-and-break-glass.md)). |
| A run interrupted mid-flight (scheduler restart) shows as failed | The default `--in-flight-policy=fail_into_audit` records a killed-mid-run trigger as a failure rather than silently resuming it. | Set `--in-flight-policy=resume` at create time if you want interrupted runs picked back up on restart. |

**Next:** [Event ingestion](event-ingestion.md).
