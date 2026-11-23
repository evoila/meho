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
  false results. Its results are stored apart and do not change any check
  today.

Resilience is built in for the flaky-uplink reality of a remote segment:
results that fail to post are written to an on-disk **spool** and
re-posted oldest-first; a failed assignment fetch keeps the **cached**
assignment running, so the satellite keeps evaluating its last-known
checks while the uplink is down.

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
# → the principal: id, Keycloak client runner:edge-dc-a, owner,
#   and a one-time enrollment code
```

Register prints a one-time enrollment code. It is shown once, it works
once, and it ends after 30 minutes. Copy it now. MEHO stores only a hash
of it. MEHO also stores the client's secret in Vault (under the same path
pattern as agent secrets). Set `MEHO_RUNNER_ID` to the runner name
(`edge-dc-a`).

**2. Deploy the runner** inside the isolated network — the same image,
started in runner mode, configured entirely through `MEHO_RUNNER_*` env.
The runner logs in in one of two ways.

*Its own key pair (recommended).* On its first start the runner makes a
key pair, keeps the private key in `MEHO_RUNNER_KEY_DIR` (default
`/var/lib/meho/runner-key`, file mode `0600`) and sends only the public
key, with the enrollment code, to central. Central puts the public key on
the runner's Keycloak client (signed JWT client login). From then on the
runner signs its own login request and gets a short token. It asks for a
new token before the old one ends, and once more after a `401`. No person
hands it a token. The private key never leaves the site.

```bash
MEHO_RUNNER_CENTRAL_URL=https://meho.example.com \
MEHO_RUNNER_ID=edge-dc-a \
MEHO_RUNNER_ENROLLMENT_CODE=<code-from-register> \
python -m meho_backplane.runner
```

Put `MEHO_RUNNER_KEY_DIR` on a volume that stays when the runner
restarts. After the first start the runner finds its key there, so the
code is not needed again. A runner whose key is lost needs a new
registration. `MEHO_RUNNER_TOKEN_URL` sets the login server's token address
when the runner must reach it under another name than central tells it.

*A fixed token.* Set `MEHO_RUNNER_TOKEN` to a bearer token of the runner's
Keycloak client instead. The runner sends it as it is and does not renew
it; give it a new one by hand when it ends. This mode works as before.

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
own container setup.

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

Revoke disables the runner's Keycloak client and removes its public key
there. The runner gets no new token, and its calls with the token it has
stop working when that token ends.

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
| The runner exits 1 at startup naming a variable | A required `MEHO_RUNNER_*` var (`CENTRAL_URL` / `ID`) is missing or malformed, or it has neither `MEHO_RUNNER_TOKEN`, nor `MEHO_RUNNER_ENROLLMENT_CODE`, nor a key in `MEHO_RUNNER_KEY_DIR`. | Set the missing one; the error names it. |
| The runner exits 1 with `runner_login_error` and "enrollment was refused" | The enrollment code is wrong, was used, has ended, or its runner is revoked. | Register the runner again and use the new code. |
| The runner shows `unknown` on `/ui/runners` after the satellite stops | The dead-man sweep flipped its assignments stale because `last_seen_at` fell behind — working as designed, not a false green. | Bring the satellite back; restart it or check its outbound path to `MEHO_RUNNER_CENTRAL_URL`. |
| A work item comes back `refused` | The satellite declined it — most often because the `safety_level` is not `safe`. | Only `safe` reads run on a satellite; run changes centrally through the approval path (see [Changes through a satellite](#changes-through-a-satellite-not-built-yet)). |
| Results are stored after the uplink comes back | The uplink was down; results spooled to disk and re-posted when it recovered. You cannot see them in MEHO yet. | Nothing — this is the spool working. If the spool fills (`SPOOL_MAX_FILES`), fix the uplink. |
| `403` registering a runner principal | `meho runner-principal register` / `revoke` need **tenant_admin**. | Enroll under a tenant_admin session; `show` / `list` are operator-level. |

## What is not built yet

- Results on dashboards, mails and the investigator.
- Changes through a satellite.
- Reads that need a stored password (the runner has no Vault login of its own).
- Generic operations (made from an API spec) on a satellite.
- A chart or installer.
- The per-check interval.
- A CLI command or page that shows runner results.

**Where next:** back to the [Do real work](index.md) index, or the
[feature-maturity index](../reference/maturity.md) for what is GA versus
Beta across the surfaces these guides cover.
