<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# VCF Fleet op surface onboarding — operator recipe

> Operator-facing recipe for the G3.6 `fleet-rest-9.0` op surface — the
> `meho vcf-fleet …` verb tree, the agent meta-tool path, and the migration
> off the consumer's `scripts/vcf-fleet.sh` wrapper. The op handlers live in
> [`backend/src/meho_backplane/connectors/vcf_fleet/`](../../backend/src/meho_backplane/connectors/vcf_fleet/);
> the recorded-fixture refresh recipe is documented at
> [`docs/cross-repo/vcf-fixture-refresh.md`](./vcf-fixture-refresh.md) (#841).
> This doc is the cookbook every RDC operator reads when retiring
> `scripts/vcf-fleet.sh` in favour of `meho vcf-fleet …`.

## What this surface is

The `fleet-rest-9.0` connector is an **ingested** connector: the 8 curated
read-only ops are stored as `EndpointDescriptor` rows seeded from
[`FLEET_CORE_OPS`](../../backend/src/meho_backplane/connectors/vcf_fleet/core_ops.py)
and dispatched through `HttpConnector._request_json` by the G0.6
`dispatch_ingested` branch. The connector registers under the
`(product="vcf-fleet", version="9.0", impl_id="fleet-rest")` registry triple —
the connector id `fleet-rest-9.0`. Auth is **HTTP Basic** on every request
against Fleet's local LCM user store (typical service account: `admin@local`);
no SSO federation, no session establish, no XSRF-token dance. The connector
handles auth transparently via `VcfFleetConnector.auth_headers` and the
shared `CredentialsCache` helper (#841).

VCF Fleet has no public CI simulator (consumer wrapper note,
[Initiative #369](https://github.com/evoila/meho/issues/369) DoD). Integration
coverage uses a **recorded-fixture respx-mock pattern** against captured
appliance responses — same posture NSX, SDDC Manager, vROps, and vRLI use.
The refresh tool lives at
[`backend/tests/fixtures/vcf/refresh.py`](../../backend/tests/fixtures/vcf/refresh.py);
the fixture-refresh recipe is documented at
[`docs/cross-repo/vcf-fixture-refresh.md`](./vcf-fixture-refresh.md) (#841).

The v0.5 op surface (Initiative
[#369](https://github.com/evoila/meho/issues/369)) is the **read** working set
the consumer's `scripts/vcf-fleet.sh` exercises daily — write ops (environment
create, product patch, lifecycle workflow start) stay in the wrapper until
v0.5.next ships policy + approval flow.

| Group | CLI verb | `op_id` | Path |
| --- | --- | --- | --- |
| fleet-about | `meho vcf-fleet about` | `GET:/lcm/lcops/api/v2/about` | vRSLCM appliance identity (HTTP 500 in 9.0 — see below) |
| fleet-datacenter | `meho vcf-fleet datacenter list` | `GET:/lcm/lcops/api/v2/datacenters` | Datacenter inventory + wrapper-verified probe |
| fleet-vcenter | `meho vcf-fleet vcenter list <vmid>` | `GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters` | vCenters registered under one datacenter |
| fleet-environment | `meho vcf-fleet environment list` | `GET:/lcm/lcops/api/v2/environments` | Environment inventory (primary Fleet entry point) |
| fleet-environment | `meho vcf-fleet environment info <id>` | `GET:/lcm/lcops/api/v2/environments/{environmentId}` | Per-environment detail (products + nodes) |
| fleet-product | `meho vcf-fleet product list <env-id>` | `GET:/lcm/lcops/api/v2/environments/{environmentId}/products` | Products deployed under one environment |
| fleet-request | `meho vcf-fleet request list` | `GET:/lcm/request/api/v2/requests` | Lifecycle request listing (deploy / patch / upgrade) |
| fleet-request | `meho vcf-fleet request info <id>` | `GET:/lcm/request/api/v2/requests/{requestId}` | Per-request detail with errorCause on FAILED |

Every op dispatches through the same `POST /api/v1/operations/call` route the
agent surface uses — auth, policy, audit, broadcast, and JSONFlux all run as
documented in [CLAUDE.md](../../CLAUDE.md) §6. The CLI verb tree is operator
ergonomics over that one route; it is **not** a separate data path and is
**not** mirrored on the MCP surface (CLAUDE.md postulate 5).

## VCF 9.0 known issue: `/about` returns HTTP 500

Fleet's first-party diagnostic endpoints — `/about`, `/health`, `/version`,
`/system-details`, `/lcm/common/api/about`, `/lcm/locker/api/v2/about` — all
return HTTP 500 in current VCF 9.0 builds. This is documented in the consumer
wrapper `scripts/vcf-fleet.sh`'s probe-block and surfaced through the
connector's `FingerprintResult.extras.diagnostic_endpoints_broken` list.

Workarounds the connector implements:

- **Probe**: `VcfFleetConnector.probe` calls `GET /lcm/lcops/api/v2/datacenters`
  (the wrapper-verified probe path) and reads the `Lcm-API-Version` response
  header. Guaranteed to respond in 9.0.
- **Reachability check**: `meho vcf-fleet datacenter list --target rdc-fleet`
  is the operator-facing reachability probe. `meho vcf-fleet about` is kept
  for spec parity + future fix but will return `status=error` on 9.0
  appliances until Broadcom fixes the endpoint.
- **Product version sourcing**: Fleet does not expose its product version via
  any working endpoint in 9.0. The fingerprint carries the LCM API version
  (e.g. `"8.0"`) under `extras.lcm_api_version`. Operators needing the
  product version cross-source from SDDC Manager (`meho sddc-manager
  operation call GET:/v1/vcf-services`).

Track the upstream fix at [Broadcom developer portal](https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager/latest/);
when the endpoint is restored, the `meho vcf-fleet about` verb starts working
without code changes.

## Prerequisites

- **A reachable vRSLCM appliance** (VCF Fleet 9.x). The connector derives the
  base URL from `target.host` + `target.port`; both direct-appliance addresses
  and VCF proxied addresses work.
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`. The
  username is sent **verbatim** in the HTTP Basic header (typically
  `admin@local`); no realm suffix is appended. Credentials are cached in-process
  per target after first use via the shared `CredentialsCache` helper (#841).
- **A registered VCF Fleet target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-fleet`). The target carries `product="vcf-fleet"`,
  `host` (the appliance FQDN — no `https://`), `port` (default 443),
  `secret_ref` (the Vault path to the credentials), and
  `auth_model="shared_service_account"`.
- **The 8 curated ops registered + enabled.** Run the G3.6-T8 curation step
  (`apply_fleet_core_curation`) once per Fleet target after the G0.7 spec
  ingest.
- **An operator session.** `meho login <backplane-url>` writes the session
  token the CLI reuses. `meho vcf-fleet …` requires `operator` role minimum
  (same gate as every dispatch verb).

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** — the
Vault-sourced credentials are used regardless of which operator invokes the
verb. Per-operator impersonation is out of scope for v0.5 (Fleet has no SSO
federation; the local LCM user store is the only auth surface).

What this means for the credentials in Vault:

- Vault path: `target.secret_ref` (e.g. `kv/data/vcf-fleet/rdc`).
- Required fields: `username` (string, typically `admin@local`),
  `password` (string).
- The backplane reads them lazily on first op invocation per target;
  credentials are then cached in the connector's per-target dict for the
  lifetime of the process.
- **No 401-retry**: HTTP Basic credentials are stateless server-side (Fleet's
  local user store has no session lifecycle). A 401 response propagates
  directly to the caller — it signals wrong credentials, not an expired
  session. Mirrors the SDDC Manager auth posture; unlike NSX (which has a
  session-create + XSRF-token dance + 401-driven re-login).
- **Credential rotation**: update the Vault secret and restart the backplane
  (the in-process credential cache clears on `aclose`). There is no per-target
  credential refresh hook in v0.5.

To register a new target via the CLI:

```bash
meho targets import \
  --name rdc-fleet \
  --product vcf-fleet \
  --host vcf-fleet.rdc.evoila.io \
  --port 443 \
  --secret-ref kv/data/vcf-fleet/rdc \
  --auth-model shared_service_account
```

Verify the fingerprint resolved correctly:

```bash
meho targets probe --name rdc-fleet --json | jq '{product, version, reachable, extras}'
# expected: {"product": "vcf-fleet", "version": "8.0", "reachable": true,
#            "extras": {"lcm_api_version": "8.0", "datacenter_count": N, ...}}
# Note: `version` carries the LCM API version (the only working version source
# in 9.0), not the product version. The connector's class-level
# `supported_version_range=">=9.0,<10.0"` is matched against the persisted
# `Target.fingerprint.version` written by the canary fixture; in production
# the fingerprint is what `probe` writes back.
```

## Quick-start

```bash
# Reachability check (the working probe — use this instead of `about` in 9.0)
meho vcf-fleet datacenter list --target rdc-fleet

# Appliance identity (RETURNS HTTP 500 in 9.0 — see "VCF 9.0 known issue")
meho vcf-fleet about --target rdc-fleet

# vCenters registered under one datacenter
meho vcf-fleet vcenter list dc-vmid-001 --target rdc-fleet

# All Fleet-managed environments
meho vcf-fleet environment list --target rdc-fleet

# Per-environment detail (products + nodes + status)
meho vcf-fleet environment info env-vrops-prod --target rdc-fleet

# Products deployed under one environment
meho vcf-fleet product list env-vrops-prod --target rdc-fleet

# Recent lifecycle requests (deploy / patch / upgrade workflows)
meho vcf-fleet request list --target rdc-fleet

# Per-request detail (state + errorCause on FAILED)
meho vcf-fleet request info req-vmid-001 --target rdc-fleet

# Machine-readable output for any verb
meho vcf-fleet environment list --target rdc-fleet --json | jq '.result[].environmentName'

# Filter requests by state via jq (no native --status flag in v0.5)
meho vcf-fleet request list --target rdc-fleet --json \
  | jq '.result[] | select(.state == "INPROGRESS")'

# Escape hatch: run any fleet-rest-9.0 op by op_id
meho vcf-fleet operation call GET:/lcm/lcops/api/v2/datacenters --target rdc-fleet
meho vcf-fleet operation search "list environments"
```

## Verb reference

### `meho vcf-fleet about`

Dispatches `GET:/lcm/lcops/api/v2/about` against `connector_id="fleet-rest-9.0"`.
**Warning**: returns HTTP 500 in current VCF 9.0 builds — use
`vcf-fleet datacenter list` as the reachability probe instead. Kept for spec
parity + future Broadcom fix. When working, renders `apiVersion`,
`productVersion`, `buildNumber`, `releaseDate`.

### `meho vcf-fleet datacenter list`

Dispatches `GET:/lcm/lcops/api/v2/datacenters`. The wrapper-verified
reachability probe — guaranteed to respond in 9.0 even when `/about` returns
500. Renders `vmid` / `name` / `type` / `city`. The `vmid` is the load-bearing
identifier for `vcf-fleet vcenter list <vmid>`.

```text
$ meho vcf-fleet datacenter list --target rdc-fleet
fleet-rest-9.0 GET:/lcm/lcops/api/v2/datacenters — status=ok (42ms)
vmid                                   name                             type             city
dc-canary-001                          primary                          PRIVATE_CLOUD    Vienna
dc-canary-002                          secondary                        PRIVATE_CLOUD    Frankfurt
```

### `meho vcf-fleet vcenter list <datacenter-vmid>`

Dispatches `GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters`.
Requires a datacenter `vmid` from `vcf-fleet datacenter list`. Renders
`vmid` / `hostname` / `version` / `build`. The `hostname` is the load-bearing
identifier for cross-referencing against vSphere targets.

### `meho vcf-fleet environment list`

Dispatches `GET:/lcm/lcops/api/v2/environments`. The primary Fleet inventory
entry point — every vRA / vROps / vRLI / vIDM deploy lives under an
environment. Large appliances return a JSONFlux handle through the shared
`HandleStore`; use `result_describe` / `result_query` to filter when the
handle is present.

### `meho vcf-fleet environment info <environment-id>`

Dispatches `GET:/lcm/lcops/api/v2/environments/{environmentId}`. Returns
`products[]` with full deployment metadata (nodes, IPs, versions, FQDN),
configuration history, and status transitions. Requires an `environmentId`
from `vcf-fleet environment list`.

### `meho vcf-fleet product list <environment-id>`

Dispatches `GET:/lcm/lcops/api/v2/environments/{environmentId}/products`.
Returns one entry per product (`vrops`, `vrli`, `vidm`, `vra`, `postgres`,
…) with deployment status, version, and node breakdown.

### `meho vcf-fleet request list`

Dispatches `GET:/lcm/request/api/v2/requests`. Returns recent lifecycle
requests by `createdOn`. Renders `vmid` / `requestType` / `state` /
`requestName`. Busy appliances commonly return a JSONFlux handle — filter
via `--json | jq` on `state` / `requestType` / time windows when needed.

### `meho vcf-fleet request info <request-id>`

Dispatches `GET:/lcm/request/api/v2/requests/{requestId}`. Returns the full
request envelope including `inputMap` (creation parameters), `outputMap`
(generated identifiers), `executionPath[]` (per-stage status + timestamps),
and `errorCause` on `state=FAILED`. The primary post-mortem surface for
Fleet workflow debugging.

### `meho vcf-fleet operation search` / `meho vcf-fleet operation call`

Meta-tool wrappers pre-scoped to `fleet-rest-9.0`. `search` runs the hybrid
BM25+cosine search across `fleet-rest-9.0` op descriptors; `call` dispatches
any op_id directly. Use these for ops not yet promoted to dedicated CLI
aliases.

```bash
meho vcf-fleet operation search "upgrade workflow" --group fleet-request
meho vcf-fleet operation call GET:/lcm/lcops/api/v2/environments --target rdc-fleet
meho vcf-fleet operation call GET:/lcm/request/api/v2/requests/{requestId} \
  --target rdc-fleet --params '{"requestId":"req-vmid-001"}'
```

## Audit and broadcast classification

Every Fleet v0.5 op is `safety_level=safe`, `requires_approval=false` —
the read-only surface never mutates state. The audit row carries:
`method='DISPATCH'`, `path=<op_id>`, `target_id=<uuid>`,
`payload={"op_id": ..., "params_hash": ..., "source_kind": "ingested",
"connector_product": "fleet", "connector_version": "9.0",
"connector_impl_id": "fleet-rest", "result_status": "ok"|"error"}`.

Broadcast events publish per-tenant on every successful dispatch.
`meho audit query --connector fleet-rest-9.0` retrieves the full dispatch
history; see [`audit-query.md`](./audit-query.md) for filter syntax.

## Migrating off `scripts/vcf-fleet.sh`

The consumer's
[`scripts/vcf-fleet.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-fleet.sh)
drives the vRSLCM REST API via a `curl` + HTTP Basic wrapper. The
`meho vcf-fleet` verbs replace it for the read-only workflows; write
workflows (environment create, product patch, lifecycle workflow start)
stay in the wrapper.

| Consumer workflow | `vcf-fleet.sh` invocation | `meho vcf-fleet …` replacement | Notes |
| --- | --- | --- | --- |
| Reachability probe | `vcf-fleet.sh probe` | `meho vcf-fleet datacenter list --target rdc-fleet` | Same wrapper-verified path; `about` is broken in 9.0 |
| Datacenter listing | `vcf-fleet.sh datacenters` | `meho vcf-fleet datacenter list --target rdc-fleet` | |
| vCenters under datacenter | `vcf-fleet.sh vcenters <vmid>` | `meho vcf-fleet vcenter list <vmid> --target rdc-fleet` | |
| Environment listing | `vcf-fleet.sh environments` | `meho vcf-fleet environment list --target rdc-fleet` | Large appliances return a JSONFlux handle |
| Environment detail | `vcf-fleet.sh environment-info <id>` | `meho vcf-fleet environment info <id> --target rdc-fleet` | |
| Product listing | `vcf-fleet.sh products <env-id>` | `meho vcf-fleet product list <env-id> --target rdc-fleet` | |
| Request listing | `vcf-fleet.sh requests [state]` | `meho vcf-fleet request list --target rdc-fleet` | `state` filter via `--json \| jq` (no native flag in v0.5) |
| Request detail | `vcf-fleet.sh request-info <id>` | `meho vcf-fleet request info <id> --target rdc-fleet` | Surfaces `errorCause` on FAILED |

What `scripts/vcf-fleet.sh` did that `meho vcf-fleet` deliberately does
**not** do (out of scope for v0.5 — keep the wrapper for these until a
future Initiative lands them):

- **Write / mutate ops** — environment create / patch / upgrade workflow
  start, product patch, locker password ops. v0.5 is read-only; write ops
  land behind a policy + approval gate (no ETA yet).
- **Custom query parameters** — the v0.5 CLI exposes only the parameters
  each op formally declares in its descriptor. Use `meho vcf-fleet operation
  call <op_id> --params '{"customKey": "value"}'` for ad-hoc query strings.
- **Non-curated API paths** — `vcf-fleet.sh` can hit arbitrary vRSLCM API
  paths; `meho vcf-fleet` exposes only the 8 curated core ops. Use
  `meho vcf-fleet operation call GET:<path>` as an escape hatch for one-off
  queries against operator-enabled paths.
- **Locker password listing** — the `/lcm/locker/api/v2/passwords` endpoint
  is not curated in v0.5. Use `meho vcf-fleet operation call
  GET:/lcm/locker/api/v2/passwords` as the escape hatch (metadata only —
  the API never returns password values).

Migration discipline: run the `meho vcf-fleet` form alongside `vcf-fleet.sh`
for an overlap window, diff the outputs, then retire the wrapper call site.
The MEHO path adds the full audit row + broadcast event the bash pattern
never had — that audit coverage is the point of migrating.

### Per-ticket wrapper-flip recipe

For each active VCF Fleet consumer ticket, apply this pattern:

1. **Identify the wrapper invocation** in the ticket's reproduction steps
   (usually `bash scripts/vcf-fleet.sh <command>`).
2. **Find the `meho vcf-fleet` equivalent** from the table above.
3. **Validate output parity**: run both side-by-side against `rdc-fleet`.
   Use `--json | jq` on the `meho vcf-fleet` side to pull the same fields
   the bash script was parsing.
4. **Update the ticket steps**: replace the `vcf-fleet.sh` invocation with
   the `meho vcf-fleet` form. Capture the `--json` output for the ticket's
   evidence block.
5. **Retire the wrapper call site** in the ticket's automation scripts once
   the MEHO form is validated.

Example — environment listing ticket:

```bash
# Before (wrapper):
bash scripts/vcf-fleet.sh environments rdc-fleet | jq '.[].environmentName'

# After (meho vcf-fleet):
meho vcf-fleet environment list --target rdc-fleet --json \
  | jq '.result[].environmentName'

# Verify parity:
diff \
  <(bash scripts/vcf-fleet.sh environments rdc-fleet | jq -S '.[].environmentName') \
  <(meho vcf-fleet environment list --target rdc-fleet --json \
      | jq -S '.result[].environmentName')
# expected: empty diff
```

Example — failed-request post-mortem:

```bash
# Before:
bash scripts/vcf-fleet.sh request-info req-vmid-009 rdc-fleet

# After:
meho vcf-fleet request info req-vmid-009 --target rdc-fleet

# JSON for scripting / ticket capture:
meho vcf-fleet request info req-vmid-009 --target rdc-fleet --json \
  | jq '{state, executionStatus, errorCause, executionPath}'
```

Example — in-flight request triage:

```bash
# Before:
bash scripts/vcf-fleet.sh requests INPROGRESS rdc-fleet

# After (jq filter on state since there's no --status flag in v0.5):
meho vcf-fleet request list --target rdc-fleet --json \
  | jq '.result[] | select(.state == "INPROGRESS")'
```

## Recorded-fixture refresh

The integration test suite
([`backend/tests/test_connectors_vcf_fleet_e2e.py`](../../backend/tests/test_connectors_vcf_fleet_e2e.py))
replays recorded fixtures against a `respx`-mocked appliance — no Docker
dependency, runs in the `meho-runners` CI lane. When the vRSLCM API surface
changes (vendor patch, appliance upgrade), an operator re-records the
fixtures against a live lab appliance using the shared refresh tool from
[#841](https://github.com/evoila/meho/issues/841):

```bash
uv run python backend/tests/fixtures/vcf/refresh.py \
  --connector vcf-fleet \
  --target rdc-fleet \
  --host fleet.rdc.evoila.io \
  --username admin@local \
  --password "$FLEET_PASSWORD" \
  --output-dir backend/tests/fixtures/vcf/vcf-fleet \
  --insecure   # only when the lab appliance has a self-signed cert
```

The refresh tool:

- Records responses for the registered Fleet recipe (`about` + `datacenters`
  at minimum — extend the recipe in `refresh.py` when adding ops).
- Redacts `Authorization` / `Set-Cookie` / `password` / `token` keys before
  writing.
- Refuses to overwrite existing fixtures without `--force` (stale fixtures
  silently mask vendor drift).
- Refuses to record non-2xx responses (the `/about` 500 case fails fast).

See [`docs/cross-repo/vcf-fixture-refresh.md`](./vcf-fixture-refresh.md) for
the full recipe including credential handling, redaction customisation, and
the canary-fixture vs recorded-fixture distinction.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired. | `meho login <url>` again. |
| `status=error connector_error: HTTP 500` on `vcf-fleet about` | VCF 9.0 known issue — Fleet's `/about` endpoint is broken in current builds. | Use `vcf-fleet datacenter list` as the reachability probe; cross-source product version from SDDC Manager `/v1/vcf-services`. |
| `status=error connector_error: … HTTP 401` | Fleet credentials invalid. HTTP Basic 401 is terminal — no re-login path. | Verify the Vault secret at `target.secret_ref`; confirm `admin@local` (or the configured local user) is enabled in the LCM user store; confirm the password hasn't been rotated. |
| `status=error … unknown_op` | The 8 core ops are not registered/enabled. | Re-run `apply_fleet_core_curation` against the Fleet connector. |
| `status=denied` | `read_only` role, or a tenant policy denied the dispatch. | Use an `operator`-role token. |
| `datacenter list` / any verb times out | Fleet appliance unreachable from the backplane host. | Verify the FQDN/IP in `target.host` resolves from the backplane; check firewall rules (port 443 from backplane → Fleet). |
| `probe` fails with TLS error | Self-signed or expired certificate on the appliance, or wrong CA bundle. | Mount the correct CA bundle in the backplane container and set `MEHO_TLS_CA_BUNDLE`; or configure the Fleet appliance with a CA-signed cert. |
| `environment info` returns 404 | `<environment-id>` doesn't match an existing Fleet environment. | List available environments first: `meho vcf-fleet environment list --target rdc-fleet`. |
| `request list` returns empty | No requests on the appliance, or all requests older than the LCM retention window. | Check `--json` for the raw envelope; the LCM retention is appliance-configured. |
| Credential cache stale after rotation | In-process cache retains old credentials. | Restart the backplane so `aclose()` clears the `CredentialsCache`. |
| `username` / `password` missing from Vault secret | Connector raises `RuntimeError` naming the missing key. | Add the missing field to the Vault secret at `target.secret_ref`. |

## References

- Initiative: [#369 G3.6 tier-3 VCF management plane](https://github.com/evoila/meho/issues/369); Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface: [#831](https://github.com/evoila/meho/issues/831) (T7 skeleton + auth + probe), [#835](https://github.com/evoila/meho/issues/835) (T8 spec ingest + 8 core ops + curation), [#839](https://github.com/evoila/meho/issues/839) (T9 CLI verbs + E2E + this doc), [#841](https://github.com/evoila/meho/issues/841) (T13 shared `vcf_auth.py` + fixture refresh tool).
- Connector source: [`backend/src/meho_backplane/connectors/vcf_fleet/`](../../backend/src/meho_backplane/connectors/vcf_fleet/).
- CLI verbs: [`cli/internal/cmd/vcf-fleet/`](../../cli/internal/cmd/vcf-fleet/).
- E2E integration test: [`backend/tests/test_connectors_vcf_fleet_e2e.py`](../../backend/tests/test_connectors_vcf_fleet_e2e.py).
- Recorded-fixture tooling: [`backend/tests/fixtures/vcf/refresh.py`](../../backend/tests/fixtures/vcf/refresh.py) and [`docs/cross-repo/vcf-fixture-refresh.md`](./vcf-fixture-refresh.md).
- Consumer wrapper retiring: [`scripts/vcf-fleet.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-fleet.sh).
- vRSLCM REST API reference: <https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager/latest/>.
- Related onboarding docs: [`nsx-onboarding.md`](./nsx-onboarding.md), [`sddc-manager-onboarding.md`](./sddc-manager-onboarding.md), [`vault-onboarding.md`](./vault-onboarding.md), [`audit-query.md`](./audit-query.md).
