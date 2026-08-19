# Connector: vra8 (vRealize Automation 8.x, legacy dual-impl of `vcfa`)

## Overview

The `vra8` connector is the hand-rolled `HttpConnector` subclass that makes
**vRealize Automation 8.x** (vRA 8) dispatchable under the
`(product="vcfa", version="8.0", impl_id="vcfa-vra8")` registry triple. It is the
**legacy dual-impl** of `product="vcfa"` filed under the legacy migration-source
connector-coverage initiative
([#3056](https://github.com/evoila/meho/issues/3056), task #3058): vRA 8 is a
migration **source** estate evoila brings customers *off*, so MEHO must be able to
read and inventory it during discovery + onboarding.

Source: `backend/src/meho_backplane/connectors/vra8/`.

**vRA 8 is the same product line as VCF Automation 9, one major version back.**
vRealize Automation 8 → Aria Automation → VCF Automation 9 is a rename lineage
across a major-version rebuild. So this is a **second implementation of
`product="vcfa"`** (the modern impl is
[`connectors-vcf-automation.md`](connectors-vcf-automation.md), `vcfa-rest`),
fingerprint-resolved per target — the **second real two-impl case after `fleet`**
(policy [#3033](https://github.com/evoila/meho/issues/3033) / #3038). The auth
flows and read surfaces diverge enough between the two versions (see below) that a
single connector cannot cleanly serve both — exactly the bifurcation the
dual-version policy exists for.

## Dual-impl resolution (`vcfa` = second two-impl case)

| Impl | Class | Band | Wildcard |
|---|---|---|---|
| modern `vcfa-rest` | `VcfAutomationConnector` | `>=9.0,<10.0` | **owns** `("vcfa","","")` |
| legacy `vcfa-vra8` | `Vra8Connector` | `>=8.0,<9.0` | none |

The two bands are **disjoint**, so resolution never reaches a specificity
tie-break (unlike `fleet`, whose bands overlap): an 8.x target has exactly one
in-range versioned candidate (legacy), a 9.x target exactly one (modern). No
re-band of the modern impl was needed.

**The wildcard inversion vs `fleet`.** In the `fleet` case the *legacy* `vcf_fleet`
owns the `("fleet","","")` wildcard; here the *modern* `vcf_automation` owns
`("vcfa","","")` (it shipped first, as the sole impl). So the new legacy `vcfa-vra8`
registers **only** its versioned triple — a second class on the wildcard key would
crash `_eager_import_connectors` at boot. Consequence: an *unfingerprinted* /
unversioned `vcfa` target resolves to **modern** `vcfa-rest`, so a vRA 8 target
**must carry an 8.x product version operator-asserted at onboarding** (e.g.
`--version 8.12`) to resolve to this legacy impl — the fingerprint reports the API
version, not a resolvable product version, so it cannot auto-populate it. The full
resolution matrix is pinned in
[`test_connectors_vra8_dual_impl_resolution.py`](../../backend/tests/test_connectors_vra8_dual_impl_resolution.py).

## Scope: typed read core (#3058)

The connector ships a curated **6-op typed read core** — the migration-inventory
surface — registered as `source_kind="typed"` and **enabled at register time**, so
a vRA 8 target dispatches an inventory read on a **fresh boot with zero catalog
ingest**.

| Group | Ops |
|---|---|
| `vcfa-vra8-inventory` | `vcfa.vra8.project.list`, `.deployment.list`, `.deployment.get`, `.about` |
| `vcfa-vra8-content` | `vcfa.vra8.blueprint.list`, `.catalog-item.list` |

Op ids are namespaced `vcfa.vra8.*` (distinct from the modern impl's
`vcfa.provider.*` / `vcfa.tenant.*`): the two impls publish different read surfaces
and the resolver picks one per target, so an agent only ever sees the set matching
the target's version.

**Why typed, not ingested.** `vmware/vra-sdk-go` (Apache-2.0) *does* publish
machine-readable specs for the vRA 8 services (IaaS / Service Broker / Blueprint /
Project) — but they are **Swagger 2.0**, and MEHO's ingest parser accepts OpenAPI
3.0/3.1 only (#2090). So the surface cannot be operator-ingested as a generic
connector; the read core is **typed** (CLAUDE.md postulate 1), the
`vcf-automation` tenant-plane / `fleet-lcm` / `vcd` posture. There is therefore no
wider `source_kind="ingested"` breadth to follow up: the typed read core **is** the
connector's surface.

**Migration-relevant surface.** The read core covers projects (tenancy/quota),
deployments (running workloads) with a by-id detail read, blueprints (IaC
definitions), catalog items (self-service offerings), and the appliance's API
capabilities. **Requests** are read via `deployment.get`'s embedded `lastRequest` /
`inprogressRequests` (the pre-cutover quiescence signal) rather than a standalone
op — vRA 8 has **no top-level list-all-requests endpoint** (confirmed by
enumerating `vra-catalog-deployment.json`; only `/deployment/api/requests/{id}` and
per-deployment `/requests` exist).

**Live-appliance dispatch verification is the deferred tail.** No vRA 8 lab was
dialled during the build; the read core is unit-tested end-to-end against a
respx-mocked vRA 8 (dispatch `status="ok"` through the two-step session-mint seam)
and manifest-pinned — the same unit-tested-first posture `vcd` / `fleet-lcm`
shipped with.

## Key types

- **`Vra8Connector`** (`connector.py`) — `HttpConnector` subclass. Class
  attributes: `product="vcfa"`, `version="8.0"`, `impl_id="vcfa-vra8"`,
  `supported_version_range=">=8.0,<9.0"`, `priority=1`. `connector_id`
  `"vcfa-vra8-8.0"` parses back to `("vcfa", "8.0", "vcfa-vra8")` (`product` = first
  hyphen-segment of `impl_id`, the round-trip invariant `register_connector_v2`
  enforces).
- **`Vra8TargetLike`** / **`Vra8CredentialsLoader`** (`session.py`) — aliases of the
  shared `VcfTargetLike` Protocol and `VcfCredentialsLoader` type. The loader is
  injectable on construction (`Vra8Connector(credentials_loader=...)`) for unit /
  integration tests. vRA 8's optional CSP `domain` is read via `getattr` (local
  users omit it).
- **`load_credentials_from_vault`** (`session.py`) — the shared operator-context
  KV-v2 read, re-exported. Returns the `{username, password}` pair.
- Canonical constants (`__init__.py`): `VRA8_PRODUCT`, `VRA8_VERSION`,
  `VRA8_IMPL_ID`, `VRA8_CONNECTOR_ID`.

The connector reuses `is_acceptable_auth_model`, `session_establish_auth_error`,
and `ConnectorAuthError` from `meho_backplane.connectors._shared.vcf_auth`.

## Control flow

### Registration

Importing `meho_backplane.connectors.vra8` registers **only** the versioned triple
`register_connector_v2(product="vcfa", version="8.0", impl_id="vcfa-vra8", ...)` —
**not** a wildcard (the modern `vcf_automation` owns `("vcfa","","")`; see the
inversion above). Both the legacy `vra8` package and the modern `vcf_automation`
package register under `product="vcfa"` at eager-import — the `vcf_fleet` +
`fleet_lcm` shape.

### Auth (two-step CSP → IaaS token exchange, single bearer)

vRA 8 mints a bearer via a **two-step** exchange, distinct from VCFA 9's
`/cloudapi/1.0.0/sessions/provider` + `/oauth/*` flow (the divergence that
justifies a separate impl). On first use `auth_headers`:

1. Rejects any `target.auth_model` other than `shared_service_account` / `None` via
   the shared `is_acceptable_auth_model`.
2. Fails closed on an empty `operator.raw_jwt` (a system-initiated call cannot read
   per-target vendor credentials) — enforced **before** the token-cache lookup so a
   primed token can never leak to an unauthenticated caller.
3. Mints the session (`_auth.vra8_login`), two steps:
   - **CSP identity** — `POST /csp/gateway/am/api/login?access_token` with
     `{username, password, domain?}` → a long-lived `refresh_token` (snake_case) in
     the response body.
   - **IaaS token exchange** — `POST /iaas/api/login` with `{refreshToken}`
     (camelCase — note the case flip) → a short-lived (~8h) bearer `token` in a
     `{tokenType, token}` response body.
   A 401/403 at *either* step maps to the structured `ConnectorAuthError`; a
   missing token field on a 2xx raises.
4. Caches the bearer per tenant-unique `(tenant_id, target.id)` key under a lock and
   returns `Authorization: Bearer <token>` (path-independent — the one bearer
   authenticates every vRA 8 service).

Reads add a constant `Accept: application/json` via `_vra_get` (no per-surface
negotiation, unlike `vcd`). A raw 401 (expired bearer) propagates to the
dispatcher's session-recovery arm, which — because the connector advertises an
`invalidate_session` hook (the SDDC Manager / NSX / vcd session-minting precedent,
#2067) — evicts the cached bearer and re-dispatches once, so the retry re-runs the
two-step login. No in-connector retry loop; the base transport's tenacity retry
(5xx / connection only) stays intact for the flaky-legacy-appliance-over-VPN case.
(`invalidate_credentials` is also implemented — the #2396 establish-failure
companion — but the data-path 401 recovery is keyed on `invalidate_session`; a
connector that implements only the former wedges permanently on an expired token,
so the wiring is proven end-to-end in
`test_data_path_401_remints_via_dispatcher`.) The refresh token is not cached
separately — a 401 eviction re-runs both steps, which is rare (only on the ~8h
bearer's expiry).

### Fingerprint / probe

`GET /iaas/api/about` is vRA 8's IaaS API-capabilities endpoint (the same surface
the VCFA tenant probe reads). `fingerprint` reads it **without minting a session**
(pure reachability) and returns a reachable `FingerprintResult` (`vendor="vmware"`,
`product="vcfa"`). `version` is left `None`: `/iaas/api/about` reports the *API*
version (a date label, e.g. `2021-07-15`), not the product build (8.11 vs 8.18),
and the connector does not fabricate a resolvable product version from a date —
dual-impl resolution relies on the **operator-asserted target version** (see
above). `extras["latest_api_version"]` carries the reported label for information.
On any transport/status failure it returns a non-reachable result carrying
`extras["error"]`. `probe()` delegates to `fingerprint()`.

### Dispatch

The typed read-core ops dispatch through `meho_backplane.operations.dispatch`: the
dispatcher resolves the persisted `module.ClassName.method` handler_ref to the
connector's bound-method shim, threads `operator` / `target` / `params`, and the
handler issues an authenticated `GET` via `_vra_get`. `execute()` is the G0.6
ABC-compatibility shim.

## Typed read core internals

The read core follows the `vcd` / `fleet-lcm` layout:

- **`typed_ops.py`** — the `Vra8TypedOp` dataclass table (`VRA8_TYPED_OPS`), the
  per-group `when_to_use` map (`VRA8_TYPED_WHEN_TO_USE_BY_GROUP`), and the
  module-level `register_vra8_typed_operations(*, embedding_service=None)`
  registrar. The list ops declare optional `$top` / `$skip` pagination params;
  `deployment.get` declares a required `deployment_id`.
- **`typed_reads.py`** — the op bodies (`async def vra8_*_impl(connector, operator,
  target, params)`), each issuing `connector._vra_get(...)`; the list reads forward
  `odata_list_query(params)`, and `deployment.get` percent-encodes the id into the
  path.
- **`_llm_instructions.py`** — the per-op `llm_instructions` blobs (`when_to_call` /
  `output_shape` / `next_step`), keyed by op id; framed around inventorying a
  migration-source estate, with explicit pagination guidance.
- **`_paths.py`** — the request-path constants (`_CSP_LOGIN_PATH`,
  `_IAAS_LOGIN_PATH`, `_ABOUT_PATH`, `_PROJECTS_PATH`, `_DEPLOYMENTS_PATH`,
  `_DEPLOYMENT_DETAIL_PATH`, `_BLUEPRINTS_PATH`, `_CATALOG_ITEMS_PATH`) and the
  `odata_list_query` helper. The reconcile lane introspects the `_*_PATH` constants.
- **`_auth.py`** — `vra8_login` (the two-step wire POSTs) + the CSP / IaaS step
  helpers.
- **`connector.py`** — 6 thin bound-method shims (`project_list`, `deployment_list`,
  …) that lazily import and delegate to their `_impl` body.

`register_typed_operation` creates each `OperationGroup` (`review_status=
"enabled"`) and inserts each descriptor `source_kind="typed"`, `is_enabled=True`,
`method=None` / `path=None`. So the read core needs no operator review — it is live
the moment the connector loads.

**Pagination.** vRA 8's list surfaces are server-paged (a `{content: [],
totalElements, numberOfElements, ...}` wrapper). The reads forward `$top` / `$skip`
and each list op's `output_shape` tells the agent to check `totalElements` and page
— a default (first-page) call is never a *silent* truncation because the wrapper
self-describes the total. Sorting/filtering is done through the JSONFlux result
handle (`result_query` / `result_aggregate`), not vendor query params (MEHO
postulate 6); `$orderby` is deliberately not forwarded — vRA 8's casing for it is
not uniform across services (IaaS `$orderBy` vs `$orderby`).

## Spec reconcile lane

[`backend/tests/test_connectors_vra8_spec_reconcile.py`](../../backend/tests/test_connectors_vra8_spec_reconcile.py)
is a **manifest pin + evidenced exclusion** (not a served-op-id compare). A
committable spec *does* exist (`vra-sdk-go` Swagger 2.0), but MEHO's ingest is
OA3-only (#2090) so it is not operator-ingestable, and the CSP login endpoint is
not in the SDK at all. The lane sweeps the connector's live `_*_PATH` constants
(the #2944 introspect-live-constants pattern) and pins the exact hand-coded surface
unconditionally, so a new hand-coded path / renamed constant / method change must
update the manifest consciously. **Strengthening (deferred):** the sibling
`vcf-automation` tenant lane shelf-arms its `/iaas/*` paths against the Swagger 2.0
`vra-iaas.json`; the same could arm this connector's `/iaas` + `/deployment` +
`/blueprint` + `/catalog` paths against the three vra-sdk-go swaggers once shelved.
Full evidence + activation trigger live in the `vcfa-vra8` entry of
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

- **Live dispatch not verified against a real appliance.** No vRA 8 lab was dialled
  during the build; the read core is unit-tested end-to-end (dispatch through the
  two-step session-mint seam against a respx mock) and manifest-pinned. Live
  verification against a vRA 8.x source appliance (and confirming the exact
  `?access_token` bare-flag wire form + the CSP `domain` value for the target
  estate) is the deferred tail.
- **API version is server-default.** The reads pass no `apiVersion` query param, so
  the appliance uses its default (reads are backward-compatible). Pinning a
  per-service `apiVersion` (IaaS `2021-07-15`, Service Broker `2020-08-25`,
  Blueprint `2019-09-12`) is a robustness follow-up.
- **List-focused read core.** Deep by-id reads beyond `deployment.get`, a
  standalone requests surface (vRA 8 has no list-all-requests endpoint — read via
  `deployment.get`), and any write surface are out of scope — a list-focused
  inventory core for migration discovery.
- **Unfingerprinted targets resolve to modern.** Because the modern impl owns the
  `("vcfa","","")` wildcard, a `vcfa` target with no operator-asserted version
  resolves to `vcfa-rest`, not this legacy impl. A vRA 8 source must be onboarded
  with an 8.x `version`.

## References

- Task: <https://github.com/evoila/meho/issues/3058>
- Parent initiative (legacy migration-source coverage):
  <https://github.com/evoila/meho/issues/3056>
- Dual-version / coverage policy of record:
  <https://github.com/evoila/meho/issues/3033>
- Read-core tests:
  [`test_connectors_vra8_auth.py`](../../backend/tests/test_connectors_vra8_auth.py)
  (two-step mint + fingerprint),
  [`test_connectors_vra8_typed_reads.py`](../../backend/tests/test_connectors_vra8_typed_reads.py)
  (dispatch + registration + 401 self-heal),
  [`test_connectors_vra8_dual_impl_resolution.py`](../../backend/tests/test_connectors_vra8_dual_impl_resolution.py)
  (the two-impl resolution matrix), and
  [`test_connectors_vra8_spec_reconcile.py`](../../backend/tests/test_connectors_vra8_spec_reconcile.py)
  (manifest pin).
- Modern sibling impl (same `product="vcfa"`):
  [`connectors-vcf-automation.md`](connectors-vcf-automation.md)
- First two-impl case (the resolver-ladder precedent):
  [`connectors-fleet-lcm.md`](connectors-fleet-lcm.md)
- Typed read-core exemplar: [`connectors-vcd.md`](connectors-vcd.md)
