<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# vROps op surface onboarding — operator recipe

> Operator-facing recipe for the G3.6 `vrops-rest-9.0` op surface — the
> `meho vcf-operations …` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/vcf-operations.sh` wrapper. The
> connector lives in
> [`backend/src/meho_backplane/connectors/vcf_operations/`](../../backend/src/meho_backplane/connectors/vcf_operations/);
> recorded-fixture refresh tooling lives in
> [`backend/tests/fixtures/vcf/refresh.py`](../../backend/tests/fixtures/vcf/refresh.py).
> This doc is the cookbook every RDC operator reads when retiring
> `scripts/vcf-operations.sh` in favour of `meho vcf-operations …`.

## What this surface is

The `vrops-rest-9.0` connector is an **ingested** connector: the 8
curated read-only ops are stored as `EndpointDescriptor` rows seeded by
G0.7 spec ingestion of the vROps `/suite-api` OpenAPI spec (G3.6-T2
[#833](https://github.com/evoila/meho/issues/833)) and dispatched
through `HttpConnector._request_json` by the G0.6 `dispatch_ingested`
branch. The connector class
[`VcfOperationsConnector`](../../backend/src/meho_backplane/connectors/vcf_operations/connector.py)
registers under the `(product="vcf-operations", version="9.0",
impl_id="vrops-rest")` registry triple — the connector id
`vrops-rest-9.0`. Auth is HTTP Basic on every request (vROps'
`/suite-api/api/*` surface is stateless — no session token, no 401
re-login loop); the connector handles credentials transparently via
the shared
[`CredentialsCache`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py).

vROps has no public CI simulator
([#536](https://github.com/evoila/meho/issues/536) proved vendor
simulators cannot serve their REST surfaces). Integration coverage uses
a recorded-fixture record/replay pattern against captured vROps
responses. The refresh tool at
[`backend/tests/fixtures/vcf/refresh.py`](../../backend/tests/fixtures/vcf/refresh.py)
captures live responses against a lab appliance with redaction; the
E2E suite at
[`backend/tests/test_connectors_vcf_operations_e2e.py`](../../backend/tests/test_connectors_vcf_operations_e2e.py)
replays them via `respx` mocks. This is a known, documented limitation
per Initiative #369's DoD.

The v0.5 op surface (Initiative
[#369](https://github.com/evoila/meho/issues/369)) is the **read**
working set the consumer's `scripts/vcf-operations.sh` exercises daily
— write ops (custom-group create / maintenance-mode set / alert-ack)
stay in the wrapper until v0.5.next ships policy + approval flow:

| Group | CLI verb | `op_id` | Path |
| --- | --- | --- | --- |
| vrops-system | `meho vcf-operations about` | `GET:/suite-api/api/versions/current` | Appliance release name + build number |
| vrops-resources | `meho vcf-operations resource list` | `GET:/suite-api/api/resources` | Resource inventory (VMs, hosts, datastores, adapters) |
| vrops-resources | `meho vcf-operations resource get <id>` | `GET:/suite-api/api/resources/{id}` | Per-resource detail by UUID |
| vrops-alerts | `meho vcf-operations alert list` | `GET:/suite-api/api/alerts` | Currently firing or recently resolved alerts |
| vrops-alert-definitions | `meho vcf-operations alertdefinition list` | `GET:/suite-api/api/alertdefinitions` | Alert definitions (the policy surface) |
| vrops-symptoms | `meho vcf-operations symptom list` | `GET:/suite-api/api/symptoms` | Per-condition signals beneath alerts |
| vrops-recommendations | `meho vcf-operations recommendation list` | `GET:/suite-api/api/recommendations` | Operator-facing remediation hints |
| vrops-supermetrics | `meho vcf-operations supermetric list` | `GET:/suite-api/api/supermetrics` | User-defined metric formulae |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6. The
CLI verb tree is operator ergonomics over that one route; it is **not**
a separate data path and is **not** mirrored on the MCP surface
(CLAUDE.md postulate 5 — agents reach these ops via
`search_operations` / `call_operation`).

## Prerequisites

- **A reachable vROps appliance** (VMware Aria Operations 9.0). The
  connector derives the base URL from `target.host` + `target.port`.
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`
  on first dispatch per target and caches them via the shared
  [`CredentialsCache`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py).
  Basic auth is stateless on vROps — no session token to refresh.
- **A registered vROps target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-vrops`). The target carries
  `product="vcf-operations"`, `host` (the vROps FQDN — no `https://`),
  `port` (default 443), `secret_ref` (the Vault path to the
  credentials), and `auth_model="shared_service_account"`. Optional
  `target.auth_source` routes the Basic challenge to a non-local vROps
  identity domain (vIDM, AD realm name, etc.); omit for the local
  realm.
- **The 8 curated ops registered + enabled.** Run G0.7 spec ingestion
  against `docs:vcf-operations-9.0/suite-api.yaml`, then
  `apply_vrops_core_curation` once per tenant after ingest. See
  [`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md)
  for the end-to-end ingestion procedure.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. `meho vcf-operations …` requires
  `operator` role minimum (same gate as every dispatch verb).

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** —
the Vault-sourced credentials are used regardless of which operator
invokes the verb. Per-operator impersonation is out of scope for v0.5.

What this means for the credentials in Vault:

- Vault path: `target.secret_ref` (e.g. `kv/data/vcf-operations/<slug>`).
- Required fields: `username` (string), `password` (string).
- The backplane reads them lazily on first op invocation per target;
  the values stay cached in the connector's per-target
  `CredentialsCache` until rotation invalidates the cache (today this
  means a backplane restart — see "Credential rotation" below).
- **No session expiry.** vROps Basic auth is stateless; a 401 from the
  appliance always means "bad credentials" (or a misconfigured
  `auth-source`) and is not retried.
- **Credential rotation**: update the Vault secret, then restart the
  backplane (or wait for a future rotation admin endpoint to call
  `CredentialsCache.invalidate(target_name)`) so the connector
  reloads.
- **Optional `auth-source` routing.** vROps can federate identity
  through multiple sources. Set `target.auth_source` on the
  registered target (e.g. `vIDM`, an AD realm name) to route the
  Basic challenge; omit to fall back to the local realm. The CLI does
  not expose this as a per-call flag — it is a per-target property.

To register a new target, write a descriptor and import it. `meho targets
import` takes a `targets.yaml` **file** (there is no `meho targets create`
verb in v0.2 — `import` is the CLI's only write path):

```yaml
# rdc-vrops.yaml
targets:
  - name: rdc-vrops
    product: vcf-operations
    host: vrops-mgr.rdc.evoila.io
    port: 443
    secret_ref: kv/data/vcf-operations/rdc-vrops
    auth_model: shared_service_account
```

```bash
meho targets import rdc-vrops.yaml   # add --update to PATCH an existing target
```

**Self-signed / internal-CA appliance.** A nested-lab or freshly-deployed
vROps commonly presents a self-signed cert, which otherwise fails the probe
and dispatch with `connector_tls_verify_failed`. Add a per-target TLS-trust
field to the same descriptor — prefer pinning the appliance CA (verification
stays on); use `verify_tls: false` only as an audited last resort (the two
are mutually exclusive — see the [per-target TLS-trust guide](../../deploy/values-examples/README.md)):

```yaml
    # secure — trust this CA; chain + hostname verification stay ON:
    tls_ca_pin: |
      -----BEGIN CERTIFICATE-----
      ...appliance CA PEM...
      -----END CERTIFICATE-----
    # last resort instead of tls_ca_pin (verification OFF for this target; MITM risk):
    # verify_tls: false
```

Verify the fingerprint resolved correctly:

```bash
meho targets probe --name rdc-vrops --json | jq '{product, version, reachable}'
# expected: {"product": "vcf-operations", "version": "9.0.0.…", "reachable": true}
```

## Quick-start

```bash
# Appliance identity + build
meho vcf-operations about --target rdc-vrops

# Resource inventory (all monitored objects)
meho vcf-operations resource list --target rdc-vrops

# Narrow by resource kind / adapter kind
meho vcf-operations resource list --target rdc-vrops \
  --params '{"resourceKind":"VirtualMachine","pageSize":50}'

# Drill into one resource by UUID
meho vcf-operations resource get 00000000-0000-4000-8000-000000000000 --target rdc-vrops

# Active alerts
meho vcf-operations alert list --target rdc-vrops --params '{"activeOnly":true}'

# Critical-only alerts
meho vcf-operations alert list --target rdc-vrops \
  --params '{"alertCriticality":"CRITICAL","activeOnly":true}'

# Alert definitions — the policy surface alerts roll up against
meho vcf-operations alertdefinition list --target rdc-vrops

# Symptoms — the per-condition signals beneath alerts
meho vcf-operations symptom list --target rdc-vrops --params '{"activeOnly":true}'

# Recommendations attached to current alerts/symptoms
meho vcf-operations recommendation list --target rdc-vrops

# Super metrics (user-defined formulae)
meho vcf-operations supermetric list --target rdc-vrops

# Machine-readable output for any verb
meho vcf-operations resource list --target rdc-vrops --json \
  | jq '.result.resourceList[] | .identifier'

# Escape hatch: run any vrops-rest-9.0 op by op_id
meho vcf-operations operation call GET:/suite-api/api/versions/current --target rdc-vrops
meho vcf-operations operation search "alerts" --target rdc-vrops
```

## Verb reference

### `meho vcf-operations about`

Dispatches `GET:/suite-api/api/versions/current` against
`connector_id="vrops-rest-9.0"`. Human output: `release` (e.g.
`9.0.0.1.23456789`), `build`, `human` (the humanly readable name when
the appliance emits it).

```text
$ meho vcf-operations about --target rdc-vrops
vrops-rest-9.0 GET:/suite-api/api/versions/current — status=ok (42ms)
  release:  9.0.0.1.23456789
  build:    23456789
  human:    VMware Aria Operations 9.0
```

### `meho vcf-operations resource list`

Dispatches `GET:/suite-api/api/resources`. Renders `identifier` /
`resourceKey.name` / `resourceKey.resourceKindKey`. Filter via
`--params` (`resourceKind`, `adapterKind`, `name`, `page`, `pageSize`).
Production deployments often have hundreds to thousands of resources —
use `--json | jq` for ad-hoc filtering or the JSONFlux handle path on
the agent side for large lists.

### `meho vcf-operations resource get <id>`

Dispatches `GET:/suite-api/api/resources/{id}` with `<id>` substituted
into the path. `<id>` is the resource UUID returned by `resource list`.
Renders identifier / name / kind / status / state.

### `meho vcf-operations alert list`

Dispatches `GET:/suite-api/api/alerts`. Renders `alertId` /
`alertDefinitionName` / `alertLevel` / `status`. Filter via `--params`
(`activeOnly`, `alertCriticality`, `alertStatus`, `resourceId`,
`page`, `pageSize`).

### `meho vcf-operations alertdefinition list`

Dispatches `GET:/suite-api/api/alertdefinitions`. Renders the
definition `id` / `name` / `adapterKindKey` / `resourceKindKey`. Filter
via `--params` (`id` (repeatable), `adapterKind`, `resourceKind`,
`name`).

### `meho vcf-operations symptom list`

Dispatches `GET:/suite-api/api/symptoms`. Renders `id` /
`symptomDefinitionName` / `severity`. Useful when debugging which
underlying condition triggered an alert.

### `meho vcf-operations recommendation list`

Dispatches `GET:/suite-api/api/recommendations`. Renders `id` /
`description` (truncated for human eyes; full text via `--json`) /
`actionId` (when an automated remediation is linked — out of scope for
v0.5 read core).

### `meho vcf-operations supermetric list`

Dispatches `GET:/suite-api/api/supermetrics`. Renders `id` / `name` /
`formula`. Useful when answering "which metric formula computes X" or
auditing custom-metric definitions across the deployment.

### `meho vcf-operations operation search` / `meho vcf-operations operation call`

Meta-tool wrappers pre-scoped to `vrops-rest-9.0`. `search` runs the
hybrid BM25+cosine search across vrops-rest-9.0 op descriptors; `call`
dispatches any op_id directly. Use these for ops not yet promoted to
dedicated CLI aliases.

```bash
meho vcf-operations operation search "list resources" --group vrops-resources
meho vcf-operations operation call GET:/suite-api/api/versions/current --target rdc-vrops
```

## Audit and broadcast classification

Every vROps v0.5 op is `safety_level=safe`, `requires_approval=false`
— the read-only surface never mutates state. The audit row carries:
`method='DISPATCH'`, `path=<op_id>`, `target_id=<uuid>`,
`payload={"op_id": ..., "params_hash": ..., "source_kind":
"ingested", "connector_product": "vrops",
"connector_version": "9.0", "connector_impl_id": "vrops-rest",
"result_status": "ok"|"error"}`.

Broadcast events publish per-tenant on every successful dispatch.
`meho audit query --connector vrops-rest-9.0` retrieves the full
dispatch history; see [`audit-query.md`](./audit-query.md) for filter
syntax.

## Recorded-fixture refresh

When an appliance upgrade changes a response shape, refresh the
checked-in fixtures via the shipped tool:

```bash
# Operator-only — runs against a live lab appliance.
uv run python backend/tests/fixtures/vcf/refresh.py \
  --connector vcf-operations \
  --target rdc-vrops \
  --host vrops-mgr.lab.evoila.io \
  --username admin \
  --password "$VROPS_PASSWORD" \
  --insecure --force
```

The tool redacts `Set-Cookie`, `Authorization`, `sessionId`, and
`X-XSRF-TOKEN` from headers and `password` / `session_token` /
`sessionId` / `token` / `access_token` / `refresh_token` from JSON
bodies before writing the JSON snapshots under
`backend/tests/fixtures/vcf/vcf-operations/`. Refuses to overwrite an
existing fixture without `--force` (stale fixtures from a prior
appliance version are the worst kind of bit-rot).

After a refresh, re-run the E2E:

```bash
uv run pytest backend/tests/test_connectors_vcf_operations_e2e.py -x
```

The fixture refresh + replay loop is the only integration coverage
vROps gets in CI (per #536). Treat checked-in fixtures as
appliance-version-pinned snapshots — bump them alongside the vROps
version they were captured against.

## Migrating off `scripts/vcf-operations.sh`

The consumer's
[`scripts/vcf-operations.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-operations.sh)
drives vROps suite-api via a `curl` + Basic-auth wrapper. The
`meho vcf-operations` verbs replace it for the read-only workflows;
write workflows stay in the wrapper.

Migration table — the wrapper-flip recipe per workflow:

| Consumer workflow | `vcf-operations.sh` invocation | `meho vcf-operations …` replacement | Notes |
| --- | --- | --- | --- |
| Appliance identity + build | `vcf-operations.sh about` | `meho vcf-operations about --target rdc-vrops` | |
| Resource inventory | `vcf-operations.sh resources` | `meho vcf-operations resource list --target rdc-vrops` | Filter via `--params '{"resourceKind":"…"}'`. |
| Per-resource detail | `vcf-operations.sh resource <uuid>` | `meho vcf-operations resource get <uuid> --target rdc-vrops` | |
| Active alerts | `vcf-operations.sh alerts --active` | `meho vcf-operations alert list --target rdc-vrops --params '{"activeOnly":true}'` | |
| Critical alerts only | `vcf-operations.sh alerts --critical` | `meho vcf-operations alert list --target rdc-vrops --params '{"alertCriticality":"CRITICAL","activeOnly":true}'` | |
| Alert definition listing | `vcf-operations.sh alertdefs` | `meho vcf-operations alertdefinition list --target rdc-vrops` | |
| Symptom listing | `vcf-operations.sh symptoms` | `meho vcf-operations symptom list --target rdc-vrops` | |
| Recommendation listing | `vcf-operations.sh recommendations` | `meho vcf-operations recommendation list --target rdc-vrops` | |
| Super-metric listing | `vcf-operations.sh supermetrics` | `meho vcf-operations supermetric list --target rdc-vrops` | |

What `scripts/vcf-operations.sh` did that `meho vcf-operations`
deliberately does **not** do (out of scope for v0.5 — keep the
wrapper for these until a future Initiative lands them):

- **Write / mutate ops** — custom-group create/delete, maintenance-mode
  set, alert acknowledge. v0.5 is read-only; write ops land in
  v0.5.next pending policy + approval workflow.
- **Custom query parameters not in the descriptor.** The CLI exposes
  every documented filter via `--params <JSON>`. For undocumented or
  experimental query strings, fall back to
  `meho vcf-operations operation call <op_id> --params '{…}'`.
- **Non-suite-api paths.** `vcf-operations.sh` can hit arbitrary
  `/suite-api/…` paths; `meho vcf-operations` exposes only the 8
  curated core ops. Use
  `meho vcf-operations operation call GET:<path>` as an escape hatch
  for one-off queries.

Migration discipline: run the `meho vcf-operations` form alongside
`vcf-operations.sh` for an overlap window, diff the outputs, then
retire the wrapper call site. The MEHO path adds the full audit row +
broadcast event the bash pattern never had — that audit coverage is
the point of migrating.

### Per-ticket wrapper-flip recipe

For each active vROps consumer ticket, apply this pattern:

1. **Identify the wrapper invocation** in the ticket's reproduction
   steps (usually `bash scripts/vcf-operations.sh <command>`).
2. **Find the `meho vcf-operations` equivalent** from the table above.
3. **Validate output parity**: run both side-by-side against
   `rdc-vrops`. Use `--json | jq` on the `meho vcf-operations` side to
   pull the same fields the bash script was parsing.
4. **Update the ticket steps**: replace the `vcf-operations.sh`
   invocation with the `meho vcf-operations` form. Capture the
   `--json` output for the ticket's evidence block.
5. **Retire the wrapper call site** in the ticket's automation
   scripts once the MEHO form is validated.

Example — resource listing:

```bash
# Before (wrapper):
bash scripts/vcf-operations.sh resources rdc-vrops | jq '.[].identifier'

# After (meho vcf-operations):
meho vcf-operations resource list --target rdc-vrops --json \
  | jq '.result.resourceList[].identifier'

# Verify parity (sorted to elide vendor pagination order drift):
diff \
  <(bash scripts/vcf-operations.sh resources rdc-vrops | jq -S '[.[].identifier] | sort') \
  <(meho vcf-operations resource list --target rdc-vrops --json \
    | jq -S '[.result.resourceList[].identifier] | sort')
# expected: empty diff
```

Example — active alerts:

```bash
# Before:
bash scripts/vcf-operations.sh alerts --active rdc-vrops

# After:
meho vcf-operations alert list --target rdc-vrops --params '{"activeOnly":true}'
```

## Goal #214 G3.6 vROps checklist

| Checklist item | Status |
| --- | --- |
| G3.6-T1 [#829](https://github.com/evoila/meho/issues/829) — `VcfOperationsConnector` skeleton + Basic + auth-source | ✅ merged |
| G3.6-T2 [#833](https://github.com/evoila/meho/issues/833) — 8 read-core curation + smoke/JSONFlux acceptance | ✅ merged |
| G3.6-T3 [#837](https://github.com/evoila/meho/issues/837) — CLI verbs + E2E recorded-fixture + this doc | ✅ this Task |
| MCP `llm_instructions` / `when_to_use` reviewed for agent legibility | ✅ done (part of T2 curation; refreshed in T3 audit) |
| All 8 core ops dispatch → `status='ok'` in CI | ✅ `test_connectors_vcf_operations_e2e.py` |
| Audit rows carry `op_id` + `target_id` + `params_hash` | ✅ `test_connectors_vcf_operations_e2e.py` |
| JSONFlux handle path tested in CI | ✅ `test_connectors_vcf_operations_e2e.py` |
| `docs/cross-repo/vcf-operations-onboarding.md` with wrapper-flip recipe | ✅ this document |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired. | `meho login <url>` again. |
| `status=error connector_error: ... 401` | vROps credentials invalid, account locked, or `auth-source` misconfigured. | Verify the Vault secret at `target.secret_ref`. If using federated identity, verify `target.auth_source` matches a configured vROps source. |
| `status=error connector_error: ... 403` | Service account lacks read role on the resource family. | Grant the service account the read-only vROps role covering the target endpoint. |
| `status=error … unknown_op` | The 8 core ops are not registered/enabled. | Re-run `apply_vrops_core_curation` against the vROps connector after G0.7 ingest. See [`connector-ingestion.md`](./connector-ingestion.md). |
| `status=denied` | `read_only` role, or a tenant policy denied the dispatch. | Use an `operator`-role token. |
| `about` / any verb times out | vROps appliance unreachable from the backplane host. | Verify the FQDN/IP in `target.host` resolves from the backplane; check firewall rules (port 443 from backplane → vROps appliance). |
| `probe` fails with TLS error | Self-signed or expired certificate, or wrong CA bundle. | Per-target (preferred): pin the appliance CA with `tls_ca_pin` (verification stays on) or, as an audited last resort, set `verify_tls: false` on the target — see the [per-target TLS-trust guide](../../deploy/values-examples/README.md). System-wide: mount the correct CA bundle in the backplane and set `MEHO_TLS_CA_BUNDLE`; or configure vROps with a CA-signed cert. |
| `resource get <id>` returns 404 | Resource UUID not found, or stale after deletion. | Re-list via `resource list` and confirm the identifier is current. |
| `resource list` returns an empty `resourceList` | No resources match the filter, or the adapter instance hasn't started collecting yet. | Drop the filter via `--params '{}'`; verify adapter instances in the vROps UI under Administration → Solutions. |

## References

- Initiative: [#369 G3.6 tier-3 VCF management plane](https://github.com/evoila/meho/issues/369);
  Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface:
  [#829](https://github.com/evoila/meho/issues/829) (T1 connector
  skeleton),
  [#833](https://github.com/evoila/meho/issues/833) (T2 spec
  ingestion + curation),
  [#837](https://github.com/evoila/meho/issues/837) (T3 CLI verbs +
  E2E + this doc),
  [#841](https://github.com/evoila/meho/issues/841) (T13 shared
  vcf-auth + fixture refresh tooling).
- Connector source:
  [`backend/src/meho_backplane/connectors/vcf_operations/`](../../backend/src/meho_backplane/connectors/vcf_operations/).
- CLI verbs:
  [`cli/internal/cmd/vcf-operations/`](../../cli/internal/cmd/vcf-operations/).
- E2E integration test:
  [`backend/tests/test_connectors_vcf_operations_e2e.py`](../../backend/tests/test_connectors_vcf_operations_e2e.py).
- Acceptance tests:
  [`backend/tests/acceptance/test_g36_vrops_dispatch_smoke.py`](../../backend/tests/acceptance/test_g36_vrops_dispatch_smoke.py),
  [`backend/tests/acceptance/test_g36_vrops_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_g36_vrops_jsonflux_force_handle.py).
- Fixture refresh tool:
  [`backend/tests/fixtures/vcf/refresh.py`](../../backend/tests/fixtures/vcf/refresh.py).
- Consumer wrapper retiring:
  [`scripts/vcf-operations.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-operations.sh).
- vROps Suite API reference:
  <https://developer.broadcom.com/xapis/vrealize-operations-manager-api/latest/>.
- Related onboarding docs:
  [`nsx-onboarding.md`](./nsx-onboarding.md),
  [`sddc-manager-onboarding.md`](./sddc-manager-onboarding.md),
  [`vault-onboarding.md`](./vault-onboarding.md),
  [`audit-query.md`](./audit-query.md),
  [`broadcast-onboarding.md`](./broadcast-onboarding.md).
