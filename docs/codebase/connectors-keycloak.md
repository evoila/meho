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
those scripts).

Registry v2 triple: `(product="keycloak", version="26.x",
impl_id="keycloak-admin")`, plus the `(keycloak, "", "")` wildcard so a
fresh target with no asserted version still resolves.

## Key types

- `KeycloakConnector` (`connectors/keycloak/connector.py`) — the
  `HttpConnector` subclass. Holds a per-target admin-token cache with
  TTL-driven refresh and an injectable admin-credential loader. Exposes a
  thin bound-method shim per read op (`realm_get`, `client_list`,
  `client_get`, `client_scope_list`, `user_list`, `role_mapping_get`) and
  per write op (`realm_create`/`realm_update`, `client_create`/
  `client_update`, `client_scope_create`, `protocol_mapper_create`,
  `user_create`/`user_reset_password`, `role_mapping_assign`), the
  `_get_admin_json` / `_get_admin_list` GET helpers (object vs array
  responses), and the `_write_admin` mutating helper plus the
  `_find_client_uuid` / `_find_user_uuid` / `_find_realm_role` name→UUID
  resolvers.
- `KeycloakOp` + `READ_OPS` (`connectors/keycloak/ops_read.py`) — the
  op-metadata dataclass and the six-op read registration table (the bind9 /
  pfSense `ops`-table precedent). `WHEN_TO_USE_BY_GROUP` maps each op
  group to its curated selection blurb.
- `WRITE_OPS` + the write handlers (`connectors/keycloak/ops_write.py`) —
  the nine approval-gated write ops, reusing the `KeycloakOp` dataclass.
  Every op is `requires_approval=True`; a create treats HTTP 409 as an
  idempotent success; `user.create`/`user.reset_password` source the
  password from Vault via `_read_password_from_vault` (never inline).
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
  fails or returns no usable `access_token`. On a non-2xx with an OAuth2
  error body it echoes Keycloak's `{error, error_description}` (RFC 6749
  §5.2 — both non-secret) so a bad secret, a client not allowed the grant,
  and a wrong realm are distinguishable from the error string alone, no
  backplane logs needed (#1474). A non-OAuth2 body adds no detail.
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

## Read ops (G3.13-T2)

Six `safety_level="safe"` / `requires_approval=False` read ops, all
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

`client.get` / `role_mapping.get` take the **internal UUID** (`id`), not
the human `clientId` / `username` — discover it via the matching `.list`
op first.

### Secret redaction

Every handler runs its response through `redact_secret_fields` before
returning, so the value of `secret` (confidential-client secret),
`credentials` / `value` / `secretData` / `credentialData` (user
credential material) is replaced with `***REDACTED***` — recursively,
including secrets nested inside protocol mappers or identity-provider
configs. The scrub happens at the connector boundary (not the broadcast
layer) because these are config reads where the secret is incidental: it
must never enter the synchronous `OperationResult` the caller receives.
The write surface redacts secret *inputs* at the classification layer per
the general posture (see "Write ops" below).

## Write ops (G3.13-T4)

Nine approval-gated write ops (`requires_approval=True`), all dispatching
via the admin-auth path, all keyed on the object's **UUID**:

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

Three load-bearing properties:

- **Name→UUID resolution.** `client.update` / `protocol_mapper.create`
  resolve `client_id` → UUID via `?clientId=`; `user.reset_password` /
  `role_mapping.assign` resolve `username` → UUID via
  `?username=&exact=true`; `role_mapping.assign` also resolves each realm
  role name to its `RoleRepresentation`. A create returns the new UUID
  from the `Location` header.
- **Idempotency.** A create that hits HTTP 409 (already-exists) is a
  success (`conflict: true`), and the existing object's UUID is resolved.
- **Password handling.** `user.create` / `user.reset_password` source the
  password from Vault (`_read_password_from_vault`, operator-context
  `vault_client_for_operator`) via a `password_secret_ref` path param —
  **never** an inline `password`. The password lands in the Keycloak
  credential body but never in op params; audit stores a `params_hash`
  (never raw params); and both ops are pinned in
  `broadcast.events._CREDENTIAL_WRITE_OPS` → aggregate-only broadcast.

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
- The write ops do **not** ship deletes — the bootstrap scripts only
  create/update; a delete surface (client/user/scope removal) is a
  separate follow-up if an operator workflow needs it.
- No logout-revoke on `aclose` — admin access tokens are short-lived;
  revoke-on-close is deferred (same posture as NSX / vRLI).
- `/admin/serverinfo` is undocumented (though stable across 26.x); the
  version read is best-effort and non-fatal by design.

## References

- Issue: https://github.com/evoila/meho/issues/1393
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
