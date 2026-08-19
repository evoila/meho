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

## Skeleton scope

This Task (#3036) ships the connector **skeleton** — auth + fingerprint +
probe + the G0.6 dispatch shim — the same shape the legacy `vcf_fleet`
connector first shipped as (#831). The 51 `/v1/*` operations are **not**
hand-coded: they arrive via G0.7 spec ingestion as `source_kind="ingested"`
`endpoint_descriptor` rows under `connector_id="fleet-lcm-9.0"`. Registering
this real `HttpConnector` subclass is the load-bearing prerequisite for those
rows to become **dispatchable** — a bare `GenericRestConnector` auto-shim is
non-dispatchable (its `auth_headers` / `execute` raise), so ingested ops ride
*this* class's `auth_headers` on dispatch (the `VmwareRestConnector` pattern).

**Live-appliance dispatch verification is a non-goal** (#1002 / #995 — no
reachable Fleet LCM appliance). The connector ships registered, spec-guarded
(reconcile lane), and reconcile-armed; live Bearer-auth verification is a
follow-up gated on a stood-up appliance (see "Auth" below).

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
- **`load_credentials_from_vault`** (`session.py`) — the shared
  operator-context KV-v2 read, re-exported. Returns the `{username, password}`
  pair (the shared basic-creds read).
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

**Bearer is a documented seam, not live-verified.** The default Vault loader
surfaces only the username/password pair, so the live default is the Basic
alternative (the appliance accepts `basicAuth` per the spec). The Bearer
*header shape* is unit-tested via an injected loader returning a `"token"`; the
Bearer-token **provisioning** — a fleet-lcm loader surfacing a Vault-stored
token, or a POST-`basicAuth` → mint-`bearerToken` session flow — is the
live-verify follow-up gated on a stood-up appliance.

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

`execute()` is the G0.6 ABC-compatibility shim (builds a synthetic `Operator`
and forwards to `meho_backplane.operations.dispatch`). Ingested `/v1/*` ops
route through the dispatcher via `POST /api/v1/operations/call` (or MCP
`call_operation`) and ride this connector's `auth_headers`.

## Spec reconcile lane

[`backend/tests/test_connectors_fleet_lcm_spec_reconcile.py`](../../backend/tests/test_connectors_fleet_lcm_spec_reconcile.py)
asserts the connector's hand-coded probe path (`_FLEET_LCM_HEALTH_PATH`,
introspected from the live constant — the #2944 pattern) is served by the
pinned `fleet-lcm-9.0/fleet-lcm-openapi.yaml`. **No server-base fold** is
applied: the spec's server url `https://vcf.broadcom.com/fleet-lcm` is
*absolute*, so `parse_openapi` does not fold the `/fleet-lcm` base onto path
keys (only *relative* server bases are folded, #1796) — the served op_id is the
raw `GET:/v1/health`, matching the probe constant. The lane arms when the shelf
provides the spec (`MEHO_CONSUMER_DOCS_ROOT`) and skips uniformly otherwise
(`tests/_spec_shelf.py` contract).

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

- **Bearer auth not live-verified.** See "Auth" — the header shape ships and is
  unit-tested; token provisioning + live dispatch are the follow-up gated on a
  reachable appliance.
- **Ops arrive by ingestion.** Until an operator ingests
  `fleet-lcm-openapi.yaml`, the connector is registered and discoverable but
  `execute` against any `op_id` resolves to "unknown operation" — correct for a
  registered-but-empty connector at this stage.

## References

- Modern-impl Task: <https://github.com/evoila/meho/issues/3036>
- Legacy re-band + resolution test Task: <https://github.com/evoila/meho/issues/3037>
- Parent initiative: <https://github.com/evoila/meho/issues/3033>
- Legacy impl: [`connectors-vcf-fleet.md`](connectors-vcf-fleet.md)
- Spec provenance: `vmware/vcf-api-specs@c3f3b52c` (Apache-2.0); shelf
  `docs/fleet-lcm-9.0/MANIFEST.md`.
