# Connector: loki (Grafana Loki 3.x)

## Overview

The `loki` connector is a hand-rolled `HttpConnector` subclass that dispatches
read-only Grafana Loki HTTP-API operations under the
`(product="loki", version="3.x", impl_id="loki-api")` registry triple (plus the
`("loki", "", "")` wildcard fallback for a fresh, unfingerprinted target). It
brings the logs half of an LGTM observability stack inside the MEHO
dispatch → policy-gate → audit seam, so an agent can run LogQL triage through
the same governed surface every other connector uses (#2235, under Initiative
#2228).

The connector is **read-only by construction** — every operation issues a GET,
and the generic passthrough is gated to the `/loki/api/v1` read surface with an
explicit `/push` + `/delete*` blocklist — and **multi-tenant** — each op accepts
an optional `tenant` selector that renders Loki's `X-Scope-OrgID` header per
call, while the readiness probe and fingerprint stay tenant-free.

Source: `backend/src/meho_backplane/connectors/loki/`.

## Key types

- **`LokiConnector`** (`connector.py`) — `HttpConnector` subclass. Class
  attributes: `product="loki"`, `version="3.x"`, `impl_id="loki-api"`,
  `supported_version_range=">=2.9,<4.0"`, `priority=1`. The priority outranks a
  future `GenericRestConnector` auto-shim (priority 0) if both somehow register
  for the same triple.
- **`LokiOp`** (`ops.py`) — frozen dataclass carrying one op's registration
  metadata (op id, handler attr, schemas, group key, safety level, tags,
  `llm_instructions`). `LOKI_OPS` is the tuple the registrar walks;
  `LOKI_WHEN_TO_USE_BY_GROUP` supplies the per-group `when_to_use` blurb the
  registration helper requires.
- **`assert_loki_read_only` / `LokiReadOnlyError`** (`read_only.py`) — the pure
  read-only gate (no I/O) and its violation type. Raised for a non-GET method,
  a path outside `/loki/api/v1`, or a path whose segments name the `push`
  ingest or any `delete*` endpoint.
- **`LokiTenantRequiredError`** (`connector.py`) — raised when a query returns
  `401` and no `tenant` was supplied (Loki's `auth_enabled` "no org id" case),
  so the operator gets an actionable "pass a tenant" message rather than a bare
  401 passthrough.

## Control flow

### Registration (two-phase, mirrors argocd/pfsense)

- **Import time** — `loki/__init__.py` calls `register_connector_v2` twice
  (versioned triple + wildcard). `_eager_import_connectors` discovers the
  subpackage by directory name, so no central import-list edit is needed. The
  connector's `product="loki"` also enters the `TargetCreate.product` OpenAPI
  enum via `registered_product_tokens()` (regenerated CLI snapshot at
  `cli/api/openapi.json`).
- **Lifespan** — `register_loki_typed_operations` (queued via
  `register_typed_op_registrar`) delegates to
  `LokiConnector.register_operations`, which upserts the six descriptors into
  `endpoint_descriptor`. Idempotent across restarts.

### Dispatch

An op dispatches through `meho_backplane.operations.dispatch`, which resolves
the connector, runs the policy gate, validates params, and invokes the bound
handler `(operator, target, params)`. Each read handler:

1. builds the query dict from `params` (`_forward` copies present, non-None
   keys),
2. calls `_loki_get`, which runs `assert_loki_read_only("GET", path)` first (so
   a bad path never reaches the wire), renders `tenant` into the
   `X-Scope-OrgID` header when set, issues the retried GET via the base
   `_request_json`, and translates a tenant-less `401` into
   `LokiTenantRequiredError`.

Ops: `loki.query`, `loki.query_range`, `loki.labels`, `loki.label_values`,
`loki.series`, and the gated `loki.get` passthrough.

### Auth (optional)

`auth_headers` returns `{}` when `target.secret_ref is None` — the common
unauthenticated port-forward case. When `secret_ref` is set, the stored KV-v2
secret selects the scheme: a `token` field → `Authorization: Bearer <token>`;
`username` + `password` → `Authorization: Basic <base64>`. A configured secret
carrying neither shape raises `VaultCredentialsReadError`. This is the explicit
"auth optional when `secret_ref is None`" branch — op execution otherwise fails
closed on an unresolved credential.

### Fingerprint / probe (tenant-free, unauthenticated)

`fingerprint()` reads `GET /loki/api/v1/status/buildinfo` (`version`,
`revision`, `branch`, `goVersion`), `GET /ready` (readiness flag), and a
best-effort `GET /loki/api/v1/labels` (`label_count`, `None` when the tenant-free
read 401s on an `auth_enabled` Loki). All three go through `_unauth_get`, which
hits the pooled client directly (no auth, no tenant), so the fingerprint works
on a freshly registered target before any secret exists — the argocd
`/api/version` precedent. `probe()` uses the tenant-free `GET /ready`
(`200 "ready"` → ok; `503`/transport error → not ok with a reason).

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`) — pooled `httpx.AsyncClient`,
  retry policy, SSRF guard, TLS trust, and the `_request_json` seam whose
  `extra_headers` carries `X-Scope-OrgID`.
- `_shared/vault_creds.py` — `load_vault_secret_data` + `strip_credential_value`
  for the optional Bearer/Basic credential read.
- `operations/typed_register.py` — `register_typed_operation` and the registrar
  queue.

## Scheme

Loki's native API is plaintext HTTP (the port-forward case), so `_base_url`
defaults to `http`. A TLS-fronted Loki is reached by setting
`extras={"scheme": "https"}` on the target — the single per-product field the
base `Target` model does not carry as a column, held in the forward-compat
`extras` bag. The port is appended unless it is the scheme default (80/443).

## Known issues

- `connector.py` (~520 lines) and `ops.py` (~550 lines) sit above the
  code-quality warn threshold (400) but below the block threshold (600); the op
  metadata is verbose by nature. Split if either approaches 600.
- `label_count` in the fingerprint is best-effort: it is `None` against an
  `auth_enabled` Loki because the tenant-free `/labels` read 401s there.

## References

- Loki HTTP API: <https://grafana.com/docs/loki/latest/reference/loki-http-api/>
- Multi-tenancy (`X-Scope-OrgID`):
  <https://grafana.com/docs/loki/latest/operations/multi-tenancy/>
- Sibling read-only connector: `docs/codebase/connectors-argocd.md`.
- Task #2235; Initiative #2228 (data-tier + hypervisor connector coverage).
