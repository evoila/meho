# Connector: nsx (NSX 4.x + VCF-9 9.x)

## Overview

The `nsx` connector is the hand-rolled `HttpConnector` subclass that
dispatches NSX REST operations under the
`(product="nsx", version="9.0", impl_id="nsx-rest")` registry triple.
G3.5-T1 (#613) shipped the skeleton -- session-cookie / XSRF auth,
fingerprint, probe, and the G0.6 dispatch shim. G3.5-T2 (#614) added an
ingested read-enable surface for the NSX read core. T7 (#2358, Initiative
#2266) retired that hand-curated apparatus, leaving the remaining reads as
generic ingested breadth enabled through `ReviewService.enable_reads`. CLI
verbs + MCP review + recorded-fixture E2E arrive in G3.5-T3 (#615).

**Audited reads are typed ops (#2302).** The audited operational read
set -- node/cluster status+version, backup config+status,
transport-zones list, tier-1 list, and alarms -- is registered as
**typed** ops (`source_kind="typed"`, `typed_ops.py` metadata +
`typed_reads.py` bodies + bound-method shims on `NsxConnector`), so it
dispatches on a fresh boot with **zero catalog ingest** (avoiding the
#2247 per-deploy catalog-state failure class). The remaining reads
(transport-node listing, segments, tier-0 gateways, distributed-firewall
policies + rules) stay as ordinary `source_kind="ingested"` breadth, enabled
generically via `ReviewService.enable_reads`, so the wider ingested breadth
remains browsable. `nsx.backup.config` is
first-class for the disk-fill incident class (Broadcom KB 442696 shape):
it surfaces `backup_enabled` + `passphrase_configured` + the
`backup_schedule` / `remote_file_server` retention-relevant fields, and
scrubs the backup passphrase + any nested SFTP credential at the boundary
(the default redaction policy masks `password`/`secret` but not
`passphrase`). `tier-1 gateway create` (a write) is out of scope -- the
first write on a read-only connector is its own approval-gated G3.x
write-surface initiative.

**Session recovery (#2067).** `NsxConnector.invalidate_session(target)`
is the public duck-typed hook the generic dispatch path calls on an
auth-class status (NSX's 401) before re-dispatching once. The typed reads
issue `_get_json` directly, so a raw 401 propagates to the dispatcher's
#2067 recovery arm, which evicts the cached session and re-dispatches --
the same seam the vmware-rest / vcf-logs connectors expose. The internal
`_get_json_with_session_retry` helper still serves the fingerprint /
probe path (which the dispatcher does not drive).

**VCF-9 version renumber (#1530).** NSX-T 4.x was renumbered onto the
VCF train at VCF 9.0 -- a live VCF-9 appliance reports NSX 9.0.x and
the vendor spec carries `info.version` in the 9.x scheme (observed
`9.1.0.0`). The class pin tracks the VCF-9-aligned `"9.0"` line and
`supported_version_range` was widened to `">=4.0,<10.0"` so a single
class covers both the standalone NSX-T 4.x line and the VCF-9 9.x
line. Dispatch and the ingest version-range pre-flight key on the
`SpecifierSet`, not the class pin, so the one class resolves every
label in the band. Same renumber posture `VmwareRestConnector` took
for the vSphere 8.x -> 9.0 jump.

Source: `backend/src/meho_backplane/connectors/nsx/`.

## Key types

- **`NsxConnector`** (`connector.py`) -- `HttpConnector` subclass.
  Class attributes: `product="nsx"`, `version="9.0"`,
  `impl_id="nsx-rest"`, `supported_version_range=">=4.0,<10.0"`,
  `priority=1`. The version pin tracks the VCF-9-aligned product line
  (#1530); the widened range keeps the standalone NSX-T 4.x line
  dispatchable through the same class. The priority outranks a future
  `GenericRestConnector` auto-shim (priority=0) defensively if both
  somehow register for the same triple.
- **`NsxTargetLike`** (`session.py`) -- runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `host`, `port`, `secret_ref`, `auth_model`. Replaced by the
  concrete `Target` model once G0.3 (#224) lands; the model
  satisfies the Protocol structurally without code edits here.
- **`NsxSessionLoader`** (`session.py`) -- async callable type
  resolving a target to `{"username": ..., "password": ...}`.
  Injectable on connector construction
  (`NsxConnector(session_loader=...)`) so unit tests, integration
  tests, and pre-G0.3 production deploys override the default Vault
  loader.
- **`load_session_credentials_from_vault`** (`session.py`) -- default
  loader, stubbed `NotImplementedError` until G0.3 lands the
  operator-context Vault read path. Mirrors the
  `load_session_credentials_from_vault` shape in
  `connectors/vmware_rest/`.
- **`NSX_PRODUCT` / `NSX_VERSION` / `NSX_IMPL_ID` / `NSX_CONNECTOR_ID`**
  (`__init__.py`) -- the `(product, version, impl_id)` identity constants
  plus the derived `connector_id` slug (`"nsx-rest-9.0"`). `NsxConnector`
  pins the same triple as class attributes; the constants give acceptance /
  typed-read / VCF-9-ingest tests one importable source of truth. Relocated
  here from the retired `core_ops` module in #2358.
- **Ingested browse breadth** -- the reads outside the typed set
  (transport-node listing, segments, tier-0 gateways, distributed-firewall
  policies + rules, and the wider NSX catalog) land as ordinary
  `source_kind="ingested"` `endpoint_descriptor` rows via G0.7 spec ingestion
  and are enabled through the generic review flow
  (`ReviewService.enable_reads`). The hand-curated ingested-enable apparatus
  (`core_ops.py`, with its `NSX_CORE_OPS` / `NSX_CORE_GROUPS` /
  `NSX_PATH_RULES` constants and the `classify_nsx_op` /
  `apply_nsx_core_curation` helpers) was retired in #2358 (T7 of #2266);
  read enablement is now generic, with no per-product curation code.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.nsx` triggers the
   module-level
   `register_connector_v2(product="nsx", version="9.0", impl_id="nsx-rest", cls=NsxConnector)`
   call.
3. The registry's v2 table now resolves `("nsx", "9.0", "nsx-rest")`
   to `NsxConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.
   The resolver binds a target by `supported_version_range`
   membership, so a 4.x or 9.x fingerprinted target both bind here.

### Per-target session

NSX auth diverges from the vSphere precedent: the canonical FQDN
behind the VCF 9 envoy proxy rejects HTTP Basic, and the session
endpoint accepts only form-encoded credentials. The flow:

1. `NsxConnector.auth_headers(target)` is called for the first time
   against `target`.
2. `_session_token(target)` acquires the per-instance `asyncio.Lock`,
   checks the `self._session_tokens` cache (keyed on `target.name`),
   misses, calls the injected `session_loader(target)` -> resolves
   to `{"username": ..., "password": ...}`.
3. `_session_token` POSTs to `/api/session/create` with
   `data={"j_username": ..., "j_password": ...}` -- `httpx 0.28.x`'s
   `client.post(url, data=<dict>)` sends
   `application/x-www-form-urlencoded`.
4. The response's `Set-Cookie: JSESSIONID=...` header is captured
   into the per-target `httpx.AsyncClient.cookies` jar automatically
   -- httpx calls `self.cookies.extract_cookies(response)` on every
   response, so subsequent requests through the same client carry
   the cookie without manual plumbing.
5. The response's `X-XSRF-TOKEN` header is captured into
   `self._session_tokens[target.name]`.
6. `auth_headers()` returns `{"X-XSRF-TOKEN": <cached>}`. The
   `JSESSIONID` cookie travels via the client jar; subsequent
   requests through the same per-target client get both
   transparently.

### 401 -> re-login -> retry-once

`_get_json_with_session_retry(target, path)` wraps the inherited
`HttpConnector._get_json`. The inherited method carries tenacity's
`@retry(retry_if_exception(_retryable))` decorator that excludes 4xx
responses, so a 401 propagates cleanly to the wrapper.

1. First call to `_get_json` succeeds -> return.
2. First call raises `httpx.HTTPStatusError(401)` ->
   `_invalidate_session(target)` acquires the lock, drops the cached
   XSRF token, and clears the per-target client cookie jar.
3. Second call to `_get_json` re-establishes the session via
   `auth_headers -> _session_token` (cache-miss path) and re-tries
   the GET.
4. Second call succeeds -> return; second call raises 401 ->
   wrapper raises
   `RuntimeError("nsx session re-login failed for target ...")`.
5. Any non-401 status error propagates untouched -- relogin would
   mask transient backend failures.

The single retry posture (not a loop) matches the consumer wrapper
in `scripts/nsx.sh` so a misconfigured credential pair fails fast
instead of hammering NSX's audit log.

### Fingerprint + probe

- `fingerprint(target)` issues `GET /api/v1/node` through
  `_get_json_with_session_retry`. On success: returns
  `FingerprintResult(vendor="vmware", product="nsx",
  version=<node_version>, build=<kernel_version>, reachable=True,
  extras={"node_uuid", "hostname", "external_id"})`. On transport,
  HTTP-status, or session-establish failure: returns
  `reachable=False` with `extras["error"] = "<ExcType>: <message>"`.
- `probe(target)` delegates to `fingerprint`. The issue body
  permits an alternative `GET /api/v1/cluster/status` call; the
  delegation path is chosen for parity with `VmwareRestConnector`
  -- one auth round-trip already covers both reachability and
  auth-challenge.

### Dispatch shim

`execute(target, op_id, params)` synthesises a minimal `Operator`
(nil-UUID tenant_id + `sub="system:nsx-rest-connector-shim"`) and
delegates to `meho_backplane.operations.dispatch` with
`connector_id="nsx-rest-9.0"`. Pre-G0.6 chassis routes that still
invoke `Connector.execute` directly reach the dispatcher through
this shim; post-G0.6 callers (the `/api/v1/operations/call` route,
MCP `call_operation`, the CLI verbs once #615 lands) construct a
real `Operator` and call `dispatch` themselves.

### Shutdown

`aclose()` clears `self._session_tokens` and delegates to
`HttpConnector.aclose()` which closes every per-target httpx
client. No `DELETE /api/session/destroy` is issued -- NSX's session
has a documented idle timeout, and a per-target network call during
lifespan shutdown is more risk than benefit (a hung DELETE on an
unreachable target can exceed Kubernetes' default 30 s
`terminationGracePeriod`, which is configurable per Pod spec but
typically left at the default). Revoke-on-close is a v0.2.next
concern, same posture vSphere takes for proactive refresh.

## Dependencies

- **httpx 0.28.x** -- per-target `AsyncClient` pool (inherited from
  `HttpConnector`); `client.post(url, data=<dict>)` for the
  form-encoded session create; automatic cookie-jar management for
  `JSESSIONID`.
- **tenacity 9.x** -- the inherited `@retry` decorator on
  `HttpConnector._request_json` retries connection errors and 5xx
  responses up to four attempts with exponential backoff; 4xx
  (including 401) propagates cleanly to the connector-level retry
  layer.
- **pydantic 2.13.x** -- `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models with `MappingProxyType` -wrapped
  `extras`; the connector constructs them by keyword.
- **respx 0.23.x (test-only)** -- the unit-test module mocks every
  request shape (form-encoded body, Set-Cookie response, X-XSRF-TOKEN
  response, 401 / 502 status sequences) without a network call.
- **structlog** -- a single `nsx_session_established` info event per
  successful session create; no other emit points in this skeleton.

## Ingested breadth + read enablement

The audited operational reads are typed ops (see the Overview) and dispatch on
a fresh boot with zero catalog state. The remaining reads (transport-node
listing, segments, tier-0 gateways, distributed-firewall policies + rules) and
the wider NSX catalog land as ordinary `source_kind="ingested"`
`endpoint_descriptor` rows after a G0.7 ingest of the NSX `policy.yaml` +
`manager.yaml` corpus (every row `is_enabled=False` until enabled).

Enablement is generic: the operator enables the reads through the review flow
— `ReviewService.enable_reads(connector_id, tenant_id=...)` (REST
`POST /api/v1/connectors/{connector_id}/enable-reads`, MCP
`meho.connector.enable_reads`). Ingested ops land under the
**operator-supplied** `version` label, so a VCF-9 spec ingested as
`version="9.1.0.0"` produces `connector_id="nsx-rest-9.1.0.0"` rather than the
class pin's `nsx-rest-9.0` (#1530); the operator passes the `connector_id` the
ingest actually produced.

The hand-curated ingested-enable apparatus (the `core_ops.py` module with its
`NSX_CORE_OPS` / `NSX_CORE_GROUPS` / `NSX_PATH_RULES` constants and the
`classify_nsx_op` / `apply_nsx_core_curation` helpers) was retired in #2358
(T7 of #2266); there is no longer any per-product curation code.

## Known issues

- The full G0.7 ingest of the NSX `policy.yaml` + `manager.yaml` is
  operator-driven via `meho connector ingest` (the runbook lives at
  `docs/cross-repo/g35-nsx-canary.md`). The env-gated canary
  acceptance test that automates the live two-spec ingest in CI is a
  follow-up — it requires the NSX spec-shelf wired to the
  meho-runners-ci pool (same env-gated pattern
  `tests/acceptance/_vcenter_spec.py` codifies for vSphere). Until
  then, the dispatch leg is exercised against seeded ingested
  descriptors in `tests/acceptance/_nsx_canary_fixtures.py`.
- Default session loader raises `NotImplementedError`. Production
  callers must inject `session_loader=...` on construction until
  G0.3 (#224) lands the operator-context Vault read path. Mirrors
  the `vmware_rest` precedent; both connectors pick up the live
  implementation in a single follow-up commit once G0.3 merges.
- Session-revoke on `aclose()` is deliberately omitted. NSX's idle
  timeout (~30 min for NSX Manager) reclaims abandoned sessions
  naturally; the cost of a hung DELETE during lifespan shutdown
  outweighs the audit-log neatness of an explicit revoke.

## References

- Issues: [G3.5-T1 #613](https://github.com/evoila/meho/issues/613)
  (skeleton); [G3.5-T2 #614](https://github.com/evoila/meho/issues/614)
  (ingested read-enable surface, retired in #2358);
  [G3.5-T3 #615](https://github.com/evoila/meho/issues/615)
  (CLI verbs + E2E recorded-fixture tests + operator onboarding doc).
- Parent Initiative: [G3.5 #368](https://github.com/evoila/meho/issues/368).
- Parent Goal: [G3 #214](https://github.com/evoila/meho/issues/214).
- CLI verbs: `cli/internal/cmd/nsx/` — thin Cobra layer over
  `POST /api/v1/operations/call` for the 9 core ops + `operation
  search/call` meta-tools; mirrors `cli/internal/cmd/vmware/`.
- Operator runbooks:
  - `docs/cross-repo/g35-nsx-canary.md` — ingest + curate + enable +
    smoke procedure for an operator standing up NSX against a fresh
    deploy.
  - `docs/cross-repo/nsx-onboarding.md` — `meho nsx …` verb reference
    + `scripts/nsx.sh` → `meho nsx` per-ticket wrapper-flip recipe.
- Integration test: `backend/tests/test_connectors_nsx_e2e.py` —
  combined E2E covering all 9 ops, session-establish, 401-retry,
  audit rows, and JSONFlux handle path; runs in the `meho-runners-ci` CI
  lane with no Docker dependency.
- Acceptance tests:
  - `backend/tests/acceptance/test_g35_nsx_dispatch_smoke.py` —
    dispatch the 9 NSX core ops against a respx-mocked NSX REST
    surface; one parametrised case per op.
  - `backend/tests/acceptance/test_g35_nsx_jsonflux_force_handle.py`
    — install a test-only `ForceHandleReducer`, dispatch the
    segment-list op, assert `OperationResult.handle` is populated
    by the dispatcher seam.
- Precedent: `connectors/vmware_rest/connector.py` (session auth +
  fingerprint + probe + dispatch shim);
  `connectors/vmware_rest/__init__.py` (registration);
  `connectors/adapters/http.py` (`HttpConnector`);
  `connectors/base.py` (`Connector` ABC);
  `connectors/registry.py:108` (`register_connector_v2`).
- Consumer wrapper this contract mirrors: `scripts/nsx.sh` in the
  consumer's `claude-rdc-hetzner-dc` repository (private to the
  `evoila-bosnia` org; the wrapper is the source of truth for the
  form-encoded `j_username`/`j_password` + `X-XSRF-TOKEN` flow).
- NSX REST API reference -- official documentation is hosted under
  Broadcom's developer portal at `developer.broadcom.com` (the
  exact version-pinned URL has shifted since the VMware-by-Broadcom
  domain consolidation; search "NSX REST API guide" from the portal
  root rather than hard-coding a path here).
