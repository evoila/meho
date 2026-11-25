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
    - Registering a runner principal needs **a person with tenant_admin**,
      signed in with their own login: the answer carries an enrollment code.
      A service login or an agent login gets `403 human_principal_required`.
      Revoking needs **tenant_admin**.
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
re-posted oldest-first. While a saved batch is not yet sent, new results
wait in the spool behind it, so MEHO gets them in the order they were
made. A failed assignment fetch keeps the **cached**
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
  cron schedule and its next due time. The runner runs it only when it is
  due, also from its cached list, on its first poll and after a restart. So
  a new sensor waits one full interval for its first run, as in the central
  pod. `meho sensor run` makes it due at once. The central check loop does
  not run these sensors.
- Only a runner of this MEHO version, or a later one, gets these items. An
  older runner gets none, and its checks turn `unknown` until you update it.
- Only an operation the runner can run unattended is allowed: a `safe` read
  with a handler on the runner that needs no approval. Create and edit refuse
  any other operation (`sensor_runner_op_unsupported`). When an operator
  later makes the operation wait for approval, MEHO takes the check off the
  runner's list.
- The runner sends the answer of the operation, also a list. MEHO reads the
  sensor's rule on that answer and sets the state. The runner never sends a
  state.
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
  reading and a recovery send both mails, in one batch or in two.
- A refused login on the runner counts toward the same pause as on a central
  check: after a few in a row (`SENSOR_AUTH_FAILURE_PARK_THRESHOLD`), MEHO
  pauses the check and the runner stops calling the target.
- Answers and error texts get the same secret cleanup as a central check.
  The runner cleans them before it sends them or keeps them in its retry
  file, and MEHO cleans them again before it stores them.
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

The runner uses the key file only when its group and other users have no
rights to it (for example mode `0600` or `0400`). With any other mode it stops
with an error that names the file and its mode and says to set mode `0600`.
In a Kubernetes pod with `fsGroup`, Kubernetes can add group rights to the key
file at each mount, and the runner then stops. Mount the key so that it keeps
mode `0600`: set no `fsGroup` for that pod, or use a volume driver that does
not change file rights. `fsGroupChangePolicy: OnRootMismatch` keeps the mode
only when the top folder of the volume already has the `fsGroup` group and the
rights Kubernetes expects. When it does not, Kubernetes changes the rights of
every file in the volume, and the runner stops (see
[Configure a Security Context](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/)).

This login follows RFC 7523, a signed JWT as the proof of a client's login
([section 3](https://www.rfc-editor.org/rfc/rfc7523#section-3)), and
Keycloak's "Signed JWT" client authenticator
([Keycloak: Confidential client credentials](https://www.keycloak.org/docs/latest/server_admin/index.html#_client-credentials)).
MEHO's live test of this login runs in CI against the Keycloak image
`keycloak/keycloak:26.0`. It was proven on the Keycloak image
`keycloak/keycloak:26.0` of the CI live test.

```bash
MEHO_RUNNER_CENTRAL_URL=https://meho.example.com \
MEHO_RUNNER_ID=edge-dc-a \
MEHO_RUNNER_ENROLLMENT_CODE=<code-from-register> \
python -m meho_backplane.runner
```

Put `MEHO_RUNNER_KEY_DIR` on a volume that stays when the runner
restarts. After the first start the runner finds its key there, so the
code is not needed again. After the first good start, take
`MEHO_RUNNER_ENROLLMENT_CODE` out of the runner's settings; the runner needs
only its key folder. When the answer to the first start is lost, start
the runner again with the same code and the same key folder: it finishes the
enrollment, also after the 30 minutes. `MEHO_RUNNER_TOKEN_URL` changes only the
address the runner sends its login request to. The signed request still names
the issuer that central gives, so the login server must know itself under that
issuer name. Both addresses must be `https://`. Plain `http://` works only
for an address on the same machine.

The runner sends its `MEHO_RUNNER_ID` with the code. When the code belongs to
a runner with another name, central refuses it and changes nothing, so the
code still works for its own runner. Check which code goes to which site, and
start again.

A runner whose key is lost stops with an error. Put the old key file back. Or
revoke the runner, register a new one under a new name, and start with the new
`MEHO_RUNNER_ID` and `MEHO_RUNNER_ENROLLMENT_CODE`. The same key folder can
stay: the runner replaces the old login there. Give the new runner a new,
empty `MEHO_RUNNER_SPOOL_DIR`, and keep the old spool folder as it is. Each
result waiting in the spool carries the name of the runner that made it, and
central refuses it under the new name.

*A fixed token.* Set `MEHO_RUNNER_TOKEN` to a bearer token of the runner's
Keycloak client instead. The runner sends it as it is and does not renew
it; give it a new one by hand when it ends.

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

Revoke works the same for every runner, with a key or with a fixed token.
It turns the runner's Keycloak client off, then deletes the client. With the
client, its public key and its Keycloak secret are gone. The runner row stays
and is marked revoked, and its enrollment code is cleared:
`meho runner-principal list --include-revoked` shows it, and `show` shows it
as revoked. The name stays taken: a new register of the same name gets `409`.
A revoked runner cannot be brought back, because the deleted client cannot be
turned on again. A new runner needs a new name and a new enrollment code. A
token the runner already has works until it ends.

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
| The runner exits 1 at startup naming a variable | A required `MEHO_RUNNER_*` var (`CENTRAL_URL` / `ID`) is missing or malformed, or it has neither `MEHO_RUNNER_TOKEN`, nor `MEHO_RUNNER_ENROLLMENT_CODE`, nor a key in `MEHO_RUNNER_KEY_DIR`. | Set the missing one; the error names it. |
| The runner exits 1 with `runner_login_error` and "enrollment was refused" | The enrollment code is wrong, was used, has ended, or its runner is revoked. | Start with the key file that enrolled with this code. Or revoke the runner, register a new one under a new name, and use the new name and code. |
| The runner exits 1 with `runner_login_error` and "is missing", "does not match the saved login" or "is for runner:…" | The login saved in `MEHO_RUNNER_KEY_DIR` does not fit the key there or `MEHO_RUNNER_ID`. | Put the right key file back, or set the right `MEHO_RUNNER_ID`. Or start with a new runner's name and code. |
| The runner exits 1 with "must start with https://" | The address of central or of the login server is plain `http://`. | Use the `https://` address. |
| The runner shows `unknown` on `/ui/runners` after the satellite stops | The dead-man sweep flipped its assignments stale because `last_seen_at` fell behind — working as designed, not a false green. | Bring the satellite back; restart it or check its outbound path to `MEHO_RUNNER_CENTRAL_URL`. |
| A work item comes back `refused` | The satellite declined it — most often because the `safety_level` is not `safe`. | Only `safe` reads run on a satellite; run changes centrally through the approval path (see [Changes through a satellite](#changes-through-a-satellite-not-built-yet)). |
| Results are stored after the uplink comes back | The uplink was down; results spooled to disk and re-posted when it recovered. With `RUNNER_CHECKS_ENABLED` on, results of a sensor with a `runner_name` count as check results. | Nothing — this is the spool working. If the spool fills (`SPOOL_MAX_FILES`), fix the uplink. |
| `403` registering a runner principal | `meho runner-principal register` needs a person with **tenant_admin**; `revoke` needs **tenant_admin**. A service login or an agent login gets `403 human_principal_required` on register, and a runner login gets `403 runner_scope_violation`. | Register with your own sign-in as a person with tenant_admin; `show` / `list` are operator-level. |
| The runner exits 1 with `runner_login_error` and "has mode", "Set its mode to 0600" | The group or other users have rights to the key file in `MEHO_RUNNER_KEY_DIR`. In a Kubernetes pod with `fsGroup`, Kubernetes can add group rights to the key file at each mount. | Run `chmod 600` on the file and start the runner again. In a pod, mount the key so that it keeps mode `0600`: set no `fsGroup` for that pod, or use a volume driver that does not change file rights. `fsGroupChangePolicy: OnRootMismatch` keeps the mode only when the top folder of the volume already has the `fsGroup` group and the rights Kubernetes expects; else Kubernetes changes the rights of every file in the volume. |

## What is not built yet

- Changes through a satellite.
- Reads that need a stored password (the runner has no Vault login of its own).
- Generic operations (made from an API spec) on a satellite.
- A chart or installer.
- The per-check interval.
- A CLI command or page that shows runner results.

**Where next:** back to the [Do real work](index.md) index, or the
[feature-maturity index](../reference/maturity.md) for what is GA versus
Beta across the surfaces these guides cover.
