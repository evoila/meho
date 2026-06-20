# Connector `auth_headers` coverage — grounding for the ExecutionProfile auth catalog

G0.28-T3 (#1969), Initiative #1965, Goal #1964.

This is the first deliverable of #1969: a grounded trace of every HTTP
connector's `auth_headers` (and session-login) implementation, used to
decide which auth shapes the closed `ExecutionProfile` named-auth catalog
covers and which stay typed (hand-coded). The catalog
(`AuthSchemeName` in `backend/src/meho_backplane/connectors/profile.py`)
is derived from this table, **not** from a list picked from memory.

## Why this gates the schema

The profile's `auth.scheme` selects a *vetted named extractor* — it carries
no path/template/expression fields (that is the rejected DSL line, #1177).
A named scheme is only sound when an existing connector already proves the
extractor is a pure `credentials -> dict[str, str]` (or
`credentials -> login -> Bearer dict[str, str]`) function. Auth shapes that
are stateful (cookie jars), need asymmetric crypto (RS256 JWT mint), or
return something other than a header dict (kubeconfig-backed `ApiClient`)
cannot be a named scheme — they are listed as **reserved** and a profile
naming one raises `ReservedAuthSchemeError` ("author a typed connector").

## Coverage table (14 HTTP connectors)

| # | Connector | File (auth method) | Mechanism | Secret field names | Returns | Catalog verdict |
|---|-----------|--------------------|-----------|--------------------|---------|-----------------|
| 1 | harbor | `harbor/connector.py` `auth_headers` | HTTP Basic `base64(user:pass)` → `Authorization: Basic` | `username`, `password` | `dict[str,str]` | **`basic`** |
| 2 | sddc_manager | `sddc_manager/connector.py` `auth_headers` | HTTP Basic `base64(user@sso_realm:pass)` | `username`, `password` | `dict[str,str]` | **`basic`** |
| 3 | vcf_fleet | `vcf_fleet/connector.py` (`_shared/vcf_auth.basic_auth_header`) | HTTP Basic (shared helper) | `username`, `password` | `dict[str,str]` | **`basic`** |
| 4 | vcf_operations | `vcf_operations/connector.py` `auth_headers` | HTTP Basic + optional `?auth-source=` query | `username`, `password` | `dict[str,str]` | **`basic`** |
| 5 | hetzner_robot | `hetzner_robot/connector.py` `_basic_auth_header` | HTTP Basic (hard-fail on first 401, IP-block guard) | `username`, `password` | `dict[str,str]` | **`basic`** |
| 6 | argocd | `argocd/connector.py` `auth_headers` | Static pre-issued token → `Authorization: Bearer <token>` | `token` | `dict[str,str]` | **`static_header`** (`value_kind=bearer`) |
| 7 | keycloak | `keycloak/connector.py` `auth_headers` | OAuth2 client-credentials form grant `POST /realms/{r}/protocol/openid-connect/token` → cached Bearer | `client_id`, `client_secret` | `dict[str,str]` (Bearer) | **`oauth2_mint`** |
| 8 | vcf_logs (vRLI) | `vcf_logs/connector.py` `auth_headers` | Session login `POST /api/v2/sessions` (JSON `{username,password,provider}`) → `sessionId` → cached Bearer | `username`, `password` | `dict[str,str]` (Bearer) | **`session_login`** |
| 9 | vmware_rest | `vmware_rest/session.py` | Session login `POST /api/session` (Basic) → token from body → cached Bearer | `username`, `password` | `dict[str,str]` (Bearer) | **`session_login`** |
| 10 | github | `github/connector.py` `auth_headers` | App-JWT: mint RS256 JWT → exchange for installation token (or PAT passthrough) | `app_id`, `private_key_pem`, `installation_id` (or `token`) | `dict[str,str]` (Bearer) | **RESERVED** `github_app_jwt` |
| 11 | gcloud | `gcloud/connector.py` `auth_headers` | ADC + `impersonated_credentials` for `target.gcp_impersonate_sa` → refreshed token | ADC source + impersonation target (no static secret) | `dict[str,str]` (Bearer) | **RESERVED** `gcp_sa_impersonation` |
| 12 | vault | `vault/connector.py` (`vault_client_for_operator`) | Operator-context OIDC JWT forward; no per-call header dict | operator `raw_jwt` | not a header dict | **RESERVED** `operator_jwt_forward` |
| 13 | kubernetes | `kubernetes/connector.py` | kubeconfig loaded from Vault → cert/token embedded in `ApiClient` | kubeconfig payload | embedded in `ApiClient` | **RESERVED** `kubeconfig` |
| 14 | nsx | `nsx/session.py` | Session create `POST /api/session/create` (form `j_username`/`j_password`) → `Set-Cookie JSESSIONID` jar + `X-XSRF-TOKEN` | `username`, `password` | mutated cookie jar + `dict[str,str]` | **RESERVED** `cookie_jar_session` |
| 15 | vcf_automation | `vcf_automation/_auth.py` | Dual-plane: provider login (`/cloudapi/.../sessions/provider`) + tenant login (`/iaas/api/login`), token from header / body | `username`, `password`, `domain` | `dict[str,str]` (plane-specific Bearer) | **RESERVED** `dual_plane_session` |

> Row count note: the connectors directory ships 15 HTTP-family connector
> packages; the issue's "14" excludes `vcf_automation`'s dual-plane shape
> as a special case. It is included here for completeness and is reserved.
> Pure non-HTTP connectors (bind9, pfsense, holodeck, secret-broker) are
> out of scope — they have no `auth_headers` HTTP surface.

## Named-scheme partition (what the catalog covers)

Six connectors (five if `vcf_operations`'s query-param merge is counted as
`basic` with a transport quirk) fit a named scheme:

- **`basic`** — harbor, sddc_manager, vcf_fleet, vcf_operations, hetzner_robot
- **`static_header`** — argocd
- **`oauth2_mint`** — keycloak
- **`session_login`** — vcf_logs, vmware_rest

Eight stay **reserved/typed** — github, gcloud, vault, kubernetes, nsx,
vcf_automation — because their auth is stateful, asymmetric-crypto, or
non-`dict[str,str]`. A profile naming any reserved scheme raises
`ReservedAuthSchemeError`; the catalog's closed `Literal` does not list
them at the API boundary.

## Non-idempotent bespoke-body write-op count

This count gates committing to the *full* profiled-write machinery
(deferred to later tasks; T3 covers read dispatch only). Hand-built
request bodies in POST/PUT/PATCH/DELETE ops across the connectors:

| Connector | Write ops (hand-built body) | Where |
|-----------|-----------------------------|-------|
| harbor | 2 | `robot_create` (JSON body), `robot_delete` |
| argocd | ~11 | `app_sync`/`app_rollback`/`app_set`/`app_refresh`/`app_delete`, `appproject_create`/`appproject_update` + `ops_write.py` handlers |
| keycloak | ~10 | `realm_create`/`realm_update`/`client_create`/`client_update`/`client_scope_create`/`protocol_mapper_create`/`user_create`/`user_reset_password`/`role_mapping_assign` + `_write_admin` |
| kubernetes | ~7 | `ops_write.py` handlers |
| others | 0 (read-only / session-establish in v0.2) | — |

**Total: ~30 non-idempotent bespoke-body write ops.** This confirms the
write surface is non-trivial and concentrated in four connectors — write
dispatch via profiles (per-op declarative bodies) is a real future cost,
deferred out of T3's read-only scope. Read dispatch (the T3 target) needs
only the auth slot filled, which the named-scheme catalog covers for the
six in-catalog connectors.

## T4 (#1970) — the runtime extractors + hoisted session harness

T3's catalog named four schemes; T4 lands the Python that runs them, so a
stamped `ProfiledRestConnector` actually dispatches. Each `AuthSchemeName`
value now resolves to one vetted extractor, with **no behaviour loss**
against the typed connectors the row was grounded on:

- **`basic` / `static_header`** — *stateless*. `build_static_headers` in
  `backend/src/meho_backplane/connectors/_shared/profile_auth.py` computes
  the header from the secret bundle on every call (Basic `base64(user:pass)`;
  bearer-wrapped or raw pre-issued token per `value_kind`). No token cache,
  no login round-trip.
- **`session_login` (vRLI) / `oauth2_mint` (keycloak)** — *session-stateful*.
  The per-target lock / token cache (keyed `(tenant_id, id)`) / single-flight
  / TTL-or-expiry refresh / empty-`raw_jwt` fail-closed harness lives **once**
  in `ProfiledRestConnector` (it was duplicated across `vcf_logs` and
  `keycloak` before). The scheme-specific login mechanics — login path, body
  encoding (`json` for vRLI's `{username,password,provider}`, `form` for the
  OAuth2 client-credentials grant), and the token + TTL extractor — are
  `SessionSchemeSpec` entries in `SESSION_SCHEME_SPECS`, selected by
  `profile.auth.scheme`. `session_login` caches until a downstream re-login
  (idle-expiry, `ttl_seconds=None`); `oauth2_mint` re-mints once the
  monotonic clock passes the margin-adjusted `expires_in`.

The login POST goes through the pooled `httpx.AsyncClient` directly (not the
`auth_headers`-stamping `_post_json` seam) — the login *is* what establishes
auth, so routing it through `auth_headers` would recurse and deadlock on the
session lock. `auth_headers` rejects `per_user` / impersonation with the
standard `NotImplementedError` (a profile is a `shared_service_account`
construct). The default credential loader reads exactly the profile's
declared `secret_fields` via the shared `load_basic_credentials` helper, so
`static_header` (`token`) and `oauth2_mint` (`client_id`/`client_secret`)
resolve the right secret shape through the one fail-closed reader.

NSX stays typed (the `cookie_jar_session` reserved scheme): its
`JSESSIONID` Set-Cookie jar cannot be modeled by the `dict[str, str]`
`auth_headers` return contract.

## References

- `backend/src/meho_backplane/connectors/profile.py` — the schema + catalog.
- `backend/src/meho_backplane/connectors/profiled.py` — T1 (#1967)
  dispatchable sibling; T4 (#1970) hoisted session harness + scheme-driven
  `auth_headers`.
- `backend/src/meho_backplane/connectors/_shared/profile_auth.py` — T4
  (#1970) named extractors (`build_static_headers`, `SESSION_SCHEME_SPECS`).
- `docs/architecture/runbooks.md` — the closed-vocabulary / no-DSL line (#1177).
- Precedents modeled on: `connectors/schemas.AuthModel` (StrEnum),
  `auth/permissions.safety_level` (Literal + ceiling),
  `operations/ingest/catalog.load_catalog` (boot-crash on malformed config).
