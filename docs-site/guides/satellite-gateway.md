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
  is not `safe`, and any handler not under the connector package. A *change*
  runs only on an additive, **opt-in write tier** that is off by default and
  tightly bounded — see [Writes through a satellite](#writes-through-a-satellite).
  Operations classified `dangerous` or `destructive` are **never** dispatched
  to a satellite under any configuration.
- **Authorization, approval, and audit stay central.** The satellite
  never self-authorizes: central mints the assignment, central holds the
  audit ledger, central runs the approval queue. A compromised satellite
  can, at worst, decline to run its assigned work — it cannot escalate.

Resilience is built in for the flaky-uplink reality of a remote segment:
results that fail to post are written to an on-disk **spool** and
re-posted oldest-first; a failed assignment fetch keeps the **cached**
assignment running, so the satellite keeps evaluating its last-known
checks while the uplink is down.

## Writes through a satellite

A satellite is **read-only by default.** The write tier is **additive and
opt-in** — it stays off until an operator turns it on, and even then it stays
tightly bounded. Enabling it is **per runner, per operation class, and per
target**: a **tenant_admin** grants a runner named capabilities through the
write-allowlist route,
`POST /api/v1/runner-principals/{name}/write-allowlist`. Each granted entry is
an `op_pattern` token (all targets) or an `op_pattern@target_scope` token (one
target scope) — together the runner's write blast radius. A runner is granted
**nothing** at enrollment and **cannot widen its own allowlist**; only a
tenant_admin can, and only through that route.

No single control admits a write. A `caution`-level operation runs on a
satellite only when **all** of these hold at once:

- **Fresh central approval**, bound to the exact operation, target, and
  parameters — the same human approval queue every central write goes through.
- **A centrally signed work item, verified offline at the edge.** The satellite
  checks central's signature **before** it runs anything, so a tampered,
  re-pointed, or expired item fails closed without a call to central.
- **The allowlist re-checked independently at the edge**, against the runner's
  **own** provisioning configuration — so an item is refused if the runner's
  local allowlist disagrees with what central minted.
- **A short-lived, single-use, single-target credential**, brokered per work
  item — never a standing secret the runner holds.

Around that gate:

- **`dangerous` and `destructive` operations never ride a satellite** — anything
  delete-shaped included. They always run centrally through the approval path,
  under every configuration.
- **Every write is audited centrally.** The remote effect is recorded
  store-and-forward and tamper-evident, and central raises an **alarm if a
  minted write never reports** it.
- **Revocation stops writes too.** Revoking a runner's principal stops it
  minting or receiving any further writes.

!!! note "The write posture in one line"

    A satellite is **read-only by default**; it performs a write only on an
    opt-in tier where a per-runner allowlist, a fresh central approval, an
    offline-verified signature, and a single-use scoped credential must **all**
    hold at once — and `dangerous` or `destructive` operations can **never** run
    on it.

The write tier is **Beta** — see the
[feature-maturity index](../reference/maturity.md#satellite_gateway); treat it
as opt-in and validate it in your own environment before you rely on it.

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

**3. Confirm central sees it:**

```bash
meho runner-principal list
meho runner-principal show edge-dc-a
```

**4. Assign its checks.** A tenant_admin sets the checks of each runner
with `PUT /api/v1/checks/assignment/{runner}`. Each check names a
target, a `safe` operation, its params and a cadence in seconds. There
is no CLI verb for it. The satellite picks up its list on its next
poll. MEHO stores the results per runner; they do not feed sensors or
dashboards.

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
Sensors always run in the central pod, on targets central can reach
directly (the background-credential story is in
[Sensors and your credential backend](sensors-quickstart.md#sensors-and-your-credential-backend)).
Satellite checks are a separate list that a tenant_admin writes for each
runner (step 4 above), for the targets central cannot reach.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| The runner exits 1 at startup naming a variable | A required `MEHO_RUNNER_*` var (`CENTRAL_URL` / `ID` / `TOKEN`) is missing or malformed. | Set all three; the error names the offender. |
| The runner shows `unknown` on `/ui/runners` after the satellite stops | The dead-man sweep flipped its assignments stale because `last_seen_at` fell behind — working as designed, not a false green. | Bring the satellite back; restart it or check its outbound path to `MEHO_RUNNER_CENTRAL_URL`. |
| A work item comes back `refused` | The satellite declined it — most often because the `safety_level` is one it is not authorised to run, or the opt-in write tier is not enabled for it. | Read-class checks always belong on a satellite; a *change* runs only on the [opt-in write tier](#writes-through-a-satellite); `dangerous` / `destructive` work always runs centrally through the approval path. |
| Results lag but eventually appear | The uplink was down; results spooled to disk and re-posted when it recovered. | Nothing — this is the spool working. If the spool fills (`SPOOL_MAX_FILES`), fix the uplink. |
| `403` registering a runner principal | `meho runner-principal register` / `revoke` need **tenant_admin**. | Enroll under a tenant_admin session; `show` / `list` are operator-level. |

**Where next:** back to the [Do real work](index.md) index, or the
[feature-maturity index](../reference/maturity.md) for what is GA versus
Beta across the surfaces these guides cover.
