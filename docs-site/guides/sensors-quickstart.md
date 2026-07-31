# Watch your estate with sensors

Everything so far was interactive: you asked, MEHO answered. Sensors
make the backplane watch on its own. A **sensor** pins one tuple —
*operation + params + assertion + cadence + severity* — that a
built-in runner evaluates on a schedule, entirely deterministically
(no LLM in the loop). **Dashboards** compose sensors into a single
rolled-up state, and an optional agent **investigator** can triage a
dashboard the moment it goes red.

This guide registers a sensor on the Kubernetes target from the
previous guides, composes a dashboard, breaks something on purpose,
and reads the result.

!!! note "Prerequisites, roles, and maturity"

    - A probe-green target and the operation ladder from
      [Run your first operations](first-operations.md) — a sensor is
      just an operation call on a timer.
    - Creating sensors and dashboards requires **tenant_admin**; an
      operator-role JWT gets `403 insufficient_role`. Listing and
      reading need only operator.
    - Sensors are **beta** — see the
      [feature-maturity index](../reference/maturity.md) for what
      that promise means and the road to GA.

## The five states

Every sensor evaluation lands in exactly one of five states — the
same vocabulary everywhere (sensor projection, dashboard rollup, UI):

| State | Meaning |
|---|---|
| `ok` | Evaluated; assertion passed. |
| `degraded` | Evaluated; assertion failed at the *degraded* band. |
| `critical` | Evaluated; assertion failed at the *critical* band. |
| `unknown` | Could not be judged — the dispatch failed (most often credentials, see [the backend caveat](#sensors-and-your-credential-backend)), the value had the wrong type, or the sensor has gone stale (no evaluation past its due time plus a 60-second grace). |
| `skip` | Not scheduled — the sensor is paused. |

## Step 1 — pick a safe operation

A sensor may only pin an operation whose `safety_level` is **`safe`**
— the runner auto-executes on a schedule with nobody watching, so
`caution`/`dangerous` ops are refused at create time (422
`sensor_requires_safe_operation`). Find the op exactly as in the
previous guide:

```bash
meho operation search k8s-1.x "list pods" --group workload
# -> k8s.pod.list   safety_level=safe   requires_approval=false
```

Our check: *no pod in the cluster should be off `Running` phase*. The
call we want the runner to repeat is the one you already ran by hand:
`k8s.pod.list` with `{"all_namespaces": true, "field_selector":
"status.phase!=Running"}` — a healthy estate returns `total: 0`.

## Step 2 — create the sensor

The **assertion** is a bounded two-part spec: one `select` (a path
into the operation's result) feeding one typed comparator —
`threshold`, `equals`, `in`, `bool`, or `freshness`. It is
deliberately not a query language.

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
  --severity critical --for-seconds 120
```

Reading the assertion: select the result's `$.total`; the state goes
non-green when `value <op> bound` is true — here `degraded` as soon as
any pod is off-phase (`> 0`), `critical` at three or more (`> 2`).
The more severe band wins.

The other knobs:

- `--cadence-kind interval --interval-seconds N` (5–86400) **or**
  `--cadence-kind cron --cron-expr "*/5 * * * *"` with an optional
  `--timezone` (IANA, default UTC). The runner ticks on a ~10-second
  grid, so very short intervals quantize to it.
- `--severity` (`degraded` | `critical`, default `critical`) is a
  **cap** on what this sensor can contribute to a dashboard — a
  `severity: degraded` sensor can never drive a dashboard critical.
- `--for-seconds` is hold-time hysteresis: a failing state only
  counts toward the rollup after it has held that long (recovery is
  immediate). Our `120` means a single flapping pod won't page
  anyone.

Verify:

```bash
meho sensor list
# ID          NAME              STATUS  LAST_STATE  CADENCE     NEXT_FIRE_AT          SEVERITY
# <uuid>      pods-not-running  active  ok          every 60s   2026-07-31T09:21:12Z  critical
```

There is deliberately **no update or pause verb** — a sensor is
immutable after create ("edit" is delete + recreate), and `paused`
happens only when the runner itself parks a persistently failing
sensor.

## Step 3 — compose a dashboard

```bash
meho dashboard create --name estate-health \
  --description "Lab estate, rollup of the deterministic checks" \
  --sensor-id <uuid-from-sensor-list>
```

`--sensor-id` repeats for more members (membership is create-only).
Read it back:

```bash
meho dashboard show <dashboard_id>
```

The dashboard's state is computed **on read**, worst-of its members,
with these rules:

- `skip` members are excluded from the fold; `unknown` members
  contribute as `degraded` — a sensor that cannot evaluate is a
  problem, not a pass.
- Each member's contribution is capped at its `severity`.
- A failing state inside its `for:` window contributes `ok` and shows
  as *pending* in the member breakdown.
- Zero members rolls up `unknown`; all-`skip` rolls up `skip`.

The same view lives in the operator console at `/ui/checks` (list)
and `/ui/checks/{dashboard_id}` (member breakdown). Dashboards have
CLI, REST, and UI surfaces; sensors additionally have MCP tools
(`meho.sensor.list` / `create` / `delete`) — **dashboards have no MCP
tools today**.

## Step 4 — break something and watch

Make one pod unschedulable in the watched cluster — point a
deployment at a nonexistent image tag, or kill a node if the lab is
yours to break:

```bash
meho operation call k8s-1.x k8s.pod.list \
  --target lab-rke2 \
  --params '{"all_namespaces": true, "field_selector": "status.phase!=Running"}'
# -> total: 1
```

Then watch the timeline unfold:

1. Within the next interval the runner evaluates, the assertion
   selects `$.total = 1`, and `last_state` flips to `degraded`
   (`meho sensor list`); `state_since` records the flip.
2. For the first 120 seconds (`--for-seconds`), the dashboard still
   reads green and the member shows **pending** — that's the
   hysteresis absorbing flaps.
3. Once held past the window, `meho dashboard show` (and
   `/ui/checks`) goes `degraded`. Escalate the break to three
   off-phase pods and it crosses to `critical`.
4. Fix the deployment. Recovery is immediate on the next evaluation —
   no hold-time on the way back to `ok`.

Each evaluation stores its evidence on the sensor's projection
(`last_value`, `last_evidence`, `last_evaluated_at`) — enough to see
*what* the assertion saw without a results-history table.

## The investigator (optional deep tier)

When a dashboard's rollup crosses from green into a non-green state,
MEHO can fire a **diagnose-only** agent investigation: affected
sensors are correlated through the topology graph so one underlying
cause produces exactly one investigation, known-noise verdicts are
suppressed, and the agent writes a structured finding — verdict,
evidence, recommended action — into tenant memory
(`checks-noise-<group-key>`), retrievable via `search_memory`.

It is off until you opt in, and it has real prerequisites:

- An **enabled agent definition named `checks-investigator`** in your
  tenant (the name is configurable deploy-side). The
  [tiered-triage example](https://github.com/evoila/meho/tree/main/examples/r1-tiered-triage)
  is the mould — read-only toolset, writes need approval.
- The agent runtime, which is **experimental** maturity — check the
  [feature-maturity index](../reference/maturity.md) before leaning
  on it.
- Topology anchors for your targets (correlation degrades to
  per-sensor findings without them).

The investigator never executes a change: any write op its agent
attempts parks in the approval queue like everyone else's.

## Sensors and your credential backend

The honest operational caveat: scheduled evaluations run in the
background, with **no operator logged in** — so a sensor whose target
needs credentials depends on your deploy having a *background
credential identity*. Whether that exists depends on the credential
backend:

| Deploy | Target-bound sensors | What to do |
|---|---|---|
| Vault (default), no `checkRunner.*` configured | Evaluate **`unknown` forever** — the runner presents no identity Vault will honour. | Configure the check-runner service principal (`checkRunner.*` chart block) — but read the warning below first. |
| GSM, platform identity (no per-operator WIF) | Work — reads run under MEHO's own service account. | Nothing. |
| GSM, per-operator WIF, on GKE with pod identity | Work — background reads fall back to the pod's identity. | Nothing. |
| GSM, per-operator WIF, on-prem (no pod identity) | Evaluate **`unknown` forever** without the runner principal. | Configure `checkRunner.*`. |

Sensors on operations that read **no target credential** evaluate
fine on every backend, with zero extra configuration.

!!! warning "`checkRunner.*` on a Vault deploy widens background reads"

    The check-runner principal is not a GSM-only knob. On a Vault
    deploy it gives *all* background dispatch a token the documented
    default Vault role will accept — which can read every target
    credential in the policy's subtree. Bound the role before
    enabling it:
    [`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md)
    and
    [`docs/cross-repo/vault-provisioning.md` § Bounding the check-runner principal](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md).

Long-unattended operation has one more open edge: durable machine
credentials for background execution (the "still running on day 30"
promise) are actively being hardened — tracked in
[evoila/meho#2668](https://github.com/evoila/meho/issues/2668), which
is also the sensors feature's road-to-GA tracker.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| 422 `sensor_requires_safe_operation` | The pinned op is `caution`/`dangerous`. Sensors auto-execute unattended, so only `safe` ops qualify — by design. | Pick a read-class op; check `safety_level` on the search hit. |
| 422 `sensor_operation_not_found` | `(connector_id, op_id)` resolves to nothing — usually a bare product name as `connector_id`. | Use the `<impl_id>-<version>` form (`k8s-1.x`), and an `op_id` from `meho operation search`. |
| 409 `sensor_name_conflict` | Sensor names are unique per tenant. | Rename or delete the old one. |
| A plain 422 validation error *before* any of the codes above | Schema validation runs first: the comparator `type` must be one of `threshold` / `equals` / `in` / `bool` / `freshness` (not `gt`/`lt` — those are the threshold's `op`), the cadence must be exactly one of interval (5–86400 s) XOR cron, the assertion is capped at 8 KiB, and a `status` field in the body is rejected. | Fix the spec shape; the error names the field. |
| Sensor stuck `unknown`, evidence says *"threshold comparator requires a number, found array"* | Your `select.path` points at a list (e.g. `$.rows`), not a scalar. | Select a scalar — `$.total` for list-shaped Kubernetes results — or add an `aggregate` (`count`, `max`, …) to the select. |
| Target-bound sensor `unknown` forever, dispatch error names a credential read | No background credential identity on this deploy — the table above. | Work [Sensors and your credential backend](#sensors-and-your-credential-backend). |
| Sensor `unknown` though it was green a minute ago | Staleness: no fresh evaluation past `next_fire_at` + 60 s grace — runner down, or evaluations overlapping/parked. | Check the runner's health and the sensor's `status` (a parked sensor shows `paused` → rollup `skip`). |
| Dashboard `unknown` with all members green | The dashboard has zero members — an empty member set rolls up `unknown` by rule. | Add members (delete + recreate the dashboard). |
| A sensor keeps auto-running an op that is no longer harmless | The safe-only guard is **create-time only**. If a connector re-ingest changes a pinned op's safety class, existing sensors keep firing; the ingest result surfaces exactly which sensors are affected (`safety_changes`, warning `ingest_safety_class_changed`). | Re-audit the named sensors after any re-ingest that touches safety classes; delete the ones that no longer qualify. |

**Where next:** compose sensors with the approvals workflow (a red
dashboard, an investigator finding, a gated remediation) — the
[approvals & break-glass guide](index.md#coming-to-this-section)
covers the gated half.
