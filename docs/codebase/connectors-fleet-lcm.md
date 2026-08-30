# Connector: fleet-lcm (VCF 9 Fleet LCM Service, modern generic impl)

## Overview

The `fleet-lcm` connector is the hand-rolled `HttpConnector` subclass that
makes the **VCF 9 Fleet LCM Service** REST API dispatchable under the
`(product="fleet", version="9.0", impl_id="fleet-lcm")` registry triple. It is
the **modern** implementation of `product=fleet` and coexists with the legacy
typed `fleet-rest` impl ([`connectors-vcf-fleet.md`](connectors-vcf-fleet.md)) —
the codebase's first real two-implementation case (initiative
[#3033](https://github.com/evoila/meho/issues/3033), tasks #3036 + #3037,
decision of record `versioned-connector-dual-impl`). The two are resolved per
target by fingerprint; see the "Dual implementation" section of the vcf-fleet
doc for the resolution matrix and the
[resolution test](../../backend/tests/test_connectors_fleet_dual_impl_resolution.py).

Source: `backend/src/meho_backplane/connectors/fleet_lcm/`.

The Fleet LCM Service (`https://vcf.broadcom.com/fleet-lcm` → `/v1/*`) is the
VCF 9 successor to vRealize Suite Lifecycle Manager (vRSLCM) 8.x. It is a
**distinct API surface** from the legacy `/lcm/lcops/api/v2/*` the `fleet-rest`
impl dispatches; the legacy surface still answers on 9.0 appliances for
back-compat, which is why both bands overlap at 9.0 and the resolver — not the
connector — picks the surface per target.

## Scope: typed read core (#3047)

The connector ships a curated **13-op typed read core** — the modern successor
to the legacy `fleet-rest` read surface — registered as `source_kind="typed"`
and **enabled at register time**, so a VCF Fleet 9.x target (which resolves
here by most-specific-version) dispatches a modern `/v1/*` read op on a **fresh
boot with zero catalog ingest**. This is what **restores 9.0 fleet dispatch**
after the "modern default now" resolver decision (#3033): the #3036 skeleton
left `fleet-lcm` registered but with no dispatchable ops, so a 9.0 target
resolved to an impl that could not serve a call; the read core closes that gap.

The read core covers the operator's "is it up, what's deployed, what's
happening, what can we upgrade to" drill path across five groups:

| Group | Ops |
|---|---|
| `fleet-lcm-system` | `fleet-lcm.health`, `.system.info`, `.config.info` |
| `fleet-lcm-sddc` | `fleet-lcm.sddc-lcm.list`, `.sddc-lcm.info` |
| `fleet-lcm-components` | `fleet-lcm.component.list`, `.component.info`, `.component.status` |
| `fleet-lcm-tasks` | `fleet-lcm.task.list`, `.task.info` |
| `fleet-lcm-lifecycle` | `fleet-lcm.upgrade-plan.list`, `.upgrade-plan.info`, `.release-version.list` |

**Why typed, not ingested.** The connector *is* the modern generic impl and its
51-op spec is pinnable, but the read core is hand-coded typed (the harbor / nsx
#2358 pattern) precisely so it is live on a fresh boot with **no** operational
`meho connector ingest` + `enable_reads` step — merging the read core *is* the
9.0-restoration, not a runbook the operator must still run. The wider surface
(the full 51-op spec as `source_kind="ingested"` breadth + the component /
upgrade / task **writes**) remains a follow-up, enabled operationally through
the generic review flow (`ReviewService.enable_reads` over an ingest of the same
pinned `fleet-lcm-openapi.yaml`). Registering this real `HttpConnector` subclass
is also the load-bearing prerequisite for those future ingested rows to become
dispatchable (a bare `GenericRestConnector` auto-shim is non-dispatchable — the
`VmwareRestConnector` pattern).

**Live-appliance dispatch verification remains a non-goal** (#1002 / #995 — no
reachable Fleet LCM appliance). The read core is unit-tested end-to-end against
a respx-mocked service (dispatch `status="ok"` through **both** the Bearer and
Basic auth seams) and reconcile-guarded; live Bearer-auth verification against
real hardware is the #3047 follow-up (see "Auth" below).

## Key types

- **`FleetLcmConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="fleet"`, `version="9.0"`, `impl_id="fleet-lcm"`,
  `supported_version_range=">=9.0,<10.0"`, `priority=1`. The range is the
  **narrower** of the two impls, so a 9.0 target resolves here by the
  resolver's most-specific-version tie-break; `priority=1` equals the legacy
  impl by design (the split is by version-specificity, not priority).
- **`FleetLcmTargetLike`** / **`FleetLcmCredentialsLoader`** (`session.py`) —
  aliases of the shared `VcfTargetLike` Protocol and `VcfCredentialsLoader`
  type (the modern impl reads the same target fields as the legacy). The
  loader is injectable on construction
  (`FleetLcmConnector(credentials_loader=...)`) for unit / integration tests.
- **`load_credentials_from_vault`** (`session.py`) — the fleet-lcm default
  loader (#3047). An operator-context KV-v2 read (via the shared
  `load_vault_secret_data`) that surfaces the required `{username, password}`
  pair **plus an optional `token`** when the operator has staged one — the
  Bearer token-provisioning seam. Mirrors the proxmox / github
  field-discriminator loaders (pick the upstream credential protocol by
  inspecting which fields the secret carries, not via `auth_model`).
- Canonical constants (`__init__.py`): `FLEET_LCM_PRODUCT`,
  `FLEET_LCM_VERSION`, `FLEET_LCM_IMPL_ID`, `FLEET_LCM_CONNECTOR_ID`.

The connector reuses `basic_auth_header`, `is_acceptable_auth_model`, and
`CredentialsCache` from `meho_backplane.connectors._shared.vcf_auth` — the same
scaffolding the legacy impl uses.

## Control flow

### Registration

Importing `meho_backplane.connectors.fleet_lcm` calls
`register_connector_v2(product="fleet", version="9.0", impl_id="fleet-lcm",
cls=FleetLcmConnector)`. **No wildcard sibling** is registered: the legacy
`vcf_fleet` package already owns the `("fleet","","")` fallback, and a second
class on that key would raise `RuntimeError` at import. One wildcard per
product suffices (it only guarantees an unfingerprinted target resolves
*something*); the versioned entries carry the real per-band matching.

### Auth (Bearer primary, Basic fallback)

Per the pinned spec's `securitySchemes`, the global scheme is `bearerToken`
(HTTP Bearer), with `basicAuth` (HTTP Basic) also defined. `auth_headers`:

1. Rejects any `target.auth_model` other than `shared_service_account` /
   `None` via the shared `is_acceptable_auth_model`.
2. Loads credentials via the shared `CredentialsCache` (which enforces the
   `{username, password}` contract).
3. If the loaded credentials carry a non-empty `"token"`, returns
   `Authorization: Bearer <token>` (the spec's primary scheme); otherwise
   returns `Authorization: Basic <b64>` off the username/password pair (the
   spec's `basicAuth` alternative), reusing the shared `basic_auth_header`.

**The token-provisioning seam has landed; the live mint + verify has not
(#3047).** The default `load_credentials_from_vault` now surfaces a
Vault-staged `token`, so an operator opts a target into Bearer by adding a
`token` field to its secret — no `auth_model` change. This is unit-tested end
to end through the **real** default loader against an in-process Vault fake
(`test_connectors_fleet_lcm_credread.py`: Basic when no token, Bearer when a
token is staged, whitespace-strip, fail-closed on a missing pair) as well as
via injected loaders (`test_connectors_fleet_lcm_auth.py`). What remains a
seam is the **live** alternative provisioning path — a `POST`-`basicAuth` →
mint-`bearerToken` exchange against the appliance's token endpoint — and the
**live Bearer verification** against real hardware; both are gated on a
reachable Fleet LCM appliance (#1002 / #995). Until then a target with no
staged token authenticates with the Basic alternative.

### Fingerprint / probe

Unlike the legacy impl (whose diagnostic endpoints return HTTP 500 on 9.0),
the modern service exposes a first-class health endpoint. The connector probes
`GET /v1/health` (`operationId` `getHealth`; the spec marks it `security: []`).
`fingerprint` returns a reachable `FingerprintResult`
(`vendor="vmware"`, `product="fleet"`, `probe_method="GET /v1/health ..."`),
with `version` taken from an explicit `version` string in the health payload
when present, else `None`. On any transport/status failure it returns a
non-reachable result carrying `extras["error"]` (the Harbor / NSX / vcf-fleet
pattern). `probe()` delegates to `fingerprint()`.

### Dispatch

The typed read-core ops dispatch through `meho_backplane.operations.dispatch`
(via `POST /api/v1/operations/call` or MCP `call_operation`): the dispatcher
resolves the persisted `module.ClassName.method` handler_ref to the connector's
bound-method shim, threads `operator` / `target` / `params`, and the handler
issues an authenticated `GET` on this connector's client — so the read rides
`auth_headers` (Bearer primary, Basic fallback). `execute()` is the G0.6
ABC-compatibility shim (builds a synthetic `Operator` and forwards to
`dispatch`); it remains the path for any future `source_kind="ingested"` op.

## Typed read core internals

The read core follows the harbor #2856 layout:

- **`typed_ops.py`** — the `FleetLcmTypedOp` dataclass table
  (`FLEET_LCM_TYPED_OPS`), the per-group `when_to_use` map
  (`FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP`), and the module-level
  `register_fleet_lcm_typed_operations(*, embedding_service=None)` registrar.
- **`typed_reads.py`** — the op bodies (`async def fleet_lcm_*_impl(connector,
  operator, target, params)`), each issuing an authenticated `GET` via
  `HttpConnector._get_json` and returning the parsed payload.
- **`_llm_instructions.py`** — the per-op `llm_instructions` blobs
  (`when_to_call` / `output_shape` / `next_step`), keyed by op id (split out
  for the file-size budget).
- **`_paths.py`** — the `/v1/*` request-path templates + `fill_path` /
  `encode_segment` (percent-encoded, fail-loud path substitution for the by-id
  ops; the reconcile lane introspects these constants).
- **`connector.py`** — 13 thin bound-method shims (`health`, `system_info`, …)
  that lazily import and delegate to their `_impl` body; each typed op's
  `handler_attr` names its shim.

`register_fleet_lcm_typed_operations` is queued onto the lifespan registrar
list via `register_typed_op_registrar` in `__init__.py`;
`register_typed_operation` creates each `OperationGroup` (`review_status=
"enabled"`) and inserts each descriptor `source_kind="typed"`, `is_enabled=
True`, `method=None` / `path=None` (the wire path lives only in the handler).
So the read core needs no operator review — it is live the moment the connector
loads.

## Spec reconcile lane

[`backend/tests/test_connectors_fleet_lcm_spec_reconcile.py`](../../backend/tests/test_connectors_fleet_lcm_spec_reconcile.py)
asserts every hand-coded `GET:/path` the typed read core dispatches — the 13
`/v1/*` path templates in `_paths.py` plus the connector's
`_FLEET_LCM_HEALTH_PATH` probe (introspected from the live constants — the
#2944 pattern) — is served by the pinned `fleet-lcm-9.0/fleet-lcm-openapi.yaml`.
The typed health op and the probe share `/v1/health`; the pin asserts that link
so the two never drift. **No server-base fold** is applied: the spec's server
url `https://vcf.broadcom.com/fleet-lcm` is *absolute*, so `parse_openapi` does
not fold the `/fleet-lcm` base onto path keys (only *relative* server bases are
folded, #1796) — the served op_ids are the raw `GET:/v1/*`, matching the
templates (the by-id placeholders `{sddcLcmId}` / `{componentId}` / `{taskId}` /
`{planId}` are byte-for-byte the spec's own path-parameter names). The lane arms
when the shelf provides the spec (`MEHO_CONSUMER_DOCS_ROOT`) and skips uniformly
otherwise (`tests/_spec_shelf.py` contract). (The wider ingested breadth is not
reconciled here — reconciling ingested rows against the spec they were ingested
from is tautological.)

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` — pooled
  `httpx.AsyncClient` per target, retry-on-idempotent transport, base-URL
  composition.
- `meho_backplane.connectors._shared.vcf_auth` — `basic_auth_header`,
  `is_acceptable_auth_model`, `CredentialsCache`, `load_credentials_from_vault`.
- `meho_backplane.connectors.schemas` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.
- `httpx`, `respx` (test-only). No new runtime deps.

## Known issues / follow-ups

- **Live Bearer not verified; the token-provisioning seam has landed.** See
  "Auth" — the default loader now surfaces a Vault-staged `token` (Bearer
  opt-in), unit-tested through the real loader against a Vault fake. What
  remains the #3047 follow-up, gated on reachable hardware (#1002 / #995), is
  the **live** `basicAuth` → mint-`bearerToken` exchange and the live Bearer
  handshake against a real appliance. Until then a target with no staged token
  authenticates with the Basic alternative.
- **Read core only; ingested breadth + writes are a follow-up.** The 13-op
  typed read core dispatches on a fresh boot. The wider 51-op `/v1/*` surface
  (as `source_kind="ingested"` breadth) and the component / upgrade / task
  writes are enabled operationally through the generic review flow
  (`meho connector ingest` of `fleet-lcm-openapi.yaml` → `ReviewService.enable_reads`),
  not shipped here — see the operator runbook
  [`docs/cross-repo/fleet-lcm-onboarding.md`](../cross-repo/fleet-lcm-onboarding.md).

## References

- Typed read-core Task: <https://github.com/evoila/meho/issues/3047>
- Modern-impl skeleton Task: <https://github.com/evoila/meho/issues/3036>
- Legacy re-band + resolution test Task: <https://github.com/evoila/meho/issues/3037>
- Parent initiative: <https://github.com/evoila/meho/issues/3033>
- Read-core tests:
  [`test_connectors_fleet_lcm_typed_reads.py`](../../backend/tests/test_connectors_fleet_lcm_typed_reads.py)
  (dispatch + registration) and
  [`test_connectors_fleet_lcm_spec_reconcile.py`](../../backend/tests/test_connectors_fleet_lcm_spec_reconcile.py)
  (13-path reconcile lane).
- Auth tests:
  [`test_connectors_fleet_lcm_auth.py`](../../backend/tests/test_connectors_fleet_lcm_auth.py)
  (injected-loader header shapes) and
  [`test_connectors_fleet_lcm_credread.py`](../../backend/tests/test_connectors_fleet_lcm_credread.py)
  (the real default loader's Bearer/Basic paths through a Vault fake).
- Operator runbook: [`fleet-lcm-onboarding.md`](../cross-repo/fleet-lcm-onboarding.md).
- Legacy impl: [`connectors-vcf-fleet.md`](connectors-vcf-fleet.md)
- Spec provenance: `vmware/vcf-api-specs@c3f3b52c` (Apache-2.0); shelf
  `docs/fleet-lcm-9.0/MANIFEST.md`.
