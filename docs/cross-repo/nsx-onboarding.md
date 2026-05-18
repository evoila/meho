<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# NSX op surface onboarding — operator recipe

> Operator-facing recipe for the G3.5 `nsx-rest-4.2` op surface — the
> `meho nsx …` verb tree, the agent meta-tool path, and the migration
> off the consumer's `scripts/nsx.sh` wrapper. The op handlers live in
> [`backend/src/meho_backplane/connectors/nsx/`](../../backend/src/meho_backplane/connectors/nsx/);
> the engineering-facing companion is
> [`docs/codebase/connectors-nsx.md`](../codebase/connectors-nsx.md).
> This doc is the cookbook every RDC operator reads when retiring
> `scripts/nsx.sh` in favour of `meho nsx …`.

## What this surface is

The `nsx-rest-4.2` connector is an **ingested** connector: the 9 curated
read-only ops are stored as `EndpointDescriptor` rows seeded from
[`NSX_CORE_OPS`](../../backend/src/meho_backplane/connectors/nsx/core_ops.py)
and dispatched through `HttpConnector._request_json` by the G0.6
`dispatch_ingested` branch. The connector registers under the
`(product="nsx", version="4.2", impl_id="nsx-rest")` registry triple —
the connector id `nsx-rest-4.2`. Auth is session-cookie + `X-XSRF-TOKEN`
(not HTTP Basic); the connector handles this transparently via
`NsxConnector.auth_headers`.

NSX has no public CI simulator (#536 proved simulators cannot serve
vendor REST). Integration coverage uses a recorded-fixture record/replay
pattern against captured NSX responses. This is a known, documented
limitation per Initiative #368's DoD.

The v0.2 op surface (Initiative
[#368](https://github.com/evoila/meho/issues/368)) is the **read**
working set the consumer's `scripts/nsx.sh` exercises daily — write
ops stay in the wrapper until v0.2.next ships policy + approval flow:

| Group | CLI verb | `op_id` | Path |
| --- | --- | --- | --- |
| nsx-identity | `meho nsx about` | `GET:/api/v1/node` | Manager identity + version |
| nsx-inventory | `meho nsx node list` | `GET:/api/v1/transport-nodes` | Transport-node inventory |
| nsx-cluster | `meho nsx cluster status` | `GET:/api/v1/cluster/status` | Management + control cluster health |
| nsx-segments | `meho nsx segment list` | `GET:/policy/api/v1/infra/segments` | Policy-API overlay/VLAN segment listing |
| nsx-transport-zones | `meho nsx transport-zone list` | `GET:/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones` | Transport-zone listing |
| nsx-routing | `meho nsx tier0 list` | `GET:/policy/api/v1/infra/tier-0s` | Provider-edge Tier-0 router listing |
| nsx-routing | `meho nsx tier1 list` | `GET:/policy/api/v1/infra/tier-1s` | Tenant Tier-1 router listing |
| nsx-policy-firewall | `meho nsx firewall policy list [--scope <domain>]` | `GET:/policy/api/v1/infra/domains/{domain-id}/security-policies` | Security policy listing per domain |
| nsx-policy-firewall | `meho nsx firewall rule list <policy-id> [--scope <domain>]` | `GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules` | Per-policy rule listing |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6. The
CLI verb tree is operator ergonomics over that one route; it is **not**
a separate data path and is **not** mirrored on the MCP surface
(CLAUDE.md postulate 5).

## Prerequisites

- **A reachable NSX Manager** (standalone NSX-T or behind the VCF 9
  envoy proxy). The connector derives the base URL from `target.host` +
  `target.port`; both plain NSX-T and VCF-proxied addresses work.
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`.
  The username/password pair is form-encoded to `POST /api/session/create`
  at the start of every new per-target session; the resulting
  `JSESSIONID` cookie + `X-XSRF-TOKEN` are cached per-target and
  re-used until a 401 forces a re-login.
- **A registered NSX target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-nsx`). The target carries `product="nsx"`,
  `host` (the NSX Manager FQDN — no `https://`), `port` (default 443),
  `secret_ref` (the Vault path to the credentials), and
  `auth_model="shared_service_account"`.
- **The 9 curated ops registered + enabled.** Run the G3.5-T2 curation
  step (`apply_nsx_core_curation`) once per NSX target after the G0.7
  spec ingest. See [`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md)
  for the end-to-end canary procedure.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. `meho nsx …` requires `operator` role
  minimum (same gate as every dispatch verb).

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** —
the Vault-sourced credentials are used regardless of which operator
invokes the verb. Per-operator impersonation is out of scope for v0.2.

What this means for the credentials in Vault:

- Vault path: `target.secret_ref` (e.g. `kv/data/nsx/<slug>`).
- Required fields: `username` (string), `password` (string).
- The backplane reads them lazily on first op invocation per target; the
  session cookie + XSRF token are then cached in the connector's
  per-target httpx client until a 401 triggers one re-login.
- **Session expiry**: NSX 4.x default session idle timeout is 30 minutes.
  The connector handles expiry transparently: a 401 on any downstream
  GET invalidates the cached token, re-POSTs to `/api/session/create`,
  and retries the request once. A second consecutive 401 raises
  `RuntimeError` (bad credentials) — operator action required.
- **Credential rotation**: update the Vault secret, then restart the
  backplane (or wait for the next session expiry) so the connector
  reloads. The in-process session cache is the only persistent state;
  there is no per-target credential refresh hook in v0.2.

To register a new target via the CLI:

```bash
meho targets import \
  --name rdc-nsx \
  --product nsx \
  --host nsx-manager.rdc.evoila.io \
  --port 443 \
  --secret-ref kv/data/nsx/rdc-nsx \
  --auth-model shared_service_account
```

Verify the fingerprint resolved correctly:

```bash
meho targets probe --name rdc-nsx --json | jq '{product, version, reachable}'
# expected: {"product": "nsx", "version": "4.2", "reachable": true}
```

## Quick-start

```bash
# Identity + version
meho nsx about --target rdc-nsx

# Transport-node inventory
meho nsx node list --target rdc-nsx

# Cluster health
meho nsx cluster status --target rdc-nsx

# Overlay segments
meho nsx segment list --target rdc-nsx

# Transport zones
meho nsx transport-zone list --target rdc-nsx

# Routing topology
meho nsx tier0 list --target rdc-nsx
meho nsx tier1 list --target rdc-nsx

# Distributed firewall — policies + rules
meho nsx firewall policy list --target rdc-nsx
meho nsx firewall rule list policy-app-tier --target rdc-nsx

# Firewall in a non-default domain
meho nsx firewall policy list --scope my-domain --target rdc-nsx

# Machine-readable output for any verb
meho nsx segment list --target rdc-nsx --json | jq '.result.results[] | .id'

# Escape hatch: run any nsx-rest-4.2 op by op_id
meho nsx operation call GET:/api/v1/node --target rdc-nsx
meho nsx operation search "firewall rules" --target rdc-nsx
```

## Verb reference

### `meho nsx about`

Dispatches `GET:/api/v1/node` against `connector_id="nsx-rest-4.2"`.
Human output: `node_version`, `kernel_version`, `hostname`, `node_uuid`.

```text
$ meho nsx about --target rdc-nsx
nsx-rest-4.2 — node_version=4.2.1.0.0 (kernel 4.2.1.0.0 build) @ nsxmgr-rdc
  node_uuid: deadbeef-1111-2222-3333-cafebabecafe
  hostname:  nsxmgr-rdc.evoila.io
```

### `meho nsx node list`

Dispatches `GET:/api/v1/transport-nodes`. Renders `id` / `display_name`
and `resource_type` from `node_deployment_info`. The list covers all
transport nodes registered on the manager (ESXi hosts, bare-metal
endpoints, edge nodes).

### `meho nsx cluster status`

Dispatches `GET:/api/v1/cluster/status`. Renders management-cluster
and control-cluster aggregate status. A healthy deployment shows
`STABLE` / `STABLE`; a degraded member shows the member UUID in the
detail list.

### `meho nsx segment list`

Dispatches `GET:/policy/api/v1/infra/segments`. Renders `id` /
`display_name` / `transport_zone_path`. This is the broadest list op —
production deployments often have hundreds of segments. Use `--json |
jq` to filter by transport zone path or subnet CIDR.

### `meho nsx transport-zone list`

Dispatches the long enforcement-point-qualified path. Renders `id` /
`display_name` / `tz_type` (`OVERLAY` or `VLAN`).

### `meho nsx tier0 list` / `meho nsx tier1 list`

Dispatch `GET:/policy/api/v1/infra/tier-0s` and
`GET:/policy/api/v1/infra/tier-1s` respectively. Render `id` /
`display_name` / `ha_mode` (Tier-0) or `tier0_path` (Tier-1). Tier-0s
are provider-owned; Tier-1s are tenant-owned and attach to a Tier-0.

### `meho nsx firewall policy list`

Dispatches `GET:/policy/api/v1/infra/domains/{domain-id}/security-policies`
with `--scope` (default `"default"`) substituted as `domain-id`. Renders
`id` / `display_name` / `category`. Most environments use the `default`
domain; multi-domain setups pass `--scope <domain-id>`.

### `meho nsx firewall rule list <policy-id>`

Dispatches the per-policy rules path with `<policy-id>` substituted as
`security-policy-id` and `--scope` (default `"default"`) as `domain-id`.
Renders `id` / `display_name` / `action` (`ALLOW` / `DROP` / `REJECT`).

### `meho nsx operation search` / `meho nsx operation call`

Meta-tool wrappers pre-scoped to `nsx-rest-4.2`. `search` runs the
hybrid BM25+cosine search across nsx-rest-4.2 op descriptors; `call`
dispatches any op_id directly. Use these for ops not yet promoted to
dedicated CLI aliases.

```bash
meho nsx operation search "cluster status" --group nsx-cluster
meho nsx operation call GET:/api/v1/node --target rdc-nsx
```

## Audit and broadcast classification

Every NSX v0.2 op is `safety_level=safe`, `requires_approval=false` —
the read-only surface never mutates state. The audit row carries:
`method='DISPATCH'`, `path=<op_id>`, `target_id=<uuid>`,
`payload={"op_id": ..., "params_hash": ..., "source_kind": "ingested",
"connector_product": "nsx", "connector_version": "4.2",
"connector_impl_id": "nsx-rest", "result_status": "ok"|"error"}`.

Broadcast events publish per-tenant on every successful dispatch.
`meho audit query --connector nsx-rest-4.2` retrieves the full
dispatch history; see [`audit-query.md`](./audit-query.md) for filter
syntax.

## Migrating off `scripts/nsx.sh`

The consumer's
[`scripts/nsx.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/nsx.sh)
drives NSX REST via a `curl` + session-cookie wrapper. The `meho nsx`
verbs replace it for the read-only workflows; write workflows stay in
the wrapper.

Active tickets using `nsx.sh` (from consumer-needs.md L85):

| Consumer workflow | `nsx.sh` invocation | `meho nsx …` replacement | Notes |
| --- | --- | --- | --- |
| Node identity + version | `nsx.sh about` | `meho nsx about --target rdc-nsx` | |
| Transport-node inventory | `nsx.sh nodes` | `meho nsx node list --target rdc-nsx` | |
| Cluster health check | `nsx.sh cluster status` | `meho nsx cluster status --target rdc-nsx` | |
| Segment listing | `nsx.sh segments` | `meho nsx segment list --target rdc-nsx` | JSON output: `--json \| jq '.result.results[] \| .id'` |
| Transport-zone listing | `nsx.sh transport-zones` | `meho nsx transport-zone list --target rdc-nsx` | |
| Tier-0 edge listing | `nsx.sh tier0s` | `meho nsx tier0 list --target rdc-nsx` | |
| Tenant router listing | `nsx.sh tier1s` | `meho nsx tier1 list --target rdc-nsx` | |
| FW policy listing | `nsx.sh fw-policies [domain]` | `meho nsx firewall policy list --target rdc-nsx [--scope <domain>]` | Default domain `"default"` if `--scope` omitted |
| FW rule listing | `nsx.sh fw-rules <policy>` | `meho nsx firewall rule list <policy-id> --target rdc-nsx` | |

What `scripts/nsx.sh` did that `meho nsx` deliberately does **not** do
(out of scope for v0.2 — keep the wrapper for these until a future
Initiative lands them):

- **Write / mutate ops** — segment create/update/delete, FW rule create,
  transport-node configuration. v0.2 is read-only; write ops land in
  v0.2.next pending policy + approval workflow.
- **Custom query parameters** — `nsx.sh` passes raw query strings;
  the v0.2 CLI exposes only the parameters each op formally declares
  in its descriptor. Use `meho nsx operation call <op_id> --params
  '{"cursor": "...", "page_size": 100}'` for custom pagination.
- **Non-policy API paths** — `nsx.sh` can hit arbitrary `/api/v1/…`
  paths; `meho nsx` exposes only the 9 curated core ops. Use
  `meho nsx operation call GET:<path>` as an escape hatch for one-off
  queries.

Migration discipline: run the `meho nsx` form alongside `nsx.sh` for
an overlap window, diff the outputs, then retire the wrapper call site.
The MEHO path adds the full audit row + broadcast event the bash
pattern never had — that audit coverage is the point of migrating.

### Per-ticket wrapper-flip recipe

For each active NSX consumer ticket, apply this pattern:

1. **Identify the wrapper invocation** in the ticket's reproduction
   steps (usually `bash scripts/nsx.sh <command>`).
2. **Find the `meho nsx` equivalent** from the table above.
3. **Validate output parity**: run both side-by-side against `rdc-nsx`.
   Use `--json | jq` on the `meho nsx` side to pull the same fields
   the bash script was parsing.
4. **Update the ticket steps**: replace the `nsx.sh` invocation with
   the `meho nsx` form. Capture the `--json` output for the ticket's
   evidence block.
5. **Retire the wrapper call site** in the ticket's automation scripts
   once the MEHO form is validated.

Example — segment listing ticket:

```bash
# Before (wrapper):
bash scripts/nsx.sh segments rdc-nsx | jq '.[].id'

# After (meho nsx):
meho nsx segment list --target rdc-nsx --json | jq '.result.results[].id'

# Verify parity:
diff \
  <(bash scripts/nsx.sh segments rdc-nsx | jq -S '.[].id') \
  <(meho nsx segment list --target rdc-nsx --json | jq -S '.result.results[].id')
# expected: empty diff
```

Example — firewall rules:

```bash
# Before:
bash scripts/nsx.sh fw-rules policy-app-tier rdc-nsx

# After:
meho nsx firewall rule list policy-app-tier --target rdc-nsx

# With --scope for non-default domain:
meho nsx firewall rule list policy-app-tier --target rdc-nsx --scope my-domain
```

## Goal #214 G3.5 NSX checklist

| Checklist item | Status |
| --- | --- |
| G3.5-T1 #613 — `NsxConnector` skeleton + session/XSRF auth | ✅ merged |
| G3.5-T2 #614 — 9 core ops + `apply_nsx_core_curation` + acceptance tests | ✅ merged |
| G3.5-T3 #615 — CLI verbs + E2E recorded-fixture tests + this doc | ✅ merged |
| MCP `llm_instructions` / `when_to_use` reviewed for agent legibility | ✅ done (part of T2 curation blobs) |
| All 9 core ops dispatch → `status='ok'` in CI | ✅ `test_connectors_nsx_e2e.py` |
| Audit rows carry `op_id` + `target_id` + `params_hash` | ✅ `test_connectors_nsx_e2e.py` |
| 401-retry path tested in CI | ✅ `test_connectors_nsx_e2e.py` |
| JSONFlux handle path tested in CI | ✅ `test_connectors_nsx_e2e.py` |
| `docs/cross-repo/nsx-onboarding.md` with wrapper-flip recipe | ✅ this document |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired. | `meho login <url>` again. |
| `status=error connector_error: RuntimeError … returned HTTP 401` | NSX credentials invalid or account locked. | Verify the Vault secret at `target.secret_ref`; check NSX audit log for the service account. |
| `status=error connector_error: RuntimeError … 401 after refresh` | Re-login also 401'd — credentials consistently rejected. | Update the Vault secret; restart the backplane to flush the in-process session cache. |
| `status=error … unknown_op` | The 9 core ops are not registered/enabled. | Re-run `apply_nsx_core_curation` against the NSX connector; see [`g35-nsx-canary.md`](./g35-nsx-canary.md). |
| `status=denied` | `read_only` role, or a tenant policy denied the dispatch. | Use an `operator`-role token. |
| `about` / any verb times out | NSX Manager unreachable from the backplane host. | Verify the FQDN/IP in `target.host` resolves from the backplane; check firewall rules (port 443 from backplane → NSX Manager). |
| `probe` fails with TLS error | Self-signed or expired certificate, or wrong CA bundle. | Mount the correct CA bundle in the backplane container and set `MEHO_TLS_CA_BUNDLE`; or configure NSX Manager with a CA-signed cert. |
| Firewall policy/rule list returns 404 | `--scope` value doesn't match an existing NSX policy domain. | List available domains via `meho nsx operation call GET:/policy/api/v1/infra/domains --target rdc-nsx`. Default domain is always `"default"`. |
| `meho nsx segment list` returns empty `results` | No segments in the default policy domain, or the policy API mount isn't reachable. | Verify segments exist via NSX Manager UI; confirm the NSX Manager version is 4.x (policy API is 4.0+). |

## References

- Initiative: [#368 G3.5 NSX REST 4.2 op surface](https://github.com/evoila/meho/issues/368); Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface: [#613](https://github.com/evoila/meho/issues/613) (T1 skeleton + auth), [#614](https://github.com/evoila/meho/issues/614) (T2 core ops + curation), [#615](https://github.com/evoila/meho/issues/615) (T3 CLI verbs + E2E + this doc).
- Engineering companion: [`docs/codebase/connectors-nsx.md`](../codebase/connectors-nsx.md).
- Canary procedure (G0.7 spec ingest → curation): [`docs/cross-repo/g35-nsx-canary.md`](./g35-nsx-canary.md).
- Connector source: [`backend/src/meho_backplane/connectors/nsx/`](../../backend/src/meho_backplane/connectors/nsx/).
- CLI verbs: [`cli/internal/cmd/nsx/`](../../cli/internal/cmd/nsx/).
- E2E integration test: [`backend/tests/test_connectors_nsx_e2e.py`](../../backend/tests/test_connectors_nsx_e2e.py).
- Acceptance tests: [`backend/tests/acceptance/test_g35_nsx_dispatch_smoke.py`](../../backend/tests/acceptance/test_g35_nsx_dispatch_smoke.py), [`backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py`](../../backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py).
- Consumer wrapper retiring: [`scripts/nsx.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/nsx.sh).
- NSX REST API reference: <https://developer.broadcom.com/xapis/nsx-data-center-rest-api/latest/>.
- Related onboarding docs: [`vault-onboarding.md`](./vault-onboarding.md), [`audit-query.md`](./audit-query.md), [`broadcast-onboarding.md`](./broadcast-onboarding.md).
