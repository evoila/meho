# Identity Broker and Workspace ONE Access connector (`idb`)

MEHO reads the single sign-on service of VMware products through the `idb`
connector. It covers two products:

- **VCF Identity Broker 9.x** (VCF 9.0, 9.1 and 9.1.1),
- **Workspace ONE Access 3.3** (used in front of Aria 8.x products).

An agent or an operator can:

- read the product, version and health,
- list the identity providers and the directories, with their sync fields,
- find users and groups by name,
- start a directory sync. This waits until a person approves it.

Sign-in problems in VCF and Aria often come from a failed directory sync or a
broken identity provider. These operations show that through MEHO.

Package: `backend/src/meho_backplane/connectors/idb/`.
Decision record: [`docs/decisions/2026-10-23-identity-broker-connector.md`](../decisions/2026-10-23-identity-broker-connector.md).

No live proof has run yet. The code is built from the vendor references (see
"API references") and tested against a fake vendor only. A separate lab proof
runs every operation on Workspace ONE Access 3.3 and Identity Broker 9.0, 9.1
and 9.1.1.

## Overview

| What | Value |
|---|---|
| Product | `idb` |
| Connectors | `idb-wsa-3.3` (Workspace ONE Access 3.3, band `>=3.3,<4.0`) and `idb-rest-9.0` (Identity Broker 9.x, band `>=9.0,<10.0`) |
| Wildcard row | `("idb", "", "")` goes to `idb-rest-9.0`; a target with no version resolves there |
| Kind | typed (hand-written) operations only, the same six ids on both connectors |
| Group | `identity` |
| Path check | `backend/tests/test_connectors_idb_spec_reconcile.py` (a pinned list; there is no API file to compare with) |

Why two connectors: the two products share almost no API paths, and the
version numbers never overlap. One product token keeps one set of operation
ids. The agent calls `idb.user.find` on either product, and MEHO runs the
right call.

## Login

Both products use an OAuth 2.0 client with the client-credentials grant. An
admin makes the client once:

- Workspace ONE Access: in the console (a service client with admin rights).
- Identity Broker: with the OAuth client API of the broker.

The target's secret (`target.secret_ref`, Vault KV-v2) holds `client_id` and
`client_secret`. MEHO sends `POST` to the token path with HTTP Basic
(`client_id:client_secret`) and `grant_type=client_credentials`:

- Workspace ONE Access: `POST /SAAS/auth/oauthtoken`
- Identity Broker: `POST /acs/t/{tenant}/token`

The token is sent as `Authorization: Bearer <token>`. It is cached per
credential and renewed 30 seconds before `expires_in` runs out. On a `401`
the dispatcher calls `invalidate_session`: the token is dropped (only when the
refused call sent it), MEHO logs in again, and the call runs once more. A
refused login (`400`, `401`, `403`) is `connector_auth_failed`, and MEHO
forgets the cached secret, so a corrected secret works without a restart.

The login call is not traced. Traced answers have every `access_token`,
`token`, `password` and `secret` field masked.

## Target shape

| Field | Value |
|---|---|
| `product` | `idb` |
| `host` / `port` | the appliance (HTTPS) |
| `secret_ref` | the Vault path of `{client_id, client_secret}` |
| `version` | `3.3` for Workspace ONE Access, or the Identity Broker version (`9.0`, `9.1`, `9.1.1`) when MEHO cannot detect it |
| `extras.tenant` | Identity Broker only; default `CUSTOMER` |

## Version detection

The fingerprint tells the two products apart without a login:

1. `GET /SAAS/API/1.0/REST/system/health`. An answer with `AllOk` is
   Workspace ONE Access. If the answer has a field whose name holds
   `version` (for example `BuildVersion`), MEHO reads the version from it.
   The 3.3 docs do not list such a field, so the lab proof checks it.
2. Otherwise `GET /acs/t/{tenant}/.well-known/openid-configuration`. An
   answer with `issuer` is Identity Broker. MEHO then reads the version from
   `GET /acs/t/{tenant}/properties` (9.1 and newer). Identity Broker 9.0 has
   no version call.

When no version is found, set `version` on the target. Without a version a
target resolves to `idb-rest-9.0` (the wildcard row).

## Operations

| Operation | What it does | Returns | Vendor call | Risk | Versions |
|---|---|---|---|---|---|
| `idb.about` | Product, version and health (also MEHO's version detection). | `product`, `version`, and `healthy`, `checks`, `failing_checks` (Workspace ONE Access) or `deployment_type`, `instance_id`, `tenant` (Identity Broker) | `GET /SAAS/API/1.0/REST/system/health` (3.3); `GET /acs/t/{tenant}/properties` (9.1+) | read (`safe`) | 3.3, 9.1+ (9.0 stops early: no version call) |
| `idb.identity_provider.list` | Identity providers: name, type, state, directories. | rows `id`, `name`, `type`, `status`, `directories`, `certificate` | `GET /SAAS/jersey/manager/api/identityProviders?onlyEnabledAdapters=true` | read (`safe`) | 3.3 (9.x stops early) |
| `idb.directory.list` | Directories with type and sync fields. | rows `directory_id`, `name`, `type`, `sync_enabled`, `delete_in_progress`, `sync`, `errors` | `GET /SAAS/jersey/manager/api/connectormanagement/directoryconfigs?includeJitDirectories=true` | read (`safe`) | 3.3 (9.x stops early) |
| `idb.directory.sync` | Start a directory sync now. | `started`, `directory_id`, `answer`, `guidance` | `POST /SAAS/jersey/manager/api/connectormanagement/directoryconfigs/{directoryId}/syncprofile/sync` | change (`caution`, waits for approval) | 3.3 (9.x stops early) |
| `idb.user.find` | Find users by user name: domain, state, groups. | rows `id`, `user_name`, `display_name`, `email`, `active`, `domain`, `source`, `user_status`, `groups`, `group_count` | `POST /SAAS/jersey/manager/api/scim/Users/.search` (3.3); `POST /usergroup/t/{tenant}/scim/v2/Users/.search` (9.x) | read (`safe`) | 3.3, 9.0, 9.1, 9.1.1 |
| `idb.group.find` | Find groups by name, with their members. | rows `id`, `display_name`, `domain`, `group_type`, `description`, `member_count`, `members` (at most 100), `members_truncated` | `POST /SAAS/jersey/manager/api/scim/Groups/.search` (3.3); `POST /usergroup/t/{tenant}/scim/v2/Groups/.search` (9.x) | read (`safe`) | 3.3, 9.0, 9.1, 9.1.1 |

Notes:

- **"Stops early"** means the handler raises before any vendor call, and the
  answer is `connector_unsupported` with a message that names the reason. On
  Identity Broker the message says the Identity Broker API has no such call,
  and that on VCF 9.1 and newer the VCF Operations connector has identity
  providers, directories and their sync. The approval card of
  `idb.directory.sync` on Identity Broker carries the same message as
  `version_problem`, so the approver sees it.
- Every list is a flat list of rows (`{total, returned, truncated, <rows>}`).
  More than about 50 rows come back as a link to the stored result; read it
  with `result_query`.
- `user.find` and `group.find` take `name` and `match` (`co` contains, the
  default; `eq` exact; `sw` starts with). MEHO builds the SCIM filter and
  escapes quotes.
- No result has a password, token or secret. User rows are built field by
  field; the SCIM `password` field is never read. The `sync` and `errors`
  objects of a directory leave out secret-named fields.
- `idb.directory.sync` sends `{"ignoreSafeguards": false}`, so the
  directory's own limits stay on. The sync runs in the background; read
  `idb.directory.list` later for the result. A wrong directory id fails with
  the vendor's error text in `extras.upstream_message`.

## Not confirmed yet (the lab proof settles these)

- Workspace ONE Access 3.3: a version field in the health answer, the field
  names of the identity provider and directory lists (the last-sync fields
  are read by name: every field that holds `sync`), and the sync call with its
  body and media type. These calls come from VMware sample code and community
  write-ups, not from an API reference for 3.3.
- Identity Broker: whether the OpenID discovery path answers without a login,
  and whether `properties` answers on 9.0.

## API references

- Identity Broker 9.0, 9.1, 9.1.1: <https://developer.broadcom.com/xapis/vmware-identity-broker/latest/>
  (operation index; no downloadable API file).
- Workspace ONE Access: the archived Identity Manager swagger file
  (`apidocs/swagger.json` in `omnissa-archive/idm`: login, SCIM), the VMware
  PowerCLI example module `VMware.WorkspaceOneAccess` (identity providers,
  directories), and the Identity Manager 3.3 monitoring docs (health).

The path list with its sources is in the docstring of
`connectors/idb/_paths.py`.

## Tests

- `backend/tests/test_connectors_idb_typed_reads.py` -- each operation on both
  products against a fake vendor (`backend/tests/_idb_fake.py`), the login
  and token cache, the fingerprint, and the early stop on 9.0, 9.1 and 9.1.1.
- `backend/tests/test_connectors_idb_governed.py` -- through the dispatcher:
  risk levels, the sync waits for approval and runs once with an audit row, a
  vendor error, `401` then a new login, a result handle, planted secrets.
- `backend/tests/test_connectors_idb_spec_reconcile.py` -- the pinned path
  list.
