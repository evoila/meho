# Connector: gcloud (GCP REST)

## Overview

The `gcloud` connector is the hand-rolled `HttpConnector` subclass that
dispatches GCP REST operations under the
`(product="gcloud", version="v1", impl_id="gcloud-rest")` registry triple.
G3.7-T4 (#845) ships the skeleton — ADC+impersonation auth, SA-JSON-key
refusal, fingerprint, probe, and the G0.6 dispatch shim. G3.7-T5 (#848)
ships the ~7 read-only typed ops (cloudresourcemanager, compute, iam,
serviceusage). G3.7-T6 (#851) adds the `meho gcloud ...` CLI verbs,
gated integration tests, and `docs/cross-repo/gcloud-onboarding.md`.

Source: `backend/src/meho_backplane/connectors/gcloud/`.

**Transport decision:** Option B — HttpConnector + `google-auth` ADC +
impersonated service-account credentials. Recorded as decision #12 in
`docs/planning/v0.2-decisions.md`. Option A (SubprocessConnector wrapping
`gcloud` CLI) was rejected.

**Org-policy constraint:** `constraints/iam.disableServiceAccountKeyCreation`
is active on the consumer's GCP organisation. The connector encodes this
constraint: SA JSON key material in any Vault `secret_ref` payload is
refused before any token is built. This is not a soft warning — it raises
`ValueError` and no token is returned.

## Key types

- **`GcloudConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="gcloud"`, `version="v1"`,
  `impl_id="gcloud-rest"`, `priority=1`. Auth via Google ADC +
  `impersonated_credentials.Credentials`; per-target token cache;
  auto-refresh on 401 via `refresh_token()`.
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
   `register_connector_v2(product="gcloud", version="v1", impl_id="gcloud-rest", cls=GcloudConnector)` call.
3. The registry's v2 table resolves `("gcloud", "v1", "gcloud-rest")` to
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
   - Slow path (under lock): calls `_fetch_token(target)` which:
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
- On 401: calls `refresh_token(target)` (acquires lock, calls
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
`connector_id="gcloud-rest-v1"`. Operations register in G3.7-T5 (#848).

## Dependencies

- **`google-auth>=2.38`** — `google.auth.default()`,
  `google.auth.impersonated_credentials.Credentials`,
  `google.auth.transport.requests.Request`.
  Installed in `backend/pyproject.toml` (added in #845).
- **`httpx>=0.27`** — GCP REST HTTP client (via `HttpConnector`).
- **`tenacity>=9.0`** — retry policy for idempotent verbs (via `HttpConnector`).

## Known issues

- `load_credentials_from_vault` is a stub until Goal #214 (Connector
  parity) wires the live Vault read. Inject `credentials_loader` on
  construction to test or run pre-production.
- Operations (T5 #848) are not registered yet; `call_operation` against
  any `op_id` resolves to "unknown operation" at the dispatcher layer.
  This is correct for the skeleton stage.
- `_fetch_token_sync` and `refresh_token` run synchronous
  `google.auth.transport.requests.Request()` calls in a thread pool
  executor. The executor is the event loop's default (unbounded); a
  bounded executor is a G3.7-T5 follow-up if token fetch latency becomes
  a concern.

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
- G3.7 Initiative: https://github.com/evoila/meho/issues/370
- G3.7-T4 (this task): https://github.com/evoila/meho/issues/845
- G3.7-T5 (ops): https://github.com/evoila/meho/issues/848
- G3.7-T6 (CLI + onboarding): https://github.com/evoila/meho/issues/851
