# Reach isolated networks with a satellite gateway

Some of the systems you need to watch sit where the central backplane
cannot dial them: behind NAT, on a private control plane, on a
ClusterIP-only service, in a customer network with no inbound path. A
**satellite gateway** solves this without poking holes in the firewall.
It is a second deploy mode of the *same* MEHO container image, run inside
the isolated network, that dials **outbound** to central, pulls the
checks it has been assigned, runs them locally, and reports the results
back.

This guide covers the model, how to enroll and deploy a satellite, and
what stays central (which is almost everything).

!!! note "Prerequisites, roles, and maturity"

    - A central backplane the satellite can reach **outbound** (the
      satellite dials it; the reverse path is never opened).
    - Registering and revoking runner principals needs **tenant_admin**.
    - The satellite gateway is **Beta** — see the
      [feature-maturity index](../reference/maturity.md#satellite_gateway).

## The model: push-only, centrally authorized

The satellite is a **dumb executor of centrally-authorized work.** Run it
with `python -m meho_backplane.runner` — the third mode of the shared
image, alongside Serve (the central API) and Migrate. It has **no** local
database, no Valkey, no UI, no MCP surface, and **no inbound listener**.
Every connection is initiated by the satellite; the center is passive.

Each tick, the satellite:

1. **Polls** central for its current assignment (a set of authorized
   work items).
2. **Executes** each item locally against the same connector surface the
   central instance uses.
3. **Reports** the results back.

Two properties make this safe to run in an untrusted segment:

- **Read-class checks by default.** Out of the box a satellite runs only
  safe, read-class work: it **refuses** any work item whose `safety_level`
  is not `safe`, and any handler not under the connector package. A
  satellite runs only `safe` reads. Changes through a satellite are not
  built yet; see [What is not built yet](#what-is-not-built-yet).
  Operations classified `dangerous` or `destructive` are **never** dispatched
  to a satellite under any configuration.
- **Authorization, approval, and audit stay central.** The satellite
  never self-authorizes: central mints the assignment, central holds the
  audit ledger, central runs the approval queue. A compromised satellite
  cannot run anything it was not sent, but it can skip its work or send
  false results. With `RUNNER_CHECKS_ENABLED` off (the default), its
  results are stored apart and do not change any check. With it on, a
  result for a check bound to this runner sets the state of that check;
  MEHO reads the check's rule itself, so the runner never sends a state.

Resilience is built in for the flaky-uplink reality of a remote segment:
results that fail to post are written to an on-disk **spool** and
re-posted oldest-first; a failed assignment fetch keeps the **cached**
assignment running, so the satellite keeps evaluating its last-known
checks while the uplink is down.

## Checks that run at a site

A check (a sensor) can run on a runner instead of in the central pod. Set
`runner_name` when you create the sensor. The name must be a runner of the
same tenant that is not revoked. This needs the switch
`RUNNER_CHECKS_ENABLED`, which is off by default.

With the switch **on**:

- The runner's list holds one item for each active sensor with its name.
  `check_ref` is the sensor id. The item carries the sensor's interval or
  cron schedule, and the runner runs it only when it is due, also from its
  cached list. The central check loop does not run these sensors.
- Only an operation the runner can run unattended is allowed: a `safe` read
  with a handler on the runner that needs no approval. Create and edit refuse
  any other operation (`sensor_runner_op_unsupported`). When an operator
  later makes the operation wait for approval, MEHO takes the check off the
  runner's list.
- The runner sends the answer of the operation. MEHO reads the sensor's rule
  on that answer and sets the state. The runner never sends a state.
- The check shows on the dashboard, sends mails on a change and on recovery,
  and can start the investigator, like a central check. The dashboard row
  says "runs at: runner `<name>`".
- A result for a `check_ref` that is not an active sensor of this runner in
  this tenant is stored and not counted.
- A result needs the runner's own time (`produced_at`). A runner time later
  than MEHO's clock is refused: MEHO does not store it and names it in the
  answer (`refused`), and the runner logs it. Check the runner's clock then.
  A result without a runner time is stored and not counted. A result made at
  or before the last counted one is dropped, also a re-post after the
  clean-up job removed the stored copy. A result that was posted before (a
  re-post from the disk spool) is not counted again.
- Each result is counted on its own, in the order of the batch. A critical
  reading and a recovery in one batch send both mails.
- A refused login on the runner counts toward the same pause as on a central
  check: after a few in a row (`SENSOR_AUTH_FAILURE_PARK_THRESHOLD`), MEHO
  pauses the check and the runner stops calling the target.
- Answers and error texts get the same secret cleanup as a central check
  before they reach the check's history.
- An answer over 256 KB, or a batch over 4 MB, gives the check the state
  `unknown` with the reason "answer too large".
- When the runner stops reporting, its checks get the state `unknown`. They
  are never green. When the runner is back, normal results follow.
- MEHO counts the results by outcome in `runner_check_results_total`
  (`counted`, `not_in_list`, `too_large`, `future_time`, `no_time`, `old`).

With the switch **off**, MEHO only stores runner results, the central check
loop runs every sensor, `runner_name` is refused on create, and no answer
shows `runner_name`.

`meho sensor create --runner <name>` and the MCP tool `meho_sensor_create`
(input `runner_name`, shown only while the switch is on) bind a new check to
a runner.

The hand-written list (`PUT /api/v1/checks/assignment/{runner}`) still works.
Prefer sensors with a `runner_name`.

While the switch is on, MEHO keeps the raw rows of runner results for 7 days
(`RUNNER_CHECK_RESULTS_RETENTION_DAYS`; `0` keeps them). With the switch off,
the clean-up job deletes none of them.

## Changes through a satellite (not built yet)

A satellite cannot run a change today. Parts of a design exist in the code
(a signed, single-use work item; a per-runner allowlist; a one-time
credential), but nothing sends an approved change to a runner, the runner
does not fetch changes, and the one-time credential step is not connected.
Run changes from the central MEHO, through the normal approval flow.
`dangerous` and `destructive` operations will never run on a satellite.

## Enroll and deploy a satellite

**1. Register a runner principal** on central. This mints the
satellite's identity (a Keycloak client `runner:<name>`, tagged
`kind=runner`):

```bash
meho runner-principal register edge-dc-a
# → the principal: id, Keycloak client runner:edge-dc-a, owner
```

Register prints no token. MEHO stores the client's secret in Vault
(under the same path pattern as agent secrets). Set `MEHO_RUNNER_ID` to
the runner name (`edge-dc-a`). `MEHO_RUNNER_TOKEN` is a bearer token of
that Keycloak client.

**2. Deploy the runner** inside the isolated network — the same image,
started in runner mode, configured entirely through `MEHO_RUNNER_*` env:

```bash
MEHO_RUNNER_CENTRAL_URL=https://meho.example.com \
MEHO_RUNNER_ID=edge-dc-a \
MEHO_RUNNER_TOKEN=<token-of-runner:edge-dc-a> \
python -m meho_backplane.runner
```

Optional knobs: `MEHO_RUNNER_TICK_INTERVAL_SECONDS` (poll cadence),
`MEHO_RUNNER_SPOOL_DIR` and `MEHO_RUNNER_SPOOL_MAX_FILES` (the retry
spool bounds).

There is no chart, package or installer for the runner yet; you write your
own container setup. The runner does not renew its token; when the token
expires, give it a new one by hand.

**3. Confirm central sees it:**

```bash
meho runner-principal list
meho runner-principal show edge-dc-a
```

**4. Assign its checks.** A tenant_admin sets the checks of each runner
with `PUT /api/v1/checks/assignment/{runner}`. Each check names a
target, a `safe` operation of a typed connector, and its params. The
list also takes a cadence in seconds, but the runner does not use it yet:
it runs every check on every loop (`MEHO_RUNNER_TICK_INTERVAL_SECONDS`,
default 60). There is no CLI verb for it. The satellite picks up its list
on its next poll. MEHO stores the results per runner; they do not feed
sensors or dashboards.

## Liveness and the kill switch

A satellite that dies or loses its uplink must not leave its checks
silently reporting last-known-good forever. Liveness is enforced on the
**central clock**, never the satellite's own:

- **Heartbeat is piggybacked**, not a separate call — every
  authenticated poll stamps the runner principal's `last_seen_at`
  server-side (the client cannot forge it). The poll cycle *is* the
  heartbeat, so a healthy idle satellite still proves liveness, and a
  wedged work loop cannot masquerade as alive.
- **Dead-man staleness.** When a runner's `last_seen_at` falls behind
  the cutoff, central flips its assignment rows to stale and writes an
  internal audit row (`gateway.runner.stale`) — the runner shows as
  `unknown` on the console page `/ui/runners`, not a stale green.

To decommission or contain a satellite, revoke its principal — the kill
switch:

```bash
meho runner-principal revoke edge-dc-a
```

## Satellite vs. the in-process check runner

Do not confuse the satellite with the **in-process check runner** that
evaluates [sensors](sensors-quickstart.md) inside the central pod.
A sensor without a `runner_name` runs in the central pod, on targets
central can reach directly. A sensor with a `runner_name` runs on that
runner while `RUNNER_CHECKS_ENABLED` is on; with the switch off the central
pod runs it too (the background-credential story is in
[Sensors and your credential backend](sensors-quickstart.md#sensors-and-your-credential-backend)).
A check that runs at a site is a sensor with a `runner_name` (see
[Checks that run at a site](#checks-that-run-at-a-site)). The list that a
tenant_admin writes for each runner (step 4 above) also still works.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| The runner exits 1 at startup naming a variable | A required `MEHO_RUNNER_*` var (`CENTRAL_URL` / `ID` / `TOKEN`) is missing or malformed. | Set all three; the error names the offender. |
| The runner shows `unknown` on `/ui/runners` after the satellite stops | The dead-man sweep flipped its assignments stale because `last_seen_at` fell behind — working as designed, not a false green. | Bring the satellite back; restart it or check its outbound path to `MEHO_RUNNER_CENTRAL_URL`. |
| A work item comes back `refused` | The satellite declined it — most often because the `safety_level` is not `safe`. | Only `safe` reads run on a satellite; run changes centrally through the approval path (see [Changes through a satellite](#changes-through-a-satellite-not-built-yet)). |
| Results are stored after the uplink comes back | The uplink was down; results spooled to disk and re-posted when it recovered. With `RUNNER_CHECKS_ENABLED` on, results of a sensor with a `runner_name` count as check results. | Nothing — this is the spool working. If the spool fills (`SPOOL_MAX_FILES`), fix the uplink. |
| `403` registering a runner principal | `meho runner-principal register` / `revoke` need **tenant_admin**. | Enroll under a tenant_admin session; `show` / `list` are operator-level. |

## What is not built yet

- Changes through a satellite.
- Reads that need a stored password (the runner has no Vault login of its own).
- Generic operations (made from an API spec) on a satellite.
- A chart or installer, and token renewal.
- The per-check interval.
- A CLI command or page that shows runner results.

**Where next:** back to the [Do real work](index.md) index, or the
[feature-maturity index](../reference/maturity.md) for what is GA versus
Beta across the surfaces these guides cover.
