# Connector: vcd (VMware Cloud Director, net-new typed read core)

## Overview

The `vcd` connector is the hand-rolled `HttpConnector` subclass that makes
**VMware Cloud Director** (vCD) — the legacy standalone product — dispatchable
under the `(product="vcd", version="10.6", impl_id="vcd-rest")` registry triple.
It is a **net-new typed** connector filed under the legacy migration-source
connector-coverage initiative
([#3056](https://github.com/evoila/meho/issues/3056), task #3057): vCD is a
migration **source** estate evoila brings customers *off*, so MEHO must be able
to read and inventory it during discovery + onboarding.

Source: `backend/src/meho_backplane/connectors/vcd/`.

**vCD is a distinct product from VCF Automation.** The Broadcom-era successor
([`connectors-vcf-automation.md`](connectors-vcf-automation.md)) absorbed only
vCD's *provider control plane* (reachable via `--plane provider`); vCD is its
own product with the full org / VDC / vApp / VM / catalog tenancy model. So this
is **not** a `vcfa` dual-impl — it is a separate `product="vcd"` connector,
resolved by fingerprint like any other. The two do share the vCloud-Director-
derived provider auth flow and cloudapi surface (which is why the auth here
mirrors the VCFA provider plane).

## Scope: typed read core (#3057)

The connector ships a curated **7-op typed read core** — the migration-inventory
surface — registered as `source_kind="typed"` and **enabled at register time**,
so a vCD target dispatches an inventory read on a **fresh boot with zero catalog
ingest**. A provider (System-org) session is estate-wide, so one session
enumerates every org's resources — exactly what a migration discovery needs.

| Group | Ops |
|---|---|
| `vcd-inventory` | `vcd.org.list`, `.vdc.list`, `.vapp.list`, `.vm.list`, `.catalog.list` |
| `vcd-networking` | `vcd.edge-gateway.list` |
| `vcd-tasks` | `vcd.task.list` |

**Why typed, not ingested.** Broadcom publishes **no committable OpenAPI** for
vCD's full REST surface: the classic `/api/*` query service is bundled into the
UI JS at build time, and the appliance serves no `/cloudapi/1.0.0/openapi*`
(the same evidence recorded for the VCFA provider plane, which speaks the same
surface). This is the CLAUDE.md postulate-1 "no usable spec → typed" case — the
`fleet-lcm` (#3047) / harbor (#2856) pattern. There is therefore no wider
`source_kind="ingested"` breadth to follow up: the typed read core **is** the
connector's surface.

**Dual REST surface, one token.** vCD exposes two co-existing surfaces the read
core reads, both authenticated by the one minted provider Bearer JWT:

- **Modern `/cloudapi/1.0.0/*`** — the clean JSON org collection (`vcd.org.list`
  → `GET /cloudapi/1.0.0/orgs`). In-repo verified: the VCFA provider plane reads
  the same path.
- **Classic `/api/query`** — the uniform, provider/System-scoped inventory
  mechanism. One path, one paging model, a `type=` selector per entity (the
  `go-vcloud-director` SDK's canonical `adminOrgVdc` / `adminVApp` / `adminVM` /
  `adminCatalog` / `adminTask` admin query types, plus `edgeGateway` — no admin
  variant; a System admin sees all), `format=records`.

The two surfaces need **different `Accept` media types** (vCD content
negotiation): `accept_for_path` returns the versioned `application/*+json` form
for `/api/*` and `application/json` for `/cloudapi/*`.

**Live-appliance dispatch verification is the deferred tail.** A vCD 10.6
provider lab exists for end-to-end verification (needs lab Vault/VPN access);
the read core is unit-tested end-to-end against a respx-mocked vCD (dispatch
`status="ok"` through the session-mint seam) and manifest-pinned — the same
unit-tested-first posture `fleet-lcm` shipped with.

## Key types

- **`VcloudDirectorConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vcd"`, `version="10.6"`, `impl_id="vcd-rest"`,
  `supported_version_range=">=10.0,<11.0"`, `priority=1`. The `product` token is
  `"vcd"` (not `"vcloud-director"`) because `register_connector_v2` enforces that
  `product` equals the first hyphen-segment of `impl_id` — a hyphenated token
  would crash the round-trip check at boot.
- **`VcloudDirectorTargetLike`** / **`VcloudDirectorCredentialsLoader`**
  (`session.py`) — aliases of the shared `VcfTargetLike` Protocol and
  `VcfCredentialsLoader` type. The loader is injectable on construction
  (`VcloudDirectorConnector(credentials_loader=...)`) for unit / integration tests.
- **`load_credentials_from_vault`** (`session.py`) — the shared operator-context
  KV-v2 read, re-exported. Returns the `{username, password}` pair.
- Canonical constants (`__init__.py`): `VCD_PRODUCT`, `VCD_VERSION`,
  `VCD_IMPL_ID`, `VCD_CONNECTOR_ID`.

The connector reuses `is_acceptable_auth_model`, `session_establish_auth_error`,
and `ConnectorAuthError` from `meho_backplane.connectors._shared.vcf_auth`.

## Control flow

### Registration

Importing `meho_backplane.connectors.vcd` registers **both** the versioned
triple `register_connector_v2(product="vcd", version="10.6", impl_id="vcd-rest",
...)` **and** the `("vcd","","")` wildcard. Unlike `fleet-lcm` — which skips the
wildcard because the legacy `vcf_fleet` package already owns `("fleet","","")` —
vCD is a net-new single-impl product with no sibling, so it takes the wildcard
itself (the harbor / nsx / keycloak / vcf-automation single-impl pattern). The
wildcard makes an *unfingerprinted* vCD target still resolve; the versioned
triple carries the `(product, version, impl_id)` coordinates the typed ops
register under and the `connector_id` (`"vcd-rest-10.6"`) parses to.

### Auth (provider Basic → Bearer session mint)

vCD's provider login is the vCloud-Director-derived Basic→Bearer flow. On first
use `auth_headers`:

1. Rejects any `target.auth_model` other than `shared_service_account` / `None`
   via the shared `is_acceptable_auth_model`.
2. Fails closed on an empty `operator.raw_jwt` (a system-initiated call cannot
   read per-target vendor credentials) — enforced **before** the token-cache
   lookup so a primed token can never leak to an unauthenticated caller.
3. Mints the session (`_auth.vcd_provider_login`): `POST /cloudapi/1.0.0/sessions/provider`
   with HTTP Basic (`<user>@System`) and the cloudapi `Accept`; the JWT rides
   back in the `X-VMWARE-VCLOUD-ACCESS-TOKEN` **response header** (absence on a
   2xx raises; a 401/403 maps to the structured `ConnectorAuthError`).
4. Caches the JWT per tenant-unique `(tenant_id, target.id)` key under a lock
   and returns `Authorization: Bearer <jwt>` (path-independent).

The per-surface `Accept` is layered on by `_vcd_get` (via the base transport's
`extra_headers` seam), so a data read carries both the Bearer and the right
`Accept`. The `version=` token defaults to `_DEFAULT_VCD_API_VERSION` (`38.0` —
served by vCD 10.5/10.6), overridable per target via `extras["vcd_api_version"]`;
version *negotiation* (read the max non-deprecated version from `/api/versions`)
is a documented follow-up.

A raw 401 (expired token) propagates to the dispatcher's session-recovery arm,
which — because the connector advertises an `invalidate_session` hook (the
SDDC Manager / NSX session-minting precedent, #2067) — evicts the cached JWT and
re-dispatches once, so the retry re-mints. There is no in-connector retry loop,
and the base transport's tenacity retry (5xx / connection only) stays intact.
(`invalidate_credentials` is also implemented — the #2396 establish-failure
companion — but the data-path 401 recovery is keyed on `invalidate_session`.)

### Fingerprint / probe

`GET /api/versions` is vCD's unauthenticated supported-versions endpoint (the
same reachability surface the VCFA provider probe reads). `fingerprint` reads it
**without minting a session** (pure reachability) and returns a reachable
`FingerprintResult` (`vendor="vmware"`, `product="vcd"`). `version` is left
`None`: `/api/versions` reports *API* versions (37/38/39), not the *product*
version (10.x), and the connector does not fabricate one — being single-impl, vCD
resolves through its wildcard regardless. On any transport/status failure it
returns a non-reachable result carrying `extras["error"]`. `probe()` delegates to
`fingerprint()`.

### Dispatch

The typed read-core ops dispatch through `meho_backplane.operations.dispatch`:
the dispatcher resolves the persisted `module.ClassName.method` handler_ref to
the connector's bound-method shim, threads `operator` / `target` / `params`, and
the handler issues an authenticated `GET` via `_vcd_get`. `execute()` is the
G0.6 ABC-compatibility shim (builds a synthetic `Operator` and forwards to
`dispatch`).

## Typed read core internals

The read core follows the `fleet-lcm` / harbor layout:

- **`typed_ops.py`** — the `VcloudDirectorTypedOp` dataclass table
  (`VCD_TYPED_OPS`), the per-group `when_to_use` map
  (`VCD_TYPED_WHEN_TO_USE_BY_GROUP`), and the module-level
  `register_vcd_typed_operations(*, embedding_service=None)` registrar.
- **`typed_reads.py`** — the op bodies (`async def vcd_*_impl(connector,
  operator, target, params)`), each issuing `connector._vcd_get(...)`; the
  query-service reads pass `{"type": <adminX>, "format": "records"}`.
- **`_llm_instructions.py`** — the per-op `llm_instructions` blobs
  (`when_to_call` / `output_shape` / `next_step`), keyed by op id; the prose is
  framed around inventorying a migration-source estate.
- **`_paths.py`** — the request-path constants (`_VERSIONS_PATH`,
  `_SESSIONS_PROVIDER_PATH`, `_ORGS_PATH`, `_QUERY_PATH`), the query-type
  selectors, the `VCD_TOKEN_HEADER`, and the `Accept`-media-type helpers. The
  reconcile lane introspects the `_*_PATH` constants.
- **`_auth.py`** — `vcd_provider_login` (the wire-level Basic→Bearer POST) +
  `compose_provider_basic_user` (`<user>@System`).
- **`connector.py`** — 7 thin bound-method shims (`org_list`, `vdc_list`, …) that
  lazily import and delegate to their `_impl` body.

`register_typed_operation` creates each `OperationGroup` (`review_status=
"enabled"`) and inserts each descriptor `source_kind="typed"`, `is_enabled=True`,
`method=None` / `path=None` (the wire path/type lives only in the handler). So
the read core needs no operator review — it is live the moment the connector
loads.

## Spec reconcile lane

[`backend/tests/test_connectors_vcd_spec_reconcile.py`](../../backend/tests/test_connectors_vcd_spec_reconcile.py)
is a **manifest pin + evidenced exclusion** (not a served-op-id compare): vCD
publishes no committable spec, so there is no artifact to reconcile against. The
lane sweeps the connector's live `_*_PATH` constants (the #2944 introspect-live-
constants pattern) and pins the exact hand-coded surface —
`GET:/api/query`, `GET:/api/versions`, `GET:/cloudapi/1.0.0/orgs`,
`POST:/cloudapi/1.0.0/sessions/provider` — unconditionally, so a new hand-coded
path / renamed constant / method change must update the manifest consciously.
Dispatch-fidelity of each path is proven live-mocked in the auth + typed-reads
tests. The full evidence + activation trigger live in the `vcd` entry of
[`spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` — pooled
  `httpx.AsyncClient` per target, retry-on-idempotent transport, base-URL
  composition, `extra_headers` seam.
- `meho_backplane.connectors._shared.vcf_auth` — `is_acceptable_auth_model`,
  `session_establish_auth_error`, `ConnectorAuthError`, the `VcfTargetLike` /
  `VcfCredentialsLoader` aliases, `load_credentials_from_vault`.
- `meho_backplane.connectors._shared.cache_key.target_cache_key`,
  `meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`.
- `meho_backplane.connectors.schemas` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.
- `httpx`, `respx` (test-only). No new runtime deps.

## Known issues / follow-ups

- **Live dispatch not verified against a real appliance.** The read core is
  unit-tested end-to-end (dispatch through the session-mint seam against a respx
  mock) and manifest-pinned; live end-to-end verification against the vCD 10.6
  provider lab is the deferred tail (needs lab Vault/VPN access).
- **Fixed API version.** The `Accept` `version=` token is a constant (`38.0`,
  overridable via `extras["vcd_api_version"]`). Reading the max non-deprecated
  version from `GET /api/versions` (the way `go-vcloud-director` negotiates) is
  the robustness follow-up; a target on a version outside 10.5/10.6 needs the
  override until then.
- **List-only read core.** By-id detail reads (org / vApp / VM by URN or href),
  non-`System` / SSO provider identities, and any write surface are out of scope
  — a list-focused inventory core for migration discovery.
- **Operator-side product-token alignment.** The operator `vcloud-director` skill
  (rdc-hetzner-dc) emits `product: vcloud-director` on `--probe`; a target must
  use the connector's canonical `product: vcd` token to resolve here. Realigning
  the skill + `targets.yaml` token to `vcd` (the #1814 VCF-suite realignment
  precedent) is a separate operator-repo follow-up.

## References

- Task: <https://github.com/evoila/meho/issues/3057>
- Parent initiative (legacy migration-source coverage):
  <https://github.com/evoila/meho/issues/3056>
- Dual-version / coverage policy of record:
  <https://github.com/evoila/meho/issues/3033>
- Read-core tests:
  [`test_connectors_vcd_auth.py`](../../backend/tests/test_connectors_vcd_auth.py)
  (session mint + fingerprint),
  [`test_connectors_vcd_typed_reads.py`](../../backend/tests/test_connectors_vcd_typed_reads.py)
  (dispatch + registration), and
  [`test_connectors_vcd_spec_reconcile.py`](../../backend/tests/test_connectors_vcd_spec_reconcile.py)
  (manifest pin).
- Auth-family sibling (shares the vCloud-Director provider flow):
  [`connectors-vcf-automation.md`](connectors-vcf-automation.md)
- Typed read-core exemplar: [`connectors-fleet-lcm.md`](connectors-fleet-lcm.md)
