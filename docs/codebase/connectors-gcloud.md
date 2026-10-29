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
`docs/decisions/locked-decisions.md`. Option A (SubprocessConnector wrapping
`gcloud` CLI) was rejected.

**Org-policy constraint:** `constraints/iam.disableServiceAccountKeyCreation`
is active on the consumer's GCP organization. The connector encodes this
constraint: SA JSON key material in any Vault `secret_ref` payload is
refused before any token is built. This is not a soft warning — it raises
`ValueError` and no token is returned.

## Typed ops (G3.7-T5 #848)

Nine read-only ops registered via `register_gcloud_typed_operations()`
at lifespan startup (eight from G3.7-T5 #848; the ninth,
`gcloud.iam.service_account_keys.list`, added by #2846). These nine have
`safety_level="safe"` and `requires_approval=False`. Five more ops for
workload identity pools and service-account policies are described in
the next section. They land in the
`endpoint_descriptor` table under `connector_id="gcloud-rest-1.0"`.

| Op ID | API surface | HTTP verb | Notes |
|---|---|---|---|
| `gcloud.about` | CRM v1 `projects.get` | GET | Identity summary; fetches CRM directly with the real operator so the SA-key gate runs under the operator's identity (#985) |
| `gcloud.project.describe` | CRM v1 `projects.get` | GET | Full CRM resource dict |
| `gcloud.services.list` | Service Usage v1 | GET | Follows `nextPageToken`; `enabled_only` param |
| `gcloud.iam.service_accounts.list` | IAM v1 | GET | Follows `nextPageToken` |
| `gcloud.compute.instances.list` | Compute v1 `aggregatedList` | GET | JSONFlux-compatible `rows`+`total`; `zone` param |
| `gcloud.compute.networks.list` | Compute v1 global networks | GET | Follows `nextPageToken` |
| `gcloud.compute.subnetworks.list` | Compute v1 `aggregatedList` | GET | Follows `nextPageToken`; `region` param |
| `gcloud.iam.policy.read` | CRM v1 `getIamPolicy` | POST | Returns `version`, `etag`, `bindings` |
| `gcloud.iam.service_account_keys.list` | IAM v1 `serviceAccounts.keys.list` | GET | Per-SA key inventory; required `service_account_email`, optional `key_types` → `keyTypes[]` filter; row carries `key_type` (USER_MANAGED/SYSTEM_MANAGED) + `valid_before_time` expiry; never returns key material; single GET (no pagination) |

Groups and `when_to_use` blurbs are defined in `_WHEN_TO_USE_BY_GROUP`
in `connector.py`: `identity`, `project`, `services`, `iam`, `compute`,
`workload_identity`.

## Workload identity and service-account policy ops

Five more ops let an operator see and remove an old outside access path
through MEHO, with an approval and an audit row. A **workload identity pool**
lets an outside system (for example Vault) get Google access without a key
file. The pool trusts an outside issuer through a provider; a binding on a
service account (usually `roles/iam.workloadIdentityUser`) lets the pool's
identities act as that account.

| Op ID | Google call | Risk level |
|---|---|---|
| `gcloud.iam.workload_identity_pools.list` | `GET iam/v1/projects/<p>/locations/global/workloadIdentityPools` | safe |
| `gcloud.iam.workload_identity_pools.providers.list` | `GET .../workloadIdentityPools/<pool>/providers` | safe |
| `gcloud.iam.workload_identity_pools.delete` | `GET` then `DELETE .../workloadIdentityPools/<pool>` | dangerous (a person must approve) |
| `gcloud.iam.service_accounts.policy.read` | `POST iam/v1/projects/<p>/serviceAccounts/<sa>:getIamPolicy` (version 3) | safe |
| `gcloud.iam.service_accounts.binding.remove` | `getIamPolicy`, drop one member, `setIamPolicy` with the read `etag` | dangerous (a person must approve) |

Source: metadata in `ops_workload_identity.py` (`WORKLOAD_IDENTITY_OPS`,
appended to `GCLOUD_OPS`); handlers in `workload_identity.py`, as the
`WorkloadIdentityOps` mixin that `GcloudConnector` inherits; the checks
before approval in `workload_identity_preflight.py`.

How they behave:

- **Lists** follow `nextPageToken`; `show_deleted=true` also shows items
  that are marked deleted. Provider rows show `kind`, `issuer_uri`
  (`issuerUri`), `allowed_audiences`, `attribute_mapping` and
  `attribute_condition` (`attributeCondition`). Key material (`jwksJson`,
  SAML metadata, trust stores) is never returned.
- **Pool delete** reads the pool first. A missing pool gives `not_found`. A
  pool that is already marked deleted is not sent again (safe to run twice).
  The `DELETE` answers with a Google operation. MEHO polls it (up to 15
  times, one second apart) until it is done. A finished operation gives
  `state: DELETED`; an operation with an `error` fails the call
  (`ConnectorUpstreamError`, status 502); an operation still running after
  the wait gives `state: DELETE_PENDING`, `operation_done: false` and a
  `note` that says how to check (`gcloud.iam.workload_identity_pools.list`
  with `show_deleted=true`). MEHO never says `DELETED` before Google does.
  Google only marks a pool deleted: it stops working at once, Google keeps
  it for 30 days and it can be restored in that time, and its ID cannot be
  used again in those days. The result says so (`restorable_for_days: 30`,
  `note`).
- **Binding removal** reads the policy (version 3, asked for with the
  query parameter `options.requestedPolicyVersion=3`; the body is empty),
  drops only the named
  member from the named role, only in bindings without a condition, drops a
  binding left with no member, and writes the policy back with the `etag` it
  read. If the member holds the role only through a binding with a
  condition, MEHO refuses. If Google still answers in an older format (a
  role name contains `_withcond_`, which hides a condition), MEHO refuses
  too. If the member is not there, nothing is sent
  (`removed: false`). If someone changed the policy in between, Google
  answers `409`; MEHO raises `GcloudPolicyChangedError` (a
  `ConnectorUpstreamError`), changes nothing and does **not** retry.
- **Strict values.** `pool_id`, `service_account_email`, `role` and `member`
  must fully match a pattern (in the parameter schema and again in the
  handler with `re.fullmatch`). A value with `/`, `..`, spaces or an unknown
  member prefix is refused before any call, so it cannot change the URL
  path.
- **Check before approval.** Both writes register a
  `register_permission_preflight` check. The binding removal calls Google's
  `testIamPermissions` on the service account. The pool delete reads the
  pool (`GET`) and then calls `testIamPermissions` on the **project**
  (`cloudresourcemanager.googleapis.com/v1/projects/<p>:testIamPermissions`)
  for `iam.workloadIdentityPools.delete`, because the IAM API does not
  answer `testIamPermissions` on every pool. A missing permission or a
  missing pool / service account (404) sets `will_be_denied`, so the
  dispatcher refuses at once (`write_capability_denied`) instead of asking a
  person to approve a call that cannot work. Any other failure skips the
  check; the call still waits for approval.
- The delete stays `dangerous`, not `destructive`: Google keeps the pool
  for 30 days and it can be restored. Both writes still match the
  delete-shaped op-id patterns (`*.delete`, `*.remove`), so a standing
  grant can never run them without a person's approval.

Tests: `backend/tests/test_connectors_gcloud_workload_identity.py` (respx
answers, no Google project needed).

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
- **`GCLOUD_OPS`** (`ops.py`) — tuple of all 14 `GcloudOp` entries (the
  nine in `ops.py` plus `WORKLOAD_IDENTITY_OPS`).
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
  this set on every `_ensure_token()` call — i.e. on every operation,
  `fingerprint()`, `probe()`, and `auth_headers()` call (#985).
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

The single token-acquisition chokepoint is `_ensure_token(target, operator)`.
Every operation (`_get_json_abs` / `_post_json_abs`), `fingerprint()`,
`probe()`, and `auth_headers()` flow through it — so the SA-JSON-key refusal
gate lives there, not only in `auth_headers()` (#985). Previously the gate
was invoked only from `auth_headers()`, which no live path calls, so the
control never fired for real usage.

1. **auth_model gate** — `auth_headers()` checks `target.auth_model` is
   `IMPERSONATION` or `None`. Any other value → `NotImplementedError`.
   (Operations skip this gate; the dispatcher resolved the connector by
   product, and the impersonation model is the only one gcloud supports.)
2. **SA-JSON-key refusal gate** — `_ensure_token(target, operator)` calls
   `_gate_sa_key_refusal(target, operator)` **first, before the cache fast
   path**, so a Vault rotation that introduces key material is refused on
   the next request even with a still-valid cached token. The gate loads the
   Vault `secret_ref` payload via the injectable `credentials_loader` (under
   the operator's identity), checks for SA key field names; any present →
   `ValueError` naming the target + fields. No token is built.
3. **Token resolution** — `_ensure_token(target, operator)` continues:
   - Fast path: cached token + `creds.valid` → return cached.
   - Slow path (under per-target lock): calls `_fetch_token(target)` which:
     - Runs `_fetch_token_sync(target)` in a thread pool executor.
     - `_fetch_token_sync` calls `google.auth.default()` for ADC source
       credentials, wraps with `impersonated_credentials.Credentials`,
       calls `creds.refresh(Request())`, returns `(token, creds)`.
     - Stores token + creds in `_token_cache` / `_creds_cache`.
4. `auth_headers()` returns `{"Authorization": "Bearer <token>"}`; operations
   build the header inline from the same token.

**Operator threading.** Typed-op handlers have signature
`(self, operator, target, params)`; the dispatcher's `dispatch_typed`
threads `operator` to a handler only when the parameter name appears in the
signature (`meho_backplane.operations._branches.dispatch_typed`). Handlers
forward `operator` to `_get_json_abs` / `_post_json_abs` → `_ensure_token`.
`fingerprint()` and `probe()` are operator-less ABC entry points, so they
synthesise a frozen system operator (empty `raw_jwt`,
`synthesise_system_operator()`) for the gate's Vault read — the same stand-in
the other operator-less probe paths use. A target whose `secret_ref` carries
SA-JSON-key fields therefore surfaces `reachable=False` (fingerprint) /
`ok=False` (probe) rather than silently proceeding.

### 401 auto-refresh

GCP REST APIs return HTTP 401 when a bearer token has expired.
`_get_json_abs(target, abs_url, *, operator)`:
- Issues the GET with the current bearer token.
- On 401: calls `refresh_token(target)` (acquires per-target lock, calls
  `creds.refresh()` in executor, updates cache) and retries once.
- Any subsequent non-2xx → `httpx.HTTPStatusError`.

### fingerprint()

`GET https://cloudresourcemanager.googleapis.com/v1/projects/<gcp_project>`
via `_get_json_abs` (with a synthesised system operator for the gate).
Returns:
- `vendor="google"`, `product="gcp-project"`, `version=None`.
- `extras["project_number"]`, `extras["lifecycle_state"]`,
  `extras["organization"]` (from `parent.id` when `parent.type="organization"`),
  `extras["project_id"]`.
- On failure (including an SA-JSON-key refusal): `reachable=False`,
  `extras["error"]`.

### probe()

Same endpoint as `fingerprint()` (also via the synthesised system operator).
Verifies:
- ADC source credentials are present and valid.
- Impersonation chain succeeds (Token Creator role granted).
- Cloud Resource Manager API reachable.
- Response `projectId` matches `target.gcp_project`.

Returns `ok=True` on success, `ok=False + reason` on failure (an SA-JSON-key
refusal surfaces as `ok=False`).

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

### _post_json_abs(target, abs_url, *, operator, json_body)

POST variant of `_get_json_abs` for ops that call GCP APIs with POST
semantics (e.g. `getIamPolicy`). Same 401-refresh-retry pattern; `operator`
is required and threaded to `_ensure_token` for the SA-JSON-key gate.

## Dependencies

- **`google-auth>=2.38`** — `google.auth.default()`,
  `google.auth.impersonated_credentials.Credentials`,
  `google.auth.transport.requests.Request`.
  Installed in `backend/pyproject.toml` (added in #845).
- **`httpx>=0.27`** — GCP REST HTTP client (via `HttpConnector`).
- **`tenacity>=9.0`** — retry policy for idempotent verbs (via `HttpConnector`).

## CLI verbs (G3.7-T6 #851)

The eight G3.7-T5 ops are reachable via `meho gcloud …` (the ninth,
`gcloud.iam.service_account_keys.list`, is dispatchable via
`call_operation` but has no CLI verb yet). Source:
`cli/internal/cmd/gcloud/`.

| File | Commands |
|---|---|
| `gcloud.go` | `NewRootCmd()`, `renderRequestError`, `resolveBackplane`, helpers |
| `dispatch.go` | Aliases `CallResult` / `callRequestBody` to `dispatch` package types + `var conn = dispatch.New(ConnectorID)` (shared transport); `dispatchOp` / `renderCallResult` thin wrappers; `decodeFlatResult` / `decodeRowsResult` decoders |
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
level E2E tests covering all 9 ops. A `_StubTarget` dataclass satisfies
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
| `test_gcloud_e2e_iam_service_account_keys_list_returns_key_rows` | Per-SA key rows: `key_type` (USER_MANAGED/SYSTEM_MANAGED) + expiry, no key material |
| `test_gcloud_e2e_iam_service_account_keys_list_requires_email` | Missing/blank `service_account_email` raises before any HTTP call |
| `test_gcloud_e2e_iam_service_account_keys_list_forwards_key_types_filter` | `key_types` → repeated `keyTypes[]` query filter |
| `test_gcloud_e2e_audit_params_hash_field_present_in_all_ops` | All 9 handlers return non-None results |
| `test_gcloud_e2e_all_ops_have_op_id_registered` | All 9 op IDs registered, handler methods exist |
| `test_gcloud_live_integration_about` _(gated)_ | Live GCP probe; skips unless `CI_GCLOUD_CREDENTIALS_PRESENT=1` |
| `test_gcloud_live_integration_all_9_ops_return_ok_status` _(gated)_ | Live 9-ops sweep |

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
- `fingerprint()` and `probe()` run the SA-JSON-key gate under a synthesised
  system operator (empty `raw_jwt`), because their ABC signatures carry no
  operator. Once the live `load_credentials_from_vault` lands (Goal #214),
  the system operator's empty `raw_jwt` will fail closed on the Vault read —
  which is the intended boundary for system-initiated credential reads, but
  means probe/fingerprint on a target with a live Vault secret will need a
  follow-up to thread a real operator (or an unauthenticated reachability
  variant). Today, with the stub/injected loader, the gate runs and refuses
  SA-key material correctly on these paths (#985).
- `_fetch_token_sync` and `refresh_token` run synchronous
  `google.auth.transport.requests.Request()` calls in a thread pool
  executor. The executor is the event loop's default (unbounded); a
  bounded executor is a future follow-up if token fetch latency becomes
  a concern.
- `gcloud.compute.instances.list` inlines all instance rows in v0.2
  (no handle spill). The JSONFlux reducer Initiative will add threshold-
  based spill without connector changes.

## References

- Decision #12: `docs/decisions/locked-decisions.md` (gcloud transport = B).
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
