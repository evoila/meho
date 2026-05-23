# Connector: gcloud (GCP REST)

## Overview

The `gcloud` connector is the hand-rolled `HttpConnector` subclass that
dispatches GCP REST operations under the
`(product="gcloud", version="1.0", impl_id="gcloud-rest")` registry triple.
G3.7-T4 (#845) ships the skeleton — ADC+impersonation auth, SA-JSON-key
refusal, fingerprint, probe, and the G0.6 dispatch shim. G3.7-T5 (#848)
ships the 8 read-only typed ops (cloudresourcemanager, compute, iam,
serviceusage). G3.7-T6 (#851) adds the `meho gcloud ...` CLI verbs,
gated integration tests, and `docs/cross-repo/gcloud-onboarding.md`.

Source: `backend/src/meho_backplane/connectors/gcloud/`.

**Transport decision:** Option B — HttpConnector + `google-auth` ADC +
impersonated service-account credentials. Recorded as decision #12 in
`docs/planning/v0.2-decisions.md`. Option A (SubprocessConnector wrapping
`gcloud` CLI) was rejected.

**Org-policy constraint:** `constraints/iam.disableServiceAccountKeyCreation`
is active on the consumer's GCP organization. The connector encodes this
constraint: SA JSON key material in any Vault `secret_ref` payload is
refused before any token is built. This is not a soft warning — it raises
`ValueError` and no token is returned.

## Typed ops (G3.7-T5 #848)

Eight read-only ops registered via `register_gcloud_typed_operations()`
at lifespan startup. All ops have `safety_level="safe"` and
`requires_approval=False`. They land in the `endpoint_descriptor` table
under `connector_id="gcloud-rest-1.0"`.

| Op ID | API surface | HTTP verb | Notes |
|---|---|---|---|
| `gcloud.about` | CRM v1 `projects.get` | GET | Identity summary; wraps `fingerprint()` |
| `gcloud.project.describe` | CRM v1 `projects.get` | GET | Full CRM resource dict |
| `gcloud.services.list` | Service Usage v1 | GET | Follows `nextPageToken`; `enabled_only` param |
| `gcloud.iam.service_accounts.list` | IAM v1 | GET | Follows `nextPageToken` |
| `gcloud.compute.instances.list` | Compute v1 `aggregatedList` | GET | JSONFlux-compatible `rows`+`total`; `zone` param |
| `gcloud.compute.networks.list` | Compute v1 global networks | GET | Follows `nextPageToken` |
| `gcloud.compute.subnetworks.list` | Compute v1 `aggregatedList` | GET | Follows `nextPageToken`; `region` param |
| `gcloud.iam.policy.read` | CRM v1 `getIamPolicy` | POST | Returns `version`, `etag`, `bindings` |

Groups and `when_to_use` blurbs are defined in `_WHEN_TO_USE_BY_GROUP`
in `connector.py`: `identity`, `project`, `services`, `iam`, `compute`.

### JSONFlux handle note

`gcloud.compute.instances.list` returns a `rows`+`total` envelope
compatible with the future JSONFlux reducer. In v0.2 the `PassThroughReducer`
never populates `OperationResult.handle` — the full row list is inlined.
The reducer (a separate Initiative) will spill large lists to MinIO/S3
and return a `ResultHandle` without any connector-side changes.

## Key types

- **`GcloudConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="gcloud"`, `version="1.0"`,
  `impl_id="gcloud-rest"`, `priority=1`. Auth via Google ADC +
  `impersonated_credentials.Credentials`; per-target token cache;
  auto-refresh on 401 via `refresh_token()`.
- **`GcloudOp`** (`ops.py`) — frozen dataclass holding metadata for one
  typed op. Fields mirror `register_typed_operation()` kwargs:
  `op_id`, `handler_attr`, `summary`, `description`, `parameter_schema`,
  `response_schema`, `group_key`, `tags`, `safety_level`,
  `requires_approval`, `llm_instructions`.
- **`GCLOUD_OPS`** (`ops.py`) — tuple of all 8 `GcloudOp` entries.
  `register_gcloud_typed_operations()` walks this tuple.
- **`GcloudTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `gcp_project`, `gcp_impersonate_sa`, `secret_ref`, and `auth_model`.
  `target.host` is intentionally unused — GCP REST is reached via
  well-known public hostnames. Replaced by the concrete `Target` model
  once G0.3 (#224) lands.
- **`GcloudCredentialsLoader`** (`session.py`) — async callable type
  resolving a target to its Vault-stored credential record. Injectable
  on connector construction (`GcloudConnector(credentials_loader=...)`)
  so unit tests, integration tests, and pre-G0.3 deployments override
  the default Vault loader. The record is checked for SA-JSON-key fields
  before use — the loader does NOT validate; the connector validates.
- **`load_credentials_from_vault`** (`session.py`) — default loader,
  stubbed `NotImplementedError` until G0.3 lands the operator-context
  Vault read path (Goal #214).
- **`_SA_KEY_FIELDS`** (`session.py`) — `frozenset` of field names that
  identify a service-account JSON key file. The connector's
  `_gate_sa_key_refusal()` intersects the Vault record's keys against
  this set on every `auth_headers()` call.
- **`_contains_sa_key_fields(record)`** (`session.py`) — pure function
  returning `True` if `record` contains any SA key field names.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.gcloud` triggers the module-level
   `register_connector_v2(product="gcloud", version="1.0", impl_id="gcloud-rest", cls=GcloudConnector)` call.
3. The registry's v2 table resolves `("gcloud", "1.0", "gcloud-rest")` to
   `GcloudConnector`. The G0.7 auto-shim's idempotency check no-ops on
   subsequent ingests against the same triple.

### Auth flow per request

1. **auth_model gate** — `auth_headers()` checks `target.auth_model` is
   `IMPERSONATION` or `None`. Any other value → `NotImplementedError`.
2. **SA-JSON-key refusal gate** — loads the Vault `secret_ref` payload
   via the injectable `credentials_loader`; checks for SA key field names.
   Any present → `ValueError` naming the target + fields.
3. **Token resolution** — calls `_ensure_token(target)`:
   - Fast path: cached token + `creds.valid` → return cached.
   - Slow path (under per-target lock): calls `_fetch_token(target)` which:
     - Runs `_fetch_token_sync(target)` in a thread pool executor.
     - `_fetch_token_sync` calls `google.auth.default()` for ADC source
       credentials, wraps with `impersonated_credentials.Credentials`,
       calls `creds.refresh(Request())`, returns `(token, creds)`.
     - Stores token + creds in `_token_cache` / `_creds_cache`.
4. Returns `{"Authorization": "Bearer <token>"}`.

### 401 auto-refresh

GCP REST APIs return HTTP 401 when a bearer token has expired.
`_get_json_abs(target, abs_url)`:
- Issues the GET with the current bearer token.
- On 401: calls `refresh_token(target)` (acquires per-target lock, calls
  `creds.refresh()` in executor, updates cache) and retries once.
- Any subsequent non-2xx → `httpx.HTTPStatusError`.

### fingerprint()

`GET https://cloudresourcemanager.googleapis.com/v1/projects/<gcp_project>`
via `_get_json_abs`. Returns:
- `vendor="google"`, `product="gcp-project"`, `version=None`.
- `extras["project_number"]`, `extras["lifecycle_state"]`,
  `extras["organization"]` (from `parent.id` when `parent.type="organization"`),
  `extras["project_id"]`.
- On failure: `reachable=False`, `extras["error"]`.

### probe()

Same endpoint as `fingerprint()`. Verifies:
- ADC source credentials are present and valid.
- Impersonation chain succeeds (Token Creator role granted).
- Cloud Resource Manager API reachable.
- Response `projectId` matches `target.gcp_project`.

Returns `ok=True` on success, `ok=False + reason` on failure.

### execute()

G0.6 dispatch shim — delegates to `meho_backplane.operations.dispatch` with
`connector_id="gcloud-rest-1.0"`. Routes to the handler registered via
`register_gcloud_typed_operations()`.

### register_gcloud_typed_operations()

Classmethod called from the lifespan after `_eager_import_connectors()` has
run. Walks `GCLOUD_OPS`, resolves `handler_attr` to the bound method on the
class, and calls `register_typed_operation()` for each op. Idempotent across
pod restarts. Raises `AttributeError` if a `handler_attr` is missing (deploy
bug, not a runtime degradation). Raises `ValueError` if a `group_key` has no
entry in `_WHEN_TO_USE_BY_GROUP`.

### _post_json_abs(target, abs_url, json_body)

POST variant of `_get_json_abs` for ops that call GCP APIs with POST
semantics (e.g. `getIamPolicy`). Same 401-refresh-retry pattern.

## Dependencies

- **`google-auth>=2.38`** — `google.auth.default()`,
  `google.auth.impersonated_credentials.Credentials`,
  `google.auth.transport.requests.Request`.
  Installed in `backend/pyproject.toml` (added in #845).
- **`httpx>=0.27`** — GCP REST HTTP client (via `HttpConnector`).
- **`tenacity>=9.0`** — retry policy for idempotent verbs (via `HttpConnector`).

## CLI verbs (G3.7-T6 #851)

All 8 ops are reachable via `meho gcloud …`. Source:
`cli/internal/cmd/gcloud/`.

| File | Commands |
|---|---|
| `gcloud.go` | `NewRootCmd()`, helper types (`CallResult`, `dispatchOp`, decoders) |
| `about.go` | `meho gcloud about` |
| `project.go` | `meho gcloud project describe` |
| `services.go` | `meho gcloud services list [--all]` |
| `iam.go` | `meho gcloud iam sa list`, `meho gcloud iam policy read` |
| `compute.go` | `meho gcloud compute instances list [--zone Z]`, `meho gcloud compute networks list`, `meho gcloud compute subnets list [--region R]` |

All verbs support `--target <slug>`, `--json`, `--backplane <url>`.
`meho gcloud compute instances list` and `meho gcloud compute subnets list`
additionally accept `--zone` / `--region` to restrict the aggregatedList
to a single zone/region.

## E2E tests (G3.7-T6 #851)

`backend/tests/test_connectors_gcloud_e2e.py` — httpx_mock (respx) unit-
level E2E tests covering all 8 ops. A `_StubTarget` dataclass satisfies
`GcloudTargetLike`; `google.auth` calls are patched so no live GCP
credentials are needed.

Key tests:

| Test | What it verifies |
|---|---|
| `test_gcloud_e2e_about_dispatches_crm_get` | `gcloud.about` calls CRM v1 project GET |
| `test_gcloud_e2e_project_describe_returns_full_resource` | Full CRM v1 dict returned |
| `test_gcloud_e2e_services_list_follows_pagination` | `nextPageToken` loop |
| `test_gcloud_e2e_services_list_enabled_only_flag` | `enabled_only=false` param |
| `test_gcloud_e2e_iam_service_accounts_list` | SA inventory with pagination |
| `test_gcloud_e2e_compute_instances_list_jsonflux_envelope` | `rows`+`total` envelope shape |
| `test_gcloud_e2e_instances_list_zone_filter_uses_per_zone_api` | `--zone` sends per-zone URL |
| `test_gcloud_e2e_instances_list_empty_project_returns_empty_envelope` | Empty aggregated list → `{rows:[], total:0}` |
| `test_gcloud_e2e_compute_networks_list` | VPC network inventory |
| `test_gcloud_e2e_compute_subnetworks_list_region_filter` | `--region` sends per-region URL |
| `test_gcloud_e2e_iam_policy_read` | IAM policy bindings |
| `test_gcloud_e2e_audit_params_hash_field_present_in_all_ops` | All 8 handlers return non-None results |
| `test_gcloud_e2e_all_ops_have_op_id_registered` | All 8 op IDs registered, handler methods exist |
| `test_gcloud_live_integration_about` _(gated)_ | Live GCP probe; skips unless `CI_GCLOUD_CREDENTIALS_PRESENT=1` |
| `test_gcloud_live_integration_all_8_ops_return_ok_status` _(gated)_ | Live 8-ops sweep |

**Design note:** The always-on tests use handler method calls directly
(not `call_operation`) because the `Target` ORM model does not yet have
`gcp_project` / `gcp_impersonate_sa` as first-class columns (planned for
a future `extras` migration). The live `@_SKIP_LIVE` tests use
`CI_GCLOUD_PROJECT` + `CI_GCLOUD_IMPERSONATE_SA` env vars to construct a
real `GcloudConnector` against a live project.

## Known issues

- `load_credentials_from_vault` is a stub until Goal #214 (Connector
  parity) wires the live Vault read. Inject `credentials_loader` on
  construction to test or run pre-production.
- `_fetch_token_sync` and `refresh_token` run synchronous
  `google.auth.transport.requests.Request()` calls in a thread pool
  executor. The executor is the event loop's default (unbounded); a
  bounded executor is a future follow-up if token fetch latency becomes
  a concern.
- `gcloud.compute.instances.list` inlines all instance rows in v0.2
  (no handle spill). The JSONFlux reducer Initiative will add threshold-
  based spill without connector changes.

## References

- Decision #12: `docs/planning/v0.2-decisions.md` (gcloud transport = B).
- google-auth impersonated_credentials:
  https://google-auth.readthedocs.io/en/master/reference/google.auth.impersonated_credentials.html
- GCP Cloud Resource Manager REST:
  https://cloud.google.com/resource-manager/reference/rest
- Org policy `disableServiceAccountKeyCreation`:
  https://cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys
- Precedent: `backend/src/meho_backplane/connectors/harbor/` (HttpConnector
  skeleton pattern + per-target credential cache).
- Operator onboarding: `docs/cross-repo/gcloud-onboarding.md`.
- G3.7 Initiative: https://github.com/evoila/meho/issues/370
- G3.7-T4 (skeleton + auth): https://github.com/evoila/meho/issues/845
- G3.7-T5 (8 ops): https://github.com/evoila/meho/issues/848
- G3.7-T6 (CLI + E2E + onboarding): https://github.com/evoila/meho/issues/851
