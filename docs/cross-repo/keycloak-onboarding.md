<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Keycloak op surface onboarding — operator recipe

> Operator-facing recipe for the G3.13 `keycloak-admin-26.x` op surface —
> registering a `keycloak` target, the `meho keycloak …` verb tree, the
> agent meta-tool path, and the load-bearing **admin-vs-operator
> credential split**. The op handlers live in
> [`backend/src/meho_backplane/connectors/keycloak/`](../../backend/src/meho_backplane/connectors/keycloak/);
> the engineering-facing companion is
> [`docs/codebase/connectors-keycloak.md`](../codebase/connectors-keycloak.md).
> This doc is the cookbook every RDC operator reads when inspecting or
> auditing the managed Keycloak realm through `meho keycloak …`.

## What this surface is

The `keycloak-admin-26.x` connector is a **typed** connector: hand-coded
handlers over `httpx` against the Keycloak 26.x Admin REST API,
registered into the G0.6 `endpoint_descriptor` table at backplane
startup. It dispatches under the
`(product="keycloak", version="26.x", impl_id="keycloak-admin")` registry
triple — the connector id `keycloak-admin-26.x`.

The `-admin` discriminator in the impl_id leaves room for a future
non-admin control surface (e.g. a `keycloak-account-26.x` over the
Account REST API) without breaking the resolver's tie-break ladder; v0.1
ships only the Admin REST surface.

The op surface (Initiative
[#1388](https://github.com/evoila/meho/issues/1388)) is a read working set
to inspect and audit the managed realm, plus an approval-gated write set
(G3.13-T4 #1406) that retires the consumer's five Keycloak bootstrap
scripts. The read ops:

| Group | Op ID | Admin REST API | Class |
| --- | --- | --- | --- |
| `realm` | `keycloak.realm.get` | `GET /admin/realms/{realm}` | read-only |
| `client` | `keycloak.client.list` | `GET .../clients` (`?clientId=`/`?max=`) | read-only |
| `client` | `keycloak.client.get` | `GET .../clients/{id}` | read-only |
| `client_scope` | `keycloak.client_scope.list` | `GET .../client-scopes` | read-only |
| `user` | `keycloak.user.list` | `GET .../users` (`?username=`/`?max=`) | read-only |
| `user` | `keycloak.role_mapping.get` | `GET .../users/{id}/role-mappings` | read-only |

Six read ops. All are `safety_level="safe"` and
`requires_approval=False`; the write ops (below) are `caution` /
`dangerous` and all `requires_approval=True`. Every op dispatches through the same
`POST /api/v1/operations/call` route the agent surface uses — auth,
policy, audit, broadcast, and JSONFlux all run as documented in
[CLAUDE.md](../../CLAUDE.md) §6. The CLI verb tree is operator ergonomics
over that one route; it is **not** a separate data path and is **not**
mirrored on the MCP surface (CLAUDE.md postulate 5 — the agent reaches
every Keycloak op via the narrow-waist meta-tools, see the
[agent meta-tool path](#the-agent-meta-tool-path) section).

`client.get` and `role_mapping.get` take the client / user **internal
UUID** (`id`), not the human `clientId` / `username` — discover it via
the matching `.list` op first.

### The write surface (G3.13-T4 #1406)

The approval-gated **write** ops retire the consumer's five Keycloak
bootstrap scripts. All are `requires_approval=True` — a write never
dispatches without going through the human approve-queue (G11.7-T1
#1401); a dispatch returns `status=awaiting_approval` until a human
approves it through the queue.

| Group | Op ID | Admin REST API | Safety |
| --- | --- | --- | --- |
| `realm` | `keycloak.realm.create` | `POST /admin/realms` | dangerous |
| `realm` | `keycloak.realm.update` | `PUT .../realms/{realm}` | caution |
| `client` | `keycloak.client.create` | `POST .../realms/{realm}/clients` | caution |
| `client` | `keycloak.client.update` | `PUT .../clients/{id}` | caution |
| `client_scope` | `keycloak.client_scope.create` | `POST .../client-scopes` | caution |
| `protocol_mapper` | `keycloak.protocol_mapper.create` | `POST .../clients/{id}/protocol-mappers/models` | caution |
| `user` | `keycloak.user.create` | `POST .../realms/{realm}/users` | caution |
| `user` | `keycloak.user.reset_password` | `PUT .../users/{id}/reset-password` | caution |
| `role_mapping` | `keycloak.role_mapping.assign` | `POST .../users/{id}/role-mappings/realm` | dangerous |

Three properties are load-bearing for correctness and safety:

- **Name → UUID resolution.** Keycloak addresses every object by an
  internal UUID, never its human name. `client.update` /
  `protocol_mapper.create` accept either `id` (the UUID) or `client_id`
  (the human clientId, resolved via `?clientId=`); `user.reset_password`
  / `role_mapping.assign` accept either `id` or `username` (resolved via
  `?username=&exact=true`). A create returns the new object's UUID parsed
  from the `Location` header.
- **Idempotency.** A create that hits an HTTP **409 already-exists** is
  treated as a no-op-equivalent success (`conflict: true`), and the
  existing object's UUID is resolved — so re-running a bootstrap step is
  safe.
- **Password handling.** `user.create` / `user.reset_password` **never**
  carry the password inline. Pass `password_secret_ref` (a Vault KV-v2
  path; optional `password_secret_mount` / `password_secret_key`) and the
  connector reads the password from Vault under the operator's identity.
  The password is set as a credential on Keycloak but never enters the op
  params, the audit row (audit stores a `params_hash`, never raw params),
  or the broadcast feed (`keycloak.user.create` /
  `keycloak.user.reset_password` classify as `credential_write` →
  aggregate-only broadcast). The CLI exposes only `--password-secret-ref`,
  never an inline `--password`, so the secret never lands in shell
  history or `ps` output.

`keycloak.idp.create` (identity-provider federation) is **deferred** —
it is not exercised by the current bootstrap scripts (#1406), so it is
out of scope for this surface. A future task can add it under the same
registrar walk.

#### Bootstrap-script → MEHO-op mapping

The five consumer bootstrap scripts now have callable MEHO equivalents.
Each script's operations decompose into the write ops above (drive them
through `meho keycloak …` or the agent meta-tools, feeding the same JSON
representations the scripts POSTed to `kcadm.sh`):

| Bootstrap script | What it does | MEHO equivalent op(s) |
| --- | --- | --- |
| `keycloak-bootstrap-meho-admin.sh` | Realm + admin service-account client + its protocol mappers + role grants | `keycloak.realm.create`, `keycloak.client.create`, `keycloak.protocol_mapper.create`, `keycloak.role_mapping.assign` |
| `keycloak-bootstrap-meho-cli.sh` | The `meho-cli` public client (device/PKCE flow) + tenant claim mappers | `keycloak.client.create`, `keycloak.protocol_mapper.create` |
| `keycloak-bootstrap-meho-mcp.sh` | The `meho-mcp` confidential client + client scopes + `tenant_id`/`tenant_role` mappers | `keycloak.client.create`, `keycloak.client_scope.create`, `keycloak.protocol_mapper.create` |
| `keycloak-bootstrap-meho-web.sh` | The `meho-web` client (redirect URIs / web origins) + claim mappers | `keycloak.client.create`, `keycloak.protocol_mapper.create` |
| `keycloak-provision-meho-user.sh` | Provision an operator user with a Vault-sourced password + realm-role grant | `keycloak.user.create` (`password_secret_ref`), `keycloak.role_mapping.assign` |

The never-built `scripts/keycloak.sh` umbrella is discharged by the
`meho keycloak …` verb tree as a whole.

> **Not** the `meho admin keycloak …` deployer-onramp. That subtree
> (#791) bootstraps Keycloak clients during initial deployment, before a
> connector target exists. This `meho keycloak …` tree reads the managed
> realm's live configuration through the registered
> `keycloak-admin-26.x` connector. Different lifecycle, different
> credential path — see the [credential split](#the-admin-vs-operator-credential-split-load-bearing).

## The admin-vs-operator credential split (load-bearing)

MEHO is its own identity provider: the backplane authenticates its own
callers with **operator-OIDC tokens that Keycloak issues**
(`operator.raw_jwt`). The connector that *manages* that Keycloak must
**not** authenticate through the same path, or it could never bootstrap a
freshly deployed Keycloak whose operator-login clients are not yet
configured — the chicken-and-egg. If the connector depended on
operator-OIDC, a brand-new Keycloak (no `meho-backplane` client, no
operator realm roles) would be unmanageable, because no operator could
ever obtain a token from it in the first place.

So the connector authenticates to the Keycloak **Admin REST API** with a
**separate admin credential** sourced from Vault. The two credential
paths are deliberately distinct:

```
Operator session (meho login)
  └─ operator-OIDC JWT (operator.raw_jwt) — issued by the managed Keycloak
       │
       │  authorises ONLY the Vault read below
       ▼
  Vault KV-v2 read at target.secret_ref  (operator-context: the operator's
  └─ admin credential                     JWT → Vault JWT/OIDC auth method,
       │                                  per-operator RBAC + audit)
       │  exchanged at Keycloak's OWN token endpoint
       ▼
  POST /realms/{admin_realm}/protocol/openid-connect/token  (form-encoded)
  └─ admin access token
       │
       ▼
  Authorization: Bearer <admin_token>   →  every Admin REST call
```

The split, restated:

1. **Operator-OIDC path** — the operator's validated JWT authorises only
   the operator-context Vault KV-v2 read of the admin credential (the
   locked Option A decision in
   [`docs/architecture/connector-auth.md`](../architecture/connector-auth.md)).
   This is the same `load_vault_secret_data` helper every operator-context
   connector uses; the read is attributed to the operator's Vault Identity
   entity with per-operator RBAC + audit.
2. **Admin-credential path** — the admin credential read out of Vault is
   exchanged at Keycloak's own token endpoint for an admin access token,
   which is then sent as the `Authorization: Bearer` on every Admin REST
   call.

**The operator's OIDC token is never sent to Keycloak.** It authorises
the Vault read; the admin credential authenticates to Keycloak. A test
(`test_keycloak_e2e_all_ops_use_admin_token_never_operator_jwt` in
[`backend/tests/test_connectors_keycloak_e2e.py`](../../backend/tests/test_connectors_keycloak_e2e.py))
asserts the operator JWT appears on no captured Keycloak request, and
`test_keycloak_e2e_admin_token_refreshes_across_dispatch` asserts the
same invariant holds across an admin-token re-mint mid-stream.

### Admin token lifecycle

The connector caches the admin token per target with a TTL-driven
refresh: the effective TTL is the token's `expires_in` minus a 30 s
refresh margin (floored at 1 s), so a near-expiry token is re-minted
*before* a downstream Admin REST call would fail on it. The re-mint is
transparent to the caller — a dispatched op whose cached token has lapsed
silently triggers a fresh token-endpoint round-trip, then proceeds. The
`aclose` path clears the cache but issues no logout-revoke (the token is
short-lived; the revoke round-trip is more risk than benefit — same
posture as NSX / vRLI).

### Admin credential discriminator

The admin Vault secret carries one of two shapes; the loader picks the
grant from which fields the operator stored (the same payload-shape
discriminator the gh-rest connector uses for App-vs-PAT):

| Vault fields present | Credential | Grant |
| --- | --- | --- |
| `client_id` + `client_secret` | `KeycloakClientCredentials` | `client_credentials` (preferred — service-account client) |
| `username` + `password` (no client pair) | `KeycloakPasswordCredentials` | `password` on `admin-cli` (break-glass) |
| neither | `KeycloakAmbiguousVaultPayloadError` | — (remediation-bearing error) |

The **preferred** path is `client_credentials` against a dedicated
confidential service-account client (e.g. a `meho-admin` client, or
`admin-cli` with a secret) holding the `realm-management` service-account
roles needed to read the managed realm. The password grant against
`admin-cli` is the break-glass fallback; the password shape accepts an
optional `client_id` field (default `admin-cli`, Keycloak's public
direct-access-grant client).

## Prerequisites

- **A reachable Keycloak 26.x deployment.** The connector talks the
  Keycloak Admin REST API over HTTPS; `supported_version_range` is
  `>=26.0,<27.0`.
- **An admin credential stored in Vault.** A confidential service-account
  client (preferred) or an admin username/password (break-glass), stored
  at the target's `secret_ref` path. For the RDC fleet the consumer path
  is `secret/rdc-hetzner-dc/keycloak/admin`. The service-account client
  needs the `realm-management` roles to read the managed realm (at
  minimum `view-realm`, `view-clients`, `view-users` for the six read
  ops). See [credential split](#the-admin-vs-operator-credential-split-load-bearing).
- **A registered `keycloak` target** in the MEHO `targets` table (see
  below).
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb. The operator's JWT
  authorises the Vault read that backs the admin-token mint; `operator`
  role is the minimum.

## Target configuration

A keycloak target row carries:

| Field | Example | Notes |
| --- | --- | --- |
| `name` | `rdc-keycloak` | Slug used with `--target` |
| `product` | `keycloak` | Must be exactly `keycloak` |
| `host` | `keycloak.rdc-hetzner-dc.evba.lab` | Keycloak host |
| `port` | `443` | HTTPS port |
| `secret_ref` | `rdc-hetzner-dc/keycloak/admin` | Vault KV-v2 path to the **admin** credential |
| `auth_model` | `shared_service_account` | The only supported auth model (admin credential is a shared service account, not a per-operator identity) |
| `preferred_impl_id` | `keycloak-admin` | G0.6 resolver tie-break override → pins the `keycloak-admin-26.x` connector |
| `extras.admin_realm` | `master` | Realm the admin client authenticates against (default `master`) |
| `extras.managed_realm` | `evba` | Realm the connector manages + fingerprints (default `evba`) |

The two realm knobs live on `target.extras` (a free-form JSONB column) so
they are target-configurable without a schema migration. Both fall back
to their defaults (`master` / `evba`) when absent, so a target on the RDC
fleet that uses those realms can omit `extras` entirely.

`auth_headers` rejects any `auth_model` other than
`shared_service_account` (or `None` for pre-G0.3 targets) with a clear
`NotImplementedError` naming the target and the requested mode.

### targets.yaml entry

```yaml
targets:
  - name: rdc-keycloak
    product: keycloak
    host: keycloak.rdc-hetzner-dc.evba.lab
    port: 443
    secret_ref: rdc-hetzner-dc/keycloak/admin
    auth_model: shared_service_account
    preferred_impl_id: keycloak-admin
    extras:
      admin_realm: master
      managed_realm: evba
```

Register with:

```console
$ meho targets import targets.yaml
```

Verify with:

```console
$ meho targets probe rdc-keycloak
```

A green probe confirms: the admin credential reads out of Vault, the
admin token mints at `POST /realms/{admin_realm}/protocol/openid-connect/token`,
and `GET /admin/realms/{managed_realm}` round-trips. Because every
Keycloak admin endpoint is authenticated, `ok=true` implies the admin
credential is **valid**, not merely that the socket is open. The probe is
the same `GET /admin/realms/{realm}` call the connector's `fingerprint`
issues; it surfaces `realm` / `enabled` / `sslRequired` / `loginTheme`
plus the resolved `admin_realm` / `managed_realm` pair, and a best-effort
server version from `GET /admin/serverinfo` (`systemInfo.version`).

## Quick-start

```console
# Verify the target is reachable + admin credentials work
$ meho targets probe rdc-keycloak

# Read the managed realm's top-level config
$ meho keycloak realm get --target rdc-keycloak

# List clients in the realm
$ meho keycloak client list --target rdc-keycloak

# Filter to one client by its human clientId
$ meho keycloak client list --target rdc-keycloak --client-id meho-backplane

# Fetch one client's full config by its internal UUID
$ meho keycloak client get --target rdc-keycloak --id 11111111-1111-1111-1111-111111111111

# List client scopes
$ meho keycloak client-scope list --target rdc-keycloak

# List users (credentials never surface)
$ meho keycloak user list --target rdc-keycloak --username operator-a

# Read a user's realm + client role mappings by internal UUID
$ meho keycloak role-mapping get --target rdc-keycloak --id 22222222-2222-2222-2222-222222222222

# JSON output for piping to jq
$ meho keycloak client list --target rdc-keycloak --json | jq '.result.rows[].id'
$ meho keycloak realm get --target rdc-keycloak --json | jq '.result.realm.sslRequired'
```

## Verb reference

Every verb takes `--target <slug>` (required for dispatch), `--json`
(emit the full `OperationResult` envelope), and `--backplane <url>`
(override the URL from the most recent `meho login`). Exit codes mirror
`meho operation call`: 0=ok, 1=error/denied, 2=auth_expired,
3=unreachable, 4=unexpected.

### `meho keycloak realm get`

Maps to `keycloak.realm.get`. GETs `/admin/realms/{realm}` against the
target's managed realm and renders the realm-wide config (`realm`,
`enabled`, `sslRequired`, `loginTheme`, token lifespans). Secrets are
redacted by the connector.

### `meho keycloak client list`

Maps to `keycloak.client.list`. Renders the realm's clients as a table of
`clientId` / `enabled` / `publicClient` / internal `id`.

Flags:
- `--client-id <id>` — filter to a single client by its human clientId
  (Keycloak `?clientId=` exact match).
- `--max <n>` — cap the result count.

Each row's confidential-client `secret` is redacted. The internal `id` is
the UUID `meho keycloak client get --id` expects.

### `meho keycloak client get`

Maps to `keycloak.client.get`. GETs `/admin/realms/{realm}/clients/{id}`
where `--id` is the client's **internal UUID** (the `id` field from
`client list`, NOT the human `clientId`). Renders the redirect URIs, web
origins, and protocol-mapper names. The client `secret` is redacted.

Flags:
- `--id <uuid>` — **required**; the client's internal UUID.

### `meho keycloak client-scope list`

Maps to `keycloak.client_scope.list`. Renders the realm's client scopes
(`name` / `protocol` / protocol-mapper count) — the reusable mapper/role
bundles clients attach as default or optional scopes.

### `meho keycloak user list`

Maps to `keycloak.user.list`. Renders the realm's users (`username` /
`enabled` / `emailVerified` / internal `id`). User credential material is
never surfaced (redacted at the connector boundary).

Flags:
- `--username <name>` — filter to matching users (Keycloak `?username=`).
- `--max <n>` — cap the result count.

The internal `id` is the UUID `meho keycloak role-mapping get --id`
expects.

### `meho keycloak role-mapping get`

Maps to `keycloak.role_mapping.get`. GETs
`/admin/realms/{realm}/users/{id}/role-mappings` where `--id` is the
user's **internal UUID** (from `user list`). Renders the realm-level role
names and the per-client role names.

Flags:
- `--id <uuid>` — **required**; the user's internal UUID.

## The agent meta-tool path

Per [CLAUDE.md](../../CLAUDE.md) postulate 5, the agent surface is the
narrow-waist meta-tools registered by G0.5 (#226). The CLI verbs are
operator ergonomics over `POST /api/v1/operations/call`; the agent
reaches every Keycloak op via:

```
search_operations(connector_id="keycloak-admin-26.x", query="realm client user role")
call_operation(connector_id="keycloak-admin-26.x", op_id="keycloak.client.list",
               target="rdc-keycloak", params={"client_id": "meho-backplane"})
```

`search_operations(connector_id="keycloak", …)` surfaces all six ops and
`call_operation` dispatches them — verified by
`test_keycloak_e2e_ops_visible_to_search_operations` and the per-op
dispatch tests in
[`backend/tests/test_connectors_keycloak_e2e.py`](../../backend/tests/test_connectors_keycloak_e2e.py).
The `llm_instructions.when_to_use` blurb on each op group guides the
agent — e.g. the `client` blurb tells the agent to call `client.list`
first to discover a client's internal `id`, then `client.get` for its
full representation.

### Write verb reference

Every write verb takes `--target <slug>`, `--json`, and `--backplane
<url>` like the read verbs. Create / update verbs take the
representation body from a JSON file via `--representation-file` / `-f`.

```console
# Create a realm (idempotent: re-running on an existing realm succeeds)
$ meho keycloak realm create --target rdc-keycloak -f realm-evba.json

# Update the managed realm's top-level config
$ meho keycloak realm update --target rdc-keycloak -f realm-patch.json

# Create a client; update one by clientId (resolved to its UUID)
$ meho keycloak client create --target rdc-keycloak -f client-meho-web.json
$ meho keycloak client update --target rdc-keycloak --client-id meho-web -f client-patch.json

# Create a client scope
$ meho keycloak client-scope create --target rdc-keycloak -f scope-roles.json

# Add the tenant_id claim mapper to a client (by clientId)
$ meho keycloak protocol-mapper create --target rdc-keycloak \
    --client-id meho-web -f mapper-tenant-id.json

# Create a user; the password is read from Vault, never passed inline
$ meho keycloak user create --target rdc-keycloak -f user-operator-a.json \
    --password-secret-ref rdc-hetzner-dc/keycloak/operator-a

# Reset a user's password from Vault (by username → UUID)
$ meho keycloak user reset-password --target rdc-keycloak \
    --username operator-a --password-secret-ref rdc-hetzner-dc/keycloak/operator-a

# Grant a realm role to a user (privilege grant; --role is repeatable)
$ meho keycloak role-mapping assign --target rdc-keycloak \
    --username operator-a --role tenant_admin
```

Because every write is `requires_approval=True`, the first dispatch
returns `status=awaiting_approval` and parks the call in the approval
queue. A human approves it through the queue; the approved write then
runs against Keycloak.

## Audit and broadcast

Every `meho keycloak …` dispatch writes an audit row to the
`operation_audit_log` table:

- `connector_id = "keycloak-admin-26.x"`
- `op_id` = the dispatched op (e.g. `keycloak.client.list`)
- `target_id` = the resolved target row ID
- `params_hash` = SHA-256 of the input params (for replay detection)
- `status` = `ok` / `error` / `denied`
- `duration_ms` = connector wall-clock time

Broadcast events follow the standard envelope (CLAUDE.md §6 §7). The six
read ops are `safety_level="safe"` and broadcast with `risk_level=LOW`
unless the agent's policy engine overrides. The write ops broadcast under
their mutation class — `keycloak.role_mapping.assign` and the create /
update verbs as `write`, and the two password ops
(`keycloak.user.create` / `keycloak.user.reset_password`) as
`credential_write`, which collapses the broadcast to aggregate-only so no
credential material reaches the feed.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `status=error connector_error: NotImplementedError: KeycloakConnector only supports auth_model='shared_service_account'` | Target `auth_model` is something else | Set `auth_model: shared_service_account` on the target; re-import |
| `status=error connector_error: KeycloakAmbiguousVaultPayloadError` | The admin Vault secret carries neither credential shape | Populate `client_id`+`client_secret` (preferred) or `username`+`password` at `target.secret_ref` |
| `status=error connector_error: KeycloakAdminTokenError … returned HTTP 401` | Admin credential invalid, or service-account client lacks `realm-management` roles | Verify the Vault secret; grant the service-account client the `view-*` realm-management roles |
| `status=error connector_error: VaultCredentialsReadError … no operator JWT` | A system-initiated (background) caller tried to read the admin credential | Dispatch under an authenticated operator session — the admin-credential read is operator-context |
| Probe fails, `extras.error` carries a transport error | Keycloak host/port unreachable or TLS failure | Verify `host`/`port`; check Keycloak is up and reachable from the backplane host |
| `client get` / `role-mapping get` 404 | `--id` is the human clientId / username, not the internal UUID | Run `client list` / `user list` first; pass the `id` field from a row |
| `status=denied` | operator token lacks the required role | Use a token with at least `operator` role |

## Goal #214 G3.13 keycloak checklist

| Checklist item | Status |
| --- | --- |
| G3.13-T1 #1393 — `KeycloakConnector` skeleton + admin credential loader + fingerprint + dual registration | ✅ merged |
| G3.13-T2 #1394 — 6 curated read ops (realm / client / client-scope / user / role-mapping), secret-redacted | ✅ merged |
| G3.13-T3 #1395 — `meho keycloak …` CLI verbs (all 6 ops) | ✅ this PR |
| MCP `search_operations` / `call_operation` dispatch reviewed | ✅ this PR (`test_connectors_keycloak_e2e.py`) |
| respx recorded-fixture E2E for all 6 ops + admin-token refresh through dispatch | ✅ `test_connectors_keycloak_e2e.py` |
| `docs/cross-repo/keycloak-onboarding.md` with admin-vs-operator split + deferred-write note | ✅ this document |
| G3.13-T4 #1406 — approval-gated write ops (realm/client/scope/protocol-mapper/user/role-mapping) that retire the 5 bootstrap scripts | ✅ this PR (`idp.create` deferred — not exercised by the scripts) |

## References

- Initiative: [#1388 G3.13 Keycloak connector](https://github.com/evoila/meho/issues/1388);
  Goal [#214](https://github.com/evoila/meho/issues/214) (connector parity).
- Tasks that shipped this surface: [#1393](https://github.com/evoila/meho/issues/1393) (T1 skeleton + auth),
  [#1394](https://github.com/evoila/meho/issues/1394) (T2 read ops),
  [#1395](https://github.com/evoila/meho/issues/1395) (T3 CLI + MCP review + E2E + this doc),
  [#1406](https://github.com/evoila/meho/issues/1406) (T4 approval-gated write surface).
- Connector source: [`backend/src/meho_backplane/connectors/keycloak/`](../../backend/src/meho_backplane/connectors/keycloak/).
- CLI verbs: [`cli/internal/cmd/keycloak/`](../../cli/internal/cmd/keycloak/).
- E2E tests: [`backend/tests/test_connectors_keycloak_e2e.py`](../../backend/tests/test_connectors_keycloak_e2e.py).
- Engineering codebase doc: [`docs/codebase/connectors-keycloak.md`](../codebase/connectors-keycloak.md).
- Keycloak 26.3 Admin REST API: <https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html>
- Keycloak token endpoint + client_credentials grant: <https://www.keycloak.org/securing-apps/oidc-layers>
- Related onboarding docs: [`targets-yaml.md`](./targets-yaml.md),
  [`vault-onboarding.md`](./vault-onboarding.md),
  [`connector-vault-policy.md`](./connector-vault-policy.md),
  [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md).
```
