# Connector: argocd (ArgoCD 3.x)

## Overview

The `argocd` connector is the hand-rolled `HttpConnector` subclass that
dispatches ArgoCD `argocd-server` REST operations under the
`(product="argocd", version="3.x", impl_id="argocd-api")` registry triple
(plus the `("argocd", "", "")` wildcard fallback). G3.12-T1 (#1390) ships the
skeleton — bearer-token auth, fingerprint, probe, the G0.6 dispatch shim, and
dual registration. G3.12-T2 (#1391) layers the curated read core on top:
`argocd.app.list` / `argocd.app.get` / `argocd.app.diff` /
`argocd.app.resource_tree` / `argocd.appproject.list` / `argocd.repo.list` —
all `safety_level="safe"`, `requires_approval=False`, read-only. G3.12-T3
(#1392) closes the connector out: the ops are operator-usable via the
per-connector `meho argocd …` Cobra verb tree (`cli/internal/cmd/argocd/`,
pre-baking `connector_id="argocd-api-3.x"`, mirroring the keycloak/harbor/nsx
siblings) or the equivalent generic `meho operation call argocd-api-3.x
<op_id> …` verb, and agent-usable via the `search_operations` /
`call_operation` meta-tools (the verb tree is operator-only — no per-op MCP
tools, CLAUDE.md postulate 5), with a recorded-fixture E2E
([`tests/test_connectors_argocd_e2e.py`](../../backend/tests/test_connectors_argocd_e2e.py))
and the operator runbook
[`docs/cross-repo/argocd-onboarding.md`](../cross-repo/argocd-onboarding.md).

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
- **`ArgoCdOp`** + **`ARGOCD_OPS`** (`ops.py`) — the typed-op metadata dataclass
  and the six-entry registration tuple (the curated read core). Mirrors the
  bind9 `Bind9Op` / `BIND9_OPS` shape: one frozen dataclass per op carrying the
  `register_typed_operation` kwargs plus a `handler_attr` naming the bound
  method on `ArgoCdConnector`. **`ARGOCD_WHEN_TO_USE_BY_GROUP`** maps each op's
  `group_key` (`argocd-apps` / `argocd-projects` / `argocd-repos`) to its
  curated `when_to_use` blurb.
- **`register_argocd_typed_operations`** (`__init__.py`) — the lifespan-driven
  registrar (queued via `register_typed_op_registrar`) that delegates to
  `ArgoCdConnector.register_operations()`.

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

### Curated read ops (G3.12-T2)

Six read-only ops are upserted into `endpoint_descriptor` at lifespan startup
by `register_argocd_typed_operations` → `ArgoCdConnector.register_operations()`,
which walks `ARGOCD_OPS` and routes each row through `register_typed_operation`
(idempotent across pod restarts — re-embed is skipped on unchanged text). Each
handler is a thin bound method on `ArgoCdConnector` (`app_list` / `app_get` /
`app_diff` / `app_resource_tree` / `appproject_list` / `repo_list`) that the
dispatcher binds to the per-process instance and threads `operator` into; the
handler forwards `operator` to `_get_json` so the bearer token is read under
the operator's identity.

| op_id | endpoint | returns |
|---|---|---|
| `argocd.app.list` | `GET /api/v1/applications` (opt. `projects[]`, `selector`) | `{items, metadata}` — apps + sync/health status |
| `argocd.app.get` | `GET /api/v1/applications/{name}` (opt. `project`) | one app's full spec + status |
| `argocd.app.diff` | `GET /api/v1/applications/{name}/managed-resources` | `{items: [ResourceDiff]}` — `liveState`/`targetState` per resource |
| `argocd.app.resource_tree` | `GET /api/v1/applications/{name}/resource-tree` | `{nodes, orphanedNodes, hosts, shardsCount}` |
| `argocd.appproject.list` | `GET /api/v1/projects` | `{items, metadata}` — AppProjects + allow-lists |
| `argocd.repo.list` | `GET /api/v1/repositories` | `{items, metadata}` — repos + connectionState |

`argocd.app.diff` is the API-level equivalent of `argocd app diff <app>`: the
managed-resources delta where each item's `liveState` (cluster) is compared
against `targetState` (Git-rendered desired). The per-app ops URL-encode the
`name` param into the path (so a slash in the name stays one segment) and
forward the optional `project` scoping query. `app.list` serialises `projects`
as repeated query pairs (the gRPC-gateway repeated-field shape).

### Approval-gated write ops (G3.12-T4)

Seven mutating ops layer onto the same registrar walk (`register_operations`
now iterates `(*ARGOCD_OPS, *ARGOCD_WRITE_OPS)`). Every one registers
`requires_approval=True`. The op metadata lives in `ops_write_schemas.py`
(`ARGOCD_WRITE_OPS` + `ARGOCD_WHEN_TO_USE_WRITE_BY_GROUP`, write group keys
carry a `-write` suffix so they never collide with the read groups); the
handler bodies live in `ops_write.py`; `ArgoCdConnector` exposes thin
bound-method shims (`app_sync` / `app_rollback` / `app_set` / `app_refresh`
/ `app_delete` / `appproject_create` / `appproject_update`) the registrar
resolves `handler_attr` against.

| op_id | safety | endpoint | notes |
|---|---|---|---|
| `argocd.app.sync` | dangerous | `POST /api/v1/applications/{name}/sync` | polls `status.operationState.phase` to terminal (Succeeded/Failed/Error); returns final phase + message + synced revision |
| `argocd.app.rollback` | dangerous | `POST .../{name}/rollback` | int64 history `id`; same operationState poll |
| `argocd.app.set` | dangerous | `PUT .../{name}/spec` | snapshots spec before/after into `proposed_effect` |
| `argocd.app.refresh` | caution | `GET .../{name}?refresh=hard` | governed replacement for the hand-run `kubectl annotate … refresh=hard`; under selfHeal can trigger an auto-sync |
| `argocd.app.delete` | dangerous | `DELETE .../{name}?cascade=…` | snapshots the managed resource tree (cascade list) into `proposed_effect` |
| `argocd.appproject.create` | dangerous | `POST /api/v1/projects` | `{project, upsert}` — tenancy boundary |
| `argocd.appproject.update` | dangerous | `PUT /api/v1/projects/{name}` | snapshots project spec before/after into `proposed_effect` |

**Approval routing (no hard-deny, no bypass).** The connector only sets the
`requires_approval=True` descriptor flag; the dispatcher's `policy_gate`
(`_validate.py`) does the rest. Since G11.7-T1 (#1401) a `USER`-principal
dispatch of a `requires_approval` op routes to the human approve-queue
(`status=awaiting_approval` + an `approval_request_id` in `extras`) rather
than hard-denying; an agent dispatch is floored to `needs-approval`
regardless of safety level. The handler functions therefore execute only on
the `_approved=True` resume path the approvals API sets after a human
approves.

**operationState polling.** `app.sync` / `app.rollback` POST a SyncOperation
that ArgoCD runs asynchronously, so the handlers mirror
`vmware.composite.vm.clone`'s task-poll: after the POST they poll
`GET .../applications/{name}` until `status.operationState.phase` reaches a
terminal value (the `TERMINAL_OPERATION_PHASES` set = Succeeded ∪ Failed ∪
Error, matching gitops-engine's `OperationPhase.Completed()`), or a bounded
`poll_timeout_seconds` (default 300) elapses — on timeout they return the
last-observed phase with `timed_out=true`.

**proposed_effect snapshots.** `app.set` / `appproject.update` read the
current spec before the PUT and return `{before_spec, after_spec}` under a
`proposed_effect` key; `app.delete` reads the resource tree before the
DELETE and returns the cascade list. These ride in the op result (the
post-approval execution evidence the reviewer/auditor sees).

The **same** snapshot is also computed at **park time** — before any human
approves — via the per-op preview-builder hook (G11.7 follow-up #1452, on
the #1437 dispatcher seam). `ops_write_preview.py` registers a builder for
`argocd.app.set`, `argocd.app.delete`, and `argocd.appproject.update`; each
reuses the read-only snapshot helpers the handlers use (`_read_app_spec` /
`_read_cascade_resources` / `_read_project_spec`), issuing **only** the read
GETs (`GET .../applications/{name}`, `.../resource-tree`, `GET
.../projects`) and never the mutating PUT/DELETE. The result lands in the
durable `ApprovalRequest.proposed_effect` (wrapped `{op_class, preview}` by
the hook) so the reviewer reads the diff / cascade in the approval queue
*before* approving. `after_spec` at park time is the proposed spec the
parked dispatch carried in `params`; the in-result `after_spec` is the
argocd-server-accepted spec echoed by the PUT response, which only exists
post-approval. The ops with no natural read-only preview (`app.sync` /
`app.rollback` / `app.refresh` / `appproject.create`) register no builder
and park with the identifier-only default `create_pending_request` computes
from params.

Writes go through the connector's `_write_json` helper (POST/PUT/DELETE,
**not** retried — a side-effecting verb must not silently re-fire on a 5xx),
which the base `HttpConnector._request_json` deliberately refuses for
non-idempotent methods.

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
- `tests/test_connectors_argocd_reads.py` — the G3.12-T2 read-core suite: each
  of the six ops dispatched live through `dispatch` against respx-mocked
  `argocd-server` (right path + bearer token + payload), `app.diff` returns the
  managed-resources drift, query-param plumbing (`projects`/`selector`/`project`
  + name URL-encoding), `search_operations` visibility, and the `ARGOCD_OPS`
  registration-shape invariants (all `safe`/no-approval/`read-only`, no write op).
- `tests/test_connectors_argocd_e2e.py` — the G3.12-T3 recorded-fixture E2E:
  each of the six ops dispatched through the `call_operation` meta-tool (the
  same narrow-waist surface the MCP server and the `meho operation call` CLI
  verb reach) against a respx-mocked `argocd-server`, resolving a real
  DB-seeded `targets` row through `resolve_target` and a stub bearer-token
  loader preseeded into the dispatcher instance cache; asserts the Vault-sourced
  bearer rides every request, the token never leaks into the envelope, and all
  six ops are visible to `search_operations`.
- `tests/test_connectors_argocd_write_e2e.py` — the G3.12-T4 write-op suite:
  the seven write ops' registration + safety-level/`requires_approval`
  invariants, the approve-queue routing of an un-approved `USER` dispatch
  (`awaiting_approval`, handler never fires), the `app.sync`/`rollback`
  operationState poll to a terminal phase, the `app.set`/`delete`/`appproject.update`
  `proposed_effect` snapshots, and the bearer-rides-never-leaks guarantee.

## Known issues

- Any full-Swagger L2 ingest of the ArgoCD API is a deferred follow-up Task
  under Initiative #1387, per Hard rule 2 (no speculative optionality). The
  curated read core (G3.12-T2) + approval-gated write surface (G3.12-T4) are
  the bounded operator-facing set.
- The fingerprint surfaces only the four build-tool fields the operator
  cares about (`BuildDate`, `KustomizeVersion`, `HelmVersion`,
  `KubectlVersion`); the full `VersionMessage` (`GitCommit`, `GitTag`,
  `GoVersion`, `Platform`, `JsonnetVersion`, `ExtraBuildInfo`) is available on
  the wire but intentionally not surfaced.

## References

- Issues: #1390 (G3.12-T1 skeleton), #1391 (G3.12-T2 curated read core),
  #1392 (G3.12-T3 CLI/MCP review + recorded-fixture E2E + onboarding doc).
  Initiative: #1387 (G3.12 argocd connector). Goal: #214 (connector parity /
  wrapper retirement).
- HttpConnector base: `backend/src/meho_backplane/connectors/adapters/http.py`
- Shared Vault loader: `backend/src/meho_backplane/connectors/_shared/vault_creds.py`
- ArgoCD API: https://argo-cd.readthedocs.io/en/stable/developer-guide/api-docs/
- `VersionMessage` proto: `argoproj/argo-cd` `server/version/version.proto`
- Precedents: `connectors/harbor/` (bearer-vs-Basic single-vendor-REST shape,
  cred loader + cache + system-operator bypass), `connectors/bind9/` (dual
  registration), `connectors/nsx/` (probe delegates to fingerprint).
