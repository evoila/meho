# Connector: tempo (Grafana Tempo 2.x)

## Overview

The `tempo` connector is a hand-rolled `HttpConnector` subclass that dispatches
read-only Grafana Tempo HTTP query-API operations under the
`(product="tempo", version="2.x", impl_id="tempo-api")` registry triple (plus
the `("tempo", "", "")` wildcard fallback for a fresh, unfingerprinted target).
It brings the tracing half of an LGTM observability stack inside the MEHO
dispatch → policy-gate → audit seam, so an agent can run TraceQL trace triage
through the same governed surface every other connector uses — the third pillar
alongside `loki-api` (#2235, logs) and `prometheus-api` (#2234, metrics). Filed
as #2903 against a consumer signal (`tempo-connector-absent-traces-outside-audit-plane`):
trace reads previously went out-of-band through the Grafana datasource proxy,
so they produced no audit rows and no target registration.

The connector is **read-only by construction** — every operation issues a GET,
and the generic passthrough is gated to the `/api` read surface — and
**multi-tenant** — each op accepts an optional `tenant` selector that renders
Tempo's `X-Scope-OrgID` header per call, while the readiness probe and
fingerprint stay tenant-free. Tempo has no operator-facing write API (ingest is
an OTLP push from collectors on the distributor, not a query-frontend surface),
so no write op ships.

Source: `backend/src/meho_backplane/connectors/tempo/`.

## Key types

- **`TempoConnector`** (`connector.py`) — `HttpConnector` subclass. Class
  attributes: `product="tempo"`, `version="2.x"`, `impl_id="tempo-api"`,
  `supported_version_range=">=2.0,<3.0"`, `priority=1`. The priority outranks a
  future `GenericRestConnector` auto-shim (priority 0) if both somehow register
  for the same triple.
- **`TempoOp`** (`ops.py`) — frozen dataclass carrying one op's registration
  metadata (op id, handler attr, schemas, group key, safety level, tags,
  `llm_instructions`). `TEMPO_OPS` is the tuple the registrar walks;
  `TEMPO_WHEN_TO_USE_BY_GROUP` supplies the per-group `when_to_use` blurb the
  registration helper requires (groups `tempo-search` / `tempo-metadata` /
  `tempo-metrics`).
- **`assert_tempo_read_only` / `TempoReadOnlyError`** (`read_only.py`) — the
  pure read-only gate (no I/O) and its violation type. Raised for a non-GET
  method or a path outside `/api`.
- **`TempoTenantRequiredError`** (`connector.py`) — raised when a query returns
  `401` and no `tenant` was supplied (Tempo's multi-tenant "no org id" case),
  so the operator gets an actionable "pass a tenant" message rather than a bare
  401 passthrough.

## Control flow

### Registration (two-phase, mirrors loki/prometheus)

- **Import time** — `tempo/__init__.py` calls `register_connector_v2` twice
  (versioned triple + wildcard). `_eager_import_connectors` discovers the
  subpackage by directory name, so no central import-list edit is needed. The
  connector's `product="tempo"` also enters the `TargetCreate.product` OpenAPI
  enum via `registered_product_tokens()` (regenerated CLI snapshot at
  `cli/api/openapi.json` + Go client `cli/internal/api/client.gen.go`).
- **Lifespan** — `register_tempo_typed_operations` (queued via
  `register_typed_op_registrar`) delegates to
  `TempoConnector.register_operations`, which upserts the six descriptors into
  `endpoint_descriptor`. Idempotent across restarts.

### Dispatch

An op dispatches through `meho_backplane.operations.dispatch`, which resolves
the connector, runs the policy gate, validates params, and invokes the bound
handler `(operator, target, params)`. Each read handler:

1. builds the query dict from `params` (`_forward` copies present, non-None
   keys),
2. calls `_tempo_get`, which runs `assert_tempo_read_only("GET", path)` first
   (so a bad path never reaches the wire), renders `tenant` into the
   `X-Scope-OrgID` header when set, issues the retried GET via the base
   `_request_json`, and translates a tenant-less `401` into
   `TempoTenantRequiredError`.

Ops and their endpoints:

| Op | Endpoint | Group |
|----|----------|-------|
| `tempo.search` | `GET /api/search` (TraceQL `q` or tag filters) | `tempo-search` |
| `tempo.trace` | `GET /api/traces/{trace_id}` | `tempo-search` |
| `tempo.search_tags` | `GET /api/v2/search/tags` | `tempo-metadata` |
| `tempo.search_tag_values` | `GET /api/v2/search/tag/{tag}/values` | `tempo-metadata` |
| `tempo.metrics_query_range` | `GET /api/metrics/query_range` | `tempo-metrics` |
| `tempo.get` | GET passthrough under `/api` | `tempo-search` |

The v2 tags/tag-values endpoints are used deliberately (the consumer's request):
scoped tag names and typed tag values. `tempo.metrics_query_range` requires the
target's metrics-generator local-blocks processor; it errors on a Tempo without
it. The instant metrics form (`GET /api/metrics/query`) is intentionally not a
curated op — it is reachable through `tempo.get` if ever needed.

### Read-only gate (no push/delete blocklist, unlike loki)

`assert_tempo_read_only` permits only `GET` and only paths under `/api`. It
carries **no** segment blocklist, and deliberately so: Tempo exposes no
state-changing endpoint that is both a `GET` and under `/api`. Its lifecycle
mutators (`/flush`, `/shutdown`) are `POST` and live outside `/api` entirely, so
the path scope already excludes them; the tenant-overrides writes under
`/api/overrides` are `POST`/`PATCH`/`DELETE`, refused by the method gate. Loki
needs its extra `/push` + `/delete*` blocklist because `GET /loki/api/v1/delete`
(a read that lists pending deletes) sits inside its read prefix; Tempo has no
analogous GET-reachable write surface.

### Auth (optional)

`auth_headers` returns `{}` when `target.secret_ref is None` — the common
unauthenticated port-forward case on `:3200`. When `secret_ref` is set, the
stored KV-v2 secret selects the scheme: a `token` field →
`Authorization: Bearer <token>`; `username` + `password` →
`Authorization: Basic <base64>`. A configured secret carrying neither shape
raises `VaultCredentialsReadError`. This is the explicit "auth optional when
`secret_ref is None`" branch — op execution otherwise fails closed on an
unresolved credential.

### Fingerprint / probe (tenant-free, unauthenticated)

`fingerprint()` reads `GET /api/status/buildinfo` (`version`, `revision`,
`branch`, `goVersion`), `GET /ready` (readiness flag), and a best-effort
`GET /api/echo` (`echo_ok` query-path liveness boolean, `None` on transport
failure). All three go through `_unauth_get`, which hits the pooled client
directly (no auth, no tenant), so the fingerprint works on a freshly registered
target before any secret exists — the loki `buildinfo` precedent. `vendor` is
`grafana` (Tempo is a Grafana product, like loki). `probe()` uses the
tenant-free `GET /ready` (`200 "ready"` → ok; `503`/transport error → not ok
with a reason).

### Wire-path pinning + spec-reconcile follow-up

`backend/tests/test_connectors_tempo.py` includes `test_each_curated_op_hits_its_exact_api_path`,
which runs every curated op's handler against a `_tempo_get`-recording subclass
and pins the exact `/api` path each puts on the wire (the passthrough is
excluded — it hand-codes no vendor path). This is the always-on half of the
spec-reconcile-guard standard (`docs/decisions/spec-reconcile-guards-standard.md`,
initiative #2979): a handler that grows a new inline path or stops funnelling
through `_tempo_get` fails the guard. The fingerprint/probe literals are pinned
separately (`test_fingerprint_probe_literals_are_pinned`), since they travel the
`_unauth_get` seam.

The **shelf-backed** reconcile lane — asserting the declared paths against a
pinned vendor artifact on the `tempo-2.x` spec shelf — is a follow-up, mirroring
the loki split (connector #2235 / reconcile lane #2991): Grafana Tempo publishes
no OpenAPI for its HTTP API, so the lane would pin the vendor's documented API
reference (`docs/sources/tempo/api_docs/_index.md`, AGPL-3.0) on a `tempo-2.x`
shelf dir in the private consumer spec-shelf repo (`evoila-bosnia/claude-rdc-hetzner-dc`,
`docs/tempo-2.x/`) and extract its route list. That shelf pin lives in a
separate repo and arms the dormant lane, exactly as the vcf-fleet lane awaits
its shelf pin.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`) — pooled `httpx.AsyncClient`,
  retry policy, SSRF guard, TLS trust, and the `_request_json` seam whose
  `extra_headers` carries `X-Scope-OrgID`.
- `_shared/vault_creds.py` — `load_vault_secret_data` + `strip_credential_value`
  for the optional Bearer/Basic credential read.
- `operations/typed_register.py` — `register_typed_operation` and the registrar
  queue.

## Scheme

Tempo's native API is plaintext HTTP (the port-forward case), so `_base_url`
defaults to `http`. A TLS-fronted Tempo is reached by setting
`extras={"scheme": "https"}` on the target — the single per-product field the
base `Target` model does not carry as a column, held in the forward-compat
`extras` bag. The port is appended unless it is the scheme default (80/443).

## Known issues

- `ops.py` (~594 lines) and `connector.py` (~527 lines) sit above the
  code-quality warn threshold (400) but below the block threshold (600); the op
  metadata is verbose by nature. Split if either approaches 600.
- `tempo.metrics_query_range` requires the target's metrics-generator
  local-blocks processor; against a Tempo without it, the op returns an error
  rather than an empty series.
- The shelf-backed spec-reconcile lane is not yet armed (see above) — the
  in-PR guard pins wire paths but does not reconcile against a pinned vendor
  reference until `tempo-2.x` is pinned on the consumer shelf.

## References

- Tempo HTTP API: <https://grafana.com/docs/tempo/latest/api_docs/>
- TraceQL: <https://grafana.com/docs/tempo/latest/traceql/>
- Multi-tenancy (`X-Scope-OrgID`):
  <https://grafana.com/docs/tempo/latest/operations/multitenancy/>
- Sibling read-only connectors: `docs/codebase/connectors-loki.md`,
  `docs/codebase/connectors-prometheus.md`.
- Spec-reconcile standard: `docs/decisions/spec-reconcile-guards-standard.md`;
  shelf provisioning `docs/decisions/vendor-spec-ci-provisioning.md`.
- Task #2903 (Connector request: read-only Tempo query surface).
