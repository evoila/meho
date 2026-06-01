# Connector: Keycloak (keycloak-admin)

## Overview

`KeycloakConnector` is the typed connector for the Keycloak Admin REST
API. MEHO runs Keycloak as its own identity provider on the RDC fleet;
this connector manages that Keycloak through its Admin REST surface.

G3.13-T1 (#1393) ships the **substrate**: the connector class, the admin
credential loader, `fingerprint()`, and dual registry registration. Read
operations land in T2 and onboarding docs in T3. The module ships **zero**
typed operations — the typed-op registrar seam is wired (so the lifespan
already drives Keycloak) but the op walk is empty until T2.

Registry v2 triple: `(product="keycloak", version="26.x",
impl_id="keycloak-admin")`, plus the `(keycloak, "", "")` wildcard so a
fresh target with no asserted version still resolves.

## Key types

- `KeycloakConnector` (`connectors/keycloak/connector.py`) — the
  `HttpConnector` subclass. Holds a per-target admin-token cache with
  TTL-driven refresh and an injectable admin-credential loader.
- `KeycloakClientCredentials` / `KeycloakPasswordCredentials`
  (`connectors/keycloak/session.py`) — the two admin-credential shapes
  (tagged union `KeycloakAdminCredentials`).
- `KeycloakTargetLike` — structural Protocol the concrete `Target` model
  satisfies; adds `extras` (carrying the realm overrides) to the common
  REST-target shape.
- `RealmConfig` — the resolved `(admin_realm, managed_realm)` pair.
- `KeycloakAdminTokenError` — raised when the token-endpoint round-trip
  fails or returns no usable `access_token`.
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

- T2 (#1394 et al.) fills the read-op walk in
  `KeycloakConnector.register_operations`.
- No logout-revoke on `aclose` — admin access tokens are short-lived;
  revoke-on-close is deferred (same posture as NSX / vRLI).
- `/admin/serverinfo` is undocumented (though stable across 26.x); the
  version read is best-effort and non-fatal by design.

## References

- Issue: https://github.com/evoila/meho/issues/1393
- Parent initiative: https://github.com/evoila/meho/issues/1388
- Keycloak token endpoint + client_credentials grant:
  https://www.keycloak.org/securing-apps/oidc-layers
- RealmRepresentation:
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/idm/RealmRepresentation.html
- ServerInfoRepresentation:
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/info/ServerInfoRepresentation.html
