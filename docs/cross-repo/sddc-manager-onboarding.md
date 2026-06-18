<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# SDDC Manager op surface onboarding — operator recipe

> Operator-facing recipe for the G3.5 `sddc-rest-9.0` op surface — the
> `meho sddc-manager …` verb tree, the agent meta-tool path, and the migration
> off the consumer's `scripts/sddc-manager.sh` wrapper. The op handlers live in
> [`backend/src/meho_backplane/connectors/sddc_manager/`](../../backend/src/meho_backplane/connectors/sddc_manager/);
> the engineering-facing companion is
> [`docs/codebase/connectors-sddc-manager.md`](../codebase/connectors-sddc-manager.md).
> This doc is the cookbook every RDC operator reads when retiring
> `scripts/sddc-manager.sh` in favour of `meho sddc-manager …`.

## What this surface is

The `sddc-rest-9.0` connector is an **ingested** connector: the 9 curated
read-only ops are stored as `EndpointDescriptor` rows seeded from
[`SDDC_CORE_OPS`](../../backend/src/meho_backplane/connectors/sddc_manager/core_ops.py)
and dispatched through `HttpConnector._request_json` by the G0.6
`dispatch_ingested` branch. The connector registers under the
`(product="sddc", version="9.0", impl_id="sddc-rest")` registry triple —
the connector id `sddc-rest-9.0`. Auth is **HTTP Basic** on every request
(`username@sso_realm:password`); no session establish or XSRF-token dance is
needed. The connector handles this transparently via `SddcManagerConnector.auth_headers`.

SDDC Manager has no public CI simulator. Integration coverage uses a
recorded-fixture respx-mock pattern against captured VCF appliance responses.
This is a known, documented limitation per Initiative #368's DoD.

The v0.2 op surface (Initiative
[#368](https://github.com/evoila/meho/issues/368)) is the **read**
working set the consumer's `scripts/sddc-manager.sh` exercises daily — write
ops stay in the wrapper until v0.2.next ships policy + approval flow:

| Group | CLI verb | `op_id` | Path |
| --- | --- | --- | --- |
| sddc-releases | `meho sddc-manager about` | `GET:/v1/releases/system` | VCF release info + BOM |
| sddc-managers | `meho sddc-manager manager list` | `GET:/v1/sddc-managers` | SDDC Manager appliance inventory |
| sddc-domains | `meho sddc-manager domain list` | `GET:/v1/domains` | All VCF workload domain listing |
| sddc-domains | `meho sddc-manager domain info <id>` | `GET:/v1/domains/{id}` | Per-domain detail (vCenters, NSX, clusters, SSO) |
| sddc-clusters | `meho sddc-manager cluster list [--domain <id>]` | `GET:/v1/clusters` | Cluster listing (optionally scoped to a domain) |
| sddc-hosts | `meho sddc-manager host list [--domain <id>] [--cluster <id>]` | `GET:/v1/hosts` | Host inventory (optionally scoped to domain or cluster) |
| sddc-network-pools | `meho sddc-manager network-pool list` | `GET:/v1/network-pools` | Network pool listing |
| sddc-bundles | `meho sddc-manager bundle list` | `GET:/v1/bundles` | LCM bundle listing |
| sddc-tasks | `meho sddc-manager workflow list [--status <state>]` | `GET:/v1/tasks` | VCF workflow task listing |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6. The
CLI verb tree is operator ergonomics over that one route; it is **not**
a separate data path and is **not** mirrored on the MCP surface
(CLAUDE.md postulate 5).

## Prerequisites

- **A reachable SDDC Manager appliance** (VCF 9.x). The connector derives the
  base URL from `target.host` + `target.port`; both direct-appliance addresses
  and VCF proxied addresses work.
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`.
  The username is sent as `username@sso_realm` (default realm:
  `"vsphere.local"`); this matches the format SDDC Manager 9.0 expects for
  HTTP Basic auth. Credentials are cached in-process per target after first
  use.
- **A registered SDDC Manager target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-sddc-manager`). The target carries `product="sddc"`,
  `host` (the appliance FQDN — no `https://`), `port` (default 443),
  `secret_ref` (the Vault path to the credentials),
  `auth_model="shared_service_account"`, and optionally `sso_realm` if the
  deployment uses a non-default SSO domain.
- **The 9 curated ops registered + enabled.** Run the G3.5-T5 curation step
  (`apply_sddc_core_curation`) once per SDDC Manager target after the G0.7
  spec ingest.
- **An operator session.** `meho login <backplane-url>` writes the session
  token the CLI reuses. `meho sddc-manager …` requires `operator` role
  minimum (same gate as every dispatch verb).

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** —
the Vault-sourced credentials are used regardless of which operator
invokes the verb. Per-operator impersonation is out of scope for v0.2.

What this means for the credentials in Vault:

- Vault path: `target.secret_ref` (e.g. `kv/data/sddc-manager/rdc`).
- Required fields: `username` (string), `password` (string).
- The backplane reads them lazily on first op invocation per target;
  credentials are then cached in the connector's per-target dict for the
  lifetime of the process.
- **No 401-retry**: HTTP Basic credentials are stateless server-side
  (no session to expire or revoke). A 401 response propagates directly to
  the caller — it signals wrong credentials, not an expired session. Unlike
  NSX, there is no re-login loop.
- **SSO realm override**: if your VCF deployment uses a custom SSO domain
  (not `vsphere.local`), set `sso_realm` on the target row. The connector
  reads it and formats the Basic auth header as `username@sso_realm`.
- **Credential rotation**: update the Vault secret and restart the backplane
  (the in-process credential cache clears on `aclose`). There is no per-target
  credential refresh hook in v0.2.

To register a new target, write a descriptor and import it. `meho targets
import` takes a `targets.yaml` **file** (there is no `meho targets create`
verb in v0.2 — `import` is the CLI's only write path):

```yaml
# rdc-sddc-manager.yaml
targets:
  - name: rdc-sddc-manager
    product: sddc
    host: sddc-manager.rdc.evoila.io
    port: 443
    secret_ref: kv/data/sddc-manager/rdc
    auth_model: shared_service_account
```

```bash
meho targets import rdc-sddc-manager.yaml   # add --update to PATCH an existing target
```

If your VCF deployment uses a custom SSO realm, add `sso_realm` to the same
entry — it is not a typed column, so it spills into the target's `extras`
(the connector reads it from there and formats the Basic auth header as
`username@sso_realm`):

```yaml
# rdc-sddc-manager.yaml
targets:
  - name: rdc-sddc-manager
    product: sddc
    host: sddc-manager.rdc.evoila.io
    port: 443
    secret_ref: kv/data/sddc-manager/rdc
    auth_model: shared_service_account
    sso_realm: vsphere.custom.domain
```

**Self-signed / internal-CA appliance.** A nested-lab or freshly-deployed
SDDC Manager commonly presents a self-signed cert, which otherwise fails the
probe and dispatch with `connector_tls_verify_failed`. Add a per-target
TLS-trust field to the same entry — prefer pinning the appliance CA
(verification stays on); use `verify_tls: false` only as an audited last
resort (the two are mutually exclusive — see the
[per-target TLS-trust guide](../../deploy/values-examples/README.md)):

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
meho targets probe rdc-sddc-manager --json | jq '{product, version, reachable}'
# expected: {"product": "sddc", "version": "9.0", "reachable": true}
```

## Quick-start

```bash
# VCF release + BOM
meho sddc-manager about --target rdc-sddc-manager

# SDDC Manager appliance inventory
meho sddc-manager manager list --target rdc-sddc-manager

# All workload domains
meho sddc-manager domain list --target rdc-sddc-manager

# Per-domain detail (vCenters, NSX cluster, clusters, SSO)
meho sddc-manager domain info domain-mgmt --target rdc-sddc-manager

# Cluster listing (all domains)
meho sddc-manager cluster list --target rdc-sddc-manager

# Cluster listing scoped to one domain
meho sddc-manager cluster list --domain domain-wld01 --target rdc-sddc-manager

# Host inventory (all domains)
meho sddc-manager host list --target rdc-sddc-manager

# Host inventory scoped to one cluster
meho sddc-manager host list --cluster cluster-mgmt-01 --target rdc-sddc-manager

# Network pools
meho sddc-manager network-pool list --target rdc-sddc-manager

# LCM bundle listing
meho sddc-manager bundle list --target rdc-sddc-manager

# VCF workflow task listing (all statuses)
meho sddc-manager workflow list --target rdc-sddc-manager

# VCF workflow tasks filtered by status
meho sddc-manager workflow list --status In_Progress --target rdc-sddc-manager

# Machine-readable output for any verb
meho sddc-manager host list --target rdc-sddc-manager --json | jq '.result.elements[] | .fqdn'

# Escape hatch: run any sddc-rest-9.0 op by op_id
meho sddc-manager operation call GET:/v1/domains --target rdc-sddc-manager
meho sddc-manager operation search "host inventory"
```

## Verb reference

### `meho sddc-manager about`

Dispatches `GET:/v1/releases/system` against `connector_id="sddc-rest-9.0"`.
Human output: `version`, `releaseDate`, `description`, and a BOM table of
`componentType` / `componentVersion` rows.

```text
$ meho sddc-manager about --target rdc-sddc-manager
sddc-rest-9.0 — VCF 9.0.0.0-24000000 (2026-01-15)
  VMware Cloud Foundation 9.0
  BOM:
    VCENTER  8.0.3
    NSX      4.2.1
    ESXI     8.0.3
```

### `meho sddc-manager manager list`

Dispatches `GET:/v1/sddc-managers`. Renders `id` / `fqdn` / `version` /
`management_domain` from the `elements[]` pagination envelope. Production
VCF deployments have exactly one appliance per management domain.

### `meho sddc-manager domain list`

Dispatches `GET:/v1/domains`. Renders `id` / `name` / `type`
(`MANAGEMENT` or `WORKLOAD`). The management domain is always present;
each `meho sddc-manager domain deploy` (out of scope for v0.2) adds a
workload domain row.

### `meho sddc-manager domain info <id>`

Dispatches `GET:/v1/domains/{id}` with `<id>` substituted as the path
parameter. Renders vCenters, NSX cluster VIP, clusters, and SSO realm for
the named domain.

```bash
meho sddc-manager domain info domain-mgmt --target rdc-sddc-manager
meho sddc-manager domain info domain-wld01 --target rdc-sddc-manager
```

### `meho sddc-manager cluster list [--domain <id>]`

Dispatches `GET:/v1/clusters`. Without `--domain`, lists every cluster
across all domains. With `--domain <domain-id>`, passes `domainId` as a
query param to scope the response. Renders `id` / `name` /
`primaryDatastoreType` / `domainId`.

### `meho sddc-manager host list [--domain <id>] [--cluster <id>]`

Dispatches `GET:/v1/hosts`. Optional `--domain` and `--cluster` flags
filter the listing. Both flags can be combined. Renders `id` / `fqdn` /
`esxiVersion` / `status`. The host list is the largest SDDC Manager read
surface in production (dozens to hundreds of rows); use `--json | jq` to
filter by cluster or status.

### `meho sddc-manager network-pool list`

Dispatches `GET:/v1/network-pools`. Renders `id` / `name` for each pool.
Network pools are referenced in cluster expand and workload domain deploy
workflows — this listing is the prerequisite lookup.

### `meho sddc-manager bundle list`

Dispatches `GET:/v1/bundles`. Renders `id` / `version` / `compliant`
(yes/no) / `applicable` / `description`. The `compliant=no` entries are
available updates; `applicable=yes` means the bundle applies to the
current VCF version.

### `meho sddc-manager workflow list [--status <state>]`

Dispatches `GET:/v1/tasks`. Optional `--status` filters by task status.
Valid values: `Successful`, `Failed`, `In_Progress`, `Pending`, `Cancelled`.
Renders `id` / `status` / `name` / `type`. Use this after a cluster expand
or domain deploy to track async task progress.

```bash
# All tasks
meho sddc-manager workflow list --target rdc-sddc-manager

# Only in-flight tasks
meho sddc-manager workflow list --status In_Progress --target rdc-sddc-manager

# Failed tasks for post-mortem
meho sddc-manager workflow list --status Failed --target rdc-sddc-manager --json
```

### `meho sddc-manager operation search` / `meho sddc-manager operation call`

Meta-tool wrappers pre-scoped to `sddc-rest-9.0`. `search` runs the hybrid
BM25+cosine search across sddc-rest-9.0 op descriptors; `call` dispatches
any op_id directly. Use these for ops not yet promoted to dedicated CLI
aliases.

```bash
meho sddc-manager operation search "host inventory" --group sddc-hosts
meho sddc-manager operation call GET:/v1/domains --target rdc-sddc-manager
meho sddc-manager operation call GET:/v1/domains/{id} \
  --target rdc-sddc-manager --params '{"id": "domain-wld01"}'
```

## Audit and broadcast classification

Every SDDC Manager v0.2 op is `safety_level=safe`, `requires_approval=false` —
the read-only surface never mutates state. The audit row carries:
`method='DISPATCH'`, `path=<op_id>`, `target_id=<uuid>`,
`payload={"op_id": ..., "params_hash": ..., "source_kind": "ingested",
"connector_product": "sddc", "connector_version": "9.0",
"connector_impl_id": "sddc-rest", "result_status": "ok"|"error"}`.

Broadcast events publish per-tenant on every successful dispatch.
`meho audit query --connector sddc-rest-9.0` retrieves the full dispatch
history; see [`audit-query.md`](./audit-query.md) for filter syntax.

## Migrating off `scripts/sddc-manager.sh`

The consumer's
[`scripts/sddc-manager.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/sddc-manager.sh)
drives SDDC Manager REST via a `curl` + HTTP Basic wrapper. The
`meho sddc-manager` verbs replace it for the read-only workflows; write
workflows stay in the wrapper.

Active tickets using `sddc-manager.sh` (from consumer-needs.md):

| Consumer workflow | `sddc-manager.sh` invocation | `meho sddc-manager …` replacement | Notes |
| --- | --- | --- | --- |
| VCF release + BOM | `sddc-manager.sh about` | `meho sddc-manager about --target rdc-sddc-manager` | |
| Appliance inventory | `sddc-manager.sh managers` | `meho sddc-manager manager list --target rdc-sddc-manager` | |
| Domain listing | `sddc-manager.sh domains` | `meho sddc-manager domain list --target rdc-sddc-manager` | |
| Domain detail | `sddc-manager.sh domain-info <id>` | `meho sddc-manager domain info <id> --target rdc-sddc-manager` | |
| Cluster listing | `sddc-manager.sh clusters [domain]` | `meho sddc-manager cluster list --target rdc-sddc-manager [--domain <id>]` | |
| Host inventory | `sddc-manager.sh hosts [domain]` | `meho sddc-manager host list --target rdc-sddc-manager [--domain <id>]` | JSON: `--json \| jq '.result.elements[] \| .fqdn'` |
| Network pools | `sddc-manager.sh network-pools` | `meho sddc-manager network-pool list --target rdc-sddc-manager` | |
| LCM bundles | `sddc-manager.sh bundles` | `meho sddc-manager bundle list --target rdc-sddc-manager` | |
| Workflow tasks | `sddc-manager.sh tasks [status]` | `meho sddc-manager workflow list --target rdc-sddc-manager [--status <state>]` | |

What `scripts/sddc-manager.sh` did that `meho sddc-manager` deliberately
does **not** do (out of scope for v0.2 — keep the wrapper for these until a
future Initiative lands them):

- **Write / mutate ops** — workload domain create/expand, cluster expand,
  host commission/decommission, bundle download. v0.2 is read-only; write ops
  land in v0.2.next pending policy + approval workflow.
- **Custom query parameters** — `sddc-manager.sh` passes raw query strings;
  the v0.2 CLI exposes only the parameters each op formally declares in its
  descriptor. Use `meho sddc-manager operation call <op_id> --params
  '{"pageNumber": 2, "pageSize": 50}'` for custom pagination.
- **Non-curated API paths** — `sddc-manager.sh` can hit arbitrary VCF API
  paths; `meho sddc-manager` exposes only the 9 curated core ops. Use
  `meho sddc-manager operation call GET:<path>` as an escape hatch for
  one-off queries.

Migration discipline: run the `meho sddc-manager` form alongside
`sddc-manager.sh` for an overlap window, diff the outputs, then retire the
wrapper call site. The MEHO path adds the full audit row + broadcast event
the bash pattern never had — that audit coverage is the point of migrating.

### Per-ticket wrapper-flip recipe

For each active SDDC Manager consumer ticket, apply this pattern:

1. **Identify the wrapper invocation** in the ticket's reproduction steps
   (usually `bash scripts/sddc-manager.sh <command>`).
2. **Find the `meho sddc-manager` equivalent** from the table above.
3. **Validate output parity**: run both side-by-side against `rdc-sddc-manager`.
   Use `--json | jq` on the `meho sddc-manager` side to pull the same fields
   the bash script was parsing.
4. **Update the ticket steps**: replace the `sddc-manager.sh` invocation with
   the `meho sddc-manager` form. Capture the `--json` output for the ticket's
   evidence block.
5. **Retire the wrapper call site** in the ticket's automation scripts once
   the MEHO form is validated.

Example — host listing ticket:

```bash
# Before (wrapper):
bash scripts/sddc-manager.sh hosts rdc-sddc-manager | jq '.[].fqdn'

# After (meho sddc-manager):
meho sddc-manager host list --target rdc-sddc-manager --json \
  | jq '.result.elements[].fqdn'

# Verify parity:
diff \
  <(bash scripts/sddc-manager.sh hosts rdc-sddc-manager | jq -S '.[].fqdn') \
  <(meho sddc-manager host list --target rdc-sddc-manager --json \
      | jq -S '.result.elements[].fqdn')
# expected: empty diff
```

Example — workflow task listing (in-flight only):

```bash
# Before:
bash scripts/sddc-manager.sh tasks In_Progress rdc-sddc-manager

# After:
meho sddc-manager workflow list --status In_Progress --target rdc-sddc-manager

# With --json for automation:
meho sddc-manager workflow list --status In_Progress --target rdc-sddc-manager \
  --json | jq '.result.elements[] | {id, name, status}'
```

Example — domain info:

```bash
# Before:
bash scripts/sddc-manager.sh domain-info domain-wld01 rdc-sddc-manager

# After:
meho sddc-manager domain info domain-wld01 --target rdc-sddc-manager

# JSON for scripting:
meho sddc-manager domain info domain-wld01 --target rdc-sddc-manager --json \
  | jq '.result | {id, name, type, vcenters: [.vcenters[].fqdn]}'
```

## Goal #214 G3.5 SDDC Manager checklist

| Checklist item | Status |
| --- | --- |
| G3.5-T4 #616 — `SddcManagerConnector` skeleton + HTTP Basic auth + fingerprint + probe | ✅ merged |
| G3.5-T5 #617 — spec ingestion + 9 core ops + `apply_sddc_core_curation` + acceptance tests | ✅ merged |
| G3.5-T6 #618 — CLI verbs + recorded-fixture E2E + this doc | ✅ merged |
| MCP `llm_instructions` / `when_to_use` reviewed for agent legibility | ✅ done (part of T5 curation blobs) |
| All 9 core ops dispatch → `status='ok'` in CI | ✅ `test_connectors_sddc_manager_e2e.py` |
| Audit rows carry `op_id` + `target_id` + `params_hash` | ✅ `test_connectors_sddc_manager_e2e.py` |
| HTTP Basic credential cache path tested in CI | ✅ `test_connectors_sddc_manager_e2e.py` |
| JSONFlux handle path tested in CI | ✅ `test_connectors_sddc_manager_e2e.py` |
| `docs/cross-repo/sddc-manager-onboarding.md` with wrapper-flip recipe | ✅ this document |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired. | `meho login <url>` again. |
| `status=error connector_error: RuntimeError … returned HTTP 401` | SDDC Manager credentials invalid. HTTP Basic 401 is terminal — no re-login path. | Verify the Vault secret at `target.secret_ref`; check the VCF audit log for the service account; confirm `username@sso_realm` format matches your SSO domain. |
| `status=error … unknown_op` | The 9 core ops are not registered/enabled. | Re-run `apply_sddc_core_curation` against the SDDC Manager connector. |
| `status=denied` | `read_only` role, or a tenant policy denied the dispatch. | Use an `operator`-role token. |
| `about` / any verb times out | SDDC Manager unreachable from the backplane host. | Verify the FQDN/IP in `target.host` resolves from the backplane; check firewall rules (port 443 from backplane → SDDC Manager). |
| `probe` fails with TLS error | Self-signed or expired certificate, or wrong CA bundle. | Mount the correct CA bundle in the backplane container and set `MEHO_TLS_CA_BUNDLE`; or configure the SDDC Manager appliance with a CA-signed cert. |
| `domain info` returns 404 | `<id>` doesn't match an existing VCF domain. | List available domains first: `meho sddc-manager domain list --target rdc-sddc-manager`. |
| `workflow list` returns empty `elements` | No tasks in the requested status, or tasks older than the SDDC Manager retention window. | Try without `--status` to see all recent tasks. |
| Credential cache stale after rotation | In-process cache retains old credentials. | Restart the backplane so `aclose()` clears the `_creds_cache` dict. |
| `username` field missing from Vault secret | Connector validates both `username` and `password` keys on credential load. | Add the `username` field to the Vault secret at `target.secret_ref`. |

## References

- Initiative: [#368 G3.5 connector batch](https://github.com/evoila/meho/issues/368); Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface: [#616](https://github.com/evoila/meho/issues/616) (T4 skeleton + auth), [#617](https://github.com/evoila/meho/issues/617) (T5 core ops + curation), [#618](https://github.com/evoila/meho/issues/618) (T6 CLI verbs + E2E + this doc).
- Engineering companion: [`docs/codebase/connectors-sddc-manager.md`](../codebase/connectors-sddc-manager.md).
- Connector source: [`backend/src/meho_backplane/connectors/sddc_manager/`](../../backend/src/meho_backplane/connectors/sddc_manager/).
- CLI verbs: [`cli/internal/cmd/sddc-manager/`](../../cli/internal/cmd/sddc-manager/).
- E2E integration test: [`backend/tests/test_connectors_sddc_manager_e2e.py`](../../backend/tests/test_connectors_sddc_manager_e2e.py).
- Acceptance tests: [`backend/tests/acceptance/test_g35_sddc_dispatch_smoke.py`](../../backend/tests/acceptance/test_g35_sddc_dispatch_smoke.py), [`backend/tests/acceptance/test_g35_sddc_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_g35_sddc_jsonflux_force_handle.py).
- Consumer wrapper retiring: [`scripts/sddc-manager.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/sddc-manager.sh).
- VCF API reference: <https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/>.
- Related onboarding docs: [`nsx-onboarding.md`](./nsx-onboarding.md), [`vault-onboarding.md`](./vault-onboarding.md), [`audit-query.md`](./audit-query.md), [`broadcast-onboarding.md`](./broadcast-onboarding.md).
