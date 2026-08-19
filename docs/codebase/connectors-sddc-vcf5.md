# Connector: sddc-vcf5 (VCF 5.x SDDC Manager, legacy dual-impl of `sddc`)

## Overview

The `sddc-vcf5` connector is the hand-rolled `HttpConnector` subclass that makes
**VCF 5.x SDDC Manager** (the SDDC Manager tier of a VMware Cloud Foundation 5.x
estate) dispatchable under the `(product="sddc", version="5.0", impl_id="sddc-vcf5")`
registry triple. It is the **legacy dual-impl** of `product="sddc"` filed under the
legacy migration-source connector-coverage initiative
([#3056](https://github.com/evoila/meho/issues/3056), task #3059): VCF 5.x is a
migration **source** estate evoila brings customers *off*, so MEHO must be able to
read and inventory it during discovery + onboarding.

Source: `backend/src/meho_backplane/connectors/sddc_vcf5/`.

**VCF 5.x is the same product line as VCF 9.x, one major generation back.** The
modern impl is [`connectors-sddc-manager.md`](connectors-sddc-manager.md)
(`sddc-rest`, `>=9.0,<10.0`); this is a second implementation of `product="sddc"`,
fingerprint-resolved per target — the third real two-impl case (after `fleet` and
`vcfa`).

## Dual-impl resolution (`sddc` = third two-impl case)

| Impl | Class | Band | Wildcard |
|---|---|---|---|
| modern `sddc-rest` | `SddcManagerConnector` | `>=9.0,<10.0` | **owns** `("sddc","","")` |
| legacy `sddc-vcf5` | `SddcVcf5Connector` | `>=5.0,<9.0` | none |

The bands are **disjoint**, so resolution never reaches a specificity tie-break. The
**modern** `sddc_manager` owns the `("sddc","","")` wildcard (it shipped first), so
`sddc-vcf5` registers **only** its versioned triple — the *inversion* of the `fleet`
case. Consequence: an *unfingerprinted* `sddc` target resolves to modern. **In
practice this is a non-issue**: SDDC Manager exposes a *real* product version at
`GET /v1/sddc-managers` (e.g. `"5.2.0.0-24276214"`), so a VCF 5.x target fingerprints
straight into `>=5.0,<9.0` and resolves to the legacy impl without an
operator-asserted version — unlike `vcfa-vra8`, whose fingerprint yields only an
API-date label. The matrix is pinned in
[`test_connectors_sddc_vcf5_dual_impl_resolution.py`](../../backend/tests/test_connectors_sddc_vcf5_dual_impl_resolution.py).

## Why a separate impl (not just widen the modern band)

SDDC Manager's REST *path* surface is **stable since VCF 4.0** — the `/v1/*` reads
here are the same shapes the modern connector reads. The split is driven by **spec
format + schema drift**:

- **Spec format.** VCF 5.x publishes only a **Swagger 2.0** definition
  (appliance-served at `/ui/assets/spec/external/swagger.json`, non-conformant —
  missing `info.version`). OpenAPI 3.x is a **VCF-9.0-net-new** deliverable
  (`vmware/vcf-api-specs` is tagged 9.0/9.1 only — no 5.x). MEHO's ingest parser is
  OA3-only (#2090), so 5.x cannot be operator-ingested — hence a **typed** read core
  (the `vcd` / `vcfa-vra8` posture), not a generic spec-ingested connector.
- **Schema drift.** Response fields differ by point release (e.g. `ipAddress`
  deprecated in 5.2 in favour of `fqdn`), so the 9.x connector's schemas cannot
  faithfully serve a 5.x target.

## Scope: typed read core (#3059)

A curated **7-op typed read core** — the VCF 5.x migration-inventory surface —
registered as `source_kind="typed"` and enabled at register time, so a VCF 5.x
target dispatches an inventory read on a **fresh boot with zero catalog ingest**.

| Group | Ops |
|---|---|
| `sddc-vcf5-inventory` | `sddc.vcf5.domain.list`, `.cluster.list`, `.host.list`, `.vcenter.list`, `.nsxt_cluster.list`, `.manager.list` |
| `sddc-vcf5-tasks` | `sddc.vcf5.task.list` |

Op ids are namespaced `sddc.vcf5.*` (distinct from the modern impl's `sddc.*`); the
resolver picks one impl per target, so an agent only sees the set matching the
target's version. The set covers the physical + logical VCF footprint a migration
must re-home (domains → clusters → hosts → vCenters → NSX-T fabric → the SDDC Manager
appliance itself) plus workflow tasks (the pre-cutover quiescence check).

**Deliberately out of scope** (vs the modern connector's 14 ops): the credential-read
(`GET /v1/credentials`, secret-bearing) and the network-pool host-commissioning
reads — this core inventories a migration *source*, it does not operate it.

## Key types

- **`SddcVcf5Connector`** (`connector.py`) — `HttpConnector` subclass.
  `product="sddc"`, `version="5.0"`, `impl_id="sddc-vcf5"`,
  `supported_version_range=">=5.0,<9.0"`, `priority=1`. `connector_id`
  `"sddc-vcf5-5.0"` parses back to `("sddc", "5.0", "sddc-vcf5")`.
- Target shape + credential loader are **reused from the modern sibling**
  (`meho_backplane.connectors.sddc_manager.session`: `SddcTargetLike`,
  `SddcCredentialsLoader`, `load_credentials_from_vault`) — the credential contract
  is product-level and version-agnostic, so a bespoke duplicate would only drift.
- Canonical constants (`__init__.py`): `SDDC_VCF5_PRODUCT`, `SDDC_VCF5_VERSION`,
  `SDDC_VCF5_IMPL_ID`, `SDDC_VCF5_CONNECTOR_ID`.

## Control flow

### Registration

Importing `meho_backplane.connectors.sddc_vcf5` registers **only** the versioned
triple — **not** a wildcard (the modern `sddc_manager` owns `("sddc","","")`; a
second class on that key would crash `_eager_import_connectors` at boot). Both the
legacy and modern packages register under `product="sddc"` at eager-import — the
`vcf_fleet` + `fleet_lcm` shape.

### Auth (token session, identical to the modern 9.x flow)

Token auth is VCF 4.0+, so 5.x and 9.x share it. SDDC Manager rejects HTTP Basic
outright and mints a bearer via `POST /v1/tokens` with a `{username, password}` JSON
body, returning `accessToken`. On first use `auth_headers`:

1. Rejects any `target.auth_model` other than `shared_service_account` / `None`.
2. Fails closed on an empty `operator.raw_jwt` (a system-initiated call cannot read
   per-target vendor credentials) — enforced **before** the token-cache lookup so a
   primed token can never leak to an unauthenticated caller.
3. Mints the session via the shared
   `meho_backplane.connectors._shared.vcf_auth.vcf_session_login` helper (the same
   helper the modern `sddc_manager` connector uses) — a non-2xx / token-less 2xx
   raises the target-named `SessionLoginError`.
4. Caches the token per tenant-unique `(tenant_id, target.id)` key and returns
   `Authorization: Bearer <accessToken>`.

A raw 401 on a **data** read propagates to the dispatcher's session-recovery arm,
which — because the connector advertises an `invalidate_session` hook (#2067) —
evicts the cached token and re-dispatches once, so the retry re-mints. The base
transport's tenacity retry (5xx / connection) stays intact. (`invalidate_credentials`
is also implemented — the #2396 establish-failure companion.) The 401 self-heal is
proven end-to-end in `test_data_path_401_remints_via_dispatcher`.

### Fingerprint / probe

SDDC Manager has **no unauthenticated version endpoint**, so `fingerprint` reads
`GET /v1/sddc-managers` **on the token session** (via `_get_json_with_session_retry`,
which the dispatcher does not drive — so it evicts + re-mints + retries once on its
own 401) and takes `elements[0]`. `version` carries the full VCF version string
(e.g. `"5.2.0.0-24276214"`) — a *real* product version, so a VCF 5.x target resolves
to this impl by fingerprint alone. An operator-less probe (no JWT) fails closed and
reports unreachable; the ABC `probe(target)` — which has no operator — therefore
always reports not-ok, and the real reachability check is `fingerprint(target,
operator)` via the probe routes (the modern `sddc_manager` posture).

### Dispatch

The typed read ops dispatch through `meho_backplane.operations.dispatch`; the handler
issues a plain `self._get_json` on the token session (dispatcher-hook 401 recovery).
`execute()` is the G0.6 ABC-compatibility shim.

## Typed read core internals

Follows the modern `sddc_manager` layout:

- **`typed_ops.py`** — the `SddcVcf5TypedOp` dataclass table, the per-group
  `when_to_use` map, inline per-op `llm_instructions` (`when_to_use` / `output_shape`
  / `parameter_hints`, the SDDC-family key shape), and the
  `register_sddc_vcf5_typed_operations` registrar.
- **`typed_reads.py`** — the op bodies (`async def sddc5_*_impl(...)`), each issuing
  `connector._get_json(...)`; the filtered reads forward `domainId` / `clusterId` /
  `status` via `_optional_query`. Holds the `_*_PATH` constants (incl. `_TOKENS_PATH`
  for the mint) the reconcile lane introspects.
- **`connector.py`** — the connector class + auth helpers + 7 bound-method shims.

## Spec reconcile lane

[`backend/tests/test_connectors_sddc_vcf5_spec_reconcile.py`](../../backend/tests/test_connectors_sddc_vcf5_spec_reconcile.py)
is a **manifest pin + evidenced exclusion**: 5.x publishes only Swagger 2.0
(OA3-only ingest can't consume it, and OA3 is VCF-9.0-net-new — no 5.x artifact). The
lane sweeps the connector's live `_*_PATH` constants and pins the exact hand-coded
surface unconditionally. **Strengthening (deferred):** the `/v1/*` paths are stable
since VCF 4.0, so these 5.x paths could be armed against the modern connector's
vendored 9.x OA3 (`vmware/vcf-api-specs` `sddc-manager-openapi.json`) — path existence
is version-stable even though response schemas drift. Full evidence + activation
trigger are in the `sddc-vcf5` entry of
[`spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` — pooled per-target client,
  tenacity 5xx/connection retry, `_get_json`.
- `meho_backplane.connectors._shared.vcf_auth` — `vcf_session_login`,
  `SessionLoginError`, `is_acceptable_auth_model`.
- `meho_backplane.connectors.sddc_manager.session` — `SddcTargetLike`,
  `SddcCredentialsLoader`, `load_credentials_from_vault` (reused).
- `meho_backplane.connectors._shared.{cache_key,vault_creds}`,
  `meho_backplane.connectors.schemas`.
- `httpx`, `respx` (test-only). No new runtime deps.

## Known issues / follow-ups

- **Live dispatch not verified against a real appliance.** No VCF 5.x lab was dialled
  during the build; the read core is unit-tested end-to-end (dispatch through the
  token-mint seam against a respx mock) and manifest-pinned. Live verification
  against a VCF 5.x source appliance (and confirming the 5.2 `ipAddress`→`fqdn`
  schema drift in the returned records) is the deferred tail.
- **List-focused inventory core.** Deep by-id reads, the credential-read surface, and
  any write surface are out of scope — a source-inventory core, not an operations
  surface.
- **Unfingerprinted targets resolve to modern.** Because the modern impl owns the
  wildcard, a `sddc` target with no fingerprint/version resolves to `sddc-rest`. In
  practice a probed VCF 5.x target fingerprints its real version and resolves here.

## References

- Task: <https://github.com/evoila/meho/issues/3059>
- Parent initiative (legacy migration-source coverage):
  <https://github.com/evoila/meho/issues/3056>
- Dual-version / coverage policy of record:
  <https://github.com/evoila/meho/issues/3033>
- Read-core tests:
  [`test_connectors_sddc_vcf5_auth.py`](../../backend/tests/test_connectors_sddc_vcf5_auth.py)
  (token mint + authenticated fingerprint),
  [`test_connectors_sddc_vcf5_typed_reads.py`](../../backend/tests/test_connectors_sddc_vcf5_typed_reads.py)
  (dispatch + registration + 401 self-heal),
  [`test_connectors_sddc_vcf5_dual_impl_resolution.py`](../../backend/tests/test_connectors_sddc_vcf5_dual_impl_resolution.py)
  (the two-impl resolution matrix), and
  [`test_connectors_sddc_vcf5_spec_reconcile.py`](../../backend/tests/test_connectors_sddc_vcf5_spec_reconcile.py)
  (manifest pin).
- Modern sibling impl (same `product="sddc"`):
  [`connectors-sddc-manager.md`](connectors-sddc-manager.md)
- Migration-source typed read-core exemplars:
  [`connectors-vra8.md`](connectors-vra8.md), [`connectors-vcd.md`](connectors-vcd.md)
