# Azure connector

MEHO reads Azure subscriptions through the Azure connector. One target is one
subscription. The connector logs in to Microsoft Entra ID with a login (a
service principal) from Vault, keeps the access token in its own cache, and
calls Azure Resource Manager (Azure's management API) with it.

Today the connector has three operations: `azure.about`,
`azure.location.list` and `azure.operation.status`. Later reads (inventory, compute, network, storage,
cost and more) build on the login, the cache, the retry budget, the paging
helper and the polling helper described here.

Package: `backend/src/meho_backplane/connectors/azure/`. Shared login code:
`backend/src/meho_backplane/connectors/_shared/microsoft_login.py`.

## Overview

| What | Value |
|---|---|
| Product | `azure` |
| Connector ID | `azure-arm-2026.10` (implementation `azure-arm`, version `2026.10`) |
| Registration | the versioned row plus the wildcard row `("azure", "", "")` |
| Kind | typed operations only; nothing is ingested from a spec |
| Operations | `azure.about`, `azure.location.list` and `azure.operation.status` (group `account`, `safe`) |
| Spec path check | `backend/tests/test_connectors_azure_spec_reconcile.py` against the shelf folder `azure-arm-2026.10` |
| Onboarding | [`docs/cross-repo/azure-onboarding.md`](../cross-repo/azure-onboarding.md) |

Azure has no product version, so the version is a date, like
`hetzner-rest-2026.04`. The wildcard row lets every `azure` target find the
connector, also a new target without a fingerprint.

Why typed only: Azure publishes its API descriptions as Swagger 2.0, which
MEHO's spec ingestion does not read. Hand-written operations also give clear
groups, risk levels and guidance for the AI.

## Key types

- `AzureConnector` (`connector.py`): the connector on the shared HTTP base
  (`connectors/adapters/http.py`). It owns the login, the cache, the two
  recovery hooks, the fingerprint and the probe.
- `AzureCloud`, `AZURE_CLOUDS`, `AzureTargetConfig`, `resolve_target_config`
  (`session.py`): the target shape and its checks.
- `AzureCredentials`, `parse_credentials`, `load_credentials_from_vault`
  (`session.py`): the secret shape and the reader.
- `fetch_client_credentials_token`, `MicrosoftAccessToken`,
  `MicrosoftLoginError`, `LOGIN_HOSTS` (`_shared/microsoft_login.py`): one
  token request to Microsoft Entra ID. The Entra ID connector can reuse it with
  another scope.
- `AZURE_TYPED_OPS`, `AZURE_TYPED_WHEN_TO_USE_BY_GROUP`,
  `register_azure_typed_operations` (`typed_ops.py`): the operation table, the
  group texts and the start-up registrar.
- `_paths.py`: every Resource Manager path and API version the connector
  calls. The spec path check (below) compares them with Azure's API
  description files.
- `send`, `check_azure_url`, `retry_after_seconds` (`_http.py`): every Azure
  call, with one retry budget, the wait after a 429, the limit per login and
  the URL check.
- `AzureApiError` and its kinds, `AzureNotFoundError`, `AzureUrlRefusedError`
  (`_errors.py`): Azure's error answers as errors the dispatcher knows.
- `list_all_pages`, `query_resource_graph`, `PageLimits`, `PagedRows`
  (`_paging.py`): reading every page of a list, with limits.
- `OperationReference`, `operation_reference`, `read_operation_state`,
  `wait_for_operation` (`_polling.py`): following long-running operations.

## Package layout

Each family of operations (inventory, compute, network and so on) has its own
module, `<family>.py`, so families built at the same time do not edit the same
lines. The module holds the family's group text (`WHEN_TO_USE`), its
operations (`OPS`), their bodies and a mixin class: a class with the thin
methods that `AzureConnector` takes over.

- `_model.py` holds what every family imports: `AzureTypedOp`, `NO_PARAMS`
  and `TEXT_OR_NULL`.
- `typed_ops.py` and `connector.py` collect the families: an import and one
  line per list for each family, in alphabetical order.
- `_paths.py` keeps the paths, in one marked section per family.

## Target shape

| Field | Value |
|---|---|
| `host` | the Resource Manager host of the cloud: `management.azure.com` for the public cloud |
| `port` | empty or `443` |
| `secret_ref` | the secret with the login (see below) |
| `extras.subscription_id` | required: the subscription ID, a GUID |
| `extras.tenant_id` | optional: the tenant (directory) ID, a GUID; must equal the secret's `tenant_id` |
| `extras.cloud` | optional: `AzureCloud` (default), `AzureUSGovernment` or `AzureChinaCloud` |

The clouds:

| Cloud | Resource Manager host | Login host | Token scope |
|---|---|---|---|
| `AzureCloud` | `management.azure.com` | `login.microsoftonline.com` | `https://management.azure.com/.default` |
| `AzureUSGovernment` | `management.usgovcloudapi.net` | `login.microsoftonline.us` | `https://management.core.usgovcloudapi.net/.default` |
| `AzureChinaCloud` | `management.chinacloudapi.cn` | `login.chinacloudapi.cn` | `https://management.chinacloudapi.cn/.default` |

The hosts and scopes are the ones Microsoft's own Python SDK uses. Only
`AzureCloud` is proven on a live tenant; the other two are not proven.

Before any login, `resolve_target_config` checks the target. MEHO sends the
access token only to the Resource Manager host of the target's cloud, over
https on port 443, with the certificate checked (`verify_tls` must stay on).
A target with another host, `extras.scheme=http`, another port, a bad GUID or
an unknown cloud fails with `AzureTargetConfigError`. The message names the
target and the field to fix, never a value.

## Secret shape

The secret at the target's `secret_ref` holds three keys:

| Key | Value |
|---|---|
| `tenant_id` | the tenant (directory) ID, a GUID |
| `client_id` | the application (client) ID of the login, a GUID |
| `client_secret` | the client secret of the login |

Other keys are ignored. Every value is stripped of surrounding white space.
A missing or empty key, or an ID that is not a GUID, fails with
`AzureSecretShapeError`. The message names the key and the secret reference,
never a value. Several targets may share one secret.

The secret is read with the shared reader
(`connectors/_shared/vault_creds.py`, `load_vault_secret_data`), under the
caller's own identity, so the read works for every secret store MEHO supports.

If the target sets `extras.tenant_id` and it is not the secret's `tenant_id`,
the call fails before the login with a clear message.

## Login

`fetch_client_credentials_token` sends one request:

```
POST https://<login host>/<tenant_id>/oauth2/v2.0/token
grant_type=client_credentials&client_id=...&client_secret=...&scope=<token scope>
```

Microsoft answers with `access_token` and `expires_in` (seconds; tokens live
60 to 90 minutes). There is no refresh token in this flow: MEHO asks for a new
token when the old one runs out.

The request is made once and never retried, so it cannot stack with MEHO's
own retries. Results:

| Answer | What MEHO does |
|---|---|
| 200 with `access_token` and `expires_in` | returns the token |
| 400, 401 or 403 (the login was refused) | raises `ConnectorAuthError` with the HTTP status, the OAuth 2.0 error code and the `AADSTS` numbers, and the hint to check the secret |
| any other status (for example 429 or 5xx), no connection, or an answer without a usable token | raises `MicrosoftLoginError` |

No error text holds the client secret or a token.

From the token MEHO reads the login's app ID (claim `appid`, or `azp` in a
version 2 token), object ID (`oid`) and tenant ID (`tid`). It does not check
the signature and uses the values only to show them. Microsoft may change the
token format, so each value is empty when the token cannot be read.

### Why MEHO makes the request itself

The plan was to use Microsoft's `azure-identity` library. We checked version
1.27.0 (with `azure-core` 1.41.0 and `msal` 1.39.0) and it does not fit MEHO's
login flow:

- Its credential object keeps its own token cache. A second request on the
  same object returned the same token without a call, so after a 401 it would
  hand back the rejected token unless MEHO threw the object away each time.
- It retries the token request itself (up to 10 times, with waits of up to
  120 seconds) unless told not to, and MSAL repeats a stored 429, 5xx or 400
  answer for a while without asking again.
- Every new credential object first reads Microsoft's login settings, so each
  login is two calls (three when it fails).
- The sync credential runs on `requests` and needs a thread; the async one
  needs `aiohttp`. Neither runs on MEHO's HTTP client, so the tests could not
  mock it with respx.
- It adds four packages.

With all of that turned off, one request is left, which MEHO already makes the
same way for Keycloak. So MEHO makes it itself, with `httpx`, and adds no
package.

## Token cache

`AzureConnector` keeps two caches:

- **Secret cache**, per MEHO tenant, target and secret reference.
- **Token cache**, per MEHO tenant, target, Azure tenant, client ID and token
  scope. MEHO asks for a new token five minutes before the old one runs out
  (Microsoft's own SDK uses the same margin).

Each cache key has its own lock. When ten calls need a new token at the same
time, one call logs in and the other nine wait and then use its token. Azure
does not cancel an older token when it makes a new one, so two targets that
share one login cannot spoil each other's token.

A call without a person's token (an empty `raw_jwt`, or MEHO's own system
identity) never gets a cached secret or token. It must read the secret itself,
and for a system call that read fails on purpose.

## Recovery

- **Azure answers 401** (the token is no longer accepted): `send` calls
  `invalidate_session(target)`, which drops the target's tokens, and tries
  once more inside the same retry budget. The retry logs in again with the
  cached secret. When Azure answers 401 again, the call fails with
  `ConnectorAuthError` (stage `after_relogin`), and the dispatcher returns
  `connector_auth_failed`. No `HTTPStatusError` 401 leaves the connector, so
  the dispatcher's own retry after a 401 does not run on top.
- **Microsoft refuses the login**: the dispatcher gets `ConnectorAuthError`,
  calls `invalidate_credentials(target)`, which drops the target's secret and
  tokens, and returns `connector_auth_failed` with the hint. It does not retry.
  After the operator fixes the secret, the next call reads it again. No
  restart is needed.

## Retries, throttling and the limit per login

Every Azure call goes through `send` in `_http.py`. It is the only retry
loop of an Azure call. The connector does not use the retry of the shared
HTTP base (`_request_json`), and it handles a 401 itself. So the HTTP base,
the connector and the dispatcher never multiply their tries.

**One retry budget per call.** The numbers are constants in `_http.py`, and
`test_connectors_azure_http.py` pins them:

| Constant | Value | Meaning |
|---|---|---|
| `MAX_ATTEMPTS` | 4 | most tries of one call, the first one included; a 401 retry counts |
| `MAX_WAIT_SECONDS` | 60 | most seconds of waiting between the tries, all waits together |
| `BACKOFF_SECONDS` | 0.5, 1, 2 | wait after a 5xx or a lost connection without `Retry-After` |
| `THROTTLE_BACKOFF_SECONDS` | 2, 4, 8 | wait after a 429 without `Retry-After` |

What `send` does per answer:

| Answer | Read (GET, or a read-only POST such as Resource Graph) | Write (POST, PUT, PATCH, DELETE) |
|---|---|---|
| below 400 | returns it | returns it |
| 401 | drops the token, logs in again, tries once more | the same: Azure did not run the call |
| 429 | waits `Retry-After` (or the back-off) and tries again | the same: Azure did not run the call |
| 5xx | waits and tries again | fails at once: Azure may have run it |
| lost connection (`ConnectError`, `ConnectTimeout`, `ReadError`) | waits and tries again | fails at once |
| timeout | fails at once | fails at once |

`Retry-After` is read as seconds or as an HTTP date. Resource Graph sends
`x-ms-user-quota-resets-after` (`hh:mm:ss`); it is read when `Retry-After`
is missing. When the next wait does not fit in the budget (not enough tries
or seconds left), the call fails at once. After a 429 it fails with
`AzureThrottledError`, which says that Azure is throttling and when to try
again.

**Limit per Azure login.** Azure counts calls per login (Azure tenant ID plus
client ID) and subscription. Several targets may share one login, so the
limit is kept per login, not per target:

- at most `MAX_CALLS_IN_FLIGHT_PER_LOGIN` (8) calls of one login run at the
  same time;
- Resource Graph queries (`resource_graph=True`) are paced to
  `RESOURCE_GRAPH_CALLS_PER_WINDOW` (15) per `RESOURCE_GRAPH_WINDOW_SECONDS`
  (5), Resource Graph's own limit per login.

The limit holds per MEHO process (and per event loop). Two MEHO processes
each have their own limit, so together they can send twice as many calls.
Azure's own limits stay the real limit; the 429 handling above covers the
rest.

Azure's numbers (Microsoft docs): Resource Manager reads 250 per bucket,
refilled at 25 per second; writes 200, refilled at 10 per second; per region,
login and subscription. Providers add their own limits (for example Network
10,000 GETs per 5 minutes, Storage 100 list calls per 5 minutes).

## Errors

`_errors.py` turns Azure's error answer (`{"error": {"code", "message"}}`)
into an error the dispatcher already knows
(`docs/codebase/error-message-shape.md`):

| Azure answer | Error | What the caller gets |
|---|---|---|
| 401 twice (after a new login) | `ConnectorAuthError` | `connector_auth_failed` |
| 403 | `AzureForbiddenError` | `connector_error` with a hint about the missing role |
| 404 | `AzureNotFoundError` | `not_found` |
| 409 | `AzureConflictError` | `connector_error`: the resource is busy |
| code `MissingSubscriptionRegistration` (any status) | `AzureProviderNotRegisteredError` | `connector_error`: ask an administrator to register the provider; MEHO never registers it, because that is a write |
| 429, budget used up | `AzureThrottledError` | `connector_error` with when to try again |
| 5xx | `AzureServerError` | `connector_error` |
| other 4xx | `AzureRequestError` | `connector_error` |

For `connector_error` the extras carry `exception_class` (the kind above),
`http_status`, `upstream_message` (Azure's code and message) and
`remediation` (what to do). Azure's message is untrusted data. `clean_azure_text`
filters secrets out of it with MEHO's default redaction policy and then cuts
it to 2,048 characters, in that order: cutting first could split a token at
the boundary into a fragment the filter no longer recognises. When the filter
itself fails, the text is withheld. The cleaned text goes only into
`upstream_message`, which the dispatcher filters and cuts once more. The same
function cleans the error message of a failed long-running operation. MEHO's
own text names Azure's error code only when it looks like a code.

## URL check

Before MEHO sends a token to a URL that came from Azure (a `nextLink` or a
poll URL), `check_azure_url` checks it:

- scheme `https`, port 443, no user name or password, at most 4,096
  characters;
- host equal to the target's Resource Manager host, and then the path must
  start with `/subscriptions/<the target's subscription>`, with no `.` or `..`
  part (also not percent-encoded);
- or a host a later read adds with `extra_hosts` (for example a storage data
  host).

Any other URL fails with `AzureUrlRefusedError` before the login and before
any request. A relative link (a path without a host) fails too, because it
could point at another subscription on the same host. The error never
repeats the URL. The hand-written paths are not checked this way: they are
constants, and the target check already allows only the cloud's Resource
Manager host.

**No redirects.** The Azure connector builds its HTTP client with
`NoRedirectClient` (`_http.py`), which follows no redirect at all. The shared
client follows a redirect on the same host, but on Azure every subscription
shares one host, so such a redirect could carry the token to another
subscription. Azure Resource Manager never redirects a call. A 3xx answer
fails with `AzureUrlRefusedError`, and nothing is sent to the new address.

## Paging

`list_all_pages` (`_paging.py`) GETs a hand-written path and follows
`nextLink` (a full URL that already holds the `api-version` and the page
token). Every `nextLink` goes through the URL check above before it is
followed. `query_resource_graph` posts a Resource Graph query to
`POST /providers/Microsoft.ResourceGraph/resources?api-version=2024-04-01`
and sends the next page as the same query with `options.$skipToken`. Resource
Graph returns at most 1,000 rows per page.

Limits per list (`PageLimits`):

| Limit | Default |
|---|---|
| `max_pages` | 50 (`MAX_PAGES`) |
| `max_seconds` | 120 (`MAX_SECONDS`) |
| `max_rows` | 10,000 (`MAX_ROWS`, the most rows one stored result holds) |

The result (`PagedRows.as_dict()`) is:

```json
{"value": [...], "truncated": true, "omission_kind": "page_limit",
 "omission": "MEHO stopped at its limit of 50 pages. Azure has more rows ...",
 "pages_read": 50}
```

The rows are under `value`, like Azure's own answers, so the dispatcher's
result reduction recognises them: a list with more than 50 rows becomes a
link to the stored result, and `truncated`, `omission`, `omission_kind` and
`pages_read` stay inline next to it because they are plain values. A
complete list has `truncated: false` and `null` for both omission fields.
`test_connectors_azure_paging_polling.py` proves this with the real reducer.

`omission_kind` is one of:

| Kind | Meaning |
|---|---|
| `page_limit`, `time_limit`, `row_limit` | MEHO stopped at its own limit; the message says how to narrow the request |
| `result_truncated` | Resource Graph cut the result itself and gave no `$skipToken` (`resultTruncated` is `"true"`); the message says why and what to change |

Resource Graph gives no token when the query uses `limit`, `sample` or
`take`, or when every output column is `dynamic` or `null`. When the answer
also says how many rows match (`totalRecords`) and MEHO read all of them,
nothing is missing and the result is not called cut. Inside `PagedRows`, the
`omission` field holds the details (`kind`, `pages_read`, `rows_returned`,
`message`, plus `limit` or `total_records`). Nothing is cut silently.

## Long-running operations

A long-running Azure call answers 201 or 202. `operation_reference`
(`_polling.py`) reads the poll URL: `Azure-AsyncOperation` when present, else
`Location`. It checks the URL (the URL check above, plus: one part of the
path has "operation" in it, such as `operations`, `operationStatuses` or
`operationResults`) and returns an `OperationReference`:

```json
{"poll_url": "...", "poll_kind": "azure-asyncoperation", "resource_id": "...",
 "started_at": "2026-10-27T09:00:00+00:00", "retry_after_seconds": 30}
```

`retry_after_seconds` is the `Retry-After` of the answer that started the
operation: how long Azure asked to wait before the first poll. It is `null`
when Azure sent none.

`read_operation_state` polls once:

- `azure-asyncoperation`: the answer is `{"status": ...}`. `Succeeded`,
  `Failed` and `Canceled` are the end states; every other value (`InProgress`,
  `Running`, `Accepted`, ...) means it still runs. `error` holds Azure's code
  and message; `properties` is the result of a success.
- `location`: 202 means it still runs. 200 or 204 means it succeeded, with
  the body as the result. A 400 or 409 answer means the operation failed.

`wait_for_operation` polls until the end or until `max_seconds` (default
300) pass. Before the first poll it waits the reference's
`retry_after_seconds`, or polls at once when Azure sent none. Between two
polls it waits the full `Retry-After` Azure asked for, never less (10 seconds
when Azure sent none or zero). A shorter wait would poll early and could make
Azure throttle. When a wait does not fit in the time left, it does not poll
early: it returns the pending state with the reference and `done: false`, and
the caller can poll later. This holds for the first wait too: a first answer
that asks for 120 seconds with 100 seconds left returns at once, without a
poll.

### azure.operation.status

`azure.operation.status` (group `account`, `safe`) takes the `operation`
reference an Azure write returned and polls it once, with the same checks.
It never sends the write again. It polls at once: the caller decides when to
ask, so the reference's `retry_after_seconds` is not waited here. This is how
a caller learns the outcome of a write after a lost answer or a MEHO restart:
the reference holds everything needed, and no state in MEHO's memory is used.
The answer is `{status, done, error, result, retry_after_seconds, operation}`.

## azure.location.list

`azure.location.list` (group `account`, `safe`, no parameters) reads
`GET /subscriptions/{subscriptionId}/locations?api-version=2022-12-01` with
`list_all_pages`, so it follows `nextLink` within the paging limits. Each
Azure `Location` becomes one row:

| Field | Source |
|---|---|
| `name` | `name` (the region name used in requests, for example `westeurope`) |
| `display_name` | `displayName` |
| `region_type` | `metadata.regionType` (`Physical` or `Logical`) |
| `geography` | `metadata.geography` (the Azure geography, for example `Europe`, `Germany` or `United States`) |
| `geography_group` | `metadata.geographyGroup` (the wider group, for example `Europe` or `US`; Azure may move a region to another group) |
| `paired_regions` | the `name` of every entry in `metadata.pairedRegion` (an empty list when there is none) |

An item that is not an object is left out. The result has the `PagedRows`
shape (`value`, `truncated`, `omission`, `omission_kind`, `pages_read`).
Azure has more than 50 regions, so the dispatcher stores the rows and
returns a link to the stored result; `result_query` reads them.
`test_connectors_azure_typed_reads.py` proves this through the real
dispatcher and reducer.

## Spec path check

`backend/tests/test_connectors_azure_spec_reconcile.py` checks every
`*_PATH` constant in `_paths.py`, with its method and API version, against
Azure's own API description files. The files are pinned on the private
spec shelf in `azure-arm-2026.10/`: Swagger 2.0 files from
`Azure/azure-rest-api-specs` (MIT license), all from one commit, with their
upstream paths kept. The shelf's `MANIFEST.md` names the commit and every
file. CI fetches the folder with the other shelf folders
(`docs/decisions/vendor-spec-ci-provisioning.md`). Without the shelf the
check skips.

How it compares:

- MEHO's spec ingest does not read Swagger 2.0, so the check reads the keys
  of `paths` and `x-ms-paths` itself. The query part of an `x-ms-paths` key
  is cut off.
- Parameter names are ignored (`{subscriptionId}` and `{subscription_id}`
  match), because Azure names one parameter differently across files.
- Case is ignored, because Resource Manager paths are case-insensitive and
  the files mix `resourceGroups` and `resourcegroups`.
- The API version counts: each file serves its `info.version`, so a path
  sent with a wrong `api-version` fails.

A new path constant needs one line in the check's `_DECLARED` table (method
and API version constant); a guard test fails until it has one. A later
read whose API version is not on the shelf yet adds that file to the shelf
first.

## azure.about, fingerprint and probe

`azure.about` (group `account`, `safe`, no parameters) reads
`GET /subscriptions/{subscriptionId}?api-version=2022-12-01` and returns:

| Field | Source |
|---|---|
| `subscription_id`, `display_name`, `state`, `tenant_id` | the Resource Manager answer |
| `cloud` | the target |
| `app_id`, `object_id` | the claims of the access token |

The token itself is never returned. `display_name` is text from Azure; the
guidance for the AI says to treat it as data.

`fingerprint` reads the same subscription and stores vendor `microsoft`,
product `azure`, no version, and `extras` with `subscription_id`,
`tenant_id`, `cloud` and `state`. `meho targets probe` stores this result.
When the read fails, the fingerprint is `reachable=False` with the reason in
`extras.error`. `probe` uses the fingerprint. A call without an operator runs
as MEHO's system identity, which cannot read the secret, so it reports
`reachable=False`.

## Dependencies

- `connectors/adapters/http.py`: pooled `httpx` client, blocking of private
  addresses, the response size cap and trace recording. Its retry is not used.
- `connectors/_shared/vault_creds.py`: the operator-context secret read.
- `connectors/_shared/vcf_auth.py`: `ConnectorAuthError` (the refused-login
  error the dispatcher knows) and the `auth_model` check.
- `connectors/_shared/system_operator.py`: recognising MEHO's system identity.
- `operations/typed_register.py`: storing the typed operations at start-up.

## Known issues

- Only the public cloud (`AzureCloud`) is proven on a live tenant.
- A 429 or 5xx from the login service is not retried: the login request is
  made once and fails with `MicrosoftLoginError`.
- The limit per login holds per MEHO process. Several MEHO processes do not
  share it.
- Paging, the polling helper, `azure.operation.status` and
  `azure.location.list` are proven with mocked HTTP only. The live proof on
  a real subscription is still open.
- `resultTruncated` from Resource Graph is read as the string `"true"` (what
  the API sends) or a real `true`.
- The cache lives in one MEHO process. Each process logs in on its own.

## References

- OAuth 2.0 client credentials flow on the Microsoft identity platform:
  <https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow>
- Access token claims (`appid`, `azp`, `oid`, `tid`):
  <https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference>
- Token lifetimes:
  <https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes>
- Throttling of Resource Manager requests:
  <https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling>
- Resource Graph paging, `$skipToken` and `resultTruncated`:
  <https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/work-with-data>
- Throttled Resource Graph requests:
  <https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/guidance-for-throttled-requests>
- Resource Graph query (API version 2024-04-01, `$skipToken`):
  <https://learn.microsoft.com/en-us/rest/api/azureresourcegraph/resourcegraph/resources/resources>
- Status of asynchronous operations:
  <https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations>
- Paging with `nextLink`: <https://learn.microsoft.com/en-us/rest/api/azure/>
- Subscriptions - Get (API version 2022-12-01):
  <https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/get>
- Subscriptions - List Locations (API version 2022-12-01):
  <https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/list-locations>
- Azure API description files: <https://github.com/Azure/azure-rest-api-specs>
- Azure Government endpoints:
  <https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure>
- Related pages: `connectors-keycloak.md` (the same client credentials request),
  `connectors-shared-vault-creds.md`, `error-message-shape.md`.

## inventory

The `inventory` family reads what a subscription holds and changes the tags of
one resource. Its Resource Graph reads can also cover other subscriptions,
each checked first.

**Proven live:** nothing yet. The lab login does not exist yet. Everything
below is tested with mocked Azure answers only.

Code, in `connectors/azure/`:

- `inventory.py`: the group text, the operation table with the guidance for
  the AI, and `InventoryOps`, the mixin of `AzureConnector` with one thin
  method per operation and `list_candidates`. It must come before the HTTP
  base in the connector's bases.
- `_inventory_schemas.py`: the parameter and response schemas.
- `inventory_reads.py`: the lists of the target's subscription, the
  subscription list, `azure.tag.update` and the discover candidates.
- `inventory_graph.py`: the Resource Graph operations and MEHO's own queries.
- `_graph_set.py`: one Resource Graph query over a checked set of
  subscriptions (batches, paging, limits, omissions). Shared; other families
  can use it.
- `_subscription_set.py`: the check of the set and the `subscriptions` block.
  Shared; other families can use it.

### Operations

All in group `inventory`.

| Operation | Risk | Azure call (API version) | Parameters |
|---|---|---|---|
| `azure.subscription.list` | `safe` | `GET /subscriptions` (2022-12-01) | none |
| `azure.resource_group.list` | `safe` | `GET /subscriptions/{s}/resourcegroups` (2021-04-01) | none |
| `azure.resource.list` | `safe` | `GET /subscriptions/{s}/resources`, or `.../resourceGroups/{rg}/resources` (2021-04-01) | `type`, `resource_group` |
| `azure.tag.list` | `safe` | `GET /subscriptions/{s}/tagNames` (2021-04-01) | none |
| `azure.lock.list` | `safe` | `GET /subscriptions/{s}/providers/Microsoft.Authorization/locks` (2020-05-01) | none |
| `azure.tag.update` | `caution`, waits for approval | `GET` and `PATCH {resource ID}/providers/Microsoft.Resources/tags/default` (2021-04-01) | `resource_id`, `mode`, `tags` |
| `azure.graph.query` | `safe` | `POST /providers/Microsoft.ResourceGraph/resources` (2024-04-01) | `query`, `subscriptions`, `page_size` |
| `azure.inventory.summary` | `safe` | the same, with MEHO's query | `subscriptions` |
| `azure.inventory.vm.list` | `safe` | the same, with MEHO's query | `subscriptions` |
| `azure.inventory.aks.list` | `safe` | the same, with MEHO's query | `subscriptions` |
| `azure.inventory.network.list` | `safe` | the same, with MEHO's query | `subscriptions` |

The API version 2025-04-01 of the resource calls is in Azure's spec too. It
is not used until the lab shows that it works in every region.

### Lists of the target's subscription

The lists read every page with `list_all_pages` and return the shape of
`PagedRows.as_dict()` (see Paging above).

- `azure.resource.list` filters by `type` with
  `$filter=resourceType eq '<type>'`. With `resource_group` it reads the list
  of that group, because Azure does not allow a filter on the type and the
  group together. A pattern checks both values, so neither can change the
  path or the filter.
- `azure.subscription.list` shows every subscription the target's login can
  see: the records only, not what is inside them. Reading inside a
  subscription still needs a registered target. The next page of this list
  is `/subscriptions` again, which is outside the target's subscription, so
  the URL check of the other lists refuses it. `list_visible_subscriptions`
  checks this link itself: https on port 443, no user name or password, the
  target's Resource Manager host, the path exactly `/subscriptions`, at most
  4,096 characters. It sends only the link's query values (the page token) to
  the hand-written path, so the token cannot go anywhere else.

### azure.tag.update

- `resource_id` must be `/subscriptions/<the target's subscription>/resourceGroups/<group>`,
  alone or followed by `/providers/...`. MEHO refuses, before any call:
  another subscription, the subscription itself, an empty, `.` or `..` part,
  `?`, `#`, `%`, a backslash, a control character, more than 2,048
  characters, and an ID that already ends with the tag path.
- `merge` sends one `PATCH` with `operation: Merge`: new names are added,
  existing names get the new value, other tags stay.
- `remove` first reads the tags (`GET`), then sends one `PATCH` with
  `operation: Delete` and the current value of each named tag that is there
  (names match without regard to case). Microsoft's docs do not say whether
  a delete needs the right value; sending the current value works either
  way. When none of the named tags is there, nothing is sent.
- The same call twice gives the same tags. A `PATCH` is sent again only after
  a 429 or a 401, when Azure did not run it.
- The answer is `{resource_id, mode, tags, not_present}`: all tags after the
  change, and the names `remove` did not find.
- The login needs a role that may write tags there, for example Tag
  Contributor. A ReadOnly lock blocks the change.

### The subscription set

A wider read names other `azure` targets in `subscriptions`.
`resolve_subscription_set` checks each name before any call:

| Check | Denied with |
|---|---|
| a live target with this name in the caller's MEHO tenant | "No live target with this name ..." |
| product `azure` | "This target is not an azure target." |
| a usable Azure target (`resolve_target_config`) | "This target is not a usable Azure target: ..." |
| the shared login (`auth_model`) | "This target does not use the shared login ..." |
| the same secret reference as the calling target | "This target uses another login ..." |
| the same Azure cloud | "This target is in another Azure cloud." |
| the caller may run this operation on it now | "You may not run ... (the reason)", or "... would wait for approval ..." |

- The permission check is the dispatcher's own: `policy_gate` with
  `decide_only=True`, so it records nothing. For an agent it runs
  `resolve_verdict` (the grants per operation and target). For a service
  principal it also applies its read scope. For a run of a hosted agent it
  takes the stricter verdict of the caller and the agent.
- A target where the read would wait for approval is denied: a read over
  several subscriptions never waits for approval.
- The calling target is always in the set; the dispatcher already checked
  it. A name given twice counts once.
- Azure then checks its own roles. What the stored login could read never
  widens what the caller gets.

The result carries `subscriptions: {requested, completed, denied, failed}`;
`denied` and `failed` are `[{name, reason}]`.

### Resource Graph over the set

`read_over_set` (`_graph_set.py`):

1. Checks the set.
2. Sends the query with `subscriptions` filled in by MEHO, at most 250
   subscriptions per query (Microsoft advises fewer than 300). It never sends
   `managementGroups` or `authorizationScopeFilter`. Options:
   `resultFormat: objectArray`, and `$top` when `page_size` is given.
3. Follows `$skipToken`. One limit of pages, rows and seconds holds for the
   whole read (the `PageLimits` defaults: 50 pages, 10,000 rows, 120 seconds).
4. Drops a row whose `subscriptionId` is not in its batch.
5. A batch that Azure answers with an error is listed under `failed`, and the
   rows read before stay. When nothing could be read at all, the first error
   is raised, so the caller gets Azure's reason (for example a query Resource
   Graph cannot parse). A login or target error stops the whole read.

With more than 250 subscriptions MEHO runs one query per batch, so
`summarize`, `take` and `top` work per batch. Grouping by `subscriptionId`
keeps the numbers exact. Results can lag a little behind recent changes.

The result:

```json
{"value": [...], "complete": false, "pages_read": 3,
 "subscriptions": {"requested": [...], "completed": [...], "denied": [...], "failed": [...]},
 "omissions": {"count": 1, "items": [{"kind": "subscriptions_denied", "count": 1, "message": "..."}]}}
```

`complete` is true only when every requested subscription was read in full
and nothing is missing. The omission kinds:

| Kind | Meaning |
|---|---|
| `subscriptions_denied` | names MEHO did not read; `subscriptions.denied` says why |
| `page_failed` | Azure answered a page with an error; later rows of that batch are missing |
| `subscription_limit` | Azure answered `x-ms-tenant-subscription-limit-hit: true`: it searched only the first 10,000 subscriptions |
| `result_truncated` | Resource Graph cut the result and gave no `$skipToken` (`limit`, `take` or `sample`, or only dynamic columns) |
| `row_limit` | MEHO stopped at 10,000 rows, the most one stored result holds |
| `page_limit`, `time_limit` | MEHO stopped at its page or time limit |
| `outside_set` | Azure sent rows of a subscription that was not asked for; MEHO dropped them |

`azure.inventory.summary` adds `resource_total`, the sum of `count_` over the
rows. The fixed queries take their property names from Microsoft's samples.
They are not checked on a live tenant yet: `power` of a VM can be empty.

### The blocks at the link to the stored result

A result over 50 rows or 4 KB comes back as a link to the stored result. The
reducer keeps the plain values (`complete`, `pages_read`, `resource_total`)
inline. `subscriptions` and `omissions` are objects, so each Resource Graph
operation names them in `llm_instructions.result_objects`
(`omissions` first, so it gets the inline room first). The reducer then keeps
their fields inline next to the link, and the whole blocks are stored with the
rows. A long list inside a block is cut inline with a note on how to read it
whole. `test_connectors_azure_inventory.py` proves this through the real
dispatcher and reducer for every Resource Graph operation.

### The audit row

`SubscriptionSet.bind_audit` binds the block as `audit_subscriptions` in the
log context. The dispatcher copies every bound `audit_*` value into the audit
row, so `payload["subscriptions"]` holds the same four lists as the result.
This also holds when the read fails. The block is cleared at the start of the
next set check. It stays bound in the log context after the call until then,
like every value a handler binds this way.

### Candidates for meho targets discover

`InventoryOps.list_candidates(seed_target, operator)` lists, with the seed's
login, every subscription the login can see that is not yet the
`extras.subscription_id` of a live `azure` target of the seed's MEHO tenant.
Each candidate carries the seed's Resource Manager host and port, its secret
reference, and the extras `subscription_id`, `cloud` and `tenant_id` (the
seed's, else the tenant of the login's token). A subscription that is not
`Enabled` gets `confidence: low`. The name is
`azure-<display name, letters and digits only>-<first 8 characters of the ID>`;
the display name itself is untrusted text and goes only into `evidence`.

The discover route passes the caller as `operator`, so the seed's secret is
read under the caller's identity. Without a seed, or with a seed that is not
an `azure` target, the list is empty.

```bash
meho targets discover azure --seed-target <target> --import-yaml > subscriptions.yaml
# review the file, then:
meho targets import subscriptions.yaml
```

### Known limits

- No live proof yet (see above).
- The `audit_subscriptions` value stays in the log context after the call, so
  a later call in the same context (a step of a multi-step operation, for
  example) can show it in its audit row until the next set check clears it.
- When an operator marks a Resource Graph operation `requires_approval`, every
  other subscription is denied: a read over several subscriptions never waits
  for approval.
- `azure.subscription.list` follows its own next link outside
  `list_all_pages`, with the same limits.
