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

## compute

Six reads of virtual machines (VMs) and their disks, images and scale sets,
plus the VM sizes Azure offers per region. All are in the group `compute`,
all are `safe` (they need no approval), and none changes anything: MEHO never
starts, stops or changes a VM.

| Operation | What it returns | Parameters |
|---|---|---|
| `azure.vm.list` | the VMs, with size, OS type, power state, disk IDs and tags | `resource_group` (optional) |
| `azure.vm.show` | one VM with its live state, OS, disks, network interfaces, zones and availability set | `resource_group`, `name` |
| `azure.disk.list` | the managed disks, with size, type, state, owner VM and encryption | `resource_group` (optional) |
| `azure.image.list` | the managed images, with OS type and state, VM generation and source VM | `resource_group` (optional) |
| `azure.vmss.list` | the VM scale sets, with VM size, capacity, orchestration and upgrade mode | `resource_group` (optional) |
| `azure.vm.size.list` | the VM sizes of one region, with their numbers and the restrictions for this subscription | `location` (required) |

`vmss` is the short name for scale sets, because `scale` is a write word.

Code: `compute.py` (group text, guidance for the AI, parameter schemas, the
operation table `OPS` and the mixin `ComputeOps`, whose methods
`AzureConnector` inherits), `compute_reads.py` (the calls),
`compute_rows.py` (the rows, built from Azure's answers) and
`compute_schemas.py` (the response schemas). The paths and API versions are in
`_paths.py`, section `compute`.

### Calls and API versions

| Operation | Azure call | API version |
|---|---|---|
| `azure.vm.list` | `GET /subscriptions/{s}/providers/Microsoft.Compute/virtualMachines` (or `/resourceGroups/{rg}/...`), then the subscription list again with `statusOnly=true` | `2026-04-01` |
| `azure.vm.show` | `GET .../resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{name}?$expand=instanceView` | `2026-04-01` |
| `azure.disk.list` | `GET .../providers/Microsoft.Compute/disks` | `2026-03-02` |
| `azure.image.list` | `GET .../providers/Microsoft.Compute/images` | `2026-04-01` |
| `azure.vmss.list` | `GET .../providers/Microsoft.Compute/virtualMachineScaleSets` | `2026-04-01` |
| `azure.vm.size.list` | `GET /subscriptions/{s}/providers/Microsoft.Compute/skus?$filter=location eq '<region>'` | `2021-07-01` |

Every path is in the spec path check, with its API version.

How many calls:

- `azure.vm.list` makes two paged reads, never one call per VM. Read 1 is the
  VM list (size, OS type, disks). Read 2 is the subscription's VM list with
  `statusOnly=true`, which holds only the run-time state of each VM. For N
  VMs that is about 2 × (N ÷ page size) calls; Azure sets the page size. With
  `resource_group`, read 2 still covers the whole subscription, because Azure
  offers `statusOnly` only on the subscription list; MEHO keeps the states of
  the group's VMs. When read 1 finds no VM, read 2 is skipped.
- `azure.vm.show` makes one call.
- The other lists make one call per page.

A login to Microsoft is added only when the token is not in the cache.

### Power state

The power state comes from the status whose code starts with `PowerState/`
in the VM's instance view: `running`, `deallocated`, `stopped`, `starting`,
`stopping` or `deallocating`. A `stopped` VM still costs compute; a
`deallocated` one does not. It is `null` when Azure gave none: Microsoft says
this can happen in rare cases and advises to ask again. The VM list and the
status list are joined by the resource ID in lower case, because Azure does
not always keep the case of a resource group the same.

`azure.vm.list` also returns `power_state_read`, an object that says how
complete the power states are:

```json
{"vms_without_state": 0, "pages_read": 1, "truncated": false,
 "omission": null, "omission_kind": null}
```

`truncated`, `omission` and `omission_kind` describe read 2 the same way the
paging fields describe read 1. When read 2 fails (for example Azure refuses
it, or keeps throttling past MEHO's retry budget), the whole operation fails
with Azure's error, like any read; ask again later. The guidance of `azure.vm.list` carries a
`result_objects` hint for this object, so it stays inline next to the
stored-result link when there are more than 50 VMs; without the hint the
result reduction would drop it. The paging fields of every list stay inline
on their own, because they are plain values. `test_connectors_azure_compute.py`
proves both with the real result reduction, for every list.

### Fields

The field names are a contract: the VM metrics read and the migration sizing
build on `azure.vm.list`, `azure.vm.show` and `azure.vm.size.list`. Change a
name only together with its readers. A field that is missing in Azure's
answer is `null` (or an empty list or object).

- VM row (`azure.vm.list`): `name`, `id`, `resource_group`, `location`,
  `zones`, `size`, `os_type`, `power_state`, `provisioning_state`,
  `os_disk_id`, `data_disk_ids`, `tags`.
- `azure.vm.show`: the VM row's fields without the disk IDs, plus `vm_id`,
  `os_name`, `os_version`, `computer_name`, `hyperv_generation` (V1 or V2),
  `os_disk` and `data_disks` (`lun`, `name`, `id`, `size_gb`, `sku`,
  `caching`), `network_interfaces` (`id`, `primary`), `availability_set_id`
  and `scale_set_id`. `os_name` and `os_version` come from the VM agent and
  are `null` when it does not run.
- Disk row: `name`, `id`, `resource_group`, `location`, `zones`, `size_gb`,
  `sku`, `disk_state` (`Attached`, `Unattached`, ...), `owner_vm_id`,
  `encryption_type`, `os_type`, `tags`.
- Image row: `name`, `id`, `resource_group`, `location`, `os_type`,
  `os_state`, `hyperv_generation`, `os_disk_size_gb`, `data_disk_count`,
  `source_vm_id`, `provisioning_state`, `tags`. Images in an Azure Compute
  Gallery are a different resource and are not listed.
- Scale set row: `name`, `id`, `resource_group`, `location`, `zones`, `size`,
  `capacity`, `orchestration_mode`, `upgrade_mode`, `os_type`,
  `provisioning_state`, `tags`.
- VM size row (`azure.vm.size.list`): `name`, `family`, `vcpus`,
  `vcpus_available`, `memory_gb`, `max_data_disks`, `premium_io`,
  `hyperv_generations` (a list), `acus`, `cpu_architecture` (x64 or Arm64),
  `zones`, `restrictions` (each with `type`, `reason_code`, `zones`). The
  answer also holds `location`.

### VM sizes

Azure's Resource SKUs answer holds every kind of compute SKU (disks,
availability sets, ...); only the rows whose resource type is
`virtualMachines` are kept. Only the `location` filter works on this call.
The numbers come from the SKU's capabilities (`vCPUs`, `vCPUsAvailable`,
`MemoryGB`, `MaxDataDiskCount`, `PremiumIO`, `HyperVGenerations`, `ACUs`,
`CpuArchitectureType`). Azure sends them as text; MEHO turns them into
numbers, and a value that is not a number becomes `null`.

How to read the restrictions:

- `type` `Location`: this subscription cannot use the size in this region.
- `type` `Zone`: it cannot use the size in the zones listed; the rest of the
  region works.
- `reason_code`: `NotAvailableForSubscription` or `QuotaId`.

`zones` lists the zones in which Azure offers the size, before restrictions.

### Safety

- Each row is built from a list of named fields, so nothing else from
  Azure's answer reaches the result: no admin user name, no custom data, no
  user data, no blob URL.
- `resource_group` and `name` may not hold `/`, `\`, `?`, `#`, `%` or spaces,
  and may not be dots only. The parameter schema checks them, the body checks
  them again (its error never repeats the value), and each one is
  percent-encoded so it stays one segment of the path. `location` must be
  letters and digits only, because it goes into Azure's filter text.
- Names, tags and states are text from Azure: the guidance tells the AI to
  treat them as data, never as instructions.

### Proven live

Nothing yet: the lab login does not exist yet. The operations are built from
Microsoft's documented answer shapes and proven with unit tests on mocked
HTTP. The place of the power state in a `statusOnly` answer (the instance
view under `properties`, as in Microsoft's VM model) is not yet seen live;
the lab proof checks it.

References:

- Virtual Machines - List All (`statusOnly`, `$expand`, `$filter`):
  <https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/list-all>
- VM power and provisioning states:
  <https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing>
- Resource SKUs - List (`$filter=location eq '<region>'`):
  <https://learn.microsoft.com/en-us/rest/api/compute/resource-skus/list>
- Disks - List: <https://learn.microsoft.com/en-us/rest/api/compute/disks/list>
