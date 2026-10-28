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
  line per list for each family.
- `_paths.py` keeps the paths, in one marked section per family. It has no
  `__all__`, because every family would add its names at the same place.

Every place that collects the families has one fixed comment line per family,
`# <family> (Task #<n>)`, in alphabetical order. A family adds its lines
directly under its own comment and leaves the comment as it is. Git merges two
changes without a conflict only when at least one unchanged line lies between
them, so two family branches then merge in any order. In the two import parts
an empty line follows each family comment, because Ruff wants an empty line
between two imports there; leave it as it is. The same holds for the
spec path check: its `_DECLARED` table has the same comment lines, and its list
of checked paths has the same lines as strings (`"# <family> (Task #<n>)"`),
because the test reads them to check that each block is sorted. The end of
this page has one section per family: a family replaces the line "Not built
yet" under its heading with its text and keeps the heading.

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

## aks

The `aks` family reads Azure Kubernetes Service (AKS): clusters, their node
pools, their upgrade options and the Kubernetes versions AKS offers per
region. All four operations are in the group `aks`, all are `safe`, and none
needs approval. Nothing changes in Azure. These reads show the Azure side of
a cluster. MEHO's Kubernetes connector reads inside a cluster, through the
cluster's own API, with a target of its own.

Files: `aks.py` (group text, guidance for the AI, operation table, and the
`AksOps` methods `AzureConnector` inherits), `aks_schemas.py` (parameter and
response schemas), `aks_rows.py` (how Azure's answers become rows, and the
parameter checks) and `aks_reads.py` (the calls).

### Operations

| Operation | Azure call | Parameters |
|---|---|---|
| `azure.aks.cluster.list` | `GET /subscriptions/{id}/providers/Microsoft.ContainerService/managedClusters` | none |
| `azure.aks.cluster.show` | `GET .../resourceGroups/{rg}/providers/Microsoft.ContainerService/managedClusters/{name}`, then `GET .../managedClusters/{name}/upgradeProfiles/default` | `resource_group`, `name` |
| `azure.aks.nodepool.list` | `GET .../managedClusters/{cluster}/agentPools` | `resource_group`, `cluster` |
| `azure.aks.version.list` | `GET /subscriptions/{id}/providers/Microsoft.ContainerService/locations/{location}/kubernetesVersions` | `location` |

Every call uses api-version `2026-08-01` (`AKS_API_VERSION`), the version of
the pinned spec file. Reader on the subscription allows every read here.

### No cluster credentials

A kubeconfig is a login to the cluster, so it is a secret. MEHO never calls
the Azure actions that return one: `listClusterUserCredential`,
`listClusterAdminCredential`, `listClusterMonitoringUserCredential`, and the
`listCredential` actions of access profiles and managed namespaces. No
operation returns a kubeconfig, a token or a certificate.

- `test_no_azure_path_ends_in_a_cluster_credential_action` fails when any
  `*_PATH` constant in `_paths.py` ends in one of these actions.
- `test_the_aks_paths_hold_no_credential_kubeconfig_or_command_path` fails
  when an AKS path names a credential, an access profile, a kubeconfig or a
  run command.
- The rows copy only the fields named below. A cluster answer can also hold
  the service principal's secret, the Windows admin password and the old
  Microsoft Entra ID server app secret (Azure marks them as secrets and
  does not return them in reads), SSH public keys, add-on settings, node
  labels and taints. None of them is copied. A test feeds made-up values
  for all of them through the real dispatcher and checks that none reaches
  the result.

### What each operation returns

The list answers have the rows under `value`, their number in `count`, and
the paging fields of `PagedRows.as_dict()` (`truncated`, `omission`,
`omission_kind`, `pages_read`).

- **Clusters**: `name`, `id`, `resource_group`, `location`,
  `kubernetes_version` (asked for; may be only `major.minor`),
  `current_kubernetes_version` (the version that runs), `provisioning_state`,
  `power_state` (`Running` or `Stopped`), `sku_tier`, `support_plan`,
  `node_resource_group`, `network_plugin`, `network_plugin_mode`,
  `network_policy`, `network_dataplane`, `outbound_type`, `private_cluster`
  (true only when `apiServerAccessProfile.enablePrivateCluster` is true),
  `identity_type`, `auto_upgrade_channel`, `node_os_upgrade_channel`,
  `node_pools` (the pool names), `node_count` (the sum of the pools' node
  counts) and `addons`.
- `identity_type` is `SystemAssigned` or `UserAssigned` when Azure reports
  that managed identity in `identity.type`. It is `ServicePrincipal` when the
  cluster has no managed identity and `servicePrincipalProfile.clientId`
  names an application: then the cluster logs in to Azure with a service
  principal and a secret. A managed identity cluster has `msi` in that
  field, which does not count. Otherwise it is null, also when Azure writes
  the text `None`.
- `addons` lists, sorted, Azure's own name of every `addonProfiles` entry
  with `enabled: true` (for example `omsagent`, `azurepolicy`,
  `azureKeyvaultSecretsProvider`, `ingressApplicationGateway`), plus MEHO's
  name for add-ons and managed features Azure keeps in other settings:

  | Name | Azure setting |
  |---|---|
  | `app_routing` | `ingressProfile.webAppRouting.enabled` |
  | `container_insights` | `azureMonitorProfile.containerInsights.enabled` |
  | `istio` | `serviceMeshProfile.mode` is `Istio` |
  | `keda` | `workloadAutoScalerProfile.keda.enabled` |
  | `managed_prometheus` | `azureMonitorProfile.metrics.enabled` |
  | `pod_identity` | `podIdentityProfile.enabled` (Microsoft's replacement is workload identity) |
  | `workload_identity` | `securityProfile.workloadIdentity.enabled` |

  Only a real `true` counts (a `1` does not). The add-on settings are never
  copied.
- **One cluster** (`azure.aks.cluster.show`): the cluster fields above, plus
  `fqdn` and `private_fqdn` (the API server host names, for example to match
  a Kubernetes target in MEHO), `pod_cidrs` and `service_cidrs` (from
  `podCidrs` and `serviceCidrs`, or the single `podCidr` and `serviceCidr`),
  and `upgrades`: `control_plane` and one entry per node pool (`name`,
  `os_type`), each with `kubernetes_version`, `upgrades` (released versions)
  and `preview_upgrades` (versions Azure marks `isPreview`). Both lists are
  sorted by version number, oldest first. A missing cluster is `not_found`,
  and then the upgrade profile is not read.
- **Node pools**: `name`, `mode` (`System`, `User` or `Gateway`), `vm_size`,
  `count`, `autoscaling`, `min_count`, `max_count`, `os_type`, `os_sku`,
  `orchestrator_version`, `current_orchestrator_version`,
  `node_image_version`, `power_state`, `provisioning_state`,
  `availability_zones`, `max_pods`, `priority` (`Regular` or `Spot`) and
  `pool_type`, plus `cluster` and `resource_group` next to the rows.
- **Versions**: one row per Kubernetes minor version: `version`,
  `is_default`, `is_preview`, `support_plans` (`KubernetesOfficial`, and
  `AKSLongTermSupport` when long-term support is offered) and
  `patch_versions`, plus `location` and `default_version`. Azure answers
  with `values` (not `value`) and no next page, so this read is one GET
  without paging fields. Rows and patch versions are sorted by version
  number, so `1.29.10` comes after `1.29.9`. A cluster version that is in no
  row's `patch_versions` is no longer offered in the region, and its support
  ends after a short grace period. A minor version in no row is out of
  regular support. A row whose `support_plans` holds only
  `AKSLongTermSupport` is supported only for clusters on the long-term
  support plan (`support_plan` `AKSLongTermSupport`).

### Parameter checks

`resource_group` (letters and digits of any script, `_`, `-`, `.`, `(` and
`)`, not only dots, at most 90 characters), the cluster `name` or `cluster`
(Azure's own pattern: 1 to 63 letters, digits, `-` and `_`, starting and
ending with a letter or a digit) and `location` (letters and digits only)
go into a path. So no slash, `?`, `#`, `%`, space or `..` can reach a
request.

The patterns are in the parameter schemas, so the dispatcher refuses a bad
value with `invalid_params` before the operation runs. The operation checks
each value again with a full match: a `$` in a JSON Schema pattern lets a
line break at the end through, and the check in the operation does not. A
value it refuses fails with `AksParameterError` (the dispatcher returns
`connector_error` with `remediation`). Both checks run before the login and
before any request, and no message repeats the value.

### Result handles

A list over 50 rows or 4 KB becomes a link to the stored result. AKS allows
up to 100 node pools per cluster, so the node pool list can pass 50 rows.
The plain values next to the rows (`count`, the paging fields, `cluster`,
`resource_group`, `location`, `default_version`) stay inline under the
reducer's default rule. No list carries an object next to its rows, so no
operation needs a `result_objects` hint. `test_connectors_azure_aks.py`
proves this for all three lists with more than 50 rows, with the real
reducer and `result_query`.

`azure.aks.cluster.show` is one object, not a list. Up to 4 KB it comes back
whole. A bigger answer (many node pools) is kept whole as a stored document:
the inline summary holds the plain values and the outline of the fields,
and `result_query` with `path` `/upgrades` reads the upgrade options. A test
proves this too.

### Untrusted text

Cluster, pool and resource group names, versions and add-on names are text
from Azure. They are copied as data and never used to build a request: the
paths take only the caller's checked parameters. The guidance of every
operation tells the AI to treat this text as data, never as instructions.

### Spec path check

The five paths are `*_PATH` constants in the `aks` section of `_paths.py`,
each with one line in the check's `_DECLARED` table, and all five are in the
pinned file
`specification/containerservice/resource-manager/Microsoft.ContainerService/aks/stable/2026-08-01/managedClusters.json`.

### Proven live

Nothing yet: the lab login does not exist. Every operation is proven with
mocked HTTP shaped like Microsoft's documented examples for api-version
2026-08-01. The lab proof lists the lab cluster with its real version and
node pools (compared with the portal), shows its upgrade options, checks
that the version list of its region holds its version, and checks that no
result holds a kubeconfig, a token or a certificate.

### Known issues

- `msi` in `servicePrincipalProfile.clientId` of a managed identity cluster
  is what Azure returns in practice; Microsoft's documentation does not name
  the value. The lab proof checks `identity_type` against the portal.
- `kubernetes_version` holds what was asked for, which may be only
  `major.minor`. Compare `current_kubernetes_version` with the version list.
- The node pool list follows `nextLink` with the shared paging helper; AKS
  usually answers in one page.

### References

- Managed Clusters - List: <https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list>
- Managed Clusters - Get: <https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get>
- Managed Clusters - Get Upgrade Profile: <https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get-upgrade-profile>
- Agent Pools - List: <https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/list>
- Managed Clusters - List Kubernetes Versions: <https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-kubernetes-versions>
- AKS add-ons: <https://learn.microsoft.com/en-us/azure/aks/integrations>
- Supported Kubernetes versions and long-term support: <https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions>
- Managed identities in AKS: <https://learn.microsoft.com/en-us/azure/aks/managed-identity-overview>
- Reader role: <https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/general#reader>

## arc

Not built yet (Task #5084).

## automation

The `automation` group reads Azure Automation: the runbooks a team runs for
maintenance, their jobs and their schedules. Every operation is a read
(`safe`, no approval) and changes nothing in Azure. Every call goes to
`Microsoft.Automation` with API version `2024-10-23`.

Files: `automation.py` (group text, guidance for the AI, operation table, and
`AutomationOps`, a class whose methods `AzureConnector` takes over as one of
its base classes), `automation_reads.py` (name checks, row shapes and the
operation bodies), `automation_schemas.py` (name rules, parameter and
response schemas), `automation_trace.py` (what the trace keeps of an
answer). The paths are in the `automation` section of `_paths.py`.

### Operations

| Operation | Parameters | Reads |
|---|---|---|
| `azure.automation.account.list` | none | `GET /subscriptions/{s}/providers/Microsoft.Automation/automationAccounts` |
| `azure.automation.runbook.list` | `resource_group`, `account` | `.../automationAccounts/{account}/runbooks` |
| `azure.automation.job.list` | `resource_group`, `account`, optional `days` (1 to 30, default 7) and `status` | `.../automationAccounts/{account}/jobs`, with a `$filter` |
| `azure.automation.schedule.list` | `resource_group`, `account` | `.../automationAccounts/{account}/schedules`, then `.../jobSchedules` |

`account` is the Automation account name and `resource_group` its resource
group, both from `azure.automation.account.list` (MEHO reads the resource
group from the account's resource ID). Every list follows `nextLink` with
`list_all_pages` and its limits (see Paging above).

| Operation | Row fields |
|---|---|
| `azure.automation.account.list` | `name`, `id`, `resource_group`, `location`, `state` (`Ok`, `Unavailable`, `Suspended`), `sku` (`Free`, `Basic`), `creation_time`, `last_modified_time` |
| `azure.automation.runbook.list` | `name`, `runbook_type`, `state` (`New`, `Edit`, `Published`), `description`, `creation_time`, `last_modified_time`, `last_modified_by` |
| `azure.automation.job.list` | `name`, `job_id`, `runbook`, `status`, `creation_time`, `start_time`, `end_time`, `started_by`, `run_on` |
| `azure.automation.schedule.list` | `name`, `enabled`, `frequency`, `interval`, `next_run`, `start_time`, `expiry_time`, `time_zone`, `week_days`, `month_days`, `monthly_occurrences`, `description`, `runbooks` |

### What is never read

Runbook code, job output and streams, credentials and variables can hold
secrets. No operation reads them:

- No path in `_paths.py` has a fixed part that ends in `content`, `output`,
  `streams`, `credentials` or `variables` (so `runbookContent` is out too).
  `test_connectors_azure_automation.py` fails when such a path is added, in
  any family.
- The rows copy only the fields above. Azure's runbook answer also holds
  content links (`publishContentLink`, `draftContentLink`) and the default
  values of runbook parameters; a job schedule holds the `parameters` the
  runbook gets; an account holds `automationHybridServiceUrl`. None of them
  is copied. The tests feed such values in and check that they never reach
  the result.
- Azure's job list has no error text or output (`exception` and
  `statusDetails` are only in the answer for one job), so
  `azure.automation.job.list` has none either.
- The trace (the flight recorder's record of each call) leaves these fields
  out too. Its shared filter finds secrets by their shape only, and a
  parameter value or a signed link can have any shape. So every Automation
  list is read through `TraceMaskingCaller` (`automation_trace.py`): the
  trace gets Azure's answer with `parameters`, `publishContentLink` and
  `draft` in each row replaced by a fixed text. A test switches the recorder
  on and checks the spans.

Names, descriptions, `last_modified_by` and `started_by` are text from Azure.
MEHO copies them as data and never puts them into a path or a filter. The
guidance tells the AI to treat them as data, never as instructions, and the
dispatcher's secret filter runs on every result.

### Jobs: the time window and the status

Azure removes a job 30 days after its last change, so `days` is at most 30
(7 by default). MEHO sends Azure a filter in the form that Microsoft's own
Az.Automation PowerShell module sends (`Get-AzAutomationJob -StartTime
-Status`):

```text
properties/startTime ge 2026-10-20T09:30:15.0000000Z and properties/status eq 'Failed'
```

The status part is there only when `status` is given. It must be one of
Azure's job states: `New`, `Activating`, `Running`, `Completed`, `Failed`,
`Stopped`, `Blocked`, `Suspended`, `Disconnected`, `Suspending`, `Stopping`,
`Resuming` or `Removing`. The result carries `window` (`days`, `from`, `to`,
in UTC) and `status_filter`. A job that has not started yet has no start
time, so it can be missing from the list.

### Schedules and their runbooks

A schedule does not name its runbooks. The links are job schedules
(`.../jobSchedules`), so `azure.automation.schedule.list` reads both lists
and joins them by schedule name (case does not matter). `runbooks` is the
sorted list of linked runbook names; `[]` means no runbook is linked.

When the job schedules cannot be read (for example after a 403), the
schedules still come back, with `runbooks: null` (not known), and
`omissions` names `runbook links` with the reason. A login error stops the
whole read.

Weekly and monthly details come from Azure's `advancedSchedule`:
`week_days`, `month_days` and `monthly_occurrences` such as `1 Monday` (the
first Monday of the month; `-1` is the last).

### Result shape and result handles

Every read returns:

```json
{"value": [...], "truncated": false,
 "omissions": {"count": 0, "items": [{"what": "...", "reason": "...", "detail": "..."}]}}
```

The job list also returns `window` and `status_filter`. `truncated` is
true when something is missing from the answer: rows, or the runbook links
of the schedules. `reason` is a paging limit (`page_limit`, `time_limit`,
`row_limit`) or, for the schedule links, an error (`no_permission`,
`not_found`, `provider_not_registered`, `throttled`, `read_failed`).
`detail` is MEHO's own text; Azure's message is never copied into it.

A list over 50 rows or 4 KB comes back as a link to the stored result. The
reducer keeps scalar values such as `truncated` next to the link, but not
objects. So each operation's `llm_instructions` carries a `result_objects`
hint that names `omissions` (and `window` for the job list); the reducer
keeps their fields inline and stores the whole objects with the rows.
`test_connectors_azure_automation.py` proves this for each operation with 60
rows, through the real dispatcher, the real reducer and the real result
store, and for a paging limit on the job list.

### Names in paths

`resource_group` must match Azure's rule for resource group names. `account`
must match Azure's rule for Automation account names: 6 to 50 letters,
digits and `-`, starting with a letter and ending with a letter or a digit.
The dispatcher checks the parameter schemas first; the bodies check again
with a full match (a schema `$` also matches before a line end). So no name
can change the path.

### Roles and provider

Every read needs the Reader role on the account, its resource group or the
subscription. `Microsoft.Automation` must be registered on the subscription;
when it is not, the call fails with `AzureProviderNotRegisteredError`, and
MEHO never registers it.

### Not in this group yet

Starting a job, publishing a runbook and changing a schedule are writes for
later Tasks. They change the customer's side, so they will be `caution` and
need approval.

### Proven live

Nothing yet. The lab login does not exist yet, so every read is proven with
mocked HTTP only, with answers shaped like the examples in Azure's pinned
API description file. Open points for the lab proof:

- that Azure accepts the job filter as MEHO sends it (the query string
  writes a space as `+`);
- whether a job that has not started yet shows up in the list;
- what Azure puts into `started_by` for a job started by a schedule.

### References

- Job - List By Automation Account (2024-10-23):
  <https://learn.microsoft.com/en-us/rest/api/automation/job/list-by-automation-account?view=rest-automation-2024-10-23>
- Get-AzAutomationJob (`-StartTime`, `-Status`):
  <https://learn.microsoft.com/en-us/powershell/module/az.automation/get-azautomationjob>
- How long Azure Automation keeps data (jobs: 30 days):
  <https://learn.microsoft.com/en-us/azure/automation/automation-managing-data>
- Naming rules (`Microsoft.Automation`, resource groups):
  <https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules>
- Pinned API description file:
  `Microsoft.Automation/Automation/stable/2024-10-23/openapi.json` in
  <https://github.com/Azure/azure-rest-api-specs>.

## avs

Not built yet (Task #5091).

## compute

Not built yet (Task #5083).

## cost

Five read operations in the group `cost`. All are `safe` and need no
approval. The cost query and the forecast are POST calls that only read.

Modules: `cost.py` (group text, guidance for the AI, the operation table and
the `CostOps` mixin that `AzureConnector` inherits), `cost_schemas.py`
(parameter and response schemas), `cost_reads.py` (query, forecast and the
shared helpers), `cost_lists.py` (budgets and Advisor) and `cost_prices.py`
(the public price list).

| Operation | Call | API version |
|---|---|---|
| `azure.cost.query` | `POST /{scope}/providers/Microsoft.CostManagement/query` | `2026-06-01` |
| `azure.cost.forecast` | `POST /{scope}/providers/Microsoft.CostManagement/forecast` | `2026-06-01` |
| `azure.cost.budget.list` | `GET /{scope}/providers/Microsoft.Consumption/budgets` | `2026-06-01` |
| `azure.advisor.recommendation.list` | `GET /subscriptions/{id}/providers/Microsoft.Advisor/recommendations` | `2026-10-01` |
| `azure.price.list` | `GET https://prices.azure.com/api/retail/prices` (public, no login) | `2023-01-01-preview` |

`{scope}` is the target's subscription, or one of its resource groups when
the call names `resource_group`. A resource group name is checked (1 to 90
letters, digits, `_ - . ( )`, not ending with a period) before it goes into
a path.

### Parameters that matter

- `azure.cost.query`:
  - `type`: `actual` (the default, cost as billed) or `amortized`
    (reservation and savings plan purchases spread over their term).
  - `period`: `month_to_date` (the default) or `last_month`, or your own
    days with `from` and `to` (`YYYY-MM-DD`). MEHO always sends dates to
    Azure (`timeframe: Custom`, days in UTC), so the result shows exactly
    which days it covers.
  - `granularity`: `total` (the default; Azure's `None`) or `daily`.
  - `group_by`: up to two of `resource_group`, `service`, `resource_type`,
    `location`, `meter`, or `tag:<key>` for a tag key.
- `azure.cost.forecast`: `type` and `resource_group`. Always this calendar
  month (UTC), per day, with the cost so far included
  (`includeActualCost: true`).
- `azure.cost.budget.list`: `resource_group`. The subscription's list can also
  hold budgets of its resource groups; each budget says which (`resource_group`).
- `azure.advisor.recommendation.list`: `category` (Azure filters it with
  `$filter`) and `impact` (MEHO filters it).
- `azure.price.list`: at least one of `service_name`, `region`, `sku` (the
  size name Azure uses, for example `Standard_D4s_v5`), `price_type`,
  `product_name`, `meter_name`, and `currency` (default `EUR`). From API
  version `2023-01-01-preview` on, values must match exactly, in upper and
  lower case; MEHO writes `region` in lower case, as the price list does.

### Results

Each list keeps its metadata in objects next to the rows, and each operation
names them in its `result_objects` hint. So when a list has more than 50 rows
(or 4 KB) and goes to a stored result, these objects stay inline next to the
link:

| Operation | Rows | Kept next to a stored result |
|---|---|---|
| `azure.cost.query` | `items` | `totals`, `period`, `scope`, `query`, `omissions` |
| `azure.cost.forecast` | `items` | `totals`, `period`, `scope`, `query`, `omissions` |
| `azure.cost.budget.list` | `budgets` | `scope`, `omissions`, and `count` |
| `azure.advisor.recommendation.list` | `items` | `filters`, `scope`, `omissions`, and `count` |
| `azure.price.list` | `items` | `lookup`, `omissions` |

- **Cost rows** have `cost`, `currency`, `date` (only per day) and one key
  per group (`resource_group`, `service`, `resource_type`, `location`,
  `meter`, `tag_key`, `tag_value`). Azure names its cost column `PreTaxCost`
  or `Cost`, depending on the agreement type. MEHO reads both, and
  `query.cost_column` says which one Azure sent. When the rows have a cost
  but no such column, the call fails: MEHO does not guess.
- **`totals.cost`** is the sum of the rows, rounded to 2 decimals. It is
  `null` when the rows have more than one currency. It is not called
  `total`, because the result reduction writes its own `total` (the number
  of rows) next to a stored result.
- **Forecast rows** have `status`: `Actual` (cost already used) or
  `Forecast` (Azure's estimate). `totals.month_total` is `actual_cost` plus
  `forecast_cost`.
- **Budgets** have `name`, `resource_group` (read from the budget's id; `null`
  for a budget of the whole subscription), `category`, `amount`, `time_grain`,
  `period: {start, end}`, `current_spend`, `forecast_spend`, `percent_used`
  and `currency`. `percent_used` is `current_spend / amount * 100`, rounded to
  2 decimals, and `null` when the amount is 0. The budgets' notification
  settings are left out, because they hold people's mail addresses.
- **Advisor items** have `category`, `impact`, `problem`, `solution`,
  `resource_id`, `status`, `savings_amount`, `annual_savings_amount` and
  `savings_currency` (from Azure's `extendedProperties`). MEHO leaves out
  dismissed, postponed and completed recommendations, as the portal does,
  and `filters.inactive_left_out` says how many. `count` is the number of
  items.
- **Prices** have `service_name`, `product_name`, `sku_name`, `arm_sku_name`
  (the size name Azure uses, which the `sku` filter matches), `meter_id`,
  `meter_name`, `region`, `location`, `unit`, `retail_price`, `unit_price`,
  `price_type`, `reservation_term`, `effective_start_date`, `currency`,
  `tier_minimum_units` and `is_primary_meter_region`. The migration estimate
  reads these names. `lookup` has `looked_up_at`, the
  currency, the filter MEHO sent, and `reference_only`: prices in other
  currencies than USD are converted reference values, not quotes.
- The Azure sensor recipes read two of these fields by path, so they are
  contracts: `budgets[*].percent_used` and Advisor's `count`.

Text from Azure (resource group and budget names, Advisor's problem and
solution, price names) is data. The guidance for the AI says so.

### The public price list never gets a token

The Retail Prices API is not Azure Resource Manager. `cost_prices.py` does
not use `send` (which adds the target's token). It opens its own short-lived
HTTP client with no `Authorization` header, follows no redirect, and caps the
answer size like every connector call. It does not read the target's secret
at all. MEHO builds the `$filter` text itself from the parameters and doubles
a single quote inside a value. From Azure's `NextPageLink` it reads only the
`$skip` number, after checking that the link points at the price list over
https, and it asks for the next page with its own query. A link to anything
else fails the call. Each page is recorded in the trace like other vendor
calls. A test proves that the request to the price list carries no
`Authorization` header, also after an Azure Resource Manager call put a token
in the cache.

### Errors that name a billing setting

Cost data can be hidden from a login that has the right role. When Azure
answers a cost query, forecast or budget read like this, MEHO's error names
what the customer must switch on:

| Azure answer | MEHO's error names |
|---|---|
| 401, also after a new login | "Account owners can view charges" (Enterprise Agreement) and the billing profile's "Azure charges" policy (Microsoft Customer Agreement), and the Cost Management Reader role |
| `AccountCostDisabled` | "Account owners can view charges" (AO view charges) |
| `DepartmentCostDisabled` | "Department admins can view charges" (DA view charges) |
| `IndirectCostDisabled` | the partner must publish its prices |
| `FailedDependency` (forecast) | about one week of cost data is needed, and only one currency |

The 401 error stays a `connector_auth_failed` error. The other errors keep
their kind (for example `AzureForbiddenError`) and Azure's code; only the
message and the remediation are MEHO's. A login that Microsoft Entra ID
refuses (for example a wrong or expired client secret) keeps its own error:
there the secret is the problem, not a billing setting. The
`FailedDependency` text is added only to the forecast.

### Proven live

Nothing yet: the lab login does not exist. The operations are built from
Microsoft's documented answers and tested with mocked HTTP and the real
result reduction (`backend/tests/test_connectors_azure_cost.py`). The lab
proof must still show:

- which cost column (`PreTaxCost` or `Cost`) the lab's agreement type sends
  (`query.cost_column`);
- whether Advisor's list includes dismissed or postponed recommendations
  (MEHO leaves them out either way);
- whether today's cost comes as an `Actual` or a `Forecast` row of the
  forecast (MEHO asks without the incomplete cost of today,
  `includeFreshPartialCost: false`);
- which HTTP status Azure uses for the cost-visibility errors.

### Known limits

- Azure's cost data arrives 8 to 24 hours late (up to 72 hours for
  pay-as-you-go), so today's cost is not complete. The costs of a month that
  has no invoice yet are estimates.
- Cost Management answers a 429 with its own wait headers
  (`x-ms-ratelimit-microsoft.costmanagement-*-retry-after`). The shared
  retry budget reads only `Retry-After`, so it waits its short default
  back-off instead.
- The price list is not retried. A 429 fails at once and says how long Azure
  asked to wait.
- All five operations read one subscription, the target's.

### References

- Query - Usage (API version 2026-06-01):
  <https://learn.microsoft.com/en-us/rest/api/cost-management/query/usage>
- Forecast - Usage (API version 2026-06-01):
  <https://learn.microsoft.com/en-us/rest/api/cost-management/forecast/usage>
- Budgets - List (Microsoft.Consumption):
  <https://learn.microsoft.com/en-us/rest/api/consumption/budgets/list>
- Recommendations - List (Advisor, API version 2026-10-01):
  <https://learn.microsoft.com/en-us/rest/api/advisor/recommendations/list>
- Advisor savings in `extendedProperties`:
  <https://learn.microsoft.com/en-us/azure/advisor/resource-graph-samples>
- Azure Retail Prices API:
  <https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices>
- Cost Management error codes:
  <https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/cost-management-error-codes>
- How fresh cost data is, and rounding:
  <https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data>
- Access to cost data (AO view charges, Azure charges):
  <https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/assign-access-acm-data>
- The evidenced exclusion of the price list from the spec path check:
  `docs/decisions/spec-reconcile-guards-standard.md`.

## inventory

Not built yet (Task #5082).

## migrate

Not built yet (Task #5093).

## monitor

Not built yet (Task #5090).

## network

Not built yet (Task #5085).

## security

Not built yet (Task #5089).

## storage

Not built yet (Task #5086).
