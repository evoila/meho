# Connector: argocd (ArgoCD 3.x)

## Overview

The `argocd` connector is the hand-rolled `HttpConnector` subclass that
dispatches ArgoCD `argocd-server` REST operations under the
`(product="argocd", version="3.x", impl_id="argocd-api")` registry triple
(plus the `("argocd", "", "")` wildcard fallback). G3.12-T1 (#1390) ships the
skeleton — bearer-token auth, fingerprint, probe, the G0.6 dispatch shim, and
dual registration. The curated read core (`argocd.app.list` /
`argocd.app.get` / `argocd.app.diff` / `argocd.app.resource_tree` /
`argocd.appproject.list` / `argocd.repo.list`) arrives in G3.12-T2; CLI verbs
+ MCP review + recorded-fixture E2E + `docs/cross-repo/argocd-onboarding.md`
arrive in G3.12-T3.

Source: `backend/src/meho_backplane/connectors/argocd/`.

## Key types

- **`ArgoCdConnector`** (`connector.py`) — `HttpConnector` subclass. Class
  attributes: `product="argocd"`, `version="3.x"`, `impl_id="argocd-api"`,
  `supported_version_range=">=2.0,<4.0"`, `priority=1`. The priority outranks
  a future `GenericRestConnector` auto-shim (priority=0) defensively if both
  somehow register for the same triple.
- **`ArgoCdTargetLike`** (`session.py`) — runtime-checkable Protocol capturing
  the minimum target shape the connector reads: `name`, `host`, `port`,
  `secret_ref`, and `auth_model`. Replaced by the concrete `Target` model once
  G0.3 (#224) lands.
- **`ArgoCdCredentialsLoader`** (`session.py`) — async callable type resolving
  a `(target, operator)` pair to `{"token": ...}`. The `operator: Operator`
  carries the dispatched identity so the live loader reads the per-target
  secret under the operator's JWT. Injectable on connector construction
  (`ArgoCdConnector(credentials_loader=...)`) so unit and integration tests
  override the default Vault loader.
- **`load_credentials_from_vault`** (`session.py`) — default loader. Performs a
  live operator-context Vault KV-v2 read of `target.secret_ref` under the
  operator's identity, delegating to the shared `load_basic_credentials` helper
  with `fields=("token",)`. Returns the `{"token": ...}` pair.
- **`ARGOCD_TOKEN_FIELD`** (`session.py`) — the single KV-v2 secret field name
  (`"token"`) an operator stores under `target.secret_ref`. Shared by the
  connector, the loader, and the tests.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order (the `argocd`
   subpackage is auto-discovered by directory name — no manual import-list
   edit needed).
2. Importing `meho_backplane.connectors.argocd` triggers two module-level
   `register_connector_v2` calls: the versioned triple
   `("argocd", "3.x", "argocd-api")` and the wildcard `("argocd", "", "")`.
3. The registry's v2 table now resolves both keys to `ArgoCdConnector`. The
   versioned entry wins the resolver tie-break when both are present; the
   wildcard lets a fresh, unfingerprinted target (`version=None`) resolve to
   the connector through the resolver's `versioned_over_wildcard` step rather
   than 501-ing with `no_connector`.

### Per-target credentials + bearer-token auth

`argocd-server` authenticates with a JWT bearer token sent on every request:
`Authorization: Bearer <token>`. The token is an ArgoCD project/account API
token (`argocd account generate-token` or a `project` token) stored under
`target.secret_ref` as a KV-v2 secret with a `token` field. There is no
username component — unlike Harbor's Basic auth, the credential is a single
opaque string.

1. `ArgoCdConnector.auth_headers(target, operator)` is called. The
   `operator: Operator` is the dispatched identity threaded down from the op
   handler (the operator-context Vault read).
2. `_load_credentials(target, operator)` acquires the per-instance
   `asyncio.Lock`, checks the `_creds_cache` dict (keyed on `target.name`),
   and calls the loader with `(target, operator)` on miss.
3. The loader (default: `load_credentials_from_vault`, which reads the secret
   under the operator's identity; injectable in tests) returns `{"token": ...}`.
4. The result is cached under `target.name` and an `argocd_credentials_loaded`
   log event is emitted (no secret value).
5. `auth_headers` returns `{"Authorization": "Bearer <token>"}`.

The cache fast-path is closed to the synthesised system operator
(`is_system_operator`): a system/operator-less caller always re-runs the loader
so its fail-closed guard applies and can never be served a warm token a real
operator primed (#1008).

### fingerprint()

`GET /api/version` → ArgoCD's `VersionMessage` payload (an unauthenticated
endpoint, so the fingerprint does not depend on a resolvable bearer token —
it works on a freshly-registered target before its Vault secret is
configured). `Version` (e.g. `"v3.3.9+abc1234"`) becomes the canonical
`version`; the bundled build-tool versions land under `extras`:
`BuildDate`, `KustomizeVersion`, `HelmVersion`, `KubectlVersion`. Field names
are the gRPC-gateway-serialized proto field names (PascalCase) from
`server/version/version.proto`.

On transport or status error, returns `FingerprintResult(reachable=False,
extras={"error": "<ExcType>: <message>"})`.

### probe()

`probe()` delegates to `fingerprint()` — the same precedent the SDDC Manager
and NSX connectors established. A reachable fingerprint maps to
`ProbeResult(ok=True)`; an unreachable one carries the fingerprint's
structured `error` string as `reason`. ArgoCD exposes no dedicated composite
health endpoint comparable to Harbor's `/api/v2.0/health`, so the
unauthenticated `GET /api/version` probe is the right reachability surface.

### execute() shim

`execute()` synthesises a system `Operator` with
`sub="system:argocd-api-connector-shim"` and delegates to
`meho_backplane.operations.dispatch(connector_id="argocd-api-3.x", ...)`.
Post-G0.6 callers (CLI verbs, MCP `call_operation`, `/api/v1/operations/call`)
construct a real `Operator` and call `dispatch` directly — they bypass this
shim.

## Dependencies

- **httpx** — async HTTP client with per-target pooling and retry decorator.
  `fingerprint()` uses `_get_json` (retried idempotent GET).
- **tenacity** — retry logic for idempotent GET requests (3 retries,
  exponential backoff, 5xx + connection errors only).
- **structlog** — structured logging for credential load events (no secret
  value).
- **`meho_backplane.connectors.adapters.http.HttpConnector`** — base class
  providing `_get_json`, `_http_client`, and `aclose`.
- **`meho_backplane.connectors._shared.vault_creds`** — `load_basic_credentials`
  (operator-context KV-v2 read, two-phase error contract,
  no-secret-in-logs discipline).
- **`meho_backplane.connectors.schemas`** — `FingerprintResult`, `ProbeResult`,
  `OperationResult`, `AuthModel`.

## Tests

- `tests/test_connectors_argocd_auth.py` — unit tests for bearer-token auth,
  caching, per-target isolation, the system-operator cache bypass, the
  auth_model boundary gate, and the fingerprint/probe shapes against mocked
  `argocd-server` endpoints.
- `tests/test_connectors_argocd_credread.py` — recorded-fixture E2E proving
  the full `dispatch -> loader -> ArgoCD` chain returns `status="ok"` with the
  live (non-injected) default loader against the in-process Vault fake +
  respx-mocked ArgoCD, plus the canary-token leak assertions.
- `tests/test_connectors_registry_v2.py::test_argocd_connector_registered_under_v2_triple_and_wildcard`
  — asserts the dual registration resolves.

## Known issues

- This Task ships **zero operations** — `execute(target, op_id, ...)` against
  any `op_id` resolves to "unknown operation" at the dispatcher layer, the
  correct behaviour for a registered-but-empty connector at this stage. The
  curated read core lands in G3.12-T2.
- The write ops (`argocd.app.sync` / `app.rollback` / `app.set`, gated by the
  approval queue) and any full-Swagger L2 ingest are deferred follow-up Tasks
  under Initiative #1387, per Hard rule 2 (no speculative optionality).
- The fingerprint surfaces only the four build-tool fields the operator
  cares about (`BuildDate`, `KustomizeVersion`, `HelmVersion`,
  `KubectlVersion`); the full `VersionMessage` (`GitCommit`, `GitTag`,
  `GoVersion`, `Platform`, `JsonnetVersion`, `ExtraBuildInfo`) is available on
  the wire but intentionally not surfaced.

## References

- Issues: #1390 (G3.12-T1 skeleton). Initiative: #1387 (G3.12 argocd
  connector). Goal: #214 (connector parity / wrapper retirement).
- HttpConnector base: `backend/src/meho_backplane/connectors/adapters/http.py`
- Shared Vault loader: `backend/src/meho_backplane/connectors/_shared/vault_creds.py`
- ArgoCD API: https://argo-cd.readthedocs.io/en/stable/developer-guide/api-docs/
- `VersionMessage` proto: `argoproj/argo-cd` `server/version/version.proto`
- Precedents: `connectors/harbor/` (bearer-vs-Basic single-vendor-REST shape,
  cred loader + cache + system-operator bypass), `connectors/bind9/` (dual
  registration), `connectors/nsx/` (probe delegates to fingerprint).
