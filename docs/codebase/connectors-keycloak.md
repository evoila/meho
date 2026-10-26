# Connector: Keycloak (keycloak-admin)

## Overview

`KeycloakConnector` is the typed connector for the Keycloak Admin REST
API. MEHO runs Keycloak as its own identity provider on the RDC fleet;
this connector manages that Keycloak through its Admin REST surface.

G3.13-T1 (#1393) shipped the **substrate**: the connector class, the
admin credential loader, `fingerprint()`, and dual registry registration.
G3.13-T2 (#1394) layers the **six curated read ops** onto that surface
(realm/client/client-scope/user/role-mapping); T3 (#1395) adds the CLI
verbs + onboarding docs. G3.13-T4 (#1406) adds the **nine approval-gated
write ops** (realm/client/client-scope/protocol-mapper/user/role-mapping
creates + updates + reset-password) that retire the consumer's five
Keycloak bootstrap scripts; `idp.create` is deferred (not exercised by
those scripts). #2843 extends the read surface to **eight** by adding the
`role` group (`role.list` + `role.users`) for realm-role access review.
#3280 adds the **group-lifecycle surface**: two reads (`group.list` +
`group.member.list`, taking the read surface to **ten**) and four
approval-gated writes (`group.create` with attributes, `group.update_attributes`,
`group.member.add`, `group.member.remove`, taking the write surface to
**thirteen**) so a tenant's role groups — the backplane's tenant-claim
primitive — can be bootstrapped governed. #3944 adds three **destructive**
deletes (`user.delete`, `group.delete`, `client.delete`, taking the write
surface to **sixteen**). Removing access when an event, a contract or a job
ends is then audited, and a second person must approve it. #3851 adds
`user.logout` (taking the write surface to **seventeen**): it ends all live
sessions of one user, so their refresh tokens stop working at once. #3886 adds a
fourth destructive delete, `realm.delete` (write surface **eighteen**), so a
short-lived realm (a test run, a demo, a failed set-up) is removed through
MEHO too, not by a direct API call.

Registry v2 triple: `(product="keycloak", version="26.x",
impl_id="keycloak-admin")`, plus the `(keycloak, "", "")` wildcard so a
fresh target with no asserted version still resolves.

## Key types

- `KeycloakConnector` (`connectors/keycloak/connector.py`) — the
  `HttpConnector` subclass. Holds a per-target admin-token cache with
  TTL-driven refresh and an injectable admin-credential loader. Exposes a
  thin bound-method shim per read op (`realm_get`, `client_list`,
  `client_get`, `client_scope_list`, `user_list`, `role_mapping_get`,
  `role_list`, `role_users`, `group_list`, `group_member_list`) and
  per write op (`realm_create`/`realm_update`, `client_create`/
  `client_update`, `client_scope_create`, `protocol_mapper_create`,
  `user_create`/`user_reset_password`, `role_mapping_assign`,
  `group_create`, `group_update_attributes`, `group_member_add`,
  `group_member_remove`, `user_delete`, `group_delete`, `client_delete`,
  `user_logout`, `realm_delete`),
  the `_get_admin_json` / `_get_admin_list` GET
  helpers (object vs array responses), and the `_write_admin` mutating
  helper (POST/PUT/**DELETE**) plus the `_find_client_uuid` /
  `_find_user_uuid` / `_find_realm_role` / `_find_group` / `_user_in_group`
  name→UUID + membership resolvers.
- `GROUP_WRITE_OPS` + the group write handlers
  (`connectors/keycloak/ops_write_groups.py`, metadata table in
  `ops_write_groups_schemas.py`) — the four approval-gated group-lifecycle
  ops (#3280). `group.create` treats HTTP 409 as an idempotent
  `already_exists`; the membership ops pre-read `_user_in_group` for an
  idempotent `unchanged` result. `KeycloakGroupNotFoundError` is raised
  when a group name/UUID resolves to nothing.
- `DELETE_WRITE_OPS` (`ops_write_delete_schemas.py`), the delete handlers
  and approval-card blocks (`ops_write_delete.py`), and the shared lookup +
  guards (`ops_write_delete_lookup.py`) — the three destructive deletes
  (#3944). A guard raises `KeycloakDeleteRefusedError` with a stable `code`;
  `KeycloakDeleteVerificationError` means the object could still be read
  after the DELETE. The realm delete (#3886) has its row in the same table
  and its guards, approval card and handler in `ops_write_realm_delete.py`.
- `KeycloakOp` + `READ_OPS` (`connectors/keycloak/ops_read.py`) — the
  op-metadata dataclass and the eight-op read registration table (the bind9 /
  pfSense `ops`-table precedent). `WHEN_TO_USE_BY_GROUP` maps each op
  group to its curated selection blurb.
- `WRITE_OPS` + the write handlers (`connectors/keycloak/ops_write.py`) —
  the nine original approval-gated write ops plus `user.logout` (#3851),
  reusing the `KeycloakOp` dataclass.
  Every op is `requires_approval=True`; a create treats HTTP 409 as an
  idempotent success; `user.create`/`user.reset_password` source the
  password through the credential-backend seam via
  `_read_password_from_vault` (never inline).
  `WHEN_TO_USE_WRITE_BY_GROUP` carries the write groups' blurbs (keyed with
  a `_write` suffix so they never collide with the read groups when
  `register_operations` merges both maps).
- `KeycloakWriteResult` (`connectors/keycloak/connector.py`) — the
  status + `Location` + `conflict` outcome of a mutating request;
  `created_uuid()` parses the new object's UUID out of `Location`.
- `redact_secret_fields` (`connectors/keycloak/redaction.py`) — the
  recursive scrubber every read handler runs its response through.
- `KeycloakClientCredentials` / `KeycloakPasswordCredentials`
  (`connectors/keycloak/session.py`) — the two admin-credential shapes
  (tagged union `KeycloakAdminCredentials`).
- `KeycloakTargetLike` — structural Protocol the concrete `Target` model
  satisfies; adds `extras` (carrying the realm overrides) to the common
  REST-target shape.
- `RealmConfig` — the resolved `(admin_realm, managed_realm)` pair.
- `KeycloakAdminTokenError` — raised when the token-endpoint round-trip
  fails or returns no usable `access_token`. On a non-2xx it echoes
  Keycloak's `{error, error_description}` (RFC 6749 §5.2 — both non-secret)
  so a bad secret, a client not allowed the grant, and a wrong realm are
  distinguishable from the error string alone, no backplane logs needed
  (#1474). The detail is echoed **only** for a genuine OAuth2 token error —
  an `application/json` body whose `error` is one of the six RFC 6749 §5.2
  codes — so an unrelated gateway/proxy JSON envelope (whose
  `error_description` is not schema-bound to be non-secret) never injects
  noise or leaks a value. Any other body adds no detail.
- `KeycloakAmbiguousVaultPayloadError` — raised when the admin Vault
  secret carries neither credential shape.

## The admin-vs-operator credential split (load-bearing)

MEHO authenticates its own callers with operator-OIDC tokens that
Keycloak issues. The connector that *manages* Keycloak must **not**
authenticate through that path, or it could never bootstrap a freshly
deployed Keycloak whose operator-login clients are not yet configured
(a chicken-and-egg).

So the connector uses a **separate admin credential**:

1. The operator's validated JWT (`operator.raw_jwt`) authorises only an
   operator-context Vault KV-v2 read of the admin credential at the
   consumer path `secret/rdc-hetzner-dc/keycloak/admin` (the locked
   Option A decision in `docs/architecture/connector-auth.md`).
2. The connector exchanges that admin credential at Keycloak's own token
   endpoint — `POST /realms/{admin_realm}/protocol/openid-connect/token`,
   form-encoded — for an admin access token.
3. The admin token is sent as `Authorization: Bearer <admin_token>` on
   every Admin REST call.

The operator's OIDC token is **never** sent to Keycloak. A unit test
(`test_admin_token_not_operator_token_used_on_admin_calls`) asserts the
operator JWT appears on no captured Keycloak request.

### Admin credential discriminator

The admin Vault secret carries one of two shapes; the loader picks the
grant from the payload (the same payload-shape discriminator the gh-rest
connector uses for App-vs-PAT):

| Vault fields present | Credential | Grant |
|---|---|---|
| `client_id` + `client_secret` | `KeycloakClientCredentials` | `client_credentials` |
| `username` + `password` (no client pair) | `KeycloakPasswordCredentials` | `password` on `admin-cli` |
| neither | `KeycloakAmbiguousVaultPayloadError` | — |

The password shape accepts an optional `client_id` field (default
`admin-cli`, Keycloak's public direct-access-grant client).

Every field the discriminator plucks is whitespace-stripped via
`strip_credential_value` before use — a `client_secret` stored with a
trailing newline would otherwise be sent verbatim and rejected as
`unauthorized_client` (#1474). The same strip guards the Vault-sourced
password the user-write op sets (`_read_password_from_vault`), which also
rejects a whitespace-only secret rather than setting an empty password.

## Control flow

- `auth_headers(target, operator)` — rejects any `auth_model` other than
  `shared_service_account` / `None`, then returns the admin Bearer via
  `_admin_token`.
- `_admin_token` — fail-closed on empty `operator.raw_jwt` *before* the
  cache lookup (a system caller must never get an authenticated caller's
  cached token); returns the cached token if fresh, else mints via
  `_mint_admin_token` under a per-connector lock.
- `_mint_admin_token` — loads the admin credential (operator-context
  Vault read), POSTs the form body to the token endpoint with no
  `Authorization` header, parses `access_token` + `expires_in`, and
  caches with `effective_ttl = expires_in - 30s` (floored at 1s).
- `fingerprint(target, operator)` — mints the admin token, GETs
  `/admin/realms/{managed_realm}`, and surfaces `realm` / `enabled` /
  `ssl_required` / `login_theme` + the resolved realm pair under
  `extras`. Best-effort server version from `/admin/serverinfo`
  (`systemInfo.version`); a 404 there leaves `version=None` but keeps
  `reachable=True`. A `None` operator falls back to the synthesised
  system operator, which fails closed at the live Vault read (surfaced as
  `reachable=False`).
- `probe(target)` — delegates to `fingerprint`; one admin round-trip
  covers reachability + admin-auth validity.
- `register_operations()` — walks `READ_OPS` **and** `WRITE_OPS`,
  resolves each `handler_attr` to a bound method, looks the group's
  `when_to_use` up in the merged `WHEN_TO_USE_BY_GROUP` ∪
  `WHEN_TO_USE_WRITE_BY_GROUP` (a missing entry is a hard error), and
  upserts via `register_typed_operation`. Idempotent across restarts.
- `_write_admin(target, method, path, *, operator, json, idempotent_conflict=True)`
  — the mutating POST/PUT helper. Rejects non-mutating verbs, never
  retries (a 5xx on a write must surface, not re-fire), and — when
  `idempotent_conflict` — swallows an HTTP 409 into a
  `KeycloakWriteResult(conflict=True)` rather than raising.
- `_find_client_uuid` / `_find_user_uuid` / `_find_realm_role` — the
  name→UUID (and role-name→representation) resolvers the write handlers
  call before keying a mutation on the object's UUID.
- `_find_group(target, realm, name, parent_id, *, operator)` — resolves a
  group by `(parent, name)` to its brief representation (id + path), scoped
  to the level (top-level vs a parent's children); backs `group.create`'s
  409 id-resolution and the group write ops' name resolution.
- `_user_in_group(target, realm, user_id, group_id, *, operator)` — reads
  the user's group membership so the membership writes report an idempotent
  `unchanged` result without re-issuing the mutation.

## Read ops (G3.13-T2, extended by #2843 + #3280)

Ten `safety_level="safe"` / `requires_approval=False` read ops, all
tagged `read-only`, all dispatching via the admin-auth path. The realm is
the target's `managed_realm` (no per-op realm param):

| op_id | Admin REST API | returns |
|---|---|---|
| `keycloak.realm.get` | `GET /admin/realms/{realm}` | realm config |
| `keycloak.client.list` | `GET .../clients` (`?clientId=`/`?max=`) | `{rows, total}` |
| `keycloak.client.get` | `GET .../clients/{id}` | one client (flows, redirect URIs, mappers) |
| `keycloak.client_scope.list` | `GET .../client-scopes` | `{rows, total}` |
| `keycloak.user.list` | `GET .../users` (`?username=`/`?max=`) | `{rows, total}`, no credentials |
| `keycloak.role_mapping.get` | `GET .../users/{id}/role-mappings` | realm + client role mappings |
| `keycloak.role.list` | `GET .../roles` | `{rows, total}` — realm role catalogue |
| `keycloak.role.users` | `GET .../roles/{role-name}/users` | `{rows, total}`, no credentials — role members |
| `keycloak.group.list` | `GET .../groups` (or `.../groups/{group-id}/children`) | `{rows, total}` — `brief=false` includes attributes |
| `keycloak.group.member.list` | `GET .../groups/{group-id}/members` | `{rows, total}`, no credentials — group members |

`keycloak.group.list` lists top-level groups, or a parent's children when
`parent_id` is set. Keycloak's default group projection is **brief** and
omits `attributes` — pass `brief=false` to include the `tenant_id` /
`tenant_role` a role group carries (what an identity-bootstrap flow reads
back to verify a group is attributed correctly). `group.member.list` keys
on the group's internal UUID.

`client.get` / `role_mapping.get` take the **internal UUID** (`id`), not
the human `clientId` / `username` — discover it via the matching `.list`
op first. `role.users` is the exception: it keys on the role **name**
(from `role.list`), not a UUID. `role.list` + `role.users` are the
access-review pair — "which realm roles exist?" and "who holds role X?" —
the reverse direction of the per-user `role_mapping.get`. Keycloak returns
`role.users` sorted by username, its default first page (100 members); a
larger role is capped at that page.

### Secret redaction

Every handler runs its response through `redact_secret_fields` before
returning, so the value of `secret` (confidential-client secret),
`credentials` / `value` / `secretData` / `credentialData` (user
credential material), **and** the generic credential key spellings
(`password` / `client_secret` / `token` / `private_key` / … — the same set
the generic params-echo default scrubs) is replaced with `***REDACTED***`
— recursively and case-insensitively, including secrets nested inside
protocol mappers, identity-provider configs, or a `smtpServer` map. The
`password` coverage matters because a `RealmRepresentation.smtpServer` is a
`Map<String,String>` carrying the SMTP relay password under the `password`
key, and a representation is `additionalProperties: true`. The scrub
happens at the connector boundary (not the broadcast layer) because these
are config reads where the secret is incidental: it must never enter the
synchronous `OperationResult` the caller receives.
The write surface redacts secret *inputs* at the classification layer per
the general posture (see "Write ops" below).

## Write ops (G3.13-T4, extended by #3280)

Seventeen approval-gated write ops (`requires_approval=True`), all
dispatching via the admin-auth path, all keyed on the object's **UUID**:

| op_id | safety | Admin REST API |
|---|---|---|
| `keycloak.realm.create` | dangerous | `POST /admin/realms` |
| `keycloak.realm.update` | caution | `PUT .../realms/{realm}` |
| `keycloak.client.create` | caution | `POST .../realms/{realm}/clients` |
| `keycloak.client.update` | caution | `PUT .../clients/{id}` |
| `keycloak.client_scope.create` | caution | `POST .../client-scopes` |
| `keycloak.protocol_mapper.create` | caution | `POST .../clients/{id}/protocol-mappers/models` |
| `keycloak.user.create` | caution | `POST .../realms/{realm}/users` |
| `keycloak.user.reset_password` | caution | `PUT .../users/{id}/reset-password` |
| `keycloak.role_mapping.assign` | dangerous | `POST .../users/{id}/role-mappings/realm` |
| `keycloak.group.create` | dangerous | `POST .../groups` (or `.../groups/{group-id}/children`) |
| `keycloak.group.update_attributes` | dangerous | `PUT .../groups/{group-id}` |
| `keycloak.group.member.add` | dangerous | `PUT .../users/{user-id}/groups/{groupId}` |
| `keycloak.group.member.remove` | dangerous | `DELETE .../users/{user-id}/groups/{groupId}` |
| `keycloak.user.delete` | destructive | `DELETE .../users/{user-id}` |
| `keycloak.group.delete` | destructive | `DELETE .../groups/{group-id}` |
| `keycloak.client.delete` | destructive | `DELETE .../clients/{client-uuid}` |
| `keycloak.user.logout` | caution | `POST .../users/{user-id}/logout` |
| `keycloak.realm.delete` | destructive | `DELETE /admin/realms/{realm}` |

The group-lifecycle writes (#3280) are all `dangerous`: group attributes
+ membership drive the backplane's tenant claims (`tenant_id` /
`tenant_role`), so creating an attributed role group and assigning
membership is a tenant-access grant — the same blast-radius class as
`role_mapping.assign`. `group.create` accepts an `attributes` map in
Keycloak's `{key: [values]}` shape (a plain string is wrapped into a
single-element list) and is idempotent by `(parent, name)` (a 409 returns
`already_exists=True` with the existing id). `group.update_attributes`
merges (default) or replaces (`replace=true`) attributes to fix a
mis-attributed group without recreating it. The membership ops resolve a
group by `group_id`/`group_name` and a user by `user_id`/`username`, and
are idempotent (an existing member add / non-member remove returns
`unchanged=True`). Note the membership sub-resource path uses Keycloak's
camelCase `{groupId}` placeholder while the groups resource uses the
hyphenated `{group-id}` — both byte-for-byte the pinned spec's names. The
group writes classify as plain `write` on the broadcast feed (attributes
are not secret material — see the flight-recorder classifier pin in
`broadcast/events.py::_WRITE_OPS`).

Three load-bearing properties (of the original nine; the group ops layer
their own idempotency above):

- **Name→UUID resolution.** `client.update` / `protocol_mapper.create`
  resolve `client_id` → UUID via `?clientId=`; `user.reset_password` /
  `role_mapping.assign` resolve `username` → UUID via
  `?username=&exact=true`; `role_mapping.assign` also resolves each realm
  role name to its `RoleRepresentation`. A create returns the new UUID
  from the `Location` header.
- **Idempotency.** A create that hits HTTP 409 (already-exists) is a
  success (`conflict: true`), and the existing object's UUID is resolved.
- **Password handling.** `user.create` / `user.reset_password` source the
  password through the credential-backend seam (`_read_password_from_vault`
  → `load_vault_secret_data`, #2229/#2401) via a `password_secret_ref`
  param — **never** an inline `password`. A schemeless ref reads the
  deployment-default backend (Vault KV-v2, honouring `password_secret_mount`
  / `password_secret_key`); a `<kind>:` ref (e.g.
  `gsm:<project>/<secret>#password`) reaches that backend, the `#field`
  fragment subsuming `password_secret_key`. The password lands in the Keycloak
  credential body but never in op params; audit stores a `params_hash`
  (never raw params); and both ops are pinned in
  `broadcast.events._CREDENTIAL_WRITE_OPS` → aggregate-only broadcast.
  When Vault refuses, cannot find, or cannot be reached for the ref (a
  connection failure or timeout during the read), the op fails with
  `vault_credentials_read_error` and sends nothing to Keycloak. The error
  shows only the ref prefix and the error class, never the full path
  (#3864).
- **Permission errors.** A write that Keycloak answers with 403 (for example
  a service account without `manage-users`) fails with `connector_http_403`.
  It is never reported as a success.

**Length guard (#3628).** Keycloak keeps some text fields in 255-character
database columns. A longer value fails the database write, and Keycloak
answers with an HTTP 500 `unknown_error` that does not say why. So the
write schemas set `maxLength: 255` on these fields:

- `client.create` / `client.update`: `representation.clientId`, `.name`,
  `.description` (and the top-level `client_id` on `client.create`).
- `user.create`: `representation.username`, `.email`, `.firstName`,
  `.lastName` (and the top-level `username`). A user has no `description`.

The dispatcher checks params against the schema before the policy gate, so
a too-long value comes back at once as `invalid_params`. No approval is
asked for. The cap counts characters, not bytes, and applies to strings
only; other representation keys stay unchecked
(`additionalProperties: true`).

### Updates read, merge, then write (#3941)

`keycloak.client.update` and `keycloak.realm.update` take a partial
representation. Keycloak's `PUT` on a client or a realm is **not** a real
partial update. For some fields that are missing from the body, Keycloak
writes its own default:

- Client `PUT`: authorization services are turned off when
  `authorizationServicesEnabled` is missing
  (`ClientResource.updateAuthorizationSettings`). When `webOrigins` is
  missing, Keycloak builds it from the redirect URIs
  (`RepresentationToModel.defaultWebOrigins`). A dogfood run also saw an
  update that sent only `{"enabled": false}` change `frontchannelLogout`
  and the groups mapper's `multivalued` setting. In the Keycloak 26.3
  code a missing `frontchannelLogout` keeps its current value, so the
  exact source of that change is not known.
- Realm `PUT`: the OAuth 2.0 device-code lifespan and polling interval and
  the CIBA and PAR settings go back to their defaults when missing, and an
  `attributes` map removes every realm attribute it does not list.

So both ops do this (`connectors/keycloak/ops_write_merge.py`):

1. `GET` the current client or realm.
2. Put the caller's fields on top. A map field (`attributes`,
   `smtpServer`) is merged key by key. A list field (`redirectUris`,
   `webOrigins`, `protocolMappers`) is replaced only when the caller
   sends it. So protocol mappers stay as they are unless the caller sends
   them.
3. `PUT` the full representation back.
4. `GET` it again and compare with step 1. The result carries
   `changed_fields` (what the caller sent), `unrequested_changes` (fields
   that changed without being sent; a key inside a sent map shows as
   `attributes.<key>`) and `read_back_checked`. Only field names are
   reported, never values, so a client `secret` or SMTP password never
   reaches the result. List order is ignored in the compare, because
   Keycloak keeps many lists as sets. The key-by-key compare also runs
   when the whole map is missing or `null` before or after the write; a
   map that is not there counts as empty. So a key the caller did not
   send that is gone after the write, or that appeared after the write,
   is reported. A missing key never counts as equal to a key holding
   `null`.

**Limit: an update cannot remove a key inside a map.** Because maps merge
key by key, `realm.update` cannot remove a realm attribute or an
`smtpServer` key, and `client.update` cannot remove a client attribute.
Leaving the key out keeps it. Sending `null` for the key does not remove
it either: Keycloak 26.3 stores the null. To empty a list, send `[]`.

The read-back is best effort: if it fails, or returns something that is
not an object, the write already happened, so the op still returns `ok`
with `read_back_checked: false`. A realm
rename (a new `realm` in the representation) is read back under the new
name.

The approval preview for both ops is the generic params echo: the
approver sees the fields the caller sent. With read-merge-write those are
exactly the fields that will change.

### User logout (#3851)

`keycloak.user.logout` ends every live session of one user. Keycloak then
rejects their refresh tokens at once. Use it when access must stop now,
for example at the end of an event or when someone leaves.

- Pass `id` (the user's UUID) or `username`. A username is looked up with
  `_find_user_uuid` (exact match), the same as `user.reset_password`.
- A user that does not exist raises `KeycloakUserNotFoundError`: no match
  for the username, or a 404 from the logout call for an `id`.
- Safe to run twice. Keycloak answers 204 whether or not the user had
  sessions. The result is `{id, username, logged_out: true}`.
- It reads and returns no credential. It does not change the password (that
  is `user.reset_password`). It signs out one user only; the realm-wide
  `POST .../logout-all` is not offered.
- Risk level `caution`, approval required, group `user_write`. No bespoke
  approval card: the generic params echo shows the `id` / `username`. On the
  broadcast feed it is a plain `write` (pinned in
  `broadcast/events.py::_WRITE_OPS`, because `.logout` is not a write suffix).
- CLI: `meho keycloak user logout (--id|--username)`.

### Governed deletes (#3944)

`keycloak.user.delete`, `keycloak.group.delete` and `keycloak.client.delete`
remove one object each, in the target's managed realm. They are on the
`destructive` risk level
([`governed-delete-operations.md`](../decisions/governed-delete-operations.md)):

- A second person must approve every call. No standing grant, no
  self-approval, and an agent cannot run them.
- The caller first runs `preview_operation` with the same params and passes
  its `preview_hash` to `call_operation`.
- The approval card shows what is deleted (see the preview table below).

How one delete runs:

1. **Find the object.** By internal `id`, or by name: `username` (exact,
   case-insensitive), `client_id` (the exact clientId), or group `name`
   (exact, searched at every level of the group tree with
   `?search=&exact=true&populateHierarchy=false`). A name that matches more
   than one object is refused (`ambiguous_name`); pass the id instead.
2. **Check the guards.** These refuse the delete:
   - `builtin_client` — Keycloak's built-in clients: `admin-cli`, `account`,
     `account-console`, `broker`, `realm-management`,
     `security-admin-console`.
   - `target_login_client` / `target_login_user` — the client and the user
     this MEHO target logs in with: the admin credential's `client_id`, and
     its `username` (password login) or the client's
     `service-account-<clientId>` user (client-secret login). They live in
     the target's `admin_realm`, so these guards apply when `managed_realm`
     is that same realm.
   - `group_has_subgroups` — a group that has sub-groups, unless
     `delete_subgroups=true`. Keycloak deletes a group's sub-groups with it.
     An unknown sub-group count counts as "has sub-groups".
3. **Delete, then read again.** `deleted: true` is reported only after a
   read of the object answers 404. If it can still be read, the call fails
   with `KeycloakDeleteVerificationError`. Only a 404 counts as absent: a
   read that answers with something other than a JSON object (a list, a
   string, `null`) fails with `KeycloakUnexpectedResponseError`, before and
   after the delete.

Steps 1 and 2 run in the preview builder **before** the request waits for
approval, and again in the handler when the approved call runs (this catches
a change made while the request waited). A refused delete never reaches an
approver: the builder raises, the park is refused with
`blast_radius_required`, and the reason carries the refusal, for example
`keycloak.client.delete refused (builtin_client): ...`. A delete by name
acts on the object that has that name when the approved call runs; pass
the `id` to pin one exact object (the card shows the id).

Safe to run twice: when the object is already gone, the handler sends no
DELETE and returns `already_absent: true`. The approval card says so too
(`present: false`, irreversibility `none-object-already-absent`), so a
repeated teardown still waits for approval as usual and then reports
`already_absent: true`.

The CLI verbs `meho keycloak user|group|client delete` run the same two
steps: without `--preview-hash` they only preview and print the hash; with
it, the request waits for approval.

### Realm delete (#3886)

`keycloak.realm.delete` removes a whole realm: every user, client, group and
session in it goes at once. It is on the same `destructive` risk level as the
other deletes, in the `realm_write` group. To stop logins but keep the realm,
use `keycloak.realm.update` with `enabled: false`; that can be undone.

- **`realm` is required.** The op never falls back to the target's
  `managed_realm`.
- **Protected realms** are refused with the `protected_realm` code, before
  the request waits for approval and again when the approved call runs:
  - the target's `admin_realm` (default `master`);
  - the target's `managed_realm`;
  - the realm MEHO's own login uses: the realm in `KEYCLOAK_ISSUER_URL`, and
    the realm in `KEYCLOAK_ADMIN_URL` when that is set. This realm is
    refused on every Keycloak target, because MEHO cannot tell for sure that
    a target is a different Keycloak. A realm with the same name on another
    Keycloak cannot be deleted through MEHO.
  - Names are compared without case.
- **Safe to run twice.** If `GET /admin/realms/{realm}` answers 404, no
  DELETE is sent and the result is `{realm, deleted: false, already_absent:
  true}`.
- **Read again after the delete.** `deleted: true` is reported only after
  the realm read answers 404. A read that answers with something other
  than a realm object is an error (`KeycloakUnexpectedResponseError`), not
  proof that the realm is gone.
- **Approval card.** `blast_radius.object` carries `{kind, resource, realm,
  present, enabled, users, clients, groups}`. The counts come from
  `GET .../users/count`, `GET .../groups/count` (all groups, not only the
  top level) and the length of `GET .../clients`. The client read must
  answer with a JSON array. Any other body is an error
  (`KeycloakUnexpectedResponseError`), so the card is not built and
  nothing waits for approval; it never shows `clients: 0` for a realm
  whose clients could not be read. The children list repeats the three
  counts. The realm representation is never echoed, so the SMTP password
  and client secrets stay out of the card.

The CLI verb is `meho keycloak realm delete --realm R`, with the same two
steps as the other delete verbs.

### Park-time approval preview (#1857)

Eleven of the write ops register bespoke `proposed_effect` preview builders
in `connectors/keycloak/ops_write_preview.py`, wired onto the per-op hook
(`operations/_preview.py`, #1437) by an import side-effect in the package
`__init__`. They give the approval reviewer a resource-centric view at park
time — secrets scrubbed via the same `redact_secret_fields` the read ops
use:

| op_id | preview keys |
|---|---|
| `keycloak.realm.create` | `{resource, realm, representation}` |
| `keycloak.user.create` | `{resource, username, realm, representation[, password_source, password_secret_ref]}` |
| `keycloak.role_mapping.assign` | `{resource, username, id, realm, granted_roles}` |
| `keycloak.group.create` | `{resource, name, parent_id, realm, attribute_keys, attributes}` |
| `keycloak.group.update_attributes` | `{resource, id, name, realm, replace, attribute_keys, attributes}` |
| `keycloak.group.member.add` / `.member.remove` | `{resource, action, user_id, username, group_id, group_name, realm}` |
| `keycloak.user.delete` | `{blast_radius}` — user name, enabled or not, groups |
| `keycloak.group.delete` | `{blast_radius}` — name, path, member count (up to 1000), sub-group count; direct sub-groups as children |
| `keycloak.client.delete` | `{blast_radius}` — client id, enabled or not, public or confidential; its service-account user as a child; never the secret |
| `keycloak.realm.delete` | `{blast_radius}` — realm, enabled or not, user / client / group counts; never the realm body |

The group builders surface `attribute_keys` (which attributes change,
e.g. `tenant_id` / `tenant_role`) and echo the attribute map scrubbed —
attributes are not secrets, but a stray `password`-keyed attribute is
redacted for defence in depth.

`keycloak.user.create` classifies as `credential_write`, so the *generic*
params-echo default (#1856) is suppressed for it — but a bespoke builder is
the deliberate exception (it owns its field discipline), so the user-create
builder runs and scrubs the inline password
(`representation.credentials[].value`) to `***REDACTED***` while the
username stays visible. Because `redact_secret_fields` also covers the
generic credential key names (notably `password`, case-insensitively), the
bespoke builders are at least as strict as the generic echo they bypass: a
`RealmRepresentation.smtpServer.password` (or any `password`-keyed field
anywhere in an `additionalProperties` representation) is scrubbed too, so no
cleartext secret reaches the durable `proposed_effect` row. A Vault-sourced
password (`password_secret_ref`) is a path, not a secret, so it is surfaced
as `password_source: "vault"` + the ref. The other seven write ops fall
through to the generic params-echo
default. Most builders are pure (no connector I/O) and so fail-soft by
construction. The four delete builders are not: they read the object live
(so the egress-free `preview_operation` path, which has no connector, gets
no card from them) and raise on purpose to refuse a delete. See
[`approvals.md`](approvals.md) "`proposed_effect` builder hook" for the
registry contract.

**Spec-reconcile lane (#2988).** Every request path the connector dispatches
lives as a `_*_PATH` template constant in
`backend/src/meho_backplane/connectors/keycloak/_paths.py` (hoisted out of
inline f-strings by #2988); handlers fill a template via `fill_path`, whose
placeholder names are byte-for-byte the pinned spec's own parameter names
(`{realm}`, `{client-uuid}`, `{user-id}`, `{role-name}`, `{group-id}` — and
the camelCase `{groupId}` on the user-group membership sub-resource, a
Keycloak spec quirk). The lane
[`backend/tests/test_connectors_keycloak_spec_reconcile.py`](../../backend/tests/test_connectors_keycloak_spec_reconcile.py)
(the #2980 harness; parse-only, runs in the required unit sweep, uniform
skip when the shelf is unconfigured) introspects those live constants and
asserts all 37 reconciled `METHOD:/path` op_ids (19 at #2988, +10 group
paths at #3280, +4 delete paths at #3944, +1 user logout path at #3851,
+3 realm-delete paths at #3886) are served by the pinned
`keycloak-26.3` shelf spec (`keycloak-admin-openapi.json` — the vendor's
Admin REST API OpenAPI at the lab's deployed 26.3.3, from Maven Central
`org.keycloak:keycloak-api-docs-dist:26.3.3`, Apache-2.0). Two dispatched
paths are pinned as evidenced exclusions rather than reconciled — the OIDC
token mint (`POST /realms/{realm}/protocol/openid-connect/token`; specified
by OAuth 2.0 / OIDC discovery, not the Admin REST OpenAPI) and
`GET /admin/serverinfo` (unmodeled by the published spec; already
best-effort in `fingerprint`) — and an armed test asserts they stay
unserved, so a future spec pin that begins serving one forces its promotion
into the reconciled set. Standard:
[`docs/decisions/spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

## Target configuration

Base URL is `https://{host}[:{port}]` from `HttpConnector._base_url`. The
two realm knobs live on `target.extras` (no schema migration):

- `admin_realm` — realm the admin client authenticates against (default
  `master`).
- `managed_realm` — realm the connector manages + fingerprints (default
  `evba`).

Resolved through `resolve_realm_config`, which tolerates a missing
`extras` attribute and falls back to the defaults.

## Dependencies

- `connectors/adapters/http.py` — `HttpConnector` base (client pooling,
  retry/timeout, `_get_json`).
- `connectors/_shared/vault_creds.py` — `load_vault_secret_data` (the
  operator-context KV-v2 read) and `VaultCredentialsReadError`.
- `connectors/_shared/vcf_auth.py` — `is_acceptable_auth_model` (the
  shared auth-model boundary gate).
- `connectors/_shared/system_operator.py` — `synthesise_system_operator`
  for the operator-less probe path.
- `operations/typed_register.py` — `register_typed_op_registrar` (the
  T2 seam).
- `httpx` 0.28.x — async HTTP transport.

## Known issues / future work

- `keycloak.idp.create` (identity-provider federation) is deferred — not
  exercised by the bootstrap scripts (#1406). A future task can add it
  under the same registrar walk.
- Object deletes cover users, groups and clients (#3944) and whole realms
  (#3886). There is no client-scope delete.
- No logout-revoke on `aclose` — admin access tokens are short-lived;
  revoke-on-close is deferred (same posture as NSX / vRLI).
- `/admin/serverinfo` is undocumented (though stable across 26.x); the
  version read is best-effort and non-fatal by design.

## References

- Issue: https://github.com/evoila/meho/issues/1393
- Group-lifecycle write ops: https://github.com/evoila/meho/issues/3280
- Governed user / group / client deletes: https://github.com/evoila/meho/issues/3944
- User session logout: https://github.com/evoila/meho/issues/3851
- Governed realm delete: https://github.com/evoila/meho/issues/3886
- Credential whitespace strip + token error_description: https://github.com/evoila/meho/issues/1474
- Parent initiative: https://github.com/evoila/meho/issues/1388
- Keycloak token endpoint + client_credentials grant:
  https://www.keycloak.org/securing-apps/oidc-layers
- OAuth 2.0 token-endpoint error response (RFC 6749 §5.2):
  https://datatracker.ietf.org/doc/html/rfc6749#section-5.2
- RealmRepresentation:
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/idm/RealmRepresentation.html
- ServerInfoRepresentation:
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/info/ServerInfoRepresentation.html
