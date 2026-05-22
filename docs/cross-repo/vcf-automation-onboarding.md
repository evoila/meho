<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# VCF Automation op surface onboarding — operator recipe

> Operator-facing recipe for the G3.6 `vcfa-rest-9.0` op surface — the
> `meho vcf-automation …` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/vcf-automation.sh` wrapper.
> The op handlers live in
> [`backend/src/meho_backplane/connectors/vcf_automation/`](../../backend/src/meho_backplane/connectors/vcf_automation/);
> the canary procedure that ingests + curates the dual-plane op set
> lives in [`g36-vcfa-canary.md`](./g36-vcfa-canary.md).
> This doc is the cookbook every RDC operator reads when retiring
> `scripts/vcf-automation.sh` in favour of `meho vcf-automation …`.

## What this surface is

The `vcfa-rest-9.0` connector is an **ingested, dual-plane** connector.
The 11 curated read-only ops are stored as `EndpointDescriptor` rows
seeded from
[`VCFA_CORE_OPS`](../../backend/src/meho_backplane/connectors/vcf_automation/core_ops.py)
and dispatched through `HttpConnector._request_json` by the G0.6
`dispatch_ingested` branch. The connector registers under the
`(product="vcf-automation", version="9.0", impl_id="vcfa-rest")`
registry triple — the connector id `vcfa-rest-9.0`.

VCFA 9.x is structurally different from the other three VCF
management-plane connectors (vROps, vRLI, Fleet) in two load-bearing
ways. Both shaped the CLI surface this doc covers.

### Dual-plane auth (provider + tenant)

VCFA 9.x runs two API planes on a single appliance:

| Plane | Path family | Login flow | Token shape |
| --- | --- | --- | --- |
| **provider** | `/cloudapi/*` + `/api/*` (vCloud-Director lineage) | `POST /cloudapi/1.0.0/sessions/provider` with HTTP Basic | `X-VMWARE-VCLOUD-ACCESS-TOKEN` response **header** (a JWT) |
| **tenant** | `/iaas/api/*` (Aria-IaaS lineage) | `POST /iaas/api/login` with JSON body | `{"token": "…"}` response **body** |

Both planes use `Authorization: Bearer <…>` on subsequent calls; the
provider Accept media type is path-family-dependent
(`application/json;version=9.0.0` for `/cloudapi/*`,
`application/*+json;version=40.0` for the classic `/api/*` family);
the tenant plane uses plain `application/json`. Tokens are bespoke
per plane — the provider JWT does **not** authenticate the tenant
plane and vice versa.

The connector's per-plane token caches and the load-bearing per-plane
401 retry-once dance are documented inline in the connector source
([`connector.py`](../../backend/src/meho_backplane/connectors/vcf_automation/connector.py));
operators don't see any of it from the CLI surface. What operators
**do** see is the `--plane provider|tenant` flag on every verb that
isn't unambiguous from the resource name (today: only `about`).

### Vhost (`Host:`) routing

VCFA enforces strict `Host:` header matching. When the appliance is
reached by IP and the `Host:` header doesn't match the canonical
appliance vhost, **every path returns 404 with an empty body** before
the application sees the request. The consumer wrapper documents
this as the silent-404 failure mode, and the connector raises
`VcfAutomationConfigurationError` at session-establish time when:

- `target.host` parses as an IP literal (IPv4 or IPv6, bracketed or bare), **and**
- `target.fqdn` is unset.

When `target.host` is itself an FQDN, `fqdn` is optional — the URL
host already carries the right vhost. The CLI exposes the override
two ways:

- **`fqdn:` in `targets.yaml`** — the canonical home. The `meho
  targets import` verb knows the field; the value persists on the
  Target row and applies to every dispatch against that target.
- **`--fqdn <vhost>` on the dispatch verb** — a per-call override
  for debugging or for targets whose canonical FQDN drifted. The
  override flows through the dispatch body's `target.fqdn` field;
  the backend mutates the resolved Target **in memory only** for
  the duration of that one dispatch. The DB row is unchanged.

If you're reaching VCFA by FQDN already, neither setting matters.
If you're reaching it by IP and you forget `fqdn:`, the dispatcher
returns a structured `status="error"` envelope whose
`extras.exception_message` names the offending IP and the `fqdn`
field — operators see a clear remediation message rather than a
confusing 404 storm.

## The v0.5 op surface

Initiative
[#369](https://github.com/evoila/meho/issues/369) ships the **read**
working set as 11 curated ops, distributed across both planes:

| Plane | Group | CLI verb | `op_id` |
| --- | --- | --- | --- |
| provider | `provider-site` | `meho vcf-automation about --plane provider` | `GET:/cloudapi/1.0.0/site` |
| provider | `provider-orgs` | `meho vcf-automation org list` | `GET:/cloudapi/1.0.0/orgs` |
| provider | `provider-orgs` | `meho vcf-automation org get <id>` | `GET:/cloudapi/1.0.0/orgs/{id}` |
| provider | `provider-regions` | `meho vcf-automation region list` | `GET:/cloudapi/1.0.0/regions` |
| provider | `provider-regions` | `meho vcf-automation region get <id>` | `GET:/cloudapi/1.0.0/regions/{id}` |
| provider | `provider-users` | `meho vcf-automation user list` | `GET:/cloudapi/1.0.0/users` |
| tenant | `tenant-about` | `meho vcf-automation about --plane tenant` | `GET:/iaas/api/about` |
| tenant | `tenant-projects` | `meho vcf-automation project list` | `GET:/iaas/api/projects` |
| tenant | `tenant-deployments` | `meho vcf-automation deployment list` | `GET:/iaas/api/deployments` |
| tenant | `tenant-deployments` | `meho vcf-automation deployment get <id>` | `GET:/iaas/api/deployments/{id}` |
| tenant | `tenant-blueprints` | `meho vcf-automation blueprint list` | `GET:/iaas/api/blueprints` |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6.
The CLI verb tree is operator ergonomics over that one route; it is
**not** a separate data path and is **not** mirrored on the MCP
surface (CLAUDE.md postulate 5). The MCP agent reaches the same ops
through `search_operations` / `call_operation` against
`connector_id="vcfa-rest-9.0"`; the per-op `llm_instructions` (set
during operator review) carry the plane-aware guidance the agent
inlines into its reasoning context.

## Prerequisites

- **A reachable VCFA 9.x appliance.** The connector derives the
  base URL from `target.host` + `target.port`; **if `host` is an IP
  literal, set `fqdn:` in `targets.yaml`** to the appliance's
  canonical FQDN. Without it the connector refuses to establish a
  session and emits a structured error naming `fqdn`.
- **Service-account credentials in Vault.** The connector reads
  `{"username": …, "password": …}` from Vault at
  `target.secret_ref`. The same credential pair drives both planes
  unless `target.provider_username` /
  `target.provider_secret_ref` are set (the `admin@System` vs
  `svc-meho` split — see "Per-plane credential override" below).
- **A registered VCFA target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-vcfa`). The target carries
  `product="vcf-automation"`, `host`, `port` (default 443), `fqdn`
  (when reached by IP), `secret_ref`, and
  `auth_model="shared_service_account"`. v0.5 supports only
  `shared_service_account`; other auth models surface a clear
  `NotImplementedError` at dispatch time.
- **The 11 curated ops registered + enabled.** Run the G3.6-T11
  curation step (`apply_vcfa_core_curation`) once per VCFA target
  after the G0.7 dual-spec ingest. See
  [`g36-vcfa-canary.md`](./g36-vcfa-canary.md) for the end-to-end
  canary procedure.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. `meho vcf-automation …` requires
  `operator` role minimum (same gate as every dispatch verb).

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** —
the Vault-sourced credentials are used regardless of which operator
invokes the verb. Per-operator impersonation is out of scope for v0.5.

### Standard target (provider + tenant share credentials)

```bash
meho targets import path/to/targets.yaml
```

…with `targets.yaml` containing:

```yaml
targets:
  - name: rdc-vcfa
    product: vcf-automation
    host: 10.5.50.100       # IP host requires fqdn below
    port: 443
    fqdn: vcfa.rdc.evoila.io  # canonical vhost
    secret_ref: kv/data/vcfa/rdc-vcfa
    auth_model: shared_service_account
```

### Per-plane credential override (admin@System vs svc-meho)

When the provider plane needs a distinct service account (the typical
`admin@System` posture documented in the consumer wrapper), add the
override fields:

```yaml
targets:
  - name: rdc-vcfa
    product: vcf-automation
    host: vcfa.rdc.evoila.io
    port: 443
    secret_ref: kv/data/vcfa/rdc-vcfa-tenant      # tenant credentials
    auth_model: shared_service_account
    extras:
      provider_username: admin@System            # provider Basic-auth user
      provider_secret_ref: kv/data/vcfa/rdc-vcfa-admin  # provider credentials
```

(The two override fields live under `extras:` until the Target schema
lands them as first-class columns — tracked under Goal #214.)

Verify the fingerprint resolved both planes:

```bash
meho targets probe --name rdc-vcfa --json \
  | jq '{product, version, reachable, planes: .extras.planes}'
# expected: {"product": "vcf-automation", "version": "9.0",
#            "reachable": true, "planes": ["provider", "tenant"]}
```

## Quick-start

```bash
# Identity per plane (dual-plane verb)
meho vcf-automation about --plane provider --target rdc-vcfa
meho vcf-automation about --plane tenant   --target rdc-vcfa

# Provider plane — appliance-wide inventory
meho vcf-automation org list      --target rdc-vcfa
meho vcf-automation org get a1b2c3 --target rdc-vcfa
meho vcf-automation region list   --target rdc-vcfa
meho vcf-automation region get r1 --target rdc-vcfa
meho vcf-automation user list     --target rdc-vcfa

# Tenant plane — per-tenant workload view
meho vcf-automation project list      --target rdc-vcfa
meho vcf-automation deployment list   --target rdc-vcfa
meho vcf-automation deployment get d1 --target rdc-vcfa
meho vcf-automation blueprint list    --target rdc-vcfa

# Machine-readable output for any verb (the agent / scripting path)
meho vcf-automation deployment list --target rdc-vcfa --json \
  | jq '.result.content[] | {id, name, status}'

# Per-call vhost override (debugging or transient FQDN drift)
meho vcf-automation about --plane provider --target rdc-vcfa \
  --fqdn vcfa.maintenance.evoila.io

# Escape hatch — run any vcfa-rest-9.0 op_id by name
meho vcf-automation operation call GET:/cloudapi/1.0.0/site --target rdc-vcfa
meho vcf-automation operation search "deployment status" --group tenant-deployments
```

## Verb reference

### `meho vcf-automation about --plane provider|tenant`

Dual-plane verb. `--plane` is **required** because the resource name
"about" exists on both planes with different shapes:

- **`--plane provider`** dispatches `GET:/cloudapi/1.0.0/site` and
  renders site identity (`id`, `name`, `restName`, `productVersion`).
- **`--plane tenant`** dispatches `GET:/iaas/api/about` and renders
  IaaS API self-describe (`latestApiVersion`, `supportedApis[]`).

Omitting `--plane` fails fast with an explicit message — no silent
default.

### `meho vcf-automation org list` / `org get <id>`

Provider-plane only. List enumerates every organization on the
appliance (the cross-tenant view the system administrator sees);
`get` returns full detail for one organization id including counts
(`orgVdcCount`, `userCount`, `catalogCount`). For per-tenant project
/ deployment / blueprint reads, switch planes to the tenant verbs.

Passing `--plane tenant` on these verbs errors early with a
"--plane provider expected" message.

### `meho vcf-automation region list` / `region get <id>`

Provider-plane only. Lists VCFA "regions" — the VCFA 9 evolution of
the vCloud-Director provider-VDC concept. Each region maps to one NSX
domain plus a collection of supervisors backed by one or more VCF
workload domains.

### `meho vcf-automation user list`

Provider-plane only. System-scope users (the System organization's
identity entries plus any cross-org provider-scope users). Use when
auditing who has provider-level access; per-tenant users live behind
the per-org user endpoints that are not in the v0.5 read core.

### `meho vcf-automation project list`

Tenant-plane only. Projects are the deployment-scoping construct —
every deployment belongs to exactly one project, and blueprint access
controls reference project membership.

### `meho vcf-automation deployment list` / `deployment get <id>`

Tenant-plane only. The largest payload on the tenant surface; large
tenants return hundreds of deployments. The dispatcher's JSONFlux
seam wraps oversized responses in a `ResultHandle`; use the
`result_describe` / `result_query` meta-tools to navigate. The
`--json` rendering of the list verb shows the raw envelope; the
default human render shows `id`, `name`, `status`, `blueprintId`.

### `meho vcf-automation blueprint list`

Tenant-plane only. The templates deployments instantiate. Cross-
reference the blueprint id against `blueprintId` on
`deployment list` results to identify which deployments instantiated
which blueprint.

### `meho vcf-automation operation search` / `operation call`

Meta-tool wrappers pre-scoped to `vcfa-rest-9.0`. `search` runs the
hybrid BM25+cosine search across vcfa-rest-9.0 op descriptors;
`call` dispatches any op_id directly. Use these for ops not yet
promoted to dedicated CLI aliases.

The raw op_id already encodes its plane via the path prefix; the
`--plane` flag is **ignored** on `operation call` (use the right
op_id). The persistent `--fqdn` flag still threads into the body.

```bash
meho vcf-automation operation search "deployments" --group tenant-deployments
meho vcf-automation operation call GET:/iaas/api/about --target rdc-vcfa
```

## Audit and broadcast classification

Every VCFA v0.5 op is `safety_level=safe`, `requires_approval=false` —
the read-only surface never mutates state. The audit row carries:
`method='DISPATCH'`, `path=<op_id>`, `target_id=<uuid>`,
`payload={"op_id": …, "params_hash": …, "source_kind": "ingested",
"connector_product": "vcf-automation", "connector_version": "9.0",
"connector_impl_id": "vcfa-rest", "result_status": "ok"|"error"}`.

Broadcast events publish per-tenant on every successful dispatch.
`meho audit query --connector vcfa-rest-9.0` retrieves the full
dispatch history; filter by op_id to focus on one plane (`--op-id
GET:/iaas/api/deployments`).

## Migrating off `scripts/vcf-automation.sh`

The consumer's
[`scripts/vcf-automation.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-automation.sh)
drives VCFA REST via a `curl` + per-plane session wrapper. The
`meho vcf-automation` verbs replace it for the read-only workflows;
write workflows stay in the wrapper.

| Consumer workflow | `vcf-automation.sh` invocation | `meho vcf-automation …` replacement | Notes |
| --- | --- | --- | --- |
| Provider identity | `vcf-automation.sh provider-about` | `meho vcf-automation about --plane provider --target rdc-vcfa` | |
| Tenant identity | `vcf-automation.sh tenant-about` | `meho vcf-automation about --plane tenant --target rdc-vcfa` | |
| Org listing | `vcf-automation.sh orgs` | `meho vcf-automation org list --target rdc-vcfa` | |
| Org detail | `vcf-automation.sh org <id>` | `meho vcf-automation org get <id> --target rdc-vcfa` | |
| Region (VDC) listing | `vcf-automation.sh regions` | `meho vcf-automation region list --target rdc-vcfa` | |
| Project listing | `vcf-automation.sh projects` | `meho vcf-automation project list --target rdc-vcfa` | |
| Deployment listing | `vcf-automation.sh deployments` | `meho vcf-automation deployment list --target rdc-vcfa` | JSONFlux handle returned on large tenants — use `result_describe` |
| Deployment detail | `vcf-automation.sh deployment <id>` | `meho vcf-automation deployment get <id> --target rdc-vcfa` | |
| Blueprint listing | `vcf-automation.sh blueprints` | `meho vcf-automation blueprint list --target rdc-vcfa` | |

What `scripts/vcf-automation.sh` did that `meho vcf-automation`
deliberately does **not** do (out of scope for v0.5):

- **Write / mutate ops** — catalog deployment, blueprint create,
  IaaS machine lifecycle (delete / power / resize). v0.5 is
  read-only; write ops land in v0.5.next pending policy + approval
  workflow.
- **Per-org tenant-context switching** — the wrapper supports
  `--org <name>` to switch the tenant context per call; the
  connector's `target.domain` is the per-target setting (one tenant
  context per target). Operators register multiple VCFA targets
  (`rdc-vcfa-acme`, `rdc-vcfa-globex`) to switch contexts.
- **Custom query parameters** — `vcf-automation.sh` passes raw
  query strings; the v0.5 CLI exposes only the parameters each op
  formally declares in its descriptor. Use `meho vcf-automation
  operation call <op_id> --params '{"$filter": "…", "$top": 100}'`
  for OData pagination / filtering.

### Per-ticket wrapper-flip recipe

For each active VCFA consumer ticket, apply this pattern:

1. **Identify the wrapper invocation** in the ticket's
   reproduction steps (usually
   `bash scripts/vcf-automation.sh <command>`).
2. **Find the `meho vcf-automation` equivalent** from the table
   above. Confirm the right plane (`--plane provider|tenant` on
   `about`; explicit per-resource verb otherwise).
3. **Confirm the target's `fqdn`** is set if the target's `host`
   is an IP. Without it the first dispatch surfaces a configuration
   error — set `fqdn:` in `targets.yaml` and re-run
   `meho targets import --update <file>`.
4. **Validate output parity**: run both side-by-side against
   `rdc-vcfa`. Use `--json | jq` on the `meho vcf-automation` side
   to pull the same fields the bash script was parsing.
5. **Update the ticket steps**: replace the `vcf-automation.sh`
   invocation with the `meho vcf-automation` form. Capture the
   `--json` output for the ticket's evidence block.
6. **Retire the wrapper call site** in the ticket's automation
   scripts once the MEHO form is validated.

Example — tenant deployment listing (the workhorse query):

```bash
# Before (wrapper):
bash scripts/vcf-automation.sh deployments rdc-vcfa | jq '.[].id'

# After (meho vcf-automation):
meho vcf-automation deployment list --target rdc-vcfa --json \
  | jq '.result.content[].id'

# Verify parity:
diff \
  <(bash scripts/vcf-automation.sh deployments rdc-vcfa | jq -S '.[].id') \
  <(meho vcf-automation deployment list --target rdc-vcfa --json \
      | jq -S '.result.content[].id')
# expected: empty diff
```

Example — provider region detail (post-VCFA-9 evolution of the vCD
provider-VDC concept):

```bash
# Before:
bash scripts/vcf-automation.sh region <region-id> rdc-vcfa

# After:
meho vcf-automation region get <region-id> --target rdc-vcfa
```

## The `--fqdn` checklist (load-bearing)

This is the single most likely failure mode operators hit on the
first dispatch against a new VCFA target. Symptoms:

- `meho vcf-automation about --plane provider --target …` returns
  `status=error` with `extras.exception_message` containing the
  IP literal and the word `fqdn`.

The fix:

1. **Inspect the target.** `meho targets describe --name rdc-vcfa`
   should show a populated `fqdn:` field. If it doesn't, the
   `targets.yaml` import didn't include the field (or the operator
   imported before `fqdn:` was added).
2. **Set `fqdn:` in `targets.yaml`** to the appliance's canonical
   FQDN — the value returned by `dig +short PTR <vcfa-ip>` or the
   `cn` / `subjectAltName` on the appliance's TLS cert. Re-import
   with `meho targets import --update <file>`.
3. **Verify with `meho targets probe`.** A green probe confirms
   the connector reached the appliance through the FQDN-rooted base
   URL — every subsequent dispatch will work.
4. **As a debugging override**, pass `--fqdn <vhost>` on a single
   dispatch:

   ```bash
   meho vcf-automation about --plane provider --target rdc-vcfa \
     --fqdn vcfa.rdc.evoila.io
   ```

   The override is in-memory only (the persisted Target row is not
   modified). Use this when the canonical FQDN drifts during
   maintenance and you want to confirm the new vhost works before
   updating `targets.yaml`.

## Goal #214 G3.6 VCFA checklist

| Checklist item | Status |
| --- | --- |
| G3.6-T10 #832 — `VcfAutomationConnector` skeleton + dual-plane auth + vhost routing | ✅ merged |
| G3.6-T11 #836 — Dual-spec ingestion + 11-op core curation | ✅ merged |
| G3.6-T12 #840 — CLI verbs + E2E recorded-fixture + this doc | ✅ this PR |
| MCP `llm_instructions` / `when_to_use` reviewed for plane-aware agent legibility | ✅ done (#836 curation blobs name their plane explicitly) |
| Both auth planes work end-to-end via `call_operation` in CI | ✅ `test_connectors_vcf_automation_e2e.py` |
| Audit rows carry `op_id` + `target_id` + `params_hash` | ✅ `test_connectors_vcf_automation_e2e.py` |
| JSONFlux handle path tested (deployment list) | ✅ `test_connectors_vcf_automation_e2e.py` |
| Vhost (`--fqdn`) override threads end-to-end | ✅ `test_connectors_vcf_automation_e2e.py` |
| IP-host-without-`fqdn` surfaces descriptive error (not blank 404) | ✅ `test_connectors_vcf_automation_e2e.py` |
| `docs/cross-repo/vcf-automation-onboarding.md` with wrapper-flip recipe | ✅ this document |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `--plane is required for this verb` on `about` | `about` is dual-plane; no implicit default. | Pass `--plane provider` or `--plane tenant`. |
| `--plane "tenant" is invalid for this verb` (on `org list`, etc.) | Wrong plane for the verb. | Omit `--plane` or pass the matching one (`--plane provider` for org/region/user; `--plane tenant` for project/deployment/blueprint). |
| `status=error connector_error: VcfAutomationConfigurationError` with `fqdn` in the message | IP-host target with no `fqdn`. | Set `fqdn:` in `targets.yaml`; re-import. See "The `--fqdn` checklist". |
| `status=error connector_error: RuntimeError … HTTP 401` on provider plane | Provider account locked / wrong `provider_username`. | Verify the `provider_secret_ref` Vault secret; check VCFA UI for account lockout. |
| `status=error connector_error: RuntimeError … HTTP 401` on tenant plane | Tenant account doesn't have access to the org. | Verify `target.domain` matches the tenant org; verify the credential has tenant-side roles. |
| `status=error connector_error: RuntimeError … vcf-automation provider session re-login failed` (HTTP 401 after refresh) | Provider credentials consistently rejected. | Update the provider Vault secret; restart the backplane to flush the in-process session caches. |
| `status=error … unknown_op` | The 11 core ops are not registered/enabled. | Re-run `apply_vcfa_core_curation` against the VCFA connector; see [`g36-vcfa-canary.md`](./g36-vcfa-canary.md). |
| `status=denied` | `read_only` role, or a tenant policy denied the dispatch. | Use an `operator`-role token. |
| Connection times out | VCFA appliance unreachable from the backplane host. | Verify the FQDN resolves from the backplane; check firewall rules (port 443 from backplane → VCFA). |
| Probe fails with TLS error | Self-signed or expired certificate, or wrong CA bundle. | Mount the correct CA bundle in the backplane container and set `MEHO_TLS_CA_BUNDLE`; or replace the VCFA cert with a CA-signed one. |
| Deployment list returns a handle envelope without rows | JSONFlux reducer wrapped the payload because it exceeded the inline threshold. | Use `result_describe <handle_id>` + `result_query <handle_id> …` to navigate; or pass `--json` to inspect the full envelope. |

## References

- Initiative: [#369 G3.6 tier-3 VCF management plane](https://github.com/evoila/meho/issues/369);
  Goal [#214](https://github.com/evoila/meho/issues/214) (connector parity).
- Tasks that shipped this surface: [#832](https://github.com/evoila/meho/issues/832) (T10 skeleton + dual-plane auth + vhost routing), [#836](https://github.com/evoila/meho/issues/836) (T11 dual-spec ingest + 11-op curation), [#840](https://github.com/evoila/meho/issues/840) (T12 CLI verbs + E2E + this doc).
- Canary procedure (G0.7 dual-spec ingest → curation): [`g36-vcfa-canary.md`](./g36-vcfa-canary.md).
- Connector source: [`backend/src/meho_backplane/connectors/vcf_automation/`](../../backend/src/meho_backplane/connectors/vcf_automation/).
- CLI verbs: [`cli/internal/cmd/vcf-automation/`](../../cli/internal/cmd/vcf-automation/).
- E2E integration test: [`backend/tests/test_connectors_vcf_automation_e2e.py`](../../backend/tests/test_connectors_vcf_automation_e2e.py).
- Consumer wrapper retiring: [`scripts/vcf-automation.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-automation.sh).
- VCFA Automation API references: provider [<https://developer.broadcom.com/xapis/vmware-cloud-foundation-automation-api/latest/>], tenant [<https://developer.broadcom.com/xapis/aria-automation-api/latest/>].
- Related onboarding docs: [`vault-onboarding.md`](./vault-onboarding.md), [`audit-query.md`](./audit-query.md), [`targets-yaml.md`](./targets-yaml.md), sibling tier-3 connectors (vROps / vRLI / Fleet via #837/#838/#839).
