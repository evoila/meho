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
| 2 | sddc_manager | `sddc_manager/connector.py` `auth_headers` | Session login `POST /v1/tokens` (JSON `{username,password}`) → `accessToken` → cached Bearer; re-login once on `401` (HTTP Basic rejected by the appliance) | `username`, `password` | `dict[str,str]` (Bearer) | **`session_login_token`** |
| 3 | vcf_fleet | `vcf_fleet/connector.py` (`_shared/vcf_auth.basic_auth_header`) | HTTP Basic (shared helper) | `username`, `password` | `dict[str,str]` | **`basic`** |
| 4 | vcf_operations | `vcf_operations/connector.py` `auth_headers` | HTTP Basic + optional `?auth-source=` query | `username`, `password` | `dict[str,str]` | **`basic`** |
| 5 | hetzner_robot | `hetzner_robot/connector.py` `_basic_auth_header` | HTTP Basic (hard-fail on first 401, IP-block guard) | `username`, `password` | `dict[str,str]` | **`basic`** |
| 6 | argocd | `argocd/connector.py` `auth_headers` | Static pre-issued token → `Authorization: Bearer <token>` | `token` | `dict[str,str]` | **`static_header`** (`value_kind=bearer`) |
| 7 | keycloak | `keycloak/connector.py` `auth_headers` | OAuth2 client-credentials form grant `POST /realms/{r}/protocol/openid-connect/token` → cached Bearer (target-relative by default; an optional external `token_url` + `scope`/`audience` covers an issuer on a separate host, #3571) | `client_id`, `client_secret` | `dict[str,str]` (Bearer) | **`oauth2_mint`** |
| 8 | vcf_logs (vRLI) | `vcf_logs/connector.py` `auth_headers` | Session login `POST /api/v2/sessions` (JSON `{username,password,provider}`) → `sessionId` → cached Bearer | `username`, `password` | `dict[str,str]` (Bearer) | **`session_login`** |
| 9 | vmware_rest | `vmware_rest/connector.py` `_establish_session` | Session login `POST /api/session` (HTTP Basic, no body) → raw JSON-string token from body → cached, sent in `vmware-api-session-id` header | `username`, `password` | `dict[str,str]` (raw `vmware-api-session-id`) | **`session_login_basic`** |
| 10 | github | `github/connector.py` `auth_headers` | App-JWT: mint RS256 JWT → exchange for installation token (or PAT passthrough) | `app_id`, `private_key_pem`, `installation_id` (or `token`) | `dict[str,str]` (Bearer) | **RESERVED** `github_app_jwt` |
| 11 | gcloud | `gcloud/connector.py` `auth_headers` | ADC + `impersonated_credentials` for `target.gcp_impersonate_sa` → refreshed token | ADC source + impersonation target (no static secret) | `dict[str,str]` (Bearer) | **RESERVED** `gcp_sa_impersonation` |
| 12 | vault | `vault/connector.py` (`vault_client_for_operator`) | Operator-context OIDC JWT forward; no per-call header dict | operator `raw_jwt` | not a header dict | **RESERVED** `operator_jwt_forward` |
| 13 | kubernetes | `kubernetes/connector.py` | kubeconfig loaded from Vault → cert/token embedded in `ApiClient` | kubeconfig payload | embedded in `ApiClient` | **RESERVED** `kubeconfig` |
| 14 | nsx | `nsx/session.py` | Session create `POST /api/session/create` (form `j_username`/`j_password`) → `Set-Cookie JSESSIONID` jar + `X-XSRF-TOKEN` | `username`, `password` | mutated cookie jar + `dict[str,str]` | **RESERVED** `cookie_jar_session` |
| 15 | vcf_automation | `vcf_automation/_auth.py` | Dual-plane: provider login (`/cloudapi/.../sessions/provider`) + tenant login (`/iaas/api/login`), token from header / body | `username`, `password`, `domain` | `dict[str,str]` (plane-specific Bearer) | **RESERVED** `dual_plane_session` |
| 16 | vcf_installer | `vcf_installer/connector.py` `auth_headers` | Session login `POST /v1/tokens` (JSON `{username,password}` → `TokenPair.accessToken`) → cached Bearer; re-login once on `401`. Second member of the scheme (SDDC Manager shape) | `username`, `password` | `dict[str,str]` (Bearer) | **`session_login_token`** |

> Row count note: the connectors directory ships 15 HTTP-family connector
> packages; the issue's "14" excludes `vcf_automation`'s dual-plane shape
> as a special case. It is included here for completeness and is reserved.
> Pure non-HTTP connectors (bind9, pfsense, holodeck, secret-broker) are
> out of scope — they have no `auth_headers` HTTP surface.

## Named-scheme partition (what the catalog covers)

Six connectors (five if `vcf_operations`'s query-param merge is counted as
`basic` with a transport quirk) fit a named scheme:

- **`basic`** — harbor, vcf_fleet, vcf_operations, hetzner_robot
- **`static_header`** — argocd
- **`oauth2_mint`** — keycloak
- **`session_login`** — vcf_logs (vRLI: JSON creds body → `sessionId` → Bearer)
- **`session_login_basic`** — vmware_rest (vCenter: HTTP Basic login, no body
  → token from a raw JSON-string body or the legacy `{"value": "<tok>"}`
  object body → `vmware-api-session-id` header; #2025, legacy shape #2047)
- **`session_login_token`** — sddc_manager (SDDC Manager: JSON creds body
  `{username, password}` → response-body `accessToken` field → Bearer). First
  member of this shape; the appliance's HTTP Basic surface is rejected —
  token-only. Grounded in `POST /v1/tokens` live evidence (#2287). The shipped
  `sddc_manager_minimal.yaml` profile selects it and the typed
  `SddcManagerConnector` derives its session from the same scheme (#2290, row 2
  above).

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

T3's catalog named four schemes (a fifth, `session_login_basic`, was added
in #2025 for vCenter's HTTP-Basic login shape); T4 lands the Python that runs them, so a
stamped `ProfiledRestConnector` actually dispatches. Each `AuthSchemeName`
value now resolves to one vetted extractor, with **no behaviour loss**
against the typed connectors the row was grounded on:

- **`basic` / `static_header`** — *stateless*. `build_static_headers` in
  `backend/src/meho_backplane/connectors/_shared/profile_auth.py` computes
  the header from the secret bundle on every call (Basic `base64(user:pass)`;
  bearer-wrapped or raw pre-issued token per `value_kind`). No token cache,
  no login round-trip.
- **`session_login` (vRLI) / `session_login_basic` (vCenter) /
  `session_login_token` (SDDC Manager shape) / `oauth2_mint`
  (keycloak)** — *session-stateful*. The per-target lock / token cache (keyed
  `(tenant_id, id)`) / single-flight / TTL-or-expiry refresh / empty-`raw_jwt`
  fail-closed harness lives **once** in `ProfiledRestConnector` (it was
  duplicated across `vcf_logs` and `keycloak` before). The scheme-specific
  login mechanics — login path, credential carriage (`body` for vRLI's
  `{username,password,provider}` JSON / the OAuth2 form grant, `basic` for
  vCenter's HTTP-Basic `POST /api/session`), body encoding, the token + TTL
  extractor, and how the established token is applied (`Bearer` in
  `Authorization` for vRLI / keycloak, raw in `vmware-api-session-id` for
  vCenter) — are `SessionSchemeSpec` entries in `SESSION_SCHEME_SPECS`,
  selected by `profile.auth.scheme`. `session_login` / `session_login_basic` /
  `session_login_token` cache until a downstream re-login (idle-expiry,
  `ttl_seconds=None`); `session_login_token` reads `accessToken` out of SDDC
  Manager's `POST /v1/tokens` response and sends it as `Bearer` (no refresh
  leg — a full re-login recovers expiry). `oauth2_mint` re-mints once the
  monotonic clock passes the margin-adjusted `expires_in`. `session_login_basic` carries the vetted vCenter
  modern→legacy session fallback (#2031): the harness POSTs the modern
  `/api/session` first and, on **HTTP 404 only** (401/403/5xx are auth/server
  failures, not "endpoint absent"), retries the legacy
  `/rest/com/vmware/cis/session` — the only path the upstream `vmware/vcsim`
  simulator registers. The fallback is a closed, per-scheme
  `LegacyFallback` constant on the `SessionSchemeSpec` (a single vetted
  modern/legacy pair, **not** a profile-supplied candidate-path list — the
  #1177 no-DSL line); only `session_login_basic` declares one. The winning
  login path is recorded per target and drives both op-path mount
  (`ProfiledRestConnector.mount_op_path`: `/api` on modern, `/rest` on
  legacy) and session teardown, mirroring the typed connector's
  `_session_paths`.

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

## `oauth2_mint` — external token issuer (#3571)

The base `oauth2_mint` scheme mints its client-credentials token
**target-relative**: the login POSTs to `/realms/master/...` on the target
host itself (keycloak's admin realm). That models an API whose token endpoint
lives on the same host as the API. It does not model a target whose OAuth2
token issuer is a **separate host** — an identity provider distinct from the
target API. `AuthSpec` therefore carries three **optional** fields, valid only
for `scheme="oauth2_mint"` and forbidden on every other scheme:

| Field | Default | Effect |
|-------|---------|--------|
| `token_url` | `None` → target-relative `/realms/master/...` | Absolute URL of the token endpoint. When set, the mint dials it directly (the pooled client resolves an absolute URL as-is, so the target `base_url` is bypassed for the mint only). |
| `scope` | `None` → no `scope` form param | Forwarded verbatim as the `scope` form parameter of the grant. |
| `audience` | `None` → no `audience` form param | Forwarded verbatim as the `audience` form parameter (Keycloak / RFC 8693-style). |

**Defaults preserve parity.** With all three omitted, the minted request is
byte-identical to the pre-#3571 keycloak shape (target-relative endpoint,
`grant_type`/`client_id`/`client_secret` only). The existing keycloak
`oauth2_mint` regression is unchanged; only a profile that opts in gets the
external issuer.

**`token_url` is not a DSL field.** It names a single absolute endpoint (and
`scope`/`audience` are two opaque strings forwarded verbatim) — never a
path/template/expression the substrate interprets — so the three stay on the
right side of the #1177 rejected-DSL line, alongside `value_kind`.

**Security note — widening the vetted auth catalog.** These fields extend a
closed, security-reviewed auth catalog, so the defaults are chosen to keep the
catalog's existing behaviour fixed and to fail closed on the new surface:

- **`https` rule (fail-closed).** A `token_url` must use `https` for a public
  host; plaintext `http` is accepted **only** for a cluster-internal /
  private-address issuer (a loopback / RFC-1918 / link-local / unique-local IP
  literal, a dotless single-label Service name, or a
  `.svc`/`.cluster.local`/`.local`/`.internal` host). This matches the
  established east-west in-cluster posture (services already dial each other
  plaintext behind a NetworkPolicy) while refusing to ship a client secret to
  a public host over cleartext. Enforced at profile validation
  (`_validate_token_url` in `profile.py`) and re-asserted by the boot-time
  scheme-load guard, which is transparent to the optional fields.
- **No secret / token in logs or spans.** `client_id`/`client_secret` still
  resolve from the Vault-backed target credential; the mint's login POST goes
  through the pooled `httpx.AsyncClient` **directly**, bypassing the recorded
  `_post_json` flight-recorder span, so neither the grant body nor the minted
  access token enters a vendor-call span. The only emitted event
  (`profiled_session_established`) carries the login path — never a secret or
  the token.
- **SSRF screening still applies to the issuer.** The pooled client's pinned
  transport screens whatever host it dials at the socket boundary, so a mint
  to an external issuer is subject to the same SSRF allowlist / private-range
  guard as a dispatch to the target. An internal issuer must therefore be
  admitted by the deploy-side SSRF allowlist, exactly like the target.
- **Known limitation.** A target's `tls_server_name` SNI override (#2398) is
  still threaded onto the login POST; it is meaningful only for a
  target-relative mint. A target that sets it *and* points at an external
  `https` issuer would offer the target's SNI to the issuer — an unusual
  combination outside the intended cluster-internal (`http`, no TLS) shape.
  The default (no override) is byte-identical to today.

### `token_url` from the per-target credential (external-issuer extension)

When the external issuer is a **per-deployment** value (each deployment mints
against its own realm), the token endpoint is not a reviewable constant that
belongs in a shipped, public profile. `oauth2_mint` therefore also sources
`token_url` from the resolved **secret bundle**: name `token_url` in
`auth.secret_fields` and the operator stores it in the target's Vault
credential alongside `client_id` / `client_secret`. At dispatch,
`_oauth2_login_path_from_secret` (`connectors/_shared/profile_auth.py`)
resolves the endpoint with precedence **credential `token_url` → profile
`auth.token_url` (#3571) → target-relative `/realms/master/...`**, wired onto
the scheme through the additive `SessionSchemeSpec.login_path_with_secret`
hook (the profile-only `login_path` signature the typed session connectors
call at import is untouched). A credential-sourced value clears the **same**
fail-closed `_validate_token_url` rule the profile field does (`https` for a
public host; plaintext `http` only for a cluster-internal / private issuer),
so a fat-fingered Vault value can never ship the client secret to a public
host over cleartext. A credential bundle with no `token_url` (keycloak) falls
straight through — the override is invisible to every existing `oauth2_mint`
user.

| Profiled connector | Scheme | Secret-bundle fields | `token_url` source | Non-secret profile knobs |
|---|---|---|---|---|
| meho-automation add-on (`mehoauto-rest`) | `oauth2_mint` | `client_id`, `client_secret`, `token_url` | per-target Vault credential (`token_url` field) | `audience: meho-automation` |

This is the meho-automation add-on connector's shape — see
`docs/codebase/connectors-meho-automation.md` for the full registration
recipe and the operator decision that launch / validate / gate all ride
`caution` without a backplane approval park (validate is a read-side dry-run
that still rides `caution` because an ingested POST never sits below the
caution floor).

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
