# Entra ID connector

MEHO reads Microsoft Entra ID (the directory of a Microsoft 365 and Azure
tenant) through the Entra ID connector. One target is one tenant. The
connector logs in with a login (an app registration with a client secret)
from Vault, keeps the access token in its own cache, and calls Microsoft
Graph v1.0 with it.

It has six reads: `entra.about`, `entra.user.list`, `entra.group.list`,
`entra.role.assignment.list`, `entra.app.list` and
`entra.service_principal.list`. Each read needs a Microsoft Graph
application permission, and an admin must grant consent for it once.

Package: `backend/src/meho_backplane/connectors/entra/`. Shared login code:
`backend/src/meho_backplane/connectors/_shared/microsoft_login.py` (the same
as for Azure, with another token scope).

No live proof has run yet: the lab login does not exist. Everything below is
built from Microsoft's documented answers and proven with mocked HTTP only.

## Overview

| What | Value |
|---|---|
| Product | `entra` |
| Connector ID | `entra-graph-1.0` (implementation `entra-graph`, version `1.0`) |
| Registration | the versioned row plus the wildcard row `("entra", "", "")` |
| Kind | typed operations only; nothing is ingested from a spec |
| Operations | six reads in the group `directory`, all `safe` |
| API | Microsoft Graph v1.0 only, never `/beta/` |
| Onboarding | [`docs/cross-repo/entra-onboarding.md`](../cross-repo/entra-onboarding.md) |

The version `1.0` is the Microsoft Graph API version. Entra ID has no product
version. The wildcard row lets every `entra` target find the connector, also
a new target without a fingerprint.

Why a product of its own: the Azure connector reads Azure Resource Manager.
Users, groups and apps live in Microsoft Graph, a different API with its own
host, token scope and permissions.

Why typed only: Graph's OpenAPI description holds thousands of paths in one
file of about 44 MB. A few hand-written reads give clear names, risk levels
and guidance for the AI.

## Key types

- `EntraConnector` (`connector.py`): the connector on the shared HTTP base
  (`connectors/adapters/http.py`). It owns the login, the cache, the two
  recovery hooks, the trace filter, the fingerprint and the probe.
- `EntraCloud`, `ENTRA_CLOUDS`, `EntraTargetConfig`, `resolve_target_config`
  (`session.py`): the target shape and its checks.
- `EntraCredentials`, `parse_credentials`, `load_credentials_from_vault`
  (`session.py`): the secret shape and the reader.
- `send`, `check_next_link` (`_http.py`): every Graph call, with one retry
  budget, the wait after a 429, the new login after a 401 or a 403, and the
  check of next-page links.
- `list_all_pages` (`_paging.py`): reading every page of a list, with limits.
- `EntraApiError`, `EntraPermissionError`, `EntraThrottledError`,
  `EntraUrlRefusedError` (`_errors.py`): Graph's error answers as errors the
  dispatcher knows.
- `summarize`, `credential_fields`, `scrub_credentials` (`_credentials.py`):
  what MEHO reads of client secrets and certificates, and the filter that
  keeps them out of traces.
- `_paths.py`: every Graph path the connector calls, with the permission it
  needs.
- `ENTRA_TYPED_OPS`, `ENTRA_TYPED_WHEN_TO_USE_BY_GROUP`,
  `register_entra_typed_operations` (`typed_ops.py`), `_schemas.py`,
  `_llm_instructions.py`, `typed_reads.py`: the operation table, the
  schemas, the guidance for the AI and the operation bodies.

## Reuse of the Azure connector

The connector imports, and does not copy:

- the login request (`fetch_client_credentials_token`, `LOGIN_HOSTS`);
- the retry numbers (`MAX_ATTEMPTS`, `MAX_WAIT_SECONDS`, `BACKOFF_SECONDS`,
  `THROTTLE_BACKOFF_SECONDS`), `retry_after_seconds` and `NoRedirectClient`
  from `connectors/azure/_http.py`;
- the paging limits and result shape (`PageLimits`, `PagedRows`) from
  `connectors/azure/_paging.py`;
- the error-body reader (`azure_error_body`, which also filters secrets out
  of the message) from `connectors/azure/_errors.py`.

Three small pieces are its own, because the Azure ones are written for
Resource Manager: the retry loop `send` (the Azure URL check needs a
subscription in the path, and its errors speak of Azure roles), the page
loop (Graph's link is `@odata.nextLink`), and the target and secret checks
(their messages name Entra ID). The behaviour is the same.

## Target shape

| Field | Value |
|---|---|
| `host` | the Microsoft Graph host of the cloud: `graph.microsoft.com` for the public cloud |
| `port` | empty or `443` |
| `secret_ref` | the secret with the login (see below) |
| `extras.tenant_id` | optional: the tenant ID, a GUID; must equal the secret's `tenant_id` |
| `extras.cloud` | optional: `AzureCloud` (default), `AzureUSGovernment` or `AzureChinaCloud` |

The clouds:

| Cloud | Graph host | Login host | Token scope |
|---|---|---|---|
| `AzureCloud` | `graph.microsoft.com` | `login.microsoftonline.com` | `https://graph.microsoft.com/.default` |
| `AzureUSGovernment` | `graph.microsoft.us` | `login.microsoftonline.us` | `https://graph.microsoft.us/.default` |
| `AzureChinaCloud` | `microsoftgraph.chinacloudapi.cn` | `login.chinacloudapi.cn` | `https://microsoftgraph.chinacloudapi.cn/.default` |

The hosts come from Microsoft's page on Graph national cloud deployments. A
token scope must name the Graph host of its own cloud. The US Government L5
(DoD) host is not supported. No cloud is proven on a live tenant yet.

Before any login, `resolve_target_config` checks the target. MEHO sends the
token only to the Graph host of the target's cloud, over https on port 443,
with the certificate checked (`verify_tls` must stay on). Any other target
fails with `EntraTargetConfigError`. The message names the target and the
field to fix, never a value.

## Secret shape

The secret at the target's `secret_ref` holds `tenant_id`, `client_id` (the
application ID of the login) and `client_secret`, the same shape as the
secret of an Azure target. So one login can serve both connectors. Every
value is stripped of surrounding white space. A missing key or an ID that is
not a GUID fails with `EntraSecretShapeError`, which names the key, never a
value. If the target sets `extras.tenant_id` and it is not the secret's
`tenant_id`, the call fails before the login.

## Login and token cache

The login is one client credentials request to Microsoft Entra ID with the
scope `https://graph.microsoft.com/.default` (see
[`connectors-azure.md`](connectors-azure.md#login) for the request and its
errors). `EntraConnector` keeps two caches, like the Azure connector:

- **Secret cache**, per MEHO tenant, target and secret reference.
- **Token cache**, per MEHO tenant, target, Entra tenant, client ID and token
  scope. MEHO asks for a new token five minutes before the old one runs out.

Each cache key has its own lock, so many calls at the same moment start one
login. A call without a person's token never gets a cached secret or token.
The Graph token lives in this connector's own cache. An Azure target that
shares the login gets its own token for Azure Resource Manager; the two never
mix (`test_connectors_entra.py` proves it).

From the token MEHO reads the login's app ID, object ID and its application
permissions (claim `roles`, field `MicrosoftAccessToken.roles`). It does not
check the signature and uses the values only to show them: in `entra.about`
and in the error after a refusal. When the token cannot be read, the
permissions are unknown (`null`).

## Recovery

- **Graph answers 401** (the token is no longer accepted): `send` drops the
  target's tokens, logs in again and tries once more inside the same retry
  budget. A second 401 fails with `ConnectorAuthError` (stage
  `after_relogin`), and the dispatcher returns `connector_auth_failed`.
- **Graph answers 403**: `send` drops the target's tokens and fails with
  `EntraPermissionError`. A token keeps the permissions it was made with, so
  after an admin grants a missing permission, the next call gets a new token
  that carries it. No restart is needed, and MEHO does not wait for the old
  token to run out.
- **Microsoft refuses the login**: the dispatcher gets `ConnectorAuthError`,
  calls `invalidate_credentials(target)`, which drops the target's secret
  and tokens, and returns `connector_auth_failed`. The next call reads the
  secret again.

## Retries and throttling

Every Graph call goes through `send` in `_http.py`, with the numbers of the
Azure connector: at most 4 tries and 60 seconds of waiting per call. A 429
waits `Retry-After` (or 2, 4, 8 seconds when it is missing) and tries again
while the budget allows, then fails with `EntraThrottledError`, which says
when to try again. A 5xx answer or a lost connection is tried again after
0.5, 1, 2 seconds. Every Graph call here is a read, so repeating it is safe.
Other answers fail at once. No redirect is followed: a 3xx answer fails with
`EntraUrlRefusedError`, and no token goes to the new address.

## Errors

| Graph answer | Error | What the caller gets |
|---|---|---|
| 401 twice (after a new login) | `ConnectorAuthError` | `connector_auth_failed` |
| 403, usually code `Authorization_RequestDenied` | `EntraPermissionError` | `connector_error`; the message names the permission the call needs; the remediation names the permissions the token carries and the admin consent step |
| 429, budget used up | `EntraThrottledError` | `connector_error` with when to try again |
| any other error | `EntraApiError` | `connector_error` |

The extras carry `exception_class`, `http_status`, `upstream_message`
(Graph's code and message, filtered for secrets and cut) and `remediation`.
The dispatcher keeps only the first 256 characters of an error message, so
the 403 message holds the facts and the remediation holds the steps. When
the token already carries the permission, the remediation says that a
missing permission is not the cause (for example a Conditional Access policy
for app logins).

## Next-page links and paging

Graph answers a list with `value` and, when more rows exist, a full URL in
`@odata.nextLink` that already holds the query and a page token.
`list_all_pages` follows it with the Azure limits: 50 pages, 120 seconds,
10,000 rows. When a limit stops it, `truncated` is `true` and `omission`
says which limit and how to narrow the request. The result keeps the Azure
shape (`value`, `truncated`, `omission`, `omission_kind`, `pages_read`), so
the dispatcher keeps those fields inline when the rows go behind a link to
the stored result.

Before the token goes to a next-page link, `check_next_link` checks it: https
on port 443, no user name or password, at most 8,192 characters, the
target's Graph host, and the same path as the first page (case does not
count), with no `.` or `..` part. Any other link fails with
`EntraUrlRefusedError` before a request is made, and the error never repeats
the link.

## Operations

All six are in the group `directory` and are `safe`; none needs approval.
Text from the directory (user, group, app and tenant names) is untrusted
data: anyone who may name an object in the tenant chooses it. The guidance
of every operation says so. A list with more than 50 rows comes back as a
link to the stored result; `result_query` reads the rows. The fields next to
the rows are plain values, so they stay inline in both shapes.

| Operation | Graph call | Permission | Parameters |
|---|---|---|---|
| `entra.about` | `GET /v1.0/organization` | `Organization.Read.All` | none |
| `entra.user.list` | `GET /v1.0/users` | `User.Read.All` | `user_type` (`Member` or `Guest`), `enabled` |
| `entra.group.list` | `GET /v1.0/groups`, and `GET /v1.0/groups/{id}/members/$count` per group on request | `Group.Read.All` | `include_member_count` |
| `entra.role.assignment.list` | `GET /v1.0/roleManagement/directory/roleDefinitions`, then `GET /v1.0/roleManagement/directory/roleAssignments?$expand=principal` | `RoleManagement.Read.Directory` | none |
| `entra.app.list` | `GET /v1.0/applications` | `Application.Read.All` | `expiring_within_days` |
| `entra.service_principal.list` | `GET /v1.0/servicePrincipals` | `Application.Read.All` | `expiring_within_days` |

`Directory.Read.All` alone also allows every read, but it allows much more.

### entra.about

Returns `tenant_id`, `display_name`, `default_domain`, `verified_domains`
(`name`, `is_default`, `is_initial`, `type`), `cloud`, `app_id`, `object_id`
and `granted_permissions` (the application permissions in the login's token).
The token itself is never returned. Use it first for a new target and after
an admin granted a permission.

### entra.user.list

Asks for `id,displayName,userPrincipalName,accountEnabled,userType,createdDateTime`
with `$top=999`. `user_type` and `enabled` become Graph's `$filter`
(`userType eq 'Guest'`, `accountEnabled eq false`), which Graph supports
without advanced queries. Only these fixed words go into the filter; no text
from the caller does. Rows: `id`, `display_name`, `user_principal_name`,
`account_enabled`, `user_type`, `created_at`. Sign-in activity is not read:
it needs another permission and a premium license.

### entra.group.list

Asks for `id,displayName,groupTypes,mailEnabled,securityEnabled` with
`$top=999`. Rows: `id`, `display_name`, `group_type`, `membership`,
`member_count`.

| `group_type` | Rule (Microsoft's group kinds) |
|---|---|
| `microsoft_365` | `groupTypes` holds `Unified` |
| `security` | security-enabled, not mail-enabled |
| `mail_enabled_security` | security-enabled and mail-enabled |
| `distribution` | mail-enabled, not security-enabled |
| `other` | neither |

`membership` is `dynamic` when `groupTypes` holds `DynamicMembership`, else
`assigned`. Graph has no member count on a group: counting takes one more
call per group (`$count` with the header `ConsistencyLevel: eventual`). So
MEHO counts only when `include_member_count` is `true` and the list has at
most 100 groups. `member_counts` says `counted`, `not_requested` or
`too_many_groups`. The count holds direct members only, and Graph v1.0
leaves service principals out of it (a known Graph issue). A group that is
gone when MEHO counts gets `null`.

### entra.role.assignment.list

Reads the role definitions once for the names, then the role assignments
with `$expand=principal`, which brings each principal's type and name. Graph
supports no `$select` inside an `$expand` on directory objects, and some
Graph lists expand only one relationship per request. So MEHO expands only
the principal and reads the role names separately. Rows: `id`, `role_name`,
`role_definition_id`, `principal_id`, `principal_type` (`user`, `group` or
`servicePrincipal`), `principal_name`, `scope` (`/` for the whole tenant).
When the login may not read a principal, Graph returns only its type and ID,
so `principal_name` is `null`. Only active assignments are listed;
assignments a person must first activate (eligible in Privileged Identity
Management) are not.

### entra.app.list and entra.service_principal.list

Ask for the credentials (`passwordCredentials`, `keyCredentials`) with the
names and IDs, `$top=999` for apps and `$top=100` for enterprise apps (Graph's
largest pages). Each row holds only counts and dates, never a credential:

| Field | Meaning |
|---|---|
| `secret_count`, `certificate_count` | how many client secrets and certificates the app has |
| `expired_secret_count`, `expired_certificate_count` | how many of them have already run out |
| `next_secret_expiry`, `next_certificate_expiry` | the end date (UTC) of the next one that is still valid, or `null` |

`entra.service_principal.list` adds `service_principal_type` and
`account_enabled`. Next to the rows: `read_count` (apps read before the
filter), `as_of` (the time the dates were compared with) and
`expiring_within_days`.

`expiring_within_days` (1 to 3,650) keeps an app when a still-valid secret
or certificate runs out within that many days. Credentials that have already
run out do not count for the filter; the expired counts show them. A secret
without an end date never runs out.

Graph never returns a secret's value after it is made, but it returns `hint`
(the first characters of each secret) and, for some reads, certificate data
(`key`). MEHO reads only `endDateTime`. The connector's trace filter
(`trace_response_body`) cuts every credential in a raw answer down to its
ID, name and dates before the answer reaches a trace. Graph limits reads
that select `keyCredentials` to 150 requests per minute per tenant; the 429
handling covers it.

## Paths and the spec check

Every path is a `*_PATH` constant in `_paths.py`, starting with `/v1.0/`, with
its `*_PERMISSION` constant. A unit test checks that every constant is a
v1.0 path and that none is a beta path. All eight paths are in Microsoft's
Graph v1.0 OpenAPI file (`openapi/v1.0/openapi.yaml` in
`microsoftgraph/msgraph-metadata`, MIT license). That file is about 44 MB,
so it is not on the private spec shelf, and no CI check compares the paths
with it yet.

## Fingerprint and probe

`fingerprint` reads the tenant and stores vendor `microsoft`, product
`entra`, no version, and `extras` with `tenant_id`, `cloud` and
`default_domain`. When the read fails, the fingerprint is `reachable=False`
with the reason in `extras.error`. `probe` uses the fingerprint. A call
without an operator runs as MEHO's system identity, which cannot read the
secret, so it reports `reachable=False`.

## Dependencies

- `connectors/_shared/microsoft_login.py`: the login request.
- `connectors/azure/_http.py`, `_paging.py`, `_errors.py`: the retry numbers,
  `Retry-After`, the client without redirects, the paging limits and result
  shape, and the error-body reader.
- `connectors/adapters/http.py`: pooled `httpx` client, blocking of private
  addresses, the response size cap and trace recording.
- `connectors/_shared/vault_creds.py`: the secret read under the caller's
  identity.
- `operations/typed_register.py`: storing the typed operations at start-up.

## Known issues

- No live proof yet: the lab login does not exist. Only mocked answers are
  tested.
- Only the public cloud is planned for the first proof; the other two clouds
  are not proven.
- Member counts are read one group at a time, so only for lists of at most
  100 groups.
- Sign-in activity and eligible role assignments are not read.
- The cache lives in one MEHO process. Each process logs in on its own.

## References

- List users: <https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0>
- List groups: <https://learn.microsoft.com/en-us/graph/api/group-list?view=graph-rest-1.0>
- List group members (and `$count`): <https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-1.0>
- List role assignments: <https://learn.microsoft.com/en-us/graph/api/rbacapplication-list-roleassignments?view=graph-rest-1.0>
- List role definitions: <https://learn.microsoft.com/en-us/graph/api/rbacapplication-list-roledefinitions?view=graph-rest-1.0>
- List applications: <https://learn.microsoft.com/en-us/graph/api/application-list?view=graph-rest-1.0>
- List service principals: <https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list?view=graph-rest-1.0>
- List organizations: <https://learn.microsoft.com/en-us/graph/api/organization-list?view=graph-rest-1.0>
- passwordCredential and keyCredential: <https://learn.microsoft.com/en-us/graph/api/resources/passwordcredential?view=graph-rest-1.0>,
  <https://learn.microsoft.com/en-us/graph/api/resources/keycredential?view=graph-rest-1.0>
- Advanced queries (`ConsistencyLevel`, `$count`): <https://learn.microsoft.com/en-us/graph/aad-advanced-queries>
- Paging: <https://learn.microsoft.com/en-us/graph/paging>
- Throttling: <https://learn.microsoft.com/en-us/graph/throttling>
- Errors: <https://learn.microsoft.com/en-us/graph/errors>
- Known issues (group members and service principals, `$expand` limits): <https://learn.microsoft.com/en-us/graph/known-issues>
- National cloud deployments: <https://learn.microsoft.com/en-us/graph/deployments>
- Permissions reference: <https://learn.microsoft.com/en-us/graph/permissions-reference>
- Access token claims (`roles`): <https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference>
- Graph OpenAPI description: <https://github.com/microsoftgraph/msgraph-metadata>
- Related pages: `connectors-azure.md` (the shared login, retry numbers and
  paging limits), `connectors-shared-vault-creds.md`, `error-message-shape.md`.
