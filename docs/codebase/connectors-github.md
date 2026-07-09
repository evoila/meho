# Connector: github (gh-rest v3)

## Overview

The `gh-rest` connector is the hand-rolled `HttpConnector` subclass that
dispatches the github.com REST API surface under the registry triple
`(product="gh", version="3", impl_id="gh-rest")` (the registry's
`version` slot must be digit-prefixed for the dispatcher's connector-id
parser; GitHub's own "v3" label lives in docs and in
`FingerprintResult.version` — the upstream-facing string). G3.11-T1 (#1221)
ships the substrate only: the connector class, the GitHub-App credential
loader (JWT mint → installation-token exchange → 50-minute cache), the
PAT fallback path, the fingerprint, and the T11-compliant error
envelopes for the four documented failure modes. Catalog ingest of the
~700 ops in github.com's published OpenAPI spec lands in T3 (#1223); the
first L1 composite (`gh.composite.pr_status_summary`) lands in T4
(#1224); `requires_approval=true` annotations on the four
highest-blast-radius write ops land in T5 (#1225).

Source: `backend/src/meho_backplane/connectors/github/`.

### Layer-1 composites (T4 #1224)

The `composites/` subpackage hosts hand-coded L1 composites that wrap
multiple L2 sub-ops into one governed call. T4 ships the first one,
`gh.composite.pr_status_summary`, mirroring the layout precedent at
`backend/src/meho_backplane/connectors/vmware_rest/composites/`:

- **`composites/_register.py`** — `_COMPOSITES` tuple and the
  `register_github_composite_operations` async registrar. T4 ships
  exactly one row; future T7+ Tasks add `release_health`,
  `board_snapshot`, etc., on the same pattern.
- **`composites/_read.py`** — module-level `async def` handlers. The
  handler `pr_status_summary_composite` declares a `connector` parameter
  and issues its three reads through the resolved `GitHubRestConnector`'s
  own session (`connector._get_json` against `connector.mount_op_path`)
  with no `endpoint_descriptor` lookup — the #2251 direct-session
  substrate (migrated at #2255). The PR read fires sequentially (drives
  the head SHA used in the next step), then the check-runs and reviews
  reads fire in parallel via `asyncio.gather`. Partial-failure tolerance
  is part of the contract — a failure on either secondary read surfaces
  as `null` for that field plus `checks_status="unknown"` /
  `review_status="unknown"`, never bailing mid-flight. The PR read itself
  is non-optional; its failure (an `httpx.HTTPStatusError`, or a
  `RuntimeError` on a malformed payload) propagates to the dispatcher's
  `connector_error` branch.
- **`composites/schemas.py`** — JSON-Schema 2020-12 parameter and
  response contracts. `additionalProperties=False` on params so operator
  typos surface as clear validation errors; response schemas describe
  the seven-key envelope and the two pre-computed status enums.
- **`composites/_preflight.py`** — per-process cache of which composites
  have already validated that every declared L2 sub-op is registered in
  `endpoint_descriptor`. **No longer wired into the composite handler**
  after the #2255 direct-session migration (the handler dispatches zero
  ingested rows, so there is no L2 dependency to pre-flight); the module
  is retained pending the L2-apparatus retirement in #2259. Same
  lazy-resolve design as the vmware-rest precedent (G0.14-T10 #1183).
- **`composites/__init__.py`** — side-effect import that queues
  `register_github_composite_operations` onto the lifespan registrar
  list. The parent `github/__init__.py` imports this subpackage so the
  registrar lands during `_eager_import_connectors`.

**Fresh-deploy behaviour after the #2255 direct-session migration.**
Because the handler now reads through the connector session rather than
dispatching ingested `endpoint_descriptor` rows, the composite works on
a fresh deploy with **no gh catalog ingest** — the pre-#1757
"enabled-but-`composite_l2_missing`" dead-end (#2050) is gone by
construction, not papered over by a listing marker. Consequently
`_register.py` no longer registers a `composite_backing` entry for the
composite and no longer runs the
`_register_and_assert_composite_backings` connector-load guard that
raised `UnbackedEnabledCompositeError`. That gh-only guard class is
retained (dormant) in `_register.py` because the platform-level
registration-time invariant (#2252) supersedes it; the class and the
preflight apparatus are removed wholesale in #2259. Regressions where a
code-shipped op accidentally dispatches an ingested row are caught by
the #2252 platform invariant, not the retired gh-specific check.

Both summarisers — `_summarize_checks` and `_summarize_reviews` — are
intentionally conservative: an unexpected payload shape collapses to
`"unknown"` rather than guessing, and `_summarize_reviews` honours
GitHub's "latest review per reviewer" rule (a single
`CHANGES_REQUESTED` vetoes the PR; a `DISMISSED` entry pops the
reviewer's prior verdict).

The composite is registered with `safety_level="safe"` (the
register-time equivalent of the operator-visible "read" label per the
issue body) and `requires_approval=False`, overriding
`register_composite_operation`'s `dangerous` / `True` defaults the same
way the vmware-rest read composites do.

**Live dispatch acceptance.** After the #2255 direct-session migration
the live dispatch acceptance test at
`backend/tests/integration/test_github_composite_dispatch.py` no longer
depends on catalog ingest or a populated `endpoint_descriptor` table: it
plugs an HTTPX-backed session stub into the handler's `connector`
parameter and hits the real GitHub REST API. It is gated on
`MEHO_GH_INGEST_LIVE=1` + `MEHO_GH_LIVE_PR=<owner/repo#number>` rather
than xfail-strict. (The separate T3 catalog-ingest parser dependency —
the G0.7 OpenAPI parser not inlining `#/components/responses/*` refs —
still applies to the ~700 ingested L2 ops, but is orthogonal to this
composite now that it reads through the session directly.)

## Key types

- **`GitHubRestConnector`** (`connector.py`) — `HttpConnector` subclass
  with class attributes `product="gh"`, `version="3"`,
  `impl_id="gh-rest"`, `priority=1`. Owns the per-target
  installation-token cache, the per-target PAT cache, and the
  `asyncio.Lock` that serialises cold-cache mint calls.
- **`GitHubAppCredentials`** (`session.py`) — frozen dataclass of
  `(app_id, private_key_pem, installation_id)`. The Vault-stored
  material; never enters a log event or an `OperationResult`.
- **`GitHubPATCredentials`** (`session.py`) — frozen single-field
  dataclass holding the bearer PAT.
- **`InstallationToken`** (`session.py`) — frozen dataclass carrying the
  minted token plus the monotonic-clock expiry timestamp the cache uses
  for validity decisions.
- **`GitHubTargetLike`** (`session.py`) — runtime-checkable structural
  Protocol with `name`, `host`, `port`, `secret_ref`, `auth_model`. Any
  concrete `Target` model satisfying these attributes plugs in unchanged.
- **`GitHubCredentialError`** + four subclasses (`session.py`) — the T11
  envelopes. Each carries a stable `code` attribute
  (`github_app_not_installed`, `github_jwt_mint_failed`,
  `github_installation_token_mint_failed`, `github_rate_limited`).

## Control flow

### Registration (import time)

`connectors/github/__init__.py` runs at startup via
`_eager_import_connectors`. It calls:

1. `register_connector("gh", GitHubRestConnector)` — v1 entry. Also
   writes the `("gh", "", "")` wildcard triple into the v2 table.
2. `register_connector_v2(product="gh", version="3",
   impl_id="gh-rest", cls=GitHubRestConnector)` — v2 versioned entry.
3. `register_typed_op_registrar(register_github_typed_operations)` —
   queues an async registrar. T1 ships a no-op body; T3 will fill it
   when the catalog ingests.

The dual registration matches the G0.15-T6 mandatory pattern (also used
by `kubernetes/__init__.py`) so an unfingerprinted `(product="gh",
version=None)` target resolves via the wildcard tie-break while a
fingerprinted target resolves via the versioned entry.

### Auth — boundary (target `auth_model`) and protocol routing

The target row carries `auth_model="shared_service_account"` (or
`None` for legacy rows). The connector boundary accepts that and
rejects every other identity model (`per_user`, `impersonation`)
with `NotImplementedError`, the same shape `VmwareRestConnector`
uses. The **upstream credential protocol** — App-installation vs
PAT — is picked one layer down by inspecting the Vault payload's
field shape; G0.16-T2 (#1304) reconciled this with the connector
code after the v0.8.0 dogfood caught the original `auth_model`-
driven routing rejecting every legal target.

```
auth_headers(target, operator)
  └── reject empty operator.raw_jwt (defence in depth)
  └── reject auth_model ∉ {shared_service_account, None}
        → NotImplementedError naming target + bad value
  └── _auth_token(target, operator)
        └── lock
        └── cache hit (installation or PAT) → return cached
        └── credentials_loader(target, operator)
              └── (default) load_github_credentials_from_vault
                    └── load_vault_secret_data (single KV-v2 read)
                    └── inspect fields:
                         {app_id, private_key, installation_id} present
                            → GitHubAppCredentials
                         {token} present (and no App fields)
                            → GitHubPATCredentials
                         neither
                            → GitHubAmbiguousVaultPayloadError
        └── isinstance(creds, GitHubAppCredentials):
              → _mint_and_cache_installation_token
                  → mint_github_app_jwt(app_id, private_key_pem)
                      → 540-second RS256 JWT (10-min cap minus skew)
                  → exchange_jwt_for_installation_token(jwt, install_id)
                      → POST /app/installations/{id}/access_tokens
                      → InstallationToken with monotonic expiry
                  → cache, return token
        └── isinstance(creds, GitHubPATCredentials):
              → cache, return token (no JWT, no mint, no exchange)
  └── return {"Authorization": "Bearer <token>", ...}
```

The App-path 50-minute cache window means a typical operator
session of ~50 dispatch calls pays the JWT/installation-token
round-trip exactly once per target. The PAT-path cache lives for
the connector instance lifetime — operator-managed expiry, no
MEHO-side refresh dance. Re-reads only on cold cache (restart /
`aclose`, or the App-path window expiring).

### Fingerprint

`fingerprint(target)` synthesises a system operator (per
`synthesise_system_operator` — non-empty placeholder JWT so the cache
fast-path admits the probe, but the live Vault loader still fails
closed) and inspects `target.host`:

- `host="api.github.com"` (or any bare host without a `/`) →
  `GET /user/installations`. Returns total count + first installation's
  `installation_id`, `installation_account`, `app_slug`, `target_type`,
  `permissions`.
- `host="api.github.com/repos/<owner>/<repo>"` (or `api.github.com/<owner>/<repo>`)
  → `GET /repos/{owner}/{repo}`. Returns repo identity (`full_name`,
  `id`, `private`, `default_branch`) + owner identity (`login`, `type`).

Transport / credential failures surface as `reachable=False` with both
the exception class+message in `extras["error"]` and the structured T11
`extras["error_code"]` (when a `GitHubCredentialError` fired) so the
operator can branch programmatically.

### Execute

`execute()` is a stub in T1: every `op_id` returns the dispatcher's
canonical `unknown_op` envelope (via `result_unknown_op` in
`operations/_errors.py`). T3 ships the catalog rows + their dispatcher
wiring.

## Dependencies

- **`HttpConnector`** (`connectors/adapters/http.py`) — pooled
  `httpx.AsyncClient` per target, retry policy for idempotent verbs,
  `_request_json` / `_get_json` / `_post_json` helpers.
- **`load_basic_credentials`** (`connectors/_shared/vault_creds.py`) —
  shared operator-context Vault KV-v2 read with the no-secret-in-logs
  discipline and the two-phase error contract. Used by the
  individual App / PAT loaders kept for backwards-compatible test
  injection.
- **`load_vault_secret_data`** (`connectors/_shared/vault_creds.py`) —
  the same Vault read primitive, but returning the raw KV-v2 data
  dict without named-field extraction. Used by
  `load_github_credentials_from_vault` for the single-read +
  payload-shape inspection pattern G0.16-T2 (#1304) introduced.
- **`synthesise_system_operator`** (`connectors/_shared/system_operator.py`)
  — non-empty placeholder JWT for the fingerprint / probe paths that
  pre-date a real operator.
- **PyJWT 2.x with the `[crypto]` extra** (`pyproject.toml`) —
  `jwt.encode(payload, key, algorithm="RS256")`. RS256 signing requires
  the `cryptography` backend, which the extra pulls in (already a dev
  dep for the test fakes; the runtime extra makes the dependency
  explicit at install time).
- **Registry v1 + v2** (`connectors/registry.py`) — `register_connector`
  for the wildcard side, `register_connector_v2` for the versioned
  side. The G0.15-T6 mandatory pattern.

## Known issues

- **No GHES support** — github.com only for v0.x. A future GHES target
  would need a per-target `api_base_url` override (the `_BASE_URL` class
  attribute drives the httpx pool's base URL today); the connector's
  shape doesn't preclude it, but the work is deferred per the
  Initiative #1220 scope.
- **`/user/installations` lists from the App identity, not the
  operator's** — the v0.x design choice: the App is the audit-attributable
  identity (per G11.2-T6c's keycloak-CIMD onramp). An operator viewing
  installation metadata sees what the App can reach, not what their
  personal GitHub account can reach. Operators wanting personal-account
  visibility use `gh` CLI on their workstation.
- **Installation token caches per-`target.name`** — same scoping
  vmware-rest uses. If two tenants legitimately hold a target named
  `"github-main"`, the second tenant's connector instance reads from a
  separate `GitHubRestConnector` instance (the registry produces one per
  process; the cache is instance-scoped — same shape as vmware-rest's
  `_session_tokens`). Cross-tenant token leakage is not possible under
  the current chassis. Re-key on `target.secret_ref` if a future change
  introduces shared connector instances across tenants.
- **PAT cache is connector-instance-scoped + does not respect upstream
  expiry** — MEHO never re-reads a PAT from Vault during a process
  lifetime once it's cached. Operators rotating a PAT must redeploy
  (or evict via `aclose`). The G3.11 follow-up Tasks can introduce a
  Vault-stored expiry hint if operators report this as a friction
  point; not pre-built.

## References

- **Task:** [#1221](https://github.com/evoila/meho/issues/1221) (G3.11-T1)
- **Parent Initiative:** [#1220](https://github.com/evoila/meho/issues/1220)
  (G3.11 — gh-rest typed connector)
- **Parent Goal:** [#214](https://github.com/evoila/meho/issues/214)
  (Connector parity with ClaudeVCF wrapper set)
- **Error message convention:**
  [docs/codebase/error-message-shape.md](error-message-shape.md) (T11
  #1141)
- **G0.15-T6 wildcard registration pattern:**
  `connectors/kubernetes/__init__.py` (the mandatory dual-registration
  precedent).
- **GitHub Apps documentation:**
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
- **PyJWT documentation:** https://pyjwt.readthedocs.io/ (RS256
  encoding with private-key signing).
- **GitHub rate-limit policy:**
  https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- **Sibling Tasks (Initiative #1220):**
  - T2 [#1222](https://github.com/evoila/meho/issues/1222) — App
    credential operator runbook.
  - T3 [#1223](https://github.com/evoila/meho/issues/1223) — `gh/v3`
    catalog entry + Layer-2 ingest acceptance.
  - T4 [#1224](https://github.com/evoila/meho/issues/1224) — first L1
    composite `gh.composite.pr_status_summary`.
  - T5 [#1225](https://github.com/evoila/meho/issues/1225) —
    `requires_approval=true` on 4 write ops.
  - T6 [#1226](https://github.com/evoila/meho/issues/1226) — connector
    operator runbook
    ([`docs/cross-repo/github-connector.md`](../cross-repo/github-connector.md)).
