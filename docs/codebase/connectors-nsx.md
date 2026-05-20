# Connector: nsx (NSX 4.x)

## Overview

The `nsx` connector is the hand-rolled `HttpConnector` subclass that
dispatches ingested NSX REST operations under the
`(product="nsx", version="4.2", impl_id="nsx-rest")` registry triple.
G3.5-T1 (#613) shipped the skeleton -- session-cookie / XSRF auth,
fingerprint, probe, and the G0.6 dispatch shim. G3.5-T2 (#614) adds
the **operator-review curation substrate**: the 9 read-only core
ops + per-op `llm_instructions` blobs + group-level `when_to_use`
hints + the `apply_nsx_core_curation` helper that the operator
review step calls against the G0.7-ingested connector. CLI verbs +
MCP review + recorded-fixture E2E arrive in G3.5-T3 (#615).

Source: `backend/src/meho_backplane/connectors/nsx/`.

## Key types

- **`NsxConnector`** (`connector.py`) -- `HttpConnector` subclass.
  Class attributes: `product="nsx"`, `version="4.2"`,
  `impl_id="nsx-rest"`, `supported_version_range=">=4.0,<5.0"`,
  `priority=1`. The priority outranks a future
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
- **`NSX_CORE_OPS`** (`core_ops.py`) -- frozen tuple of the 9
  curated read-only NSX core ops (`NsxCoreOp` dataclass:
  `op_id` + `group_key` + `llm_instructions` blob). The
  operator-review pass on a G0.7-ingested NSX connector flips
  exactly these ops to `is_enabled=True`; every other op the
  spec ingestion produced stays `is_enabled=False`.
- **`NSX_CORE_GROUPS`** (`core_ops.py`) -- frozen tuple of the 8
  `NsxCoreGroup` entries (`group_key` + `name` + `when_to_use`)
  the read-only core spans. One entry per LLM-grouping-pass output
  group; two ops share the `policy-firewall` group_key.
- **`NSX_PATH_RULES`** (`core_ops.py`) -- path-prefix to group_key
  classifier rules, same shape `_PathPrefixStubLlmClient._PATH_RULES`
  in the G0.7 vSphere canary uses. First match wins; order is
  most-specific-first so e.g. `/policy/api/v1/infra/tier-0s`
  doesn't fall into a broader future catch-all.
- **`apply_nsx_core_curation`** (`core_ops.py`) -- async helper that
  drives `ReviewService.edit_group` + `enable_group` + `edit_op`
  (the new `llm_instructions=` keyword from G3.5-T2) against the
  ingested connector. Idempotent — re-runnable during a rollout
  or test rerun without duplicate audit rows.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.nsx` triggers the
   module-level
   `register_connector_v2(product="nsx", version="4.2", impl_id="nsx-rest", cls=NsxConnector)`
   call.
3. The registry's v2 table now resolves `("nsx", "4.2", "nsx-rest")`
   to `NsxConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.

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
`connector_id="nsx-rest-4.2"`. Pre-G0.6 chassis routes that still
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

## Operator-review curation flow (G3.5-T2)

The `core_ops.py` constants land the operator-review side of the
G0.7 ingest. After a G0.7 ingest of `nsx-4.2/policy.yaml` +
`manager.yaml` lands the full NSX descriptor set in the
`endpoint_descriptor` table (every row `is_enabled=False`,
`source_kind='ingested'`), the operator runs:

```python
from meho_backplane.connectors.nsx import apply_nsx_core_curation
from meho_backplane.operations.ingest import ReviewService

review_service = ReviewService(operator)
await apply_nsx_core_curation(review_service, tenant_id=None)
```

The helper drives the substrate through three substrate calls per
group (`edit_group` for the operator-reviewed `name` /
`when_to_use`, `enable_group` to flip `review_status='enabled'` and
cascade child ops to `is_enabled=True`) plus one `edit_op` per
curated op carrying the `llm_instructions` blob. Every other op
the spec ingestion produced stays `is_enabled=False`, matching the
"~9-op read-only core enabled, everything else staged" acceptance
criterion in #614.

The `ReviewService.edit_op(..., llm_instructions=...)` keyword is
the G3.5-T2 substrate extension to a method that previously only
covered `custom_description` / `safety_level` / `requires_approval`
/ `is_enabled`. The new field is persistent verbatim, audited as a
`fields_updated` entry without echoing the blob into the payload
(operator-authored prose belongs out of the audit table, same
posture `edit_group` takes for `when_to_use`).

## Known issues

- The full G0.7 ingest of `nsx-4.2/policy.yaml` + `manager.yaml` is
  operator-driven via `meho connector ingest` (the runbook lives at
  `docs/cross-repo/g35-nsx-canary.md`). The env-gated canary
  acceptance test that automates the live two-spec ingest in CI is a
  follow-up — it requires the NSX spec-shelf wired to the
  meho-runners-ci pool (same env-gated pattern
  `tests/acceptance/_vcenter_spec.py` codifies for vSphere). Until
  then, the dispatch leg is exercised against `NSX_CORE_OPS`-seeded
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
  (core-ops curation + `edit_op(llm_instructions=)` substrate);
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
