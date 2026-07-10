# Connector: vcf-fleet (VCF Fleet 9.0, vRSLCM-derived)

## Overview

The `vcf-fleet` connector is the hand-rolled `HttpConnector` subclass that
dispatches VCF Fleet REST operations under the
`(product="vcf-fleet", version="9.0", impl_id="fleet-rest")` registry triple.
G3.6-T7 (#831) shipped the skeleton — HTTP Basic auth against the LCM-local
user store, the wrapper-verified fingerprint/probe call, and the G0.6
dispatch shim. G3.6-T8 (#835) added the operator-review curation surface —
`FLEET_CORE_GROUPS` + `FLEET_CORE_OPS` + `apply_fleet_core_curation`. T4
(#2304, Initiative #2266) then converted the audited read set (about/health
probe + component inventory) to **typed** ops in `typed_ops.py`, trimming
the ingested curation to the 6 declined ops (5 groups) and removing the
Fleet-LCM-spec ingest dependency for the operational surface. G3.6-T9
(#839) shipped the CLI verb tree + recorded-fixture E2E.

Source: `backend/src/meho_backplane/connectors/vcf_fleet/`.

VCF Fleet is the rebrand of vRealize Suite Lifecycle Manager (vRSLCM) under
the VCF 9 umbrella; the appliance still identifies as vRSLCM in its internal
config and every response carries an `Lcm-API-Version` header.

## Key types

- **`VcfFleetConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vcf-fleet"`, `version="9.0"`,
  `impl_id="fleet-rest"`, `supported_version_range=">=9.0,<10.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim defensively if both somehow register for the same triple.
- **`VcfFleetTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`, `host`,
  `port`, `secret_ref`, `auth_model`. No `sso_realm` field — Fleet does
  NOT federate with SSO, so the Basic auth header carries
  `username:password` directly. Replaced by the concrete `Target` model in
  `meho_backplane.targets` (#224) structurally; no code edits here when
  the model lands.
- **`VcfFleetCredentialsLoader`** (`session.py`) — async callable type
  resolving a target to `{"username": ..., "password": ...}`. Injectable
  on connector construction (`VcfFleetConnector(credentials_loader=...)`)
  so unit tests, integration tests, and pre-G0.3 production deploys
  override the default Vault loader.
- **`load_credentials_from_vault`** (`session.py`) — default loader,
  stubbed `NotImplementedError` until the live operator-context per-target
  Vault read lands. Mirrors `load_credentials_from_vault` in
  `connectors/harbor/` and `connectors/vcf_automation/` — all stubs flip
  to live implementations together under Goal #214.

The connector reuses three helpers from
`meho_backplane.connectors._shared.vcf_auth` (the #841 cross-cutting
module landed in G3.6-T13):

- `basic_auth_header(username, password)` — produces the
  `Authorization: Basic <b64>` value.
- `is_acceptable_auth_model(value)` — gates the SHARED_SERVICE_ACCOUNT
  enum / string / `None` triple.
- `CredentialsCache(loader, product_label=...)` — load-once-per-target
  `{"username": str, "password": str}` cache with the missing-key →
  `RuntimeError` contract and a serialised `clear()` for `aclose`.

Sister G3.6 connectors `vcf_operations` (#829) and `vcf_logs` (#830) will
import the same helpers; `vcf_automation` (#832 / merged) deliberately
does not — its dual-plane auth shape is bespoke.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_fleet` triggers the
   module-level
   `register_connector_v2(product="vcf-fleet", version="9.0", impl_id="fleet-rest", cls=VcfFleetConnector)`
   call.
3. The registry's v2 table now resolves `("vcf-fleet", "9.0",
   "fleet-rest")` to `VcfFleetConnector`. The G0.7 auto-shim's
   idempotency check no-ops on subsequent ingests against the same
   triple.

### Auth

1. The chassis dispatcher calls `auth_headers(target, operator)`
   before issuing the request.
2. `auth_headers` rejects any `target.auth_model` other than
   `shared_service_account` / `None` via the shared
   `is_acceptable_auth_model` predicate, raising `NotImplementedError`
   naming the target and requested mode.
3. The shared `CredentialsCache.get(target)` invokes the injected loader
   on first call per target. The loader returns
   `{"username": "admin@local", "password": "..."}`; missing keys
   raise `RuntimeError` naming both the target and the missing key.
4. The cached dict is fed to `basic_auth_header(username, password)` and
   returned as `{"Authorization": "Basic <b64>"}`.

The Fleet account is typically `admin@local` — the `@local` suffix is
part of the literal username, not a realm decoration. Fleet does **not**
federate with vCenter SSO out of the box (consumer wrapper header,
verified 2026-05-21: a `vsphere.local` service account was rejected with
HTTP 401 "Bad credentials" during the discovery journey).

### Fingerprint / probe (wrapper-verified)

Fleet's first-party diagnostic endpoints all return HTTP 500 in VCF 9.0
builds — known appliance issue:

- `/lcm/lcops/api/v2/about`
- `/lcm/lcops/api/v2/health`
- `/lcm/lcops/api/v2/version`
- `/lcm/lcops/api/v2/system-details`
- `/lcm/common/api/about`
- `/lcm/locker/api/v2/about`

The consumer wrapper `scripts/vcf-fleet.sh` documents this explicitly and
works around it by calling `GET /lcm/lcops/api/v2/datacenters` with HTTP
Basic auth and reading the `Lcm-API-Version` response header for the LCM
API version. The connector follows the wrapper's contract verbatim:

- `probe_method` = `"GET /lcm/lcops/api/v2/datacenters with HTTP Basic; read Lcm-API-Version response header"`.
- `version` ← the `Lcm-API-Version` header value (e.g. `"8.0"`) when
  present, `None` otherwise.
- `build` ← `None` (no working endpoint exposes a build string in 9.0).
- `extras` ← `{lcm_api_version, datacenter_count, product_lineage,
  diagnostic_endpoints_broken}`.

The product version itself is **not** surfaced by any working endpoint in
9.0. Operators cross-source it from SDDC Manager's `/v1/vcf-services`
(LCM service entry) — that's an operator-context concern above the
per-product connector and out of scope for this skeleton.

`probe()` delegates to `fingerprint()`: Fleet has no working dedicated
health endpoint, and the datacenters call already proves both transport
and HTTP Basic auth, so reusing one round-trip is the right shape (same
delegation pattern as `vcf_automation`, `sddc_manager`, `nsx`).

### Dispatch

The audited read set dispatches as **typed** ops (`source_kind="typed"`)
off the connector's hand-rolled HTTP Basic session; the declined breadth
stays **generic-ingested**. Typed ops need no catalog ingest — they are
registered in code at lifespan startup and dispatch on a fresh boot with
zero catalog state. Ingested ops still take the G0.7 path: the operator
ingests the Fleet OpenAPI spec into `endpoint_descriptor`, reviews and
enables the curated op_ids, then dispatches. Both paths route through
`meho_backplane.operations.dispatch` via `POST /api/v1/operations/call`
(or MCP `call_operation`). The connector's `execute()` is the G0.6
ABC-compatibility shim that builds a synthetic `Operator` and forwards to
the dispatcher; post-G0.6 callers construct a real `Operator` and invoke
`dispatch` directly.

### Typed read set (T4 · #2304, Initiative #2266)

`typed_ops.py` + `VcfFleetConnector.register_operations()` carry the
**audited** read set — the two ops the adopter actually uses (#2294 T0
audit, row 21) — converted from ingested-row curation to typed ops so the
operational surface no longer depends on ingesting the crash-prone Fleet
LCM spec (the #2272 datetime-crash artifact):

- **`fleet.about`** (`GET /lcm/lcops/api/v2/about`, group `fleet-about`) —
  the about/health probe. Returns HTTP 500 in VCF 9.0 (see known-issue
  below); the `llm_instructions` carry the 500 warning + reachability
  fallback that moved with the op.
- **`fleet.environment.list`** (`GET /lcm/lcops/api/v2/environments`,
  group `fleet-inventory`) — the component inventory ("what's deployed").
  Wraps Fleet's bare environments array under an `environments` key
  (the vSphere typed-reads envelope shape).

`register_operations` walks `FLEET_TYPED_OPS`, resolves each op's
`handler_attr` to the bound method on the connector, and routes it through
`register_typed_operation` — the argocd / bind9 / vmware_rest mold. The
module-level `register_fleet_typed_operations` wrapper is queued via
`register_typed_op_registrar` in `__init__.py` so the rows land before the
first dispatch.

### Declined ingested read-core (G3.6-T8 #835, trimmed by #2304)

`core_ops.py` carries the operator-review metadata for the **declined**
ingested read core — the six ops outside the audited set (kept as
browsable ingested breadth until T7 retires the curation apparatus):

- **`FLEET_CORE_GROUPS`** — 5 `FleetCoreGroup` entries: `fleet-datacenter`,
  `fleet-vcenter`, `fleet-environment` (now carries only
  `fleet.environment.get`), `fleet-product`, `fleet-request`.
  (`fleet-about` was removed — its op is typed now.)
- **`FLEET_CORE_OPS`** — 6 `FleetCoreOp` entries (all `GET:`, read-only):
  `fleet.datacenter.list`, `fleet.vcenter.list`, `fleet.environment.get`,
  `fleet.product.list`, `fleet.request.list`, `fleet.request.get`.
  `fleet.about` + `fleet.environment.list` are intentionally absent —
  they are typed now, and the ingested duplicate must not shadow the typed
  op.
- **`FLEET_PATH_RULES` + `classify_fleet_op`** — deterministic path-prefix
  classifier feeding the curated `group_key` per op_id. The `/about` rule
  was removed with the typed conversion (an ingested `/about` row now
  classifies `none` and stays disabled). Rule ordering is load-bearing
  (vcenters before datacenters; products before environments; request
  paths under `/lcm/request/` independent of LCM-ops paths under
  `/lcm/lcops/`).
- **`apply_fleet_core_curation`** — operator-review-time substrate call
  that runs against an already-ingested connector: disables non-core ops
  in curated groups via the audit-log-driven operator-override exclusion,
  enables each curated group, and lands the per-op `llm_instructions`
  blob. Mirrors `apply_harbor_core_curation` / `apply_nsx_core_curation`
  verbatim.

The runbook for end-to-end ingest + curate + verify is
[`docs/cross-repo/g36-fleet-canary.md`](../cross-repo/g36-fleet-canary.md).

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` — base class
  carrying the pooled `httpx.AsyncClient` per target, retry-on-idempotent
  transport, and base-URL composition.
- `meho_backplane.connectors._shared.vcf_auth` — `basic_auth_header`,
  `is_acceptable_auth_model`, `CredentialsCache`. Shipped in #841 / merged
  into main 2026-05-22.
- `meho_backplane.connectors.schemas` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.
- `httpx>=0.27` (0.28.1 resolved), `pydantic>=2.13.4`, `respx>=0.21`
  (test-only). No new runtime deps introduced by this Task.

## Known issues

- **Product version unreachable in 9.0.** None of Fleet's `/about` /
  `/version` / `/health` / `/system-details` endpoints work in 9.0. The
  connector surfaces only the `Lcm-API-Version` header value (the
  underlying vRSLCM API version, typically `"8.0"`) — not the marketing
  product version. Operators needing the product version cross-source it
  from SDDC Manager's `/v1/vcf-services` LCM service entry.
- **`auth_model="per_user"` / `"impersonation"` rejected.** v0.2 locks
  the connector to `shared_service_account` / `None`. Once the per-user
  identity model lands, the gate moves to the shared module and every
  consumer (including this connector) flips together.
- **Default Vault loader is a stub.** Until Goal #214 (Connector parity)
  wires the operator-context per-target Vault read, production deploys
  must inject a custom `credentials_loader` on construction.

## References

- Task: <https://github.com/evoila/meho/issues/831>
- Parent initiative: <https://github.com/evoila/meho/issues/369>
- Parent goal: <https://github.com/evoila/meho/issues/214>
- Shared scaffolding (G3.6-T13): <https://github.com/evoila/meho/issues/841>
- Sibling skeletons: vROps #829, vRLI #830, VCFA #832 (merged).
- Consumer wrapper (authoritative contract):
  <https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-fleet.sh>
- VCF Fleet / vRSLCM API:
  <https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager-api/latest/>
