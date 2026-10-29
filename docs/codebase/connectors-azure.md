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

The arc family reads the Azure Arc machines of the target's subscription
(servers outside Azure, in a data center or another cloud, that are
connected to Azure) and the patching results Azure Update Manager stores.
It can also check one machine for missing updates, after a person approves.
All five operations are in group `arc`.

| Operation | Risk level | What it does | Parameters |
|---|---|---|---|
| `azure.arc.machine.list` | `safe` | every Arc machine: name, OS, connection status, agent version, last status change, location, tags; plus counts by status | none |
| `azure.arc.machine.show` | `safe` | one Arc machine: the list fields plus host name, domain, OS edition, update settings (`assessment_mode`, `patch_mode`), the agent's services and its errors | `resource_group`, `name` |
| `azure.patching.assessment.list` | `safe` | the last stored update check of each Azure VM and Arc machine: OS, time of the check, missing updates by class (critical, security, other), reboot pending | none |
| `azure.patching.installation.list` | `safe` | past update installation runs: machine, time, status, installed, failed and pending counts, reboot status | `days` (1 to 30, default 30) |
| `azure.patching.assessment.start` | `caution`, waits for approval | checks one Azure VM or Arc machine for missing updates now and returns the counts by class and the time of the check | `machine_id` |

The ids say "patching", not "patch": `patch` is a write word
(`WRITE_WORDS` in `delete_names.py`), and a `safe` id has none.

**API versions.** The Arc calls use `2026-09-01` (`ARC_API_VERSION`), the
check of an Azure VM `2026-04-01` (`VM_ASSESS_API_VERSION`), and the two
patching lists use Resource Graph (`2024-04-01`). Every path is a `*_PATH`
constant in the `arc` section of `_paths.py`, and the spec path check finds
each of them, with its API version, in the pinned spec files
(`hybridcompute` and `compute`).

**Files.** `arc.py` holds the group text, the guidance for the AI, the
operation table (`OPS`) and the mixin `ArcOps`. `arc_reads.py` holds the
bodies of the four reads, `arc_assess.py` the body of the check,
`arc_rows.py` builds the rows and counts, `arc_schemas.py` holds the schemas.

**Stored patching results.** Azure Update Manager keeps its results in two
Azure Resource Graph tables: `patchassessmentresources` (the last check of
each machine, kept about 7 days) and `patchinstallationresources`
(installation runs, kept 30 days). Both lists send one fixed query with
`query_resource_graph`, with `subscriptions` set to the target's own
subscription only, and follow `$skipToken`. The queries leave out the rows
of single updates (`softwarepatches`) and use no `limit`, `take` or
`sample`, so every page can be read. The only value MEHO puts into a query
is the start of the installation window, a time it computes itself from the
checked `days`. A result whose ID names another subscription is left out.
The machine of a result is read from the result's ID: the part before
`/patchAssessmentResults/` or `/patchInstallationResults/`.

**Counts.** `critical` and `security` are Azure's counts of those classes;
`other` adds up every other class (definitions, rollups, tools, ...). When
Azure reports no counts, all three are `null`, never `0`. The check of an
Azure VM answers with two totals (`criticalAndSecurityPatchCount`,
`otherPatchCount`) and a list of the missing updates; MEHO counts
`critical`, `security` and `by_class` from that list. An Arc machine answers
with counts per class (`availablePatchCountByClassification`).

**Result shapes.** Each list returns its rows under `value`, a `summary`
object with counts over every row read, and the paging fields (`truncated`,
`omission`, `omission_kind`, `pages_read`). The installation list also
returns `window: {days, from, to}`. More than 50 rows come back as a link to
the stored result; the `llm_instructions` of each list carry a
`result_objects` hint for `summary` (and `window`), so these objects stay
inline next to the link.

**The check (`azure.patching.assessment.start`).**

1. `machine_id` must be the ID of one Azure VM
   (`Microsoft.Compute/virtualMachines`) or one Arc machine
   (`Microsoft.HybridCompute/machines`) in the target's subscription, in any
   letter case. Another subscription, another type, a longer or shorter ID,
   or a name with `..`, `?`, `#` or `%` is refused before any call.
2. MEHO sends one `POST .../assessPatches` with no body. Like every write it
   is sent again only after a 429 or a 401, never after a timeout or a 5xx.
3. Azure answers 202. Both API descriptions put the result at the `Location`
   URL (`final-state-via: location`), so MEHO polls that URL with
   `wait_for_operation` (202 while the check runs, 200 with the result), at
   most 5 minutes. `operation_reference` would prefer `Azure-AsyncOperation`,
   so the family builds the reference itself; it falls back to
   `operation_reference` when there is no `Location`.
4. The answer is `{machine_id, machine_type, status, done, assessment, error,
   retry_after_seconds, operation}`. When the wait runs out, or a poll fails,
   `done` is `false` and `operation` is the reference:
   `azure.operation.status` reads the outcome later, also after a restart,
   without sending the check again.

A check installs no updates. The first check on a machine makes Azure add
its update extension to the machine. A second check is a new check: it
changes nothing on the machine and only gives fresh counts. The login needs
a role that allows `assessPatches` on the machine, for example Virtual
Machine Contributor for a VM.

**Secrets and untrusted text.** Every row is built from a fixed list of
fields. An Arc machine's answer also holds the agent's settings (for
example its proxy URL), a public key and a TPM certificate; none of them
reaches a result. Error codes and messages from Azure are filtered with
`clean_azure_text`. Names, tags and other text from Azure are data; the
guidance for the AI says so.

**Tests.** `backend/tests/test_connectors_azure_arc.py` runs every operation
through the real dispatcher with mocked HTTP: the row shapes, paging with
`nextLink` and `$skipToken`, the Resource Graph request body, 60 rows per
list through the real result reduction and `result_query` (the `summary`
and `window` objects stay inline), refused names, days and machine IDs,
`not_found`, the wait for approval, the check of a VM and of an Arc machine,
a wait that runs out followed by `azure.operation.status`, a failed poll, a
failed check and no second `POST` after a 5xx.

**Proven live:** nothing yet. The lab login does not exist yet, so the live
proof (a check on the lab test VM after approval, the same counts in
`azure.patching.assessment.list`, and one lab Linux VM connected to Azure
Arc) is still open.

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

The `avs` family reads Azure VMware Solution: VMware vSphere, vSAN and NSX
on dedicated Azure hosts. A customer's environment there is a private cloud
with a management cluster and maybe more clusters; a cluster starts at three
hosts. All four operations are in group `avs`, are `safe` and need no
approval: they only read, and nothing changes in Azure.

| Operation | Reads | Parameters |
|---|---|---|
| `azure.avs.privatecloud.list` | every private cloud of the subscription: host type (SKU), state, management cluster size and host names, network block and extra blocks, internet access, availability (single zone or stretched over two zones), managed identity type, VMware Cloud Foundation license kind | none |
| `azure.avs.cluster.list` | the clusters of one private cloud: host type (SKU), size, host names, state | `resource_group`, `private_cloud` |
| `azure.avs.host.list` | the hosts of one cluster: name, name and ID in vCenter (`display_name`, `mo_ref_id`), kind, state, maintenance, fault domain, host type (SKU), zones | `resource_group`, `private_cloud`, `cluster` |
| `azure.avs.quota.show` | the host quota of the subscription in one region: whether quota is on, and the hosts left per host type | `location` (a region name such as `westeurope`) |

**Group key.** The Task named the group `vmware-solution`. Each family module
has one group key named after the family, so the group is `avs`.

**API version.** Every call uses `2026-03-01` (`AVS_API_VERSION`). Every path
is a `*_PATH` constant in the `avs` section of `_paths.py`, and the spec path
check (see above) finds each of them, with its API version, in the pinned
`Microsoft.AVS` spec file. `checkQuotaAvailability` is a POST that only
reads: it has no body and is sent as a repeatable call, so it is tried again
after a 5xx like a GET.

**Files.** `avs.py` holds the group text, the guidance for the AI, the
operation table (`OPS`) and the mixin `AvsOps`, whose thin methods
`AzureConnector` inherits. `avs_reads.py` holds the bodies, the row builders
and the name checks; `avs_schemas.py` holds the schemas and the name rules.

**Names in a URL path.** `resource_group`, `private_cloud`, `cluster` and
`location` go into the URL path, so they are checked twice: by the parameter
schema (the dispatcher answers `invalid_params`) and again in the body. A
resource group has 1 to 90 letters, digits, `_`, `-`, `.`, `(` or `)` and
does not end with `.`. A private cloud or cluster name has 1 to 80 letters,
digits, `_`, `-` and `.` (the characters Azure's API description allows) and
is not only dots. A region name has letters and digits only. So a `/`, a `?`
or a name such as `..` is refused before any request and can never move the
call to another path or another subscription. A checked name is then
percent-encoded: a special character such as `(` is written as `%28`.

**Result shapes.**

- A list has its rows under `value`, plus `truncated`, `omission`,
  `omission_kind` and `pages_read` from the paging helper. The cluster and
  host lists also repeat the names they were asked for (`resource_group`,
  `private_cloud`, `cluster`). More than 50 rows (or more than 4 KB) come
  back as a link to the stored result. All these extra fields are plain
  values, so the result reduction keeps them next to the link without a
  `result_objects` hint; the tests prove it with the real reduction.
- Azure leaves the management cluster out of the cluster list (Microsoft's
  own CLI says the same). Its size and host names are in the private cloud
  row (`management_cluster_size`, `management_cluster_hosts`). The total host
  count of a private cloud is the management cluster size plus the sizes of
  the other clusters.
- Azure reports no vSphere, vSAN or NSX version for a private cloud. The
  version information in the row is `vcf_license_kind`: the VMware Cloud
  Foundation license the customer brought, for example `vcf5` (VCF 5.0), or
  null when none is attached. Azure VMware Solution needs such a license for
  new deployments.
- `azure.avs.quota.show` answers `{location, quota_enabled, hosts_remaining}`.
  `hosts_remaining` maps each host type to the hosts left, for example
  `{"AV36P": 3}`; an entry whose count is not a whole number is left out.
  Quota comes from a support request to Microsoft; the guidance tells the AI
  so.

**Provider not registered.** When the `Microsoft.AVS` provider is not
registered on the subscription, Azure answers `MissingSubscriptionRegistration`.
The caller gets `connector_error` with `exception_class`
`AzureProviderNotRegisteredError` and a hint to ask an administrator to
register it. MEHO never registers a provider, because that is a change to the
subscription.

**Secrets.** MEHO never calls the private cloud's `listAdminCredentials`
action (it returns the vCenter and NSX passwords), nor `getVcfLicense` or a
license's `getProperties` (they return license keys). A test fails when a
path to one of them is added to `_paths.py`. The private cloud's other secret
fields (`vcenterPassword`, `nsxtPassword`, an identity source's `password`
and the license key) are write-only in Azure's API description, so Azure's
answers to these reads do not hold them. Every row is also built from a fixed
list of fields, so even an answer that held them would not pass them on: a
test proves that they reach neither the result nor the stored rows. Names are
text from Azure; the guidance tells the AI to treat them as data.

**Tests.** `backend/tests/test_connectors_azure_avs.py` runs every operation
through the real dispatcher with mocked HTTP. Its Azure answers have the
shape of the examples next to the pinned API description, with made-up
values, and fill every field of every row. It covers the row shapes, a list
of 60 rows per list operation through the real result reduction and
`result_query`, a stopped list with its omission, the quota POST and its
retry after a 5xx, the provider-not-registered message, refused names,
`not_found`, and the secrets above.

**Proven live:** nothing yet. The lab login does not exist yet. The lab has
no private cloud (one needs at least three dedicated hosts), so the live
proof is an empty list or the clear "provider not registered" message from
`azure.avs.privatecloud.list`, and an answer or the same message from
`azure.avs.quota.show` for the lab region.

**References.**

- Azure VMware Solution REST API: <https://learn.microsoft.com/en-us/rest/api/avs/>
- Clusters of a private cloud, without the management cluster (`az vmware cluster`):
  <https://learn.microsoft.com/en-us/cli/azure/vmware/cluster>
- Host quota and the three-host minimum:
  <https://learn.microsoft.com/en-us/azure/azure-vmware/request-host-quota-azure-vmware-solution>
- Naming rules of private clouds and resource groups:
  <https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules>

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
The rows have no region field of their own, so MEHO also checks the region:
a row whose `locations`, or a restriction whose `values`, names only other
regions is left out, the same way Microsoft's own SKU library (skewer)
reads the answer.
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

## migrate

The `migrate` group reads what Azure Migrate knows about the servers a team
plans to move to Azure. Every operation is a read (`safe`, no approval) and
changes nothing in Azure.

Azure Migrate keeps its data with two providers, each with its own pinned
API version:

- `Microsoft.Migrate` (`2024-01-15`): assessment projects, the machines a
  project knows, groups of machines, assessments and the assessed machines
  of an assessment.
- `Microsoft.OffAzure` (`2023-06-06`): discovery sites. The Azure Migrate
  appliance puts the machines it finds into VMware, Hyper-V or physical
  server sites. A CSV import puts them into an import site. A master site
  links the other sites of a project.

Files: `migrate.py` (group text, guidance for the AI, operation table, and
`MigrateOps`, a class whose methods `AzureConnector` takes over as one of its
base classes), `migrate_reads.py` (the operation
bodies), `migrate_rows.py` (row shapes, name checks, counts; no calls to
Azure), `migrate_schemas.py` (parameter and response schemas). The paths
and API versions are in the `migrate` section of `_paths.py`.

### Operations

| Operation | Parameters | Reads |
|---|---|---|
| `azure.migrate.project.list` | none | `GET /subscriptions/{s}/providers/Microsoft.Migrate/assessmentProjects` |
| `azure.migrate.site.list` | `resource_group` | the five site lists of the resource group: `vmwareSites`, `hypervSites`, `serverSites`, `importSites`, `masterSites` |
| `azure.migrate.machine.list` | `resource_group`, `project` | `.../assessmentProjects/{project}/machines` |
| `azure.migrate.group.list` | `resource_group`, `project` | `.../assessmentProjects/{project}/groups` |
| `azure.migrate.assessment.list` | `resource_group`, `project`, optional `group` | `.../groups/{group}/assessments`, for one group or for every group |
| `azure.migrate.assessment.machine.list` | `resource_group`, `project`, `group`, `assessment` | `.../groups/{group}/assessments/{assessment}`, then `.../assessedMachines` |

`project` is the assessment project name and `resource_group` its resource
group, both from `azure.migrate.project.list`. Azure's paths hold the
resource group, so every project read needs it. `migrate_project` in a
project row is the Azure Migrate project name the portal shows; it is read
from the project's `assessmentSolutionId` and can differ from the assessment
project name.

Every list follows `nextLink` with `list_all_pages` and its limits (see
Paging above).

### Appliance or CSV import

Results from a CSV import and from the appliance stay apart. Every machine
row (`azure.migrate.machine.list`) and every assessed machine row
(`azure.migrate.assessment.machine.list`) has:

| Field | Value |
|---|---|
| `source` | `appliance`, `import` or `unknown` |
| `site_kind` | `vmware`, `hyperv`, `server` or `import` (or `null`) |
| `site` | the name of the discovery site |
| `discovery_machine_id` | the resource ID of the discovered machine |

`machine_origin` (`migrate_rows.py`) reads them from the ID of the
discovered machine (`discoveryMachineArmId` of a machine,
`datacenterMachineArmId` of an assessed machine):
`.../providers/Microsoft.OffAzure/<site type>/<site>/machines/<machine>`. A
VMware, Hyper-V or physical server site means `appliance`, an import site
means `import`. Any other ID gives `unknown`; MEHO never guesses. The ID is
read by position after `/providers/Microsoft.OffAzure/`, so a resource group
called like a site type does not confuse it. Machine lists are sorted by
source (appliance, import, unknown), then by name. Groups and assessments
also carry Azure's own `type` / `group_type` (`Default` or `Import`).

An imported machine has only the numbers of its CSV file: no measured
history, and it cannot be migrated. The guidance tells the AI to report the
two sources apart.

### Result shape and result handles

Every read returns:

```json
{"value": [...], "truncated": false,
 "omissions": {"count": 0, "items": [{"what": "...", "reason": "...", "detail": "..."}]},
 "...": "count blocks"}
```

`truncated` is true when rows are missing; `omissions` says what is missing
and why. `reason` is a paging limit (`page_limit`, `time_limit`,
`row_limit`), an error of one part (`no_permission`, `not_found`,
`provider_not_registered`, `throttled`, `read_failed`), `name_not_usable`
(Azure returned a group name MEHO does not put into a path) or
`group_limit`. `detail` is MEHO's own text; Azure's free text is never
copied into it.

Count blocks:

| Operation | Blocks |
|---|---|
| `azure.migrate.site.list` | `kind_counts`: sites per kind; `null` for a kind that could not be read |
| `azure.migrate.machine.list` | `source_counts`: machines per source |
| `azure.migrate.assessment.machine.list` | `assessment` (the assessment with its currency, sizing criterion, region and status), `source_counts`, `readiness_counts` (Azure's readiness values plus `Other`) |

A list over 50 rows or 4 KB comes back as a link to the stored result. The
reducer keeps scalar values such as `truncated` next to the link, but not
objects. So each operation's `llm_instructions` carries a `result_objects`
hint that names `omissions` and its count blocks; the reducer keeps their
fields inline and stores the whole objects with the rows (a `path` read on
the handle returns them). `test_connectors_azure_migrate.py` proves this for
each operation with 60 rows, through the real dispatcher, the real reducer
and the real result store.

### Reads made of several parts

- `azure.migrate.site.list` reads five lists. A kind that cannot be read
  (for example a 403 on one site type) becomes an omission and its count is
  `null`; the other kinds still count. When no kind can be read, the first
  error is returned as it is (a missing resource group comes back as
  `not_found`).
- `azure.migrate.assessment.list` without `group` lists the groups first,
  because Azure lists assessments per group only, and then reads each group,
  at most 50 (`MAX_GROUPS`; more groups give a `group_limit` omission). A
  group that cannot be read becomes an omission; when no group can be read,
  the first error is returned. With `group`, it reads that group only.
- A login error always stops the whole read.

### Names in paths

`resource_group` must match Azure's rule for resource group names.
`project`, `group` and `assessment` must match Azure's rule for Azure Migrate
names, without `%`, `\`, control characters, `.` or `..`. The dispatcher
checks the parameter schemas first; the bodies check again with a full match
(a schema `$` also matches before a line end). Group names that come from
Azure are checked the same way before MEHO puts them into a path. So no name
can change the path.

### Roles and providers

Reads need the Reader role on the resource group (or the subscription) of
the Azure Migrate project. Azure Migrate has no read-only role of its own.
`Microsoft.Migrate` and `Microsoft.OffAzure` must be registered on the
subscription; when they are not, the call fails with
`AzureProviderNotRegisteredError`, and MEHO never registers them.

### Not in this group yet

- `azure.migrate.report.link` (a short-lived download link for an assessment
  report, `dangerous`, after approval) is not built yet. The link is a
  secret: it must reach only the caller, never the audit row, a trace or the
  stored result.
- The writes (CSV import, groups, assessments) come later. They can reuse the
  name checks and rows in `migrate_rows.py` and `read_assessment` in
  `migrate_reads.py`.

### Proven live

Nothing yet. The lab login does not exist yet, so every read is proven with
mocked HTTP only, with answers shaped like the examples in Azure's pinned
API description files. Open points for the lab proof:

- that the ID of an imported machine points at its import site, as the API
  description implies;
- Azure's page size for machine lists, which is not documented; MEHO reads
  at most 50 pages and 10,000 rows and names the cut in `omissions`.

### References

- Machines - List By Assessment Project (2024-01-15):
  <https://learn.microsoft.com/en-us/rest/api/migrate/assessment/machines-operations/list-by-assessment-project?view=rest-migrate-assessment-2024-01-15>
- Azure Migrate built-in roles:
  <https://learn.microsoft.com/en-us/azure/migrate/prepare-azure-accounts>
- Assess servers from an imported CSV file:
  <https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import>
- Pinned API description files: `Microsoft.Migrate/AssessmentProjects/stable/2024-01-15/migrate.json`
  and `Microsoft.OffAzure/OffAzure/stable/2023-06-06/migrate.json` in
  <https://github.com/Azure/azure-rest-api-specs>.

## monitor

Not built yet (Task #5090).

## network

Not built yet (Task #5085).

## security

The `security` family reads the security and governance state of the
target's subscription: Microsoft Defender for Cloud, Azure Policy, role
assignments and the activity log. All nine operations are in the group
`security`, all are `safe`, and none needs approval. Nothing changes in Azure.
The compliance summary is a POST, but it only reads, so it is `safe` too.

Files: `security.py` (group text, guidance for the AI, operation table, and
the `SecurityOps` methods `AzureConnector` inherits), `security_schemas.py`
(parameter and response schemas), `security_parse.py` (how Azure's answers
become rows, and the parameter checks) and `security_reads.py` (the calls).
The shared pieces (`AzureTypedOp`, `NO_PARAMS`, `TEXT_OR_NULL`) are in
`_model.py`, so a family module and `typed_ops.py` never import each other in
a circle.

### Operations

| Operation | Azure call | api-version | Parameters |
|---|---|---|---|
| `azure.security.score.list` | `GET /subscriptions/{id}/providers/Microsoft.Security/secureScores` | 2020-01-01 | none |
| `azure.security.control.list` | `GET .../Microsoft.Security/secureScoreControls?$expand=definition` | 2020-01-01 | none |
| `azure.security.recommendation.list` | `GET /{scope}/providers/Microsoft.Security/assessments`, and when needed `GET .../Microsoft.Security/assessmentMetadata` | 2020-01-01, 2025-05-04 | `status`: `Unhealthy` (default), `Healthy`, `NotApplicable` or `All` |
| `azure.security.alert.list` | `GET /subscriptions/{id}/providers/Microsoft.Security/alerts` | 2022-01-01 | `days`: 1 to 90, default 7 |
| `azure.security.plan.list` | `GET /{scope}/providers/Microsoft.Security/pricings` | 2024-01-01 | none |
| `azure.policy.compliance.summary` | `POST /subscriptions/{id}/providers/Microsoft.PolicyInsights/policyStates/latest/summarize` | 2024-10-01 | none |
| `azure.policy.assignment.list` | `GET /subscriptions/{id}/providers/Microsoft.Authorization/policyAssignments` | 2026-07-01 | none |
| `azure.role.assignment.list` | `GET /{scope}/providers/Microsoft.Authorization/roleAssignments`, then `GET .../roleDefinitions?$filter=atScopeAndBelow()` once | 2022-04-01 | `scope` (optional, inside the subscription) |
| `azure.activity.list` | `GET /subscriptions/{id}/providers/Microsoft.Insights/eventtypes/management/values?$filter=...` | 2015-04-01 | `from`, `to`, `resource_group`, `caller`, `status` (all optional) |

`{scope}` is `subscriptions/<id>` unless the caller gives a scope. The API
versions are the ones Microsoft's documentation pages show. The pinned spec
files also hold newer versions for the recommendations (2025-05-04) and the
Defender plans (2026-07-01); MEHO uses them only after the lab proof shows
they work.

Roles: Reader on the subscription allows every read here. Security Reader
also allows the Defender for Cloud reads (Microsoft's table: "View alerts and
recommendations" for Security Reader and Reader).

### What each operation returns

Every list answer has the rows under `value`, their number in `count`, and
the paging fields of `PagedRows.as_dict()` (`truncated`, `omission`,
`omission_kind`, `pages_read`). The paging fields describe what MEHO read
from Azure, before a filter of MEHO's own.

- **Secure score**: `name`, `display_name`, `score`, `max_score`,
  `percentage`. Azure gives the percentage as a ratio from 0 to 1; MEHO
  returns a percent from 0 to 100.
- **Controls**: the same score fields per control, the healthy, unhealthy and
  not-applicable resource counts, and `recommendation_names`: the names of
  the recommendations in the control (from `$expand=definition`). Each name
  matches the `name` of a recommendation row.
- **Recommendations**: `name` (the recommendation type, the same for every
  resource), `display_name`, `severity`, `status`, `resource_id`, plus
  `status_filter`. Azure's list has no status filter, so MEHO reads every page
  and keeps the rows with the asked status. Severity comes from the row's
  `metadata` when Azure includes it; otherwise MEHO reads the assessment
  metadata of the subscription once per call and matches it by name.
  `resource_id` comes from `resourceDetails.id` (the key is read without
  regard to case) or, when missing, from the assessment id without its end.
- **Alerts**: `name`, `display_name`, `alert_type`, `severity`, `status`,
  `time` (`timeGeneratedUtc`, else `startTimeUtc`), `resource_id` (the
  `AzureResource` entry of `resourceIdentifiers`), `compromised_entity`, and
  `window: {days, from, to}`. Azure's list has no time filter, so MEHO keeps
  the alerts made inside the window. An alert without a readable time is
  kept. The alert description, the remediation steps, the entities and the
  evidence are left out.
- **Defender plans**: `name`, `tier` (`Standard` is on, `Free` is off),
  `sub_plan`, `deprecated`.
- **Compliance summary**: `non_compliant_resources`, `non_compliant_policies`
  and `compliant_resources` of the subscription at the top, and one row per
  policy assignment with the same three counts, `assignment_id`,
  `assignment_name` and `policy_set_definition_id` (set for an initiative).
  Azure sorts the assignments by non-compliant resources and writes the ids
  in lower case. MEHO sends no `$top`, so it gets every assignment Azure
  returns. The call is sent as repeatable: after a 5xx it is tried again,
  like a GET.
- **Policy assignments**: `id`, `name`, `display_name`, `scope`,
  `policy_definition_id`, `is_initiative`, `enforcement_mode` (`Default`
  when Azure leaves it out, Azure's documented default). No `$filter` is
  sent, so the list holds the assignments on the subscription, those
  inherited from management groups, and those on resource groups and
  resources in it.
- **Role assignments**: `name`, `principal_id`, `principal_type`,
  `role_name`, `role_definition_id`, `scope`, plus the `scope` MEHO read.
  Role ids become names through one role definitions read per call, matched
  by the role's GUID (so `/providers/...` and `/subscriptions/<id>/providers/...`
  forms both match). `role_name` is `null` when the role is not found.
  Principal names live in Microsoft Entra ID, which a separate Entra ID
  connector reads (a later Task); this operation returns ids only.
- **Activity log**: `time`, `operation`, `status`, `caller`, `resource_id`,
  `correlation_id`, `level`, and `window: {from, to, clamped}`. The event's
  `claims`, `httpRequest` (with the caller's IP address) and `properties`
  are left out.

### Parameter checks

A value that would change a request is checked before the login and before
any request. A bad value fails with `AzureParameterError` (the dispatcher
returns `connector_error` with `remediation`); the message never repeats the
value.

- `scope` of `azure.role.assignment.list` must start with the target's own
  subscription (`/subscriptions/<id>`, any case). Every further part may
  hold only letters and digits (any script), `_`, `-`, `.`, `(` and `)`, and
  must not be `.` or `..`. So no scope leaves the subscription, and no `?`,
  `#`, `%` or quote reaches the path.
- `resource_group` of `azure.activity.list` follows the same character rule
  (at most 90 characters), because it goes into the filter
  `resourceGroupName eq '<name>'`.
- The activity log window: `to` is now when not given, and a `to` after now
  is moved to now; `from` is 24 hours before `to` when not given. A `from`
  more than 90 days back is moved to 90 days back (the activity log keeps 90
  days). `clamped` says that MEHO moved one of them. An empty window, or a
  time that is not ISO 8601, fails. A time without an offset is UTC. The
  checks run before any date arithmetic, so a time such as the year 1 cannot
  overflow.

The activity log filter allows only the time range plus one of resource
group, resource, provider or correlation id ("No other syntax is allowed",
says Microsoft). So `caller` and `status` are compared by MEHO after reading,
without regard to case; they do not make the read smaller. MEHO sends no
`$select`, because Microsoft's list of selectable fields has no `caller`.

### Result handles

A list over 50 rows or 4 KB becomes a link to the stored result. The scalar
fields next to the rows (`count`, `status_filter`, `scope`, the three policy
totals, the paging fields) stay inline under the reducer's default rule.
The `window` objects of the alert list and the activity log stay inline
because their guidance carries a `result_objects` hint naming them.
`test_connectors_azure_security.py` proves this for every operation whose
list can pass 50 rows, with the real reducer and `result_query`.

### Untrusted text

Names, display names, alert titles, affected resource names and callers are
text from Azure. Some of it can be chosen by an attacker (for example a
resource name in an alert). It is copied as data and never used to build a
request, and the guidance of every operation tells the AI to treat it as
data, never as instructions.

### Spec path check

Every path is a `*_PATH` constant in `_paths.py`, in the section for this
family, with one line in the check's `_DECLARED` table. The check compares
each path and its API version with the pinned spec files.

### Proven live

Nothing yet: the lab login does not exist. Every operation is proven with
mocked HTTP shaped like Microsoft's documented examples. The lab proof checks
the secure score and controls against the portal, real recommendations and
plans, the non-compliant count against the portal's Policy page, the lab
logins' own role assignments, and the activity log of the last 7 days.

### Known issues

- The recommendation list may or may not carry `metadata.severity`; when it
  does not, one extra read of the assessment metadata fills it. The lab
  proof shows which case is real.
- The next page of a list is the `nextLink` as Azure sends it (the shared
  paging helper). Azure's own SDKs add the `api-version` again to a next-page
  request; the lab proof checks a second page of the activity log.
- MEHO sends no `$top` to the compliance summary. If Azure cuts the number
  of assignments without saying so, the rows would miss some; the lab proof
  compares the count with the portal.

### References

- Secure Scores - List: <https://learn.microsoft.com/en-us/rest/api/defenderforcloud/secure-scores/list>
- Secure Score Controls - List: <https://learn.microsoft.com/en-us/rest/api/defenderforcloud/secure-score-controls/list>
- Assessments - List: <https://learn.microsoft.com/en-us/rest/api/defenderforcloud/assessments/list>
- Assessments Metadata - List By Subscription: <https://learn.microsoft.com/en-us/rest/api/defenderforcloud/assessments-metadata/list-by-subscription>
- Alerts - List: <https://learn.microsoft.com/en-us/rest/api/defenderforcloud/alerts/list>
- Pricings - List: <https://learn.microsoft.com/en-us/rest/api/defenderforcloud/pricings/list>
- Policy States - Summarize For Subscription: <https://learn.microsoft.com/en-us/rest/api/policyinsights/policy-states/summarize-for-subscription>
- Policy Assignments - List: <https://learn.microsoft.com/en-us/rest/api/policy-authorization/policy-assignments/list>
- Role Assignments - List For Scope: <https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/list-for-scope>
- Role Definitions - List: <https://learn.microsoft.com/en-us/rest/api/authorization/role-definitions/list>
- Activity Logs - List: <https://learn.microsoft.com/en-us/rest/api/monitor/activity-logs/list>
- Activity log (90 days): <https://learn.microsoft.com/en-us/azure/azure-monitor/platform/activity-log>
- Defender for Cloud roles: <https://learn.microsoft.com/en-us/azure/defender-for-cloud/permissions>

## storage

The `storage` family reads Azure Storage: the storage accounts of the
target's subscription, the containers of one account, the files (blobs) of
one container, and one small text file. All four operations are in the group
`storage`, all are `safe`, and none needs approval. Nothing changes in Azure,
and MEHO never reads account keys, SAS tokens or connection strings.

Files: `storage.py` (group text, guidance for the AI, operation table, and
the `StorageOps` methods `AzureConnector` inherits), `storage_schemas.py`
(parameter and response schemas), `storage_parse.py` (name checks, rows, the
XML of the blob list, the text check) and `storage_reads.py` (the calls).

### Operations

| Operation | Host and call | Version | Parameters |
|---|---|---|---|
| `azure.storage.account.list` | Resource Manager: `GET /subscriptions/{id}/providers/Microsoft.Storage/storageAccounts` | api-version 2026-09-01 | none |
| `azure.storage.container.list` | Resource Manager: `GET .../storageAccounts/{account}/blobServices/default/containers` | api-version 2026-09-01 | `resource_group`, `account` |
| `azure.storage.blob.list` | Blob host: `GET /{container}?restype=container&comp=list` with `maxresults`, `prefix`, `marker` | x-ms-version 2026-10-06 | `account`, `container`, `prefix` (optional), `max_results` (1 to 10,000, default 5,000) |
| `azure.storage.blob.read` | Blob host: `HEAD /{container}/{name}`, then `GET` with `x-ms-range` | x-ms-version 2026-10-06 | `account`, `container`, `name` |

Roles: Reader on the subscription (or the account) allows the account and
container lists. The blob list and the file read need a data role, Storage
Blob Data Reader, on the account or the container; Reader alone gives no
access to the files. A new role can take up to 10 minutes to work.

### Two hosts, two tokens

The account and container lists go to Resource Manager with the usual
Resource Manager token. The blob list and the file read go to the account's
own blob host, `https://<account>.blob.core.windows.net` (in the other
clouds `blob.core.usgovcloudapi.net` and `blob.core.chinacloudapi.cn`). That
host needs a token for Azure Storage: scope `https://storage.azure.com/.default`,
the same in every cloud. It answers in XML, and the Blob service version goes
in the `x-ms-version` header.

- **Token cache.** `AzureConnector.authorize` and `_token_and_login` take a
  `scope`; without it the token is for Resource Manager. The cache key holds
  the scope, so a target has one Resource Manager token and one storage
  token, and a token never goes to the other API. The 401 hook drops both.
- **Through the shared transport.** The blob calls use `send()` in `_http.py`
  like every Azure call: one retry budget, the wait when Azure throttles, one
  new login after a 401, the limit per login and the URL check. `send()` asks
  its connector for the request headers before each try. `_StorageCaller` in
  `storage_reads.py` answers that question with the storage token and the
  Blob service headers; everything else is the connector's own.
- **Accounts of this subscription only.** Before a blob call, MEHO reads the
  subscription's storage accounts (one list call) and looks for the account
  by name. An account of another subscription is `not_found`, and nothing
  goes to its host.
- **The host is built, never taken.** The account name must pass Azure's rule
  (3 to 24 lower-case letters and digits) before the host
  `<name>.blob.<cloud suffix>` is built from it, so no parameter can change
  the host. The host must also be the blob endpoint Azure reports for the
  account (`primaryEndpoints.blob`). An account with another endpoint, for
  example an Azure DNS zone endpoint, is refused. The URL check admits this
  one host for the call and no other.
- **Names in paths.** Container names follow Azure's rule (or are `$root`,
  `$web`, `$logs`). A file name may hold any character and is sent
  percent-encoded, but no part between slashes may be `.` or `..`: the HTTP
  client would remove such a part and read another file. Every query value
  goes in `params`, because `httpx` replaces a URL's own query with `params`.

### What each operation returns

Every list answer has the rows under `value`, their number in `count`, and
the paging fields (`truncated`, `omission`, `omission_kind`, `pages_read`).
More than 50 rows (or 4 KB) go to a stored result. The container list and
the blob list also return `account: {name, resource_group}`; their guidance
carries a `result_objects` hint, so this object stays next to the
stored-result link. The blob list also returns `container` and `prefix`.

- **Accounts**: `name`, `resource_group` (from the account's id), `location`,
  `kind`, `sku`, `access_tier`, `shared_key_access`,
  `public_network_access`, `network_default_action`. Azure documents a
  missing `allowSharedKeyAccess` as true, so MEHO shows it as `true`.
  `network_default_action` (Allow or Deny) says what the account's firewall
  does with a network without a rule; it explains a public network access of
  `Enabled`.
- **Containers**: `name`, `public_access` (None, Blob or Container),
  `last_modified`, `lease_state`, `has_immutability_policy`,
  `has_legal_hold`, `version_immutability`.
- **Blobs**: `name`, `size` (bytes), `last_modified` (ISO 8601 UTC, from
  Azure's HTTP date), `content_md5` (Azure's base64 MD5 as 32 hex digits, or
  null), `content_type`, `access_tier`. A name Azure sends percent-encoded
  (`Encoded="true"`) is decoded. MEHO follows `NextMarker` page by page, up to
  5,000 rows a page, until the list ends, `max_results` is reached or a
  paging limit stops it; the omission then says so.
- **File read**: `account`, `container`, `name`, `size`, `content_type`,
  `content_md5`, `last_modified`, `encoding` (always `utf-8`) and `content`.
  A long file comes back as a stored-result link; `result_query` with the
  path `/content` reads it in parts.

### The file read

1. `HEAD` reads the file's properties. A file over 1 MiB (1,048,576 bytes) is
   refused with its size named; so is a file in the Archive tier, a file
   whose content type is image, audio, video, font or model, and a file
   stored with a content encoding such as gzip (the HTTP client would unpack
   it, and unpacked it can be far bigger than the limit). None of these bytes
   are read. An empty file is returned without a second call.
2. `GET` with `x-ms-range: bytes=0-1048575` reads the bytes, so a file that
   grew in between is never read past the limit. The `ETag` is compared with
   the first answer before the bytes are touched, then the size: when either
   differs, the file changed while MEHO read it, and the read is refused.
3. The bytes must be UTF-8 text: a byte order mark is dropped; invalid UTF-8,
   a NUL byte or another control character that text never has means binary,
   and the read is refused. Binary content is never returned as text.

The content is untrusted data. Like every result, it passes MEHO's secret
filter before the caller sees it.

### Errors

- A 403 from the blob host becomes `AzureForbiddenError` with MEHO's text: the
  login needs Storage Blob Data Reader on the account or the container, the
  Reader role alone is not enough, and the account's network rules may block
  MEHO. The blob host answers in XML, which `error_for_response` does not
  read, so Azure's own storage error code is not shown.
- A 404 from the blob host is `not_found` and names the container or the
  file.
- A parameter that breaks a name rule fails before any request with
  `StorageParameterError`; a refused file, an unusual account endpoint or an
  unreadable list fails with `StorageReadError`. Both carry a `remediation`.

### Secrets

Rows are built field by field, so a key, a SAS token or a connection string
in an Azure answer never reaches a result. The file read copies only the
properties it needs from the answer headers; `x-ms-copy-source`, which can
hold a SAS token, is never copied. MEHO never calls `listKeys`.

### Limits and choices

- Each blob list and file read starts with one storage account list call on
  Resource Manager. Azure allows about 100 such list calls per 5 minutes per
  subscription.
- Blob service version 2026-10-06: the pinned spec files hold 2026-10-06 and
  2026-12-06. The newer one is dated after this code was written, so the
  service may not take it yet. The lab proof checks which version the lab
  account accepts.
- Spec path check: the blob paths are data-plane paths, relative to the
  account's host. The check cuts the fixed query part, so List Blobs compares
  as `GET /{container}`, which Get Container Properties also matches.

### Proven

Nothing is proven live yet: the lab login does not exist. Every behaviour
above is proven with mocked HTTP in `backend/tests/test_connectors_azure_storage.py`,
built from Microsoft's documented answer shapes. The lab proof lists a real
account, container and blobs with real sizes and MD5 values, reads a small
checksum file, and checks the "needs Storage Blob Data Reader" message with a
Reader-only login.
