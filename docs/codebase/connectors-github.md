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

### Auth — GitHub App path (`auth_model="github-app"`)

```
auth_headers(target, operator)
  └── reject empty operator.raw_jwt (defence in depth)
  └── _installation_token(target, operator)
        └── lock
        └── cache hit (expires_at_monotonic > now) → return cached
        └── credentials_loader(target, operator)
              └── (default) load_basic_credentials with
                  fields=("app_id", "private_key", "installation_id")
                  → operator-context Vault KV-v2 read
        └── mint_github_app_jwt(app_id, private_key_pem)
              → 540-second RS256 JWT (10-min cap minus skew margin)
        └── exchange_jwt_for_installation_token(jwt, installation_id)
              → POST /app/installations/{id}/access_tokens
              → 201 → InstallationToken with monotonic expiry
        └── cache, return token
  └── return {"Authorization": "Bearer <token>", ...}
```

The 50-minute cache window means a typical operator session of ~50
dispatch calls pays the JWT/installation-token round-trip exactly once
per target. The mint endpoint is hit again only on the next cold cache
(after restart / `aclose`, or after the 50-minute window expires).

### Auth — PAT fallback (`auth_model="github-pat"`)

```
auth_headers(target, operator)
  └── reject empty operator.raw_jwt
  └── _pat_token(target, operator)
        └── lock + cache fast-path (PATs cache for connector lifetime)
        └── credentials_loader(target, operator)
              └── (default) load_basic_credentials with fields=("token",)
        └── cache the token, return
  └── return {"Authorization": "Bearer <token>", ...}
```

No JWT, no mint, no exchange. PAT TTL is whatever GitHub's PAT-expiry
policy enforces (operator-set in the GitHub UI); MEHO does not re-read
until restart or `aclose`.

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
  discipline and the two-phase error contract.
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
