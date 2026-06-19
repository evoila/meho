# `ui/` — Operator-console chassis

Initiative [#337](https://github.com/evoila/meho/issues/337) (G10.0
Frontend chassis) introduces a server-rendered web UI inside the
backplane FastAPI process at `/ui/*`. This doc covers the **chassis**
that Task [#863](https://github.com/evoila/meho/issues/863) (G10.0-T2)
landed — module layout, template-rendering shape, Tailwind 4 build
pipeline, vendored JS assets — plus the **session storage** that Task
[#864](https://github.com/evoila/meho/issues/864) (G10.0-T3) layered on
top, the **BFF auth flow** that Task
[#865](https://github.com/evoila/meho/issues/865) (G10.0-T4) added,
and the **FastAPI integration + dashboard + CSRF + surface stubs**
that Task [#866](https://github.com/evoila/meho/issues/866) (G10.0-T5)
wires into `main.py`. All four chassis Tasks have landed; the surface
Initiatives G10.1–G10.5 fill in the per-surface views the stubs
placeholder.

## Overview

Goal G10 ([#336](https://github.com/evoila/meho/issues/336)) ships an
Operator Console as a third operator surface alongside the
`meho` CLI and the MCP server. The console is server-rendered with
HTMX + Jinja2 + Tailwind 4 + DaisyUI 5 + Alpine.js (+ Cytoscape.js
island for topology) — a stack picked to add zero new toolchains, no
`node_modules` in CI, no separate deploy artifact. The backplane
FastAPI process serves `/ui/*` from the same uvicorn workers that
already serve `/api/v1/*` and `/mcp`.

Locked decisions:

- [#9 frontend stack](../planning/v0.2-decisions.md) — HTMX 2 + Jinja2
  + Tailwind 4 + DaisyUI 5 + Alpine.js + Cytoscape.js.
- [#10 deploy shape](../planning/v0.2-decisions.md) — server-rendered
  from the existing FastAPI backplane at `/ui/*`; tiny static
  CSS+JS bundle at `/ui/static/*`. No second image, no separate
  ingress, no parallel CI job.
- [#11 auth](../planning/v0.2-decisions.md) — Backend-for-Frontend
  (BFF) with `HttpOnly; Secure; SameSite=Strict` session cookie;
  tokens stay server-side.

## Key types

| Module | Purpose |
| ------ | ------- |
| `meho_backplane.ui.paths` | Resolves `templates/`, `static/src/`, `static/dist/` directories at runtime. Source-tree dev and image deploy both work via `Path(__file__).resolve().parent`. |
| `meho_backplane.ui.templating` | Jinja2 `Environment` factory with `FileSystemLoader`, `select_autoescape`, `StrictUndefined`, and the `app_version` global pre-bound from `meho_backplane.version.deployed_version_label()` -- the deployed-build label (`v<CHART_VERSION>`, else 12-char `GIT_SHA` truncation, else `unknown`) read from the same env vars `GET /version` reports, bound once at env construction because the values are process-immutable (#1698; the global used to bind the static package `__version__`, so every deploy's footer said `0.1.0-dev`). Routes must not pass their own `app_version` context key -- render context shadows env globals. `get_templates()` registers `_ui_session_context_processor` so every render sees a `session_tenant` dict ({id, slug, name}, or None on unauthenticated auth surfaces) -- G0.15-T9 #1217 -- and the live `ready` verdict for the footer pill, read from `request.state.ui_ready` (stashed by the session middleware from `health.ui_readiness_verdict`, the stale-while-revalidate hot-path accessor that never sweeps probes on the request path); G10.7-T1 #1776. Context processors run *after* the route context and `update` over it, so the injected `ready` wins over any per-route literal. |
| `meho_backplane.ui.routes` | Aggregate `APIRouter`. `build_router()` aggregates the dashboard (`GET /ui/`), the real broadcast routes (`GET /ui/broadcast` + `/ui/broadcast/stream`, G10.1-T1 #867), the real topology routes (`GET /ui/topology` + node detail, G10.5-T1 #880), the real memory + connectors routes (G10.4-T1 #877 / G10.3-T1 #873), the real KB routes (`GET /ui/kb` + search + detail + preview, G10.2-T1 #870), the real docs-corpus routes (`GET /ui/corpus` + `POST /ui/corpus/search` -- collection picker, default-if-one, + ask-the-corpus over the reused `search_docs` / `doc_collections` backends, G10.7-T1 #1777 -- plus the admin Collections lifecycle table `GET /ui/corpus/collections` + register modal `GET`/`POST /ui/corpus/collections/register`, rendered as a second tab on `/ui/corpus` rather than a 9th sidebar entry; the table lists the tenant's FULL registry NOT the entitlement-filtered search catalogue -- admins manage rows they may not personally hold `meho-docs:<key>` for -- and the register submit calls the in-process `create_doc_collection` service, catching `DocCollectionConflictError`->409 / `DocCollectionBackendTypeError`->422 explicitly since neither subclasses `ValueError`; the literal `/register` route registers ahead of the `/{collection_key}` param route, G10.10-T1 #1882 -- plus the per-collection **detail** + lifecycle surface `GET /ui/corpus/collections/{collection_key}` + `POST .../{collection_key}/probe` + `GET`/`POST .../{collection_key}/disable` + `POST .../{collection_key}/enable`: the detail page renders the full `DocCollection` read shape via `project_doc_collection` (the server-side-only `backend{type, ref}` rendered ONLY for a `tenant_admin` via `resolve_role_probe`, never the `ref` value for a plain operator), the re-probe calls the in-process `probe_collection` inside the route's `get_session` transaction and swaps the readiness card `#collection-readiness-card` on success / a `CorpusUnavailable`->503 alert leaving the row status unchanged (success-only write-back) / a `DocCollectionStateError`->409 alert, the slow-probe button carries `hx-disabled-elt`+`hx-indicator`, and enable/disable call the in-process `set_collection_enabled` (idempotent no-op, `DocCollectionStateError`->`409 invalid_collection_transition` legible alert) with disable fronted by an availability-destructive confirm modal and enable a plain button; the action sub-routes' extra literal segment + the literal `/register` route (included before this detail router) keep the bare `{collection_key}` GET from binding a literal as a key, G10.10-T2 #1883), the real runbooks routes (G10.6-T1 #1382), and the real approvals routes (`GET /ui/approvals/badge` + `GET /ui/approvals` panel + `GET /ui/approvals/{id}` detail modal + `POST .../approve` + `POST .../reject` -- the bell/badge + approve/deny modal over a session BFF that calls the `approval_queue` service in-process, G10.7-T3 #1778), and the real scheduler routes (`GET /ui/scheduler` list + `GET /ui/scheduler/{id}` detail + `GET`/`POST /ui/scheduler/create` create modal + live `POST /ui/scheduler/validate-cron` cron preview + `GET`/`POST /ui/scheduler/{id}/cancel` terminal-confirm cancel -- the trigger control plane over a session BFF that calls `SchedulerAdminService` in-process; reads `operator`, writes `tenant_admin`, cancel fronted by a strong native-`<dialog>` confirm since it is terminal, G10.8-T6 #1826), the real agents routes (`/ui/agents` definitions list + detail + tenant_admin CRUD, G10.8-T1 #1825), the real agent-runs routes (`GET /ui/agents/runs` cross-agent run list + `GET /ui/agents/runs/{handle}` per-run detail/poll, operator-read, G10.8-T3 #1830), the real conventions routes (`GET /ui/conventions` kind-tabbed (operational / workflow / reference) summary table + an **always-on preamble token-budget banner** that surfaces the otherwise-silent overflow drop -- estimated/`DEFAULT_MAX_PREAMBLE_TOKENS` token math + every `dropped_slug` rendered in red with "agents never see this rule" copy, reflecting the full operational set regardless of the active kind tab -- + `GET /ui/conventions/{slug}` full-body detail rendered through the sanitised `render_markdown`; operator-read, calling the shared in-process `ConventionsService` (`list_conventions` / `get_convention`) so the budget arithmetic + `priority DESC, created_at ASC` ordering match the REST surface and T4's MCP preamble packer exactly; the literal `/ui/conventions` list route registers ahead of `/ui/conventions/{slug}` so T2's static-prefix write routes (`/create` + `/preview`) drop in without binding a literal as a slug, G10.12-T1 #1895), and the real operations launcher routes (`GET /ui/operations` picker + group browse + `GET /ui/operations/search` debounced hybrid search + `GET /ui/operations/descriptor/{id}` read-only detail drawer + `POST /ui/operations/preview` -- the in-drawer read-only request **preview** that renders the literal would-be HTTP request (`method` / `resolved_path` / `query` / `redacted_body`) for an HTTP-ingested op WITHOUT dispatching it, CSRF double-submit gated, calling `preview_operation` in-process; the in-envelope `status="error"`/`"unavailable"` faults render inline at HTTP 200 while the meta-tool's missing-target-name `ValueError` maps to an inline 400 -- `GET /ui/operations/run/{id}` + `POST /ui/operations/call` -- the confirm-gated **run** action (G10.9-T3 #1881): the run-confirm modal renders an unmissable `safety_level` / `requires_approval` banner over the target / params / optional `work_ref` inputs, and the CSRF-bound confirm POST calls `call_operation` in-process and renders the `OperationResult` envelope inline (`ok` shows the result or, on out-of-band spill, the `ResultHandle` metadata rather than a blob; `error`/`denied` shows `extras.error_code`; **`awaiting_approval`** shows the parked-request banner with `extras["approval_request_id"]` and a deep-link to `/ui/approvals/{id}` so a governed write is never a silent success). Run is operator-tier; the policy gate (not RBAC) escalates a `requires_approval` op to `awaiting_approval` -- a session BFF over the `list_operation_groups` / `search_operations` / `describe_descriptor` / `preview_operation` / `call_operation` meta-tools called in-process; operator-read for browse/preview/run (no `tenant_admin` step), with the per-op `llm_instructions` prompt rendered only for `tenant_admin`, G10.9-T1 #1879 / T2 #1880 / T3 #1881), and the real connector-**registry** routes (`GET /ui/connectors/registry` role-scoped list -- DISTINCT from the `/ui/connectors` **targets** list: it lists the ingested/typed/composite connectors via the in-process `list_ingested_connectors` (operator-scoped visibility: built-ins + the caller's tenant, never cross-tenant), with a `?status=staged|enabled|disabled|all` enum filter (the `all` sentinel, never `value=""`, since the `ConnectorStatusFilter` enum 422s out-of-range) + a `?product=` exact-match dropdown computed in the handler from the returned rows; surfaced as its own `connectors-registry` sidebar entry ("Connector Registry") -- plus the confirm-gated per-row actions `POST /ui/connectors/registry/{connector_id}/enable|disable|enable-reads` + `DELETE /ui/connectors/registry/{connector_id}` which call the shipped REST `enable_endpoint`/`disable_endpoint`/`enable_reads_endpoint`/`delete_endpoint` in-process (the `forms_router` pattern -- UI + REST share one validation + state-machine + audit path); read = `operator` via `resolve_role_probe` (soft-hide of the write affordances), writes = `tenant_admin` via `resolve_operator_or_403`; every loosening verb (enable / enable-reads / disable) is fronted by a confirm modal naming the projected blast radius and delete is type-to-confirm surfacing the `enabled_operations_deleted` advisory; a `409 connector_scope_ambiguous` (the `candidates[]` envelope) or `InvalidStateTransitionError` `409` from the in-process handler renders an inline error panel against the row rather than a 5xx, and a successful verb re-renders the affected row via `hx-swap-oob` (a delete returns an empty OOB stub that removes the row); `{connector_id}` is the slash-free `<impl_id>-<version>` string so a plain `{connector_id}` param suffices (no `:path` converter), G10.13-T1 #1885). All surfaces now ship real routers, so the stubs aggregate is empty; real routers are still included **before** the stubs so their concrete paths win the first-match-wins lookup (the literal `/ui/approvals/badge` is registered ahead of the `/ui/approvals/{request_id}` slug for the same reason; the scheduler router registers its literal `/ui/scheduler/create` + `/ui/scheduler/validate-cron` ahead of the `/ui/scheduler/{trigger_id}` detail slug; the agent-runs router is included ahead of the agents router so `/ui/agents/runs` is not captured by `/ui/agents/{name}`; the connector-registry list + per-row action routers are included ahead of the connectors detail router so the literal `/ui/connectors/registry` is not captured as `/ui/connectors/{name}`; the operations launcher's `{param}` routes sit under the distinct `/ui/operations/descriptor/` + `/ui/operations/run/` prefixes so the literal `/ui/operations/search` never binds as a descriptor id, and the literal `/ui/operations/preview` + `/ui/operations/call` are `POST`s so they cannot collide with the GET slug routes). |
| `meho_backplane.ui.csrf` | T5 (#866) double-submit-cookie CSRF middleware on state-changing `/ui/*` requests (POST/PATCH/PUT/DELETE). Signed-double-submit per OWASP -- the token is `hmac_sha256(session_secret, session_id || random) + "." + random`; the cookie is JS-readable (`meho_csrf`) so HTMX can echo it in `X-CSRF-Token`. Mismatch / missing token / forged signature -> 403. Read-only methods + out-of-prefix paths pass through. |
| `meho_backplane.ui.auth` | BFF auth subpackage. T3 (#864) landed `session_store` (encrypted token custody + RFC 9700 refresh-token rotation); T4 (#865) lands `/ui/auth/{login,callback,logout}` + session middleware; G0.25 (#1694) wires the rotation primitive into the request path (`refresh` + `errors` modules). |
| `meho_backplane.ui.auth.session_store` | Fernet-encrypted server-side session storage. `create_session`, `load_session`, `load_session_for_update` (side-effect-free `SELECT ... FOR UPDATE` variant for the refresh path, G0.25 #1694), `revoke_session`, `rotate_refresh` against the `web_session` Postgres table. `rotate_refresh` optionally extends `expires_at` by a refreshed token's lifetime (`new_lifetime=`), monotonic and clamped to `created_at + ui_session_absolute_lifetime_seconds`, and accepts a caller-supplied clock (`now=`, default wall clock) so a caller that pre-checks `expires_at` (the inline refresh path) shares one reading with the internal replay gate -- two independent clock reads would leave a microsecond gap where the gate's "expired" branch self-deadlocks on the caller's own row lock. Replay of a used refresh token revokes the session and writes a `ui.session.refresh_replay` audit row on a dedicated transaction so the security signal survives caller rollback. |
| `meho_backplane.ui.auth.flow` | OAuth 2.1 + PKCE client primitives layered on authlib's `AsyncOAuth2Client`. `build_authorization_request` mints the Keycloak redirect URL (S256 PKCE + RFC 8707 `resource` parameter) and registers the per-flow verifier in a server-side `PKCEVerifierStore`. `exchange_code_for_tokens` pops the verifier and exchanges code+verifier at the token endpoint. `resolve_oidc_endpoints` caches the discovery doc on the same TTL the JWKS cache uses. |
| `meho_backplane.ui.auth.routes` | FastAPI `APIRouter` for `/ui/auth/{login,callback,logout}`. `build_router()` returns the router for T5 to mount. Callback verifies the access token through the chassis JWT chain (`verify_jwt_for_audience`) so the BFF inherits issuer / audience / sub / tenant_id / tenant_role checks. Sets `meho_session` cookie with `HttpOnly; Secure; SameSite=Strict; Path=/`. Logout revokes the session, clears the cookie, and 302s to Keycloak's `end_session_endpoint` (best-effort -- a missing endpoint falls back to a local `/ui/auth/login` redirect). |
| `meho_backplane.ui.auth.middleware` | Pure-ASGI `UISessionMiddleware` for `/ui/*`. Loads operator identity from the session cookie on every request; 302s to login on missing/expired session. Bypasses `/ui/static/*` (chassis assets) and `/ui/auth/*` (the BFF surfaces themselves). Per-request `UISessionContext` (frozen dataclass: `session_id`, `operator_sub`, `tenant_id`, plus `tenant_slug` + `tenant_name` populated from a same-transaction `tenant` PK lookup added by G0.15-T9 #1217) lands on `request.state.ui_session`; route handlers read it via `Depends(require_ui_session)`. `require_ui_admin` (the write-route RBAC gate) loads + verifies the stored access token through `meho_backplane.ui.auth.refresh`, so expired tokens silently refresh instead of 401ing (G0.25 #1694). |
| `meho_backplane.ui.auth.refresh` | Inline token-refresh lifecycle (G0.25 #1694). `load_fresh_session` (proactive: refresh when the row is within 60 s of `expires_at`), `verify_access_token_with_refresh` (reactive: refresh once on the JWT chain's `token_expired`, re-verify), both funnelling into `refresh_session_tokens` -- the `SELECT ... FOR UPDATE`-serialised chokepoint that POSTs the RFC 6749 § 6 refresh grant (single attempt, 5 s timeout) and rotates the row via `rotate_refresh` (RFC 9700 § 4.14). Concurrent refreshes: first wins; the loser observes the rotated pair under the lock and skips its network call. Failures log `ui_auth_token_refresh_failed` (reason: `invalid_grant` / `network_error` / `timeout` / `malformed_response`) and raise `401 session_expired`; successes log `ui_auth_token_refresh_succeeded` (session_id, old/new expires_at, time_cost_ms). No token material in logs. The refresh performs zero `Set-Cookie` operations -- `meho_session` and `meho_csrf` stay byte-identical, so in-flight pages and their CSRF tokens survive a rotation (no #1706-class cookie desync). The seam serves **token-presenting** dependencies (`require_ui_admin` today); the dashboard-feed fix (#1696) needed no new caller here — it re-pointed the tray at the existing session-gated `/ui/broadcast/stream` bridge, which reads Valkey directly under `require_ui_session` and never presents the access token. |
| `meho_backplane.ui.auth.errors` | App-level `HTTPException` handler registered in `main.py` for the whole app (G0.25 #1694). Intercepts exactly `401 session_expired` on `/ui/*`: HTML requests (`Accept: text/html`) get `302 /ui/auth/login?return_to=<path>` + `meho_session` cookie clear; non-HTML callers keep the JSON body (cookie cleared too). Every other HTTPException delegates byte-for-byte to FastAPI's stock `http_exception_handler`, so `/api/*` 401 codes are untouched. |

The Jinja2 `Environment` is a module-level singleton (constructed on
first `get_jinja_env()` call); the template cache it holds is keyed
by filename so a `uvicorn --reload` cycle picks up template edits
without an explicit cache flush.

## Build pipeline

Two artifacts ship in the backend image:

1. **Vendored JS** — `htmx.min.js`, `alpine.min.js`, `cytoscape.min.js`,
   plus the DaisyUI 5 `daisyui.js` plugin loaded by the Tailwind CLI.
   Committed to git under
   `backend/src/meho_backplane/ui/static/src/vendor/` with pinned
   SHA256s recorded in
   [`VENDOR.md`](../../backend/src/meho_backplane/ui/static/src/vendor/VENDOR.md).
   No build step; the files ship verbatim into the image as package
   data.
2. **Compiled CSS** — `static/dist/tailwind.css`, produced at image
   build time by the **Tailwind 4 standalone CLI binary**. The
   Dockerfile downloads the CLI by URL with a SHA256 check (mirroring
   the `kubeconform` install pattern in
   [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)),
   selects the right arch by `$TARGETPLATFORM`, then runs:

   ```bash
   tailwindcss \
     -i src/meho_backplane/ui/static/src/styles.css \
     -o src/meho_backplane/ui/static/dist/tailwind.css \
     --minify
   ```

   Tailwind 4's automatic content detection scans `templates/**` for
   utility usage; no `tailwind.config.js` exists. DaisyUI is loaded
   via `@plugin "./vendor/daisyui.js"` in `styles.css` itself.

   `static/dist/` is `.gitignore`'d — only built artifacts live
   there.

### Local dev workflow

The same standalone CLI runs locally in watch mode alongside
`uvicorn --reload`:

```bash
# Terminal 1 -- watch + recompile on template edits
tailwindcss \
  -i backend/src/meho_backplane/ui/static/src/styles.css \
  -o backend/src/meho_backplane/ui/static/dist/tailwind.css \
  --watch

# Terminal 2 -- backplane in reload mode
cd backend && uv run uvicorn meho_backplane.main:app --reload
```

The watcher writes to the same `static/dist/` path the image build
populates; no `npm` / `pnpm` / `node` enters this loop. The Tailwind
download (~38 MB binary) is the one-time cost.

## Session storage (Task #864)

The BFF (Backend-for-Frontend) shape locked by
[decision #11](../planning/v0.2-decisions.md) keeps tokens
server-side. Task #864 ships the storage substrate the
upcoming T4 login flow writes to:

- **`web_session` Postgres table** — migration
  [`0013_create_web_session.py`](../../backend/alembic/versions/0013_create_web_session.py).
  Columns: `id` (UUID PK, the cookie value the browser holds),
  `operator_sub` (Keycloak `sub` claim), `tenant_id`,
  `created_at`, `expires_at`, `access_token` (bytea, Fernet
  ciphertext), `refresh_token` (bytea, Fernet ciphertext),
  `last_seen_at`, `revoked_at` (nullable; non-null = soft-deleted).
  Indexes on `operator_sub` and `expires_at` (future
  bulk-revoke / idle-sweep surfaces).
- **ORM model** — [`WebSession` in
  `db/models.py`](../../backend/src/meho_backplane/db/models.py).
  Mirrors the column shape; no helper methods (write-once /
  read-mostly).
- **Encryption** — every token write goes through one
  [`cryptography.fernet.Fernet`](https://cryptography.io/en/latest/fernet/)
  instance constructed from `UI_SESSION_ENCRYPTION_KEY` (URL-safe
  base64-encoded 32-byte key). Production deploys render this from
  Vault into the pod environment by the same chain that lands
  `DATABASE_URL` / Keycloak client secrets. Empty value =
  fail-fast: any session-store call raises
  `EncryptionKeyMissingError`. The key is process-wide
  (one-key-per-deploy in v0.2); rotation is a future Initiative
  (every active session becomes un-decryptable on rotation).
- **Refresh-token rotation** — `rotate_refresh` implements the
  [RFC 9700 § 4.14](https://datatracker.ietf.org/doc/rfc9700/)
  one-time-use contract. The happy path swaps both columns to
  fresh ciphertext on the caller's session; the replay path
  (mismatched / already-revoked / expired / missing session)
  revokes the row and writes a `ui.session.refresh_replay`
  `audit_log` row on a **dedicated session** so the security
  signal commits independently of the caller's transaction. The
  raised `RefreshReplayError` carries the revoked session id +
  the freshly-written audit row id so the caller (T4 refresh
  handler) can map both into the 401 + cookie-clear response.

T4 (#865) builds the login / callback / logout flow + the BFF
middleware on top of this substrate; T3 ships storage only.

## Login flow (Task #865)

The BFF login flow is OAuth 2.1 Authorization Code + PKCE against
the `meho-web` confidential Keycloak client (see
[`docs/cross-repo/keycloak-web-client.md`](../cross-repo/keycloak-web-client.md)).
Tokens stay server-side; the browser holds only the opaque
`meho_session` cookie.

Round-trip shape:

1. **Operator hits a `/ui/*` page unauthenticated.** The
   `UISessionMiddleware` finds no usable session and 302s to
   `/ui/auth/login?return_to=<original-path>`.
2. **`/ui/auth/login`.** `build_authorization_request` generates a
   per-flow PKCE `code_verifier`, asks authlib to build the
   authorization URL (carries `code_challenge`,
   `code_challenge_method=S256`, and the RFC 8707 `resource` parameter
   set to `<backplane_url>/api`), and registers the verifier +
   `return_to` in the `PKCEVerifierStore` keyed on `state`. The
   verifier never leaves the server.
3. **Keycloak authenticates the operator.** Browser arrives at
   `/ui/auth/callback?code=...&state=...`.
4. **`/ui/auth/callback`.** `exchange_code_for_tokens` pops the
   verifier from the store (single-use), POSTs
   `code+code_verifier+client_secret` to Keycloak's token endpoint,
   and returns the access + refresh tokens. The callback then
   validates the access token through the chassis JWT chain
   (`verify_jwt_for_audience`) so the BFF inherits issuer / audience
   / sub / tenant_id / tenant_role defences; on success it calls
   `create_session` to write the encrypted row and sets the
   `meho_session` cookie.
5. **302 to the original `return_to`.** Operator lands on the page
   they were originally bounced from.

Per-flow state custody:

| What | Where | Lifetime |
| --- | --- | --- |
| `code_verifier` | Server-side `PKCEVerifierStore` keyed on `state` | One authorization round-trip (≤10 min) |
| `state` (CSRF) | On the IdP's authorization URL + echoed in callback | Same one round-trip |
| `meho_session` cookie | Browser, `HttpOnly` + `Secure` + `SameSite=Strict` | Session lifetime (seeded at login from access-token expiry minus 60s margin; extended by the sliding window #869 and by every successful token refresh #1694, both capped at `created_at + ui_session_absolute_lifetime_seconds`) |
| Access + refresh tokens | `web_session` row, Fernet-encrypted | Rotated in place on every silent refresh (#1694); the row -- and therefore the cookie value -- survives rotation unchanged |

The `code_verifier` deliberately does NOT live in a cookie -- a
verifier alongside the code on the redirect URI would defeat the
property PKCE protects (an attacker capturing one captures both).
Server-side custody is the whole point.

Mid-session token refresh (G0.25 #1694): Keycloak's access token
(~5 min TTL) routinely dies long before the session row does --
the sliding extension keeps an active row alive for hours. When a
token-consuming dependency (`require_ui_admin`) finds the row near
expiry, or the JWT chain reports `token_expired`, the
`meho_backplane.ui.auth.refresh` module silently POSTs the RFC 6749
§ 6 refresh grant and rotates the row via `rotate_refresh`
(one-time-use, RFC 9700 § 4.14), serialised per session with
`SELECT ... FOR UPDATE`. The handler then runs against the fresh
token; the browser sees nothing. A failed refresh raises `401
session_expired`, which the `meho_backplane.ui.auth.errors` handler
maps to a `302 /ui/auth/login?return_to=<path>` + cookie clear for
HTML requests (JSON callers keep the structured body) -- never raw
JSON at a browser. The refresh path performs zero `Set-Cookie`
operations: `meho_session` (the row id) and `meho_csrf` (HMAC-keyed
on that id) are byte-identical before and after a rotation, so
already-rendered pages and their forms keep working -- the
cookie-rotation desync class #1706 diagnosed cannot occur here.

Logout: revoke the row, clear the cookie, 302 to Keycloak's
`end_session_endpoint` with `client_id` + `post_logout_redirect_uri`
pointing back to `/ui/auth/login`. The IdP-side hop is best-effort
-- the local session is already revoked when the redirect fires.
A revoked row never reaches the refresh path -- the middleware
bounces the next request to login before any token validation runs.

## Vendored asset versions

Current as of Task #863 (refresh procedure documented in
`VENDOR.md`):

| Library | Version |
| ------- | ------- |
| Tailwind CSS | 4.3.0 |
| DaisyUI | 5.5.20 |
| HTMX | 2.0.9 |
| htmx-ext-sse | 2.2.4 |
| Alpine.js | 3.15.12 |
| Cytoscape.js | 3.33.4 |

`htmx-ext-sse` (`sse.min.js`) is the SSE extension HTMX 2 split out of
core (HTMX 1 bundled it). It is loaded in `_head_assets.html` right
after `htmx.min.js` (script order matters — the extension calls
`htmx.defineExtension` at load) and is required by both the dashboard
recent-activity snippet (G10.0) and the broadcast live feed (G10.1):
without it every `hx-ext="sse"` wrapper is inert.

**Alpine component-registration ordering (#1692).** The vendored
Alpine CDN bundle auto-starts via `queueMicrotask(() =>
Alpine.start())` at the end of its own script task; the microtask
queue drains before the next deferred script executes, so any surface
script that registers components on `alpine:init` must appear BEFORE
`alpine.min.js` in document order (deferred scripts execute in
document order — the [Alpine extension
contract](https://alpinejs.dev/advanced/extending)). `base.html`
exposes a head-level `{% block component_scripts %}` rendered before
the `_head_assets.html` include for exactly this: the broadcast feed
and connectors detail pages inject their controller scripts there
(the standalone `wall.html` places the tag directly in its head). A
controller loaded from the body-end `{% block scripts %}` instead
registers its `alpine:init` listener after the event has fired —
`Alpine.data()` never runs and every `x-data` element referencing the
component renders dead (the v0.14.0 cycle-10 broadcast outage).
Plain-JS page scripts and heavy vendor bundles (Cytoscape, CodeMirror)
stay in the body-end block so they don't delay Alpine boot.

Every bump lands on its own `chore(ui): bump <library> to <version>`
PR so the supply-chain trail records each move (same discipline the
backplane Python base image already follows).

## Control flow (chassis)

`base.html` is the only template Task #863 ships. Its structure:

```html
<html data-theme="meho-dark">
  <head>
    {% block component_scripts %}{% endblock %}   <!-- Alpine.data() registrations; MUST precede alpine.min.js (#1692) -->
    <!-- _head_assets.html include (shared with wall.html): -->
    <script src="/ui/static/src/app/theme.js">    <!-- sync: pre-paint theme -->
    <link href="/ui/static/dist/tailwind.css">
    <script src="/ui/static/src/vendor/htmx.min.js" defer>
    <script src="/ui/static/src/vendor/sse.min.js" defer>
    <script src="/ui/static/src/vendor/alpine.min.js" defer>
  </head>
  <body>
    <div class="drawer lg:drawer-open" x-data="{...}">
      <input id="meho-drawer-toggle" class="drawer-toggle">
      <div class="drawer-content">
        <header class="navbar">…tenant select…user menu…</header>
        <main>{% block content %}{% endblock %}</main>
        <footer>{{ app_version }} · ready/starting pill</footer>  <!-- deployed-build label, self-prefixed (#1698) -->
      </div>
      <aside class="drawer-side">
        <nav>…sidebar with 5 surface links…</nav>
      </aside>
    </div>
  </body>
</html>
```

Surface templates extend it via `{% extends "base.html" %}` and
override `{% block content %}` (and optionally `{% block page_title %}`).
HTMX partial responses don't extend `base.html`; they render the
fragment template directly.

The chassis template references the five surface URLs
(`/ui/broadcast`, `/ui/kb`, `/ui/topology`,
`/ui/connectors`, `/ui/memory`) — T5 (#866) shipped stub routes at
each; the KB stub was retired by G10.2-T1 (#870) which registered
the real `/ui/kb` routes. The broadcast and topology stubs were
retired by G10.1-T1 (#867) and G10.5-T1 (#880) respectively.

### Tenant chip (G0.15-T9 #1217)

The header's tenant chip used to render the stub literal
`tenant: (sign in to choose)` on a disabled `<select>` regardless of
whether the operator had a session — a leftover of the chassis
landing T5 (#866) ahead of the auto-select wiring. After #1217 the
chip's data source is the same `UISessionContext` the BFF middleware
attaches to `request.state.ui_session`: the JWT's `tenant_id` claim
is the operator's default tenant and is auto-selected at
session-create time by
[`_persist_session_from_tokens`](../../backend/src/meho_backplane/ui/auth/routes.py),
so the chip can always read it.

The plumbing is a Jinja2 context processor in
[`templating.py`](../../backend/src/meho_backplane/ui/templating.py),
`_ui_session_context_processor`, registered on the chassis
`Jinja2Templates` wrapper. The processor reads
`request.state.ui_session` and exposes a `session_tenant` template
variable (dict carrying `id` / `slug` / `name`, or `None` on the
unauthenticated auth surfaces). `base.html` renders the chip
conditionally on `session_tenant is not none` and falls through
`name → slug → id` so a tenant row deleted out from under an active
session still produces a readable chip (the `web_session.tenant_id`
FK is intentionally soft; the chassis logs
`ui_session_tenant_row_missing` and serves the page).

The middleware itself
([`UISessionMiddleware`](../../backend/src/meho_backplane/ui/auth/middleware.py))
loads tenant slug + name in the same transaction as the session row
(a PK lookup on the tiny `tenant` table); both fields land on
`UISessionContext` so every render is IO-free at the processor seam.
Cross-tenant switching is a future Initiative — today the chip is a
read-only label, not a selector.

The cascade symptoms the v0.7.0 dogfood flagged (`/ui/memory` showing
0 rows, `/ui/broadcast` empty despite live MCP activity) turned out
to be unrelated to the chip: the Memory and Broadcast routes already
scoped their queries by `session_ctx.tenant_id`. The chip's stub
placeholder was the only real defect; the empty-state observations
were a measurement artifact of the consumer running with seed data
that didn't match the active tenant. The `test_ui_tenant_chip.py`
suite pins the cascade contract (Memory and Broadcast surfaces both
render against the session tenant) as a regression guard.

### Readiness pill (G10.7-T1 #1776)

`base.html`'s sidebar-footer pill colours `bg-success` / "ready" vs
`bg-warning` / "starting" off a `ready` template variable. Originally
only the dashboard computed it; every other `/ui/*` surface hardcoded
`ready=False` in its own context dict (~14 routes, several with a "the
dashboard owns readiness" comment), so the pill was stuck on yellow
"starting" on every page but the dashboard regardless of actual backend
health. The accurate `GET /ready` endpoint existed but the console
never consumed it.

The fix injects the live verdict into *every* render through the same
`_ui_session_context_processor` that surfaces the tenant chip. The
processor exposes a second variable, `ready`, read from
`request.state.ui_ready`. That value is read once per request by
`UISessionMiddleware` (`_stash_ui_readiness`) from
[`ui_readiness_verdict`](../../backend/src/meho_backplane/health.py) — the
*stale-while-revalidate* hot-path accessor over the same short-TTL
(`DEFAULT_READINESS_TTL_S`, 2 s) cache `readiness_snapshot` maintains. The
processor is synchronous (Starlette contract), so it cannot itself await a
probe sweep — computing the verdict in the async middleware and reading it
off `request.state` is the seam that bridges the two.

The accessor keeps the page-render hot path at "negligible cost" by
**never running a probe sweep on the request path**:

* **Cache present (any age).** Return the cached `ready` verdict
  immediately — stale is fine; the pill is an at-a-glance hint, not the
  kubernetes readiness contract. If the entry is older than the TTL,
  schedule a *single-flight* background refresh (`asyncio.create_task`,
  guarded by the `_readiness_refresh_task` handle so at most one runs at a
  time; the reference is retained on the module so the task isn't
  GC'd mid-flight, and its `finally` clears the handle) and serve the
  last-known value without awaiting it.
* **Cache absent (first-ever call).** Do *one* bounded sweep to warm the
  cache so a healthy backend renders green "ready" on first paint
  (preserving the original acceptance criteria). Concurrent first callers
  share that one sweep via the `_readiness_lock` single-flight. Unlike
  `readiness_snapshot`, the warm path caches **even a timeout** verdict so
  a cold-start black-holed probe can't make every subsequent request
  re-sweep; the TTL makes it self-healing (the next render past the window
  schedules a background refresh that picks up recovery).

So under any probe health the per-request cost is a dict read plus, at
most, scheduling one background task — never a serialised sweep.

Two properties make this correct:

* **Processor wins over route literals.** Starlette runs context
  processors *after* the route's own context dict and `dict.update`s
  their output over it
  (`starlette.templating.Jinja2Templates.TemplateResponse`), so the
  injected `ready` overrides any stray per-route value. The ~14
  `ready=False` literals were therefore dropped (they were dead once the
  processor owns the key); `StrictUndefined` still requires the key
  present, and the processor's `False` default ("starting") is the
  fail-safe for the auth/static surfaces where no session middleware
  runs and for any bare `Environment.render`.
* **Dashboard behaviour unchanged.** The dashboard still runs a *fresh*
  probe sweep (`readiness_snapshot(max_age_s=0)`) for its detailed
  readiness card, and writes that fresh verdict back to
  `request.state.ui_ready` so the processor re-injects the dashboard's
  own value rather than the (possibly staler) cached one — the footer
  pill and the dashboard card stay in lock-step.

**Why the decoupling, not just a bound (the #1776 CI-unit-lane overrun).**
The first cut at this feature awaited the probe sweep *inline* in
`_stash_ui_readiness` on every render, with a short `timeout_s`
(`_READINESS_TIMEOUT_S`, 1 s) to keep a hung dependency from blocking the
page. That stopped the event-loop *hang* but not the overrun. In CI the 5
real network probes (`keycloak`, `vault`, `db`, `broadcast`,
`docs_backends`, registered in `main.py`) leak into the process-global
registry — `conftest.py` has no global probe isolation and several
suites register them without clearing — and those endpoints are
black-holed. `readiness_snapshot` deliberately **does not cache** a
timed-out sweep (a transient stall must not pin "starting" on `/ready`
for the whole TTL), so every `/ui/*` request re-swept, serialised through
the readiness single-flight lock, orphaning a probe-worker thread each
time. Hundreds of UI tests x ~1 s serialised + thread accumulation
overran the unit lane's hard `timeout-minutes` job cap (it died at
~15 min on every full run); locally the connects fail fast with
`ECONNREFUSED`, which is why the local sweep passed. The accessor removes
the sweep from the request path entirely (stale-while-revalidate, above),
which is the actual fix; `test_ui_readiness_pill.py` pins it with a
deterministic regression test (a blocking sync probe + N=30 sequential
`/ui/memory` renders must stay well under N x the bound in wall-time and
sweep the probe only a small constant number of times — it FAILS on the
inline-sweep design at ≈ N x bound).

The bounded sweeps the accessor *does* run (the cold-cache warm and each
background refresh) go through `readiness_snapshot(max_age_s=0,
timeout_s=…)`, which wraps the sweep in `asyncio.wait_for`. That bound
only fires if the sweep yields to the loop: `wait_for` can cancel an
*awaiting* coroutine but cannot interrupt a **synchronous** call blocking
it — and several probes are `def` (the `docs_backends` probe, and the
Keycloak/Vault sync-client probes). `run_probes_async` closes that gap by
running sync probes via `asyncio.to_thread`, so the blocking call happens
on a worker thread and the loop stays free for `wait_for` to fire. On
timeout the orphaned thread is not killed — it drains harmlessly — and
single-flight (`_readiness_refresh_task` for background refreshes,
`_readiness_lock` for the cold warm) caps how many threads can ever be in
flight at once: one, not one per request.

`_stash_ui_readiness` still degrades to `False` ("starting") on any
exception out of the accessor and logs `ui_readiness_snapshot_failed`,
and an unbound `ui_ready` key falls back to "starting" in the processor —
the same fail-safe default.

`GET /ready` and the dashboard's fresh sweep (`max_age_s=0`,
`timeout_s=None`) are deliberately **not** routed through the accessor —
they call `readiness_snapshot` directly for a fresh, full-fidelity sweep
on every hit (the kubernetes readiness contract), and `/ready` keeps its
`features` block, which the UI verdict deliberately omits. The dashboard
still writes its own fresh verdict back to `request.state.ui_ready` so
the processor re-injects it, keeping the footer pill and the dashboard
card in lock-step. The `test_ui_readiness_pill.py` suite also pins the
cache semantics, the processor injection + fail-safe, and the end-to-end
pill state on a non-dashboard surface (`/ui/memory`) — including that
both a hung **async** probe and a blocking **sync** probe render
"starting" promptly rather than blocking the render.

## Dependencies

- **`jinja2 >= 3.1.6`** — already a backplane dependency; no
  pyproject change.
- **`cryptography >= 42`** — already a backplane dependency (vendored
  for Vault TLS + JWT). T3 (#864) uses
  `cryptography.fernet.Fernet` for at-rest session-token
  encryption.
- **No new Python deps** for the chassis. T4 (#865) leans on the
  existing `authlib` dependency.
- **Tailwind standalone CLI** — runtime-of-image-build dependency
  only; never enters the running container or the wheel.

## FastAPI integration (Task #866)

T5 wires the chassis into the FastAPI app in
[`backend/src/meho_backplane/main.py`](../../backend/src/meho_backplane/main.py).
Five things land together:

1. **`StaticFiles` mount** at `/ui/static` against the parent
   `ui/static/` tree, so the URLs `/ui/static/src/vendor/htmx.min.js`
   and `/ui/static/dist/tailwind.css` (referenced by `base.html`)
   resolve to the same mount. `check_dir=False` keeps the mount
   resilient on a fresh clone where `static/dist/` is empty; a
   request for the compiled stylesheet 404s until the operator runs
   the Tailwind build, which is the operator-facing remediation
   surfaced by this doc and by the lifespan log line. The lifespan
   startup also calls `ensure_static_dist_dir()` to create the
   directory so the mount construction never raises on a fresh
   clone.
2. **UI auth router** -- `/ui/auth/{login,callback,logout}`. Mounted
   via `build_router()` from `meho_backplane.ui.auth`. Reachable
   unauthenticated.
3. **UI surface router** -- aggregates the dashboard
   (`GET /ui/`) + the five surface stubs. Mounted via
   `build_router()` from `meho_backplane.ui.routes`. Every route
   requires a session via the `require_ui_session` dependency.
4. **`UISessionMiddleware`** registered as the OUTERMOST middleware
   (added last in `add_middleware` LIFO order). Out-of-prefix paths
   (`/api/*`, `/mcp/*`, `/healthz`, etc.) pass through untouched;
   `/ui/*` paths are gated by the session-cookie check, with
   `/ui/static/` and `/ui/auth/` exempted so the login flow + asset
   loads work for unauthenticated browsers. The JWT dependency on
   `/api/*` routes is untouched -- the session middleware short-
   circuits `/ui/*` BEFORE the JWT chain runs, satisfying the
   "session middleware before JWT" property the Initiative #337
   acceptance criterion #4 requires.
5. **`CSRFMiddleware`** registered just inside `UISessionMiddleware`.
   Guards state-changing `/ui/*` requests via the OWASP signed
   double-submit cookie pattern. The dashboard + stub handlers
   mint a fresh token on every authenticated render and set the
   `meho_csrf` cookie with `SameSite=Strict; Secure; Path=/ui`.
   HTMX surfaces echo the token in the `X-CSRF-Token` header via
   the `hx-headers` directive on the page `<body>`; classic HTML
   forms include the value in a `csrf_token` hidden field.

The dashboard view (`GET /ui/`) renders three components per
Initiative #337 work-item #6:

* A 3x2 grid of DaisyUI `card` tiles linking to the five surface
  routes. The sixth tile shows the backplane version + readiness
  badge sourced from `meho_backplane.health.run_probes_async`.
* A live "recent activity" snippet wired to the session-gated SSE
  bridge `/ui/broadcast/stream` (G0.25 `#1696`; the chassis
  originally pointed at the Bearer-only `/api/v1/feed`, which a
  browser `EventSource` can never authenticate against — no
  `Authorization` header support — so that wiring 401-looped behind
  its "Connecting…" placeholder). Frames are consumed through the
  same hidden-sink + Alpine pattern as the broadcast and connectors
  surfaces: `hx-ext="sse"` + `sse-connect` + `sse-swap="broadcast"`
  sit on a hidden sink, and the `dashboardFeedTray` controller
  (`static/src/app/dashboard-feed.js`, registered on `alpine:init`
  from the head-level `component_scripts` block per #1692) cancels
  the raw swap on `htmx:sse-before-message`, parses the
  `BroadcastEvent` JSON, and renders time / principal / op_id /
  result_status rows via `x-text` bindings (markup-bearing event
  fields stay inert), trimmed to a 50-row in-DOM cap.
* A "readiness checks" panel listing every registered probe with a
  green/orange pill matching `/ready`'s shape.

The remaining surface stubs render `_stub.html` with a "Coming soon"
panel referencing the surface Initiative number (G10.3=#340,
G10.4=#341). Broadcast (#867), KB (#870), and topology (#880) have
replaced their stubs with real surface routers.

## KB read surface (G10.2-T1 #870)

`meho_backplane.ui.routes.kb` ships the read surface at `/ui/kb`:

- `GET /ui/kb` — entry list (empty query, paginated) or search results
  (non-empty query, HTMX fragment when `HX-Request: true`).
- `POST /ui/kb/search` — HTMX keyup-debounced search partial (returns
  `kb/_results.html` fragment).
- `GET /ui/kb/<slug>` — entry detail with server-side Markdown rendered
  via `markdown-it-py` (tables + strikethrough enabled) and pygments
  syntax highlight. 404 for missing or cross-tenant slugs.
- `GET /ui/kb/<slug>/preview` — hover-preview HTMX partial with
  query-term `<mark>` highlight markup.

**Dependencies added:** `markdown-it-py >= 3.0`, `pygments >= 2.18`,
`python-multipart >= 0.0.12`.

The renderer singleton lives in `meho_backplane.ui.routes.kb.render`.
A module-level `threading.Lock` guards the shared `MarkdownIt` instance
(not thread-safe for concurrent `render()` calls). The pygments CSS is
generated once at module load and injected as an inline `<style>` block
in the entry-detail template.

## KB editor modal + mobile reflow (G10.2-T3 #872)

T3 extends `meho_backplane.ui.routes.kb` with two write routes and
mobile-reflow CSS on the entry detail page:

- `POST /ui/kb/editor-preview` — HTMX live-preview partial. Accepts a
  `body` form field (max 65 536 bytes), renders it via `render_markdown`,
  returns the `kb/_editor_preview.html` fragment. Any authenticated
  operator can call this (read-only Markdown transform; no role gate).
- `POST /ui/kb/new` — editor save. Requires `tenant_admin` role (enforced
  by `_require_tenant_admin()`: loads the full `DecryptedSession` via
  `load_session()`, verifies the access token via
  `verify_jwt_for_audience()`, checks `operator.tenant_role ==
  TenantRole.TENANT_ADMIN`). On success returns HTTP 204 +
  `HX-Redirect: /ui/kb/<slug>`; on failure re-renders the
  `kb/_editor_modal.html` fragment with a 422 and an inline error banner.

**Editor modal** (`kb/_editor_modal.html`): DaisyUI `<dialog>` element
with a slug input, a CodeMirror 6 editor pane (`#kb-editor-cm`), a live
preview column (`#kb-editor-preview-content`), and a hidden textarea
(`#kb-editor-body`) that HTMX reads for the preview POST. Variables use
`| default('')` / `| default(none)` so the template is safe when
`{% include %}`'d from `index.html` without the editor-specific context
keys (Jinja2 StrictUndefined catches bare names; the `| default()`
filter intercepts the Undefined object before the strict check fires).

**CodeMirror 6** (`static/src/vendor/codemirror-bundle.min.js`): a
single-file IIFE bundle (SHA256 `a411a47c…`, 606 KB) built offline with
esbuild from `codemirror@6.0.1` + `@codemirror/lang-markdown@6.3.2`.
Exposes the `CM` global with `EditorView`, `EditorState`, `basicSetup`,
`markdown`, `keymap`, `defaultKeymap`, `historyKeymap`, `indentWithTab`.
Mounted via a `MutationObserver` on the `<dialog>` `open` attribute — the
editor is created on first modal open and reused across open/close cycles
so the operator's draft persists without a hidden-textarea seed.

**Mobile reflow** (`kb/detail.html` `{% block scripts %}`): CSS added to
`.kb-body` prevents horizontal page overflow at narrow widths:
`overflow-wrap: break-word`, `word-break: break-word`; code blocks cap at
`max-width: 100%` and scroll inside their own box; tables use
`display: block; overflow-x: auto`; images cap at `max-width: 100%`.

## KB upload surface (G10.2-T2 #871)

Task [#871](https://github.com/evoila/meho/issues/871) extends
`meho_backplane.ui.routes.kb` with the upload surface. All upload
routes require `tenant_admin` role enforced by the new
`require_ui_admin` dependency in `meho_backplane.ui.auth.middleware`.

### Routes

| Route | Renders |
| ----- | ------- |
| `GET /ui/kb/upload` | Upload page (`kb/upload.html`) with Alpine.js drag-and-drop zone. Mints a CSRF token and sets the `meho_csrf` cookie. `tenant_admin` required. |
| `POST /ui/kb/upload` | Single-file upload. Accepts one `.md` file (field `file`) via `multipart/form-data`. Optional `slug` field overrides the filename-derived slug. Calls `KbService.create_entry` (idempotent on same body_hash). Returns `kb/_upload_progress.html` partial with `alert-success` or `alert-error`. |
| `POST /ui/kb/upload/bulk` | Bulk upload. Accepts multiple `.md` files under the `file` field. Processes each independently (partial failure allowed). Returns the same `kb/_upload_progress.html` partial with a per-file results table. |

**Route ordering:** `GET /ui/kb/upload` is registered before
`GET /ui/kb/{slug}` in `build_kb_router()` so FastAPI's first-match-wins
routing does not swallow the literal "upload" path segment as a slug.

### RBAC gate (`require_ui_admin`)

`UISessionContext` (the context returned by `require_ui_session`)
deliberately omits the tenant role to keep read-only routes free of
JWT-decode overhead. Upload routes chain `require_ui_admin` on top:

1. Loads the `DecryptedSession` from the DB (Fernet-decrypted
   `access_token`).
2. Calls `verify_jwt_for_audience` to decode the JWT and extract
   `TenantRole`.
3. Compares rank against `TENANT_ADMIN`; raises HTTP 403 with
   `detail="tenant_admin_required"` if insufficient.
4. Returns the same `UISessionContext` so callers use `tenant_id` /
   `operator_sub` without a second dependency.

`operator` and `read_only` roles receive a 403, not a redirect.
Unauthenticated requests still get the standard 302 → login from
`require_ui_session`.

### File validation

`_process_upload_files()` enforces the following per file before
calling `KbService.create_entry`:

- Extension must be `.md` (case-insensitive).
- Size cap: 512 KiB (`_MAX_UPLOAD_BYTES`). Enforced by reading only
  `_MAX_UPLOAD_BYTES + 1` bytes and checking the length.
- Content must decode as valid UTF-8.
- Slug derivation: strips `.md`, NFKD-normalises, lower-cases,
  replaces non-alphanumeric runs with hyphens, strips leading/trailing
  hyphens, caps at 200 chars (`_filename_to_slug()`).
- Slug is validated by `KbService.create_entry` (raises
  `InvalidKbSlugError` on invalid shape).

Errors are caught per-file; the list always has one entry per uploaded
file so the template renders every row.

### OOB live-list update

On success, `kb/_upload_progress.html` emits a bare `<tr
hx-swap-oob="afterbegin:#kb-results-body">` element for each
newly created entry. HTMX 2 picks this up and prepends the row into
the visible KB list table (`#kb-results-body` in `_results.html`)
without a page reload. If the operator navigated directly to
`/ui/kb/upload` (so the list table is not rendered), HTMX silently
ignores the OOB swap because the target element does not exist in the
active DOM.

### Idempotency

`KbService.create_entry` is body-hash idempotent: re-uploading the
same Markdown content returns the existing entry rather than creating a
duplicate. The upload endpoint reports `status="success"` on both the
first ingest and subsequent identical re-uploads.

### Tests

`backend/tests/test_ui_kb_upload.py` covers: route-ordering regression,
auth boundary (unauthenticated → 302), RBAC (operator → 403), upload
page render, single-file success + OOB row, slug override, non-`.md`
rejection, oversized rejection, binary/non-UTF-8 rejection, invalid slug
override, bulk upload with partial failure, idempotent re-upload, and
CSRF enforcement.

The chassis smoke test
[`backend/tests/test_ui_chassis_smoke.py`](../../backend/tests/test_ui_chassis_smoke.py)
exercises every acceptance criterion: unauth dashboard -> 302
login, login -> 302 Keycloak, callback -> session row + 302 /ui/,
authenticated dashboard render (page title + 5 sidebar links + 6
card cells + HTMX SSE wiring), 5 stub routes -> 200 with placeholder
content, CSRF rejection on missing/mismatched/forged tokens,
positive control on matched token, and middleware-order sanity
(/api/* passes through untouched).

## Broadcast surface (Tasks #867, #868, #869)

Initiative [#338](https://github.com/evoila/meho/issues/338) (G10.1
Activity broadcast UI), Task [#867](https://github.com/evoila/meho/issues/867)
(G10.1-T1) replaces the chassis stub at `/ui/broadcast` with the real
**live activity feed**: an SSE-streamed, reverse-chronological event
list with an empty state and a 1000-row in-DOM cap. Task
[#868](https://github.com/evoila/meho/issues/868) (G10.1-T2) adds the
**filter bar** (op_class / principal / target / op_id), the **event
detail drawer**, and the **PII 🔒 visualisation**. Task
[#869](https://github.com/evoila/meho/issues/869) (G10.1-T3) adds the
**wall-monitor mode** (`?wall=1`), the **Last-24h replay tab**, and the
**long-display session refresh** (a sliding-session extension) that
keeps a full-screen wall display from logging out mid-stream.

### Routes

| Route | Renders |
| ----- | ------- |
| `GET /ui/broadcast` | Full-page live-feed view (`broadcast/feed.html`, extends `base.html`). Sidebar active-state on Broadcast. Accepts `?op_class=&principal=&target=&op_id=` so a copy-pasted filtered URL reproduces the view. `?wall=1` selects the no-chrome wall-monitor layout (`broadcast/wall.html`) instead. |
| `GET /ui/broadcast/feed` | Filtered feed **fragment** (`broadcast/_feed.html`) — the filter-bar submit target. Re-renders the feed with the active server-side filters baked into a fresh `sse-connect` URL. |
| `GET /ui/broadcast/stream` | Session-gated SSE bridge (`text/event-stream`). The feed view's `sse-connect` target. Accepts `op_class` / `principal` / `target` filters. |
| `GET /ui/broadcast/history` | Last-24h replay **fragment** (`broadcast/_history.html`) — the "Last 24h" tab's HTMX target. A finite `XRANGE` pull of the tenant's last-24h events seeded into the shared `broadcastFeed` controller. |
| `GET /ui/broadcast/event/{audit_id}` | Event detail drawer **fragment** (`broadcast/_event_drawer.html`; `_event_drawer_not_found.html` + HTTP 404 for a missing / cross-tenant id). Shared by the live feed, the wall view, and the history pane. |

### Why a UI-owned SSE bridge instead of subscribing to `/api/v1/feed`

The canonical per-tenant SSE feed is `GET /api/v1/feed` (G6.1-T4 #310),
which authenticates via the `Authorization: Bearer <jwt>` header. The
browser's `EventSource` — which the HTMX `sse` extension uses under the
hood — **cannot set custom request headers** (the WHATWG `EventSource`
constructor exposes only `withCredentials`); it sends cookies, not a
Bearer token. So pointing `sse-connect` at `/api/v1/feed` from a
logged-in operator's browser would be answered with a 401 and the SSE
state machine would tighten into a reconnect loop. (The chassis
dashboard's recent-activity snippet originally wired
`sse-connect="/api/v1/feed"` directly and was inert for exactly this
reason — it never left its "Connecting…" placeholder until G0.25
`#1696` re-pointed it at this bridge.)

The broadcast view instead subscribes to `/ui/broadcast/stream`, a
UI-owned route under `/ui/` so the existing `UISessionMiddleware` gates
it with the BFF session cookie — the same auth boundary as every other
`/ui/*` page. The stream's tenant comes from the validated session
(`UISessionContext.tenant_id`), never a query parameter, so the
cross-tenant isolation guarantee is identical to `/api/v1/feed`. The
SSE frame format, per-entry filter/parse/skip logic, cursor-resolution
precedence (`Last-Event-Id` > `since` > `$`), and cursor validation are
reused verbatim from `meho_backplane.api.v1.feed` so a reconnect that
started on either surface replays identically.

### Live-streaming model (JSON feed → server-authored rows)

`/api/v1/feed` (and the bridge) stream `BroadcastEvent` **JSON** — the
same shape `meho status --watch` and the MCP resource consume, out of
scope to reshape into HTML frames. The HTMX `sse` extension would
otherwise swap that JSON in as raw text. Instead a hidden sink element
carries `sse-swap="broadcast"` (so the extension subscribes and owns
reconnect/backoff); the `broadcastFeed` Alpine controller hooks
`htmx:sse-before-message`, parses the JSON, `preventDefault()`s the raw
swap, and prepends the event to its bounded `events` array. Each event
renders through the server-authored `broadcast/_event_row.html` partial
via an Alpine `<template x-for>` — **server-side markup, client-side
data binding** (the only split the JSON feed allows). The op_class →
DaisyUI badge colour table is serialised server-side into the page so
the colour policy stays one auditable map.

### Templates + JS

| File | Purpose |
| ---- | ------- |
| `broadcast/feed.html` | Full-page view: header, filter bar include, feed-fragment include, drawer slot. Injects the controller `<script src>` into the head-level `{% block component_scripts %}` so it executes before `alpine.min.js` (#1692). |
| `broadcast/_filter_bar.html` | The op_class / principal / target / op_id controls. The three server filters `hx-get` to `/ui/broadcast/feed`; op_id dispatches a `broadcast-op-id-changed` window event for the client-side filter. |
| `broadcast/_feed.html` | The swappable feed fragment: the SSE sink (with the filtered `sse-connect` URL), status bar, column header, empty state, `<template x-for>`. Wraps the `broadcastFeed` Alpine controller so a filter re-render resets the event list and re-subscribes. |
| `broadcast/_event_row.html` | Server-authored row markup (timestamp · principal badge · op_id · op_class badge · result_status icon · target · payload summary). Click opens the drawer; aggregate-only events render the 🔒 marker + placeholder. |
| `broadcast/_event_drawer.html` | Event detail drawer: op identity, operation metadata, identifiers (audit_id / request_id / broadcast event_id), full payload (or the 🔒 placeholder for sensitive ops). Alpine `click.outside` / Escape / Close dismiss. |
| `broadcast/_event_drawer_not_found.html` | The 404 drawer fragment for a missing / cross-tenant audit id. |
| `broadcast/wall.html` | The no-chrome wall-monitor view (Task #869): a standalone document (not `extends base.html`) that drops the sidebar / navbar / filter bar and embeds `_feed.html` with `wall=True` (taller rows + auto-scroll). |
| `broadcast/_history.html` | The Last-24h replay fragment (Task #869): seeds the shared `broadcastFeed` controller with the historical events the `/ui/broadcast/history` route pulled via `XRANGE`, so the rows render through `_event_row.html` and open the same drawer as the live feed. |
| `static/src/app/broadcast-feed.js` | The `broadcastFeed` Alpine component (registered on `alpine:init`; loaded from the head-level `component_scripts` block — before `alpine.min.js`, or the registration is lost, #1692). External deferred script, not inline, to stay CSP-ready. Holds the parse + prepend + 1000-row trim, the `visibleEvents` op_id client filter, the `init` re-read of the live op_id input (so the filter survives a server-side fragment swap, gated to `#broadcast-feed` only), the `openDrawer` helper, the badge/timestamp/payload/aggregate-only helpers, the `opts.seedFrom` data-island seed (for the history replay pane — reads + `JSON.parse`s a `<script type="application/json">` block rather than receiving events through the `x-data` attribute, closing B1's stored-XSS hole), and the `opts.autoScroll` wall-monitor behaviour. |

### Performance + empty state

* **1000-row cap** — `IN_DOM_ROW_CAP` is passed into the page; the
  controller `unshift`s each event and trims `events.length` to the cap
  so a sustained stream keeps the DOM bounded (work item #9).
* **Empty state** — shown while `events` is empty (or, in T2, when
  filters match nothing): "No activity matching your filters…" (work
  item #8).

### Filters (Task #868, work item #3) — server-side three vs client-side op_id

The filter bar exposes four controls but they split across two layers:

* **op_class / principal / target — server-side.** These are the three
  filters the stream bridge (and `/api/v1/feed`) accept. The filter bar
  `hx-get`s `/ui/broadcast/feed` (`hx-include` carries the three values,
  `hx-target="#broadcast-feed"`, `hx-swap="outerHTML"`,
  `hx-push-url="true"`). The fragment route embeds them in a fresh
  `sse-connect="/ui/broadcast/stream?op_class=…&principal=…&target=…"`
  URL (built by `_stream_url`, `urlencode`-escaped). HTMX
  auto-processes the swapped fragment, so the `sse` extension tears down
  the prior subscription (the replaced node) and opens the filtered one
  — the **server** drops non-matching events before they reach the
  browser. Empty filters are omitted from the URL so "All" streams
  everything.
* **op_id — client-side.** The stream exposes no op_id parameter, and
  adding one would diverge the bridge from `/api/v1/feed` (out of
  scope). Instead the op_id input lives **outside** the swapped
  fragment, so the input **element** (and the operator's typed value)
  survives a server-filter re-render. The **active filtering**, though,
  lives in the `broadcastFeed` controller **inside** the swapped
  fragment: a server-side op_class/principal/target change `hx-get`s the
  fragment route **without** op_id (it is excluded from `hx-include`),
  re-mounts the controller, and seeds `opIdFilter` empty — so without
  more, the op_id filter would silently stop applying after every server
  re-render even though the input still shows the text. The controller
  closes that gap in its `init`: on every mount (initial load **and**
  each swap) it re-reads the live op_id input as the single source of
  truth, so the client-side narrowing keeps applying. On each debounced
  keystroke the input also dispatches a `broadcast-op-id-changed` window
  event the controller listens for
  (`x-on:broadcast-op-id-changed.window`) and recomputes `visibleEvents`
  — a case-insensitive substring filter over the already-streamed
  `events` — without touching the live SSE subscription. The op_id seed
  is also passed into the controller on render so a copy-pasted
  `?op_id=` URL narrows the view on first paint (the `init` read then
  reconciles it with the live input value).

The target dropdown is populated from the `targets` table scoped to the
session tenant (`feed._target_names`, capped at 500) — a tenant-A
operator can only filter by tenant-A targets.

### Event detail drawer (Task #868, work item #4) — keyed on audit_id

A feed-row click calls the controller's `openDrawer(ev)`, which
`htmx.ajax`-GETs the drawer fragment into `#event-drawer`. The path
parameter is the **audit id**, not the broadcast event id:

* The broadcast `event_id` lives only on the ephemeral, MAXLEN-trimmed
  Valkey stream — it is not a column on any table.
* The canonical, queryable record is the `audit_log` row, keyed by
  `audit_log.id`, which every `BroadcastEvent` carries as `audit_id`.
  The drawer's full payload + `request_id` exist only there.

So the row builder reads `ev.audit_id` for the path and passes
`ev.event_id` as the `event_id` query param for display only. The route
resolves the row tenant-scoped (`audit_log.tenant_id =
session.tenant_id` as the first `WHERE` predicate; a cross-tenant id is
an opaque 404) and renders identity + metadata + the three identifiers
(audit_id / request_id / broadcast event_id). Alpine
`x-on:click.outside` (+ Escape + Close) dismisses the drawer; Alpine
only evaluates `.outside` while the element is visible, so the swap-in
click cannot immediately re-close it.

### PII discipline in the drawer (Task #868, work item #7)

The feed row's 🔒 marker keys off the **redacted** broadcast payload
(missing `params` ⇒ aggregate-only — the T1 signal). The drawer is the
sharper case: it reads the **unredacted** `audit_log` payload, so
rendering it raw for a `credential_read` op would defeat decision #3 on
click. The drawer therefore reproduces the publisher's verdict:

* When the audit row carries `payload["broadcast_detail_effective"]`
  (the G6.3 resolver's recorded `"full"` / `"aggregate"` decision —
  including any per-tenant override), the drawer honours it verbatim.
* Otherwise it classifies the op via `classify_op` against the op id
  (recovered from `payload["op_id"]`, falling back to the publisher's
  own `http.{method}:{path}` heuristic so the class matches) and treats
  the `credential_read` / `credential_mint` / `audit_query` classes as
  aggregate-only.

For an aggregate-only verdict the drawer never builds the payload view
at all — it renders the 🔒 placeholder. For the full-detail path it
strips the audit-only keys (`op_id`, `op_class`,
`broadcast_detail_origin`, `broadcast_detail_effective`) before the
`| tojson` dump so the drawer shows the request params only, never the
internal forensic metadata (`tenant_rule:<uuid>` origins stay
audit-side).

### Performance + empty state

* **1000-row cap** — `IN_DOM_ROW_CAP` is passed into the page; the
  controller `unshift`s each event and trims `events.length` to the cap
  so a sustained stream keeps the DOM bounded (work item #9).
* **Empty state** — shown while `visibleEvents` is empty (no events
  streamed yet, or the op_id client filter narrowed everything out):
  "No activity matching your filters…" (work item #8).

### Wall-monitor mode (Task #869, work item #5) — `?wall=1`

`GET /ui/broadcast?wall=1` selects `broadcast/wall.html` instead of
`broadcast/feed.html`. The wall view is a **standalone document** (not
`{% extends "base.html" %}`) so it drops the DaisyUI drawer shell —
no sidebar, no top navbar, no filter bar — and maximises the feed for a
full-screen team-room monitor. It is opened in a new tab from the
in-chrome view's "Wall mode" button so the monitor can be cast
independently of the operator's working session.

The wall view embeds the **same** `broadcast/_feed.html` fragment with
`wall=True`, so it inherits the live SSE wiring, the `EventSource`
auto-reconnect + `Last-Event-Id` replay durability, and the 1000-row
in-DOM cap unchanged — only the chrome and the visual density differ.
`wall=True` flips the `broadcastFeed` controller's `autoScroll` on (it
scrolls the list to the top, where the newest event prepends, as events
arrive) and renders taller rows (`_event_row.html` reads the `wall`
flag). A clicked row still opens the T2 event-detail drawer, overlaid
as a right-hand panel on the wall layout.

### Long-display session refresh (Task #869, work item #5) — sliding session

The wall monitor runs for hours, but the BFF session row's `expires_at`
is set at login to roughly the access-token TTL (minutes to ~an hour).
The load-bearing failure this guards against: the browser `EventSource`
**permanently fails on a non-200 response** (WHATWG SSE spec) and does
not reconnect. So once the session lapses, the next SSE reconnect to
`/ui/broadcast/stream` is 302-redirected to login by
`UISessionMiddleware` — a non-200 — and the feed dies silently with no
recovery.

The fix is a **server-side sliding-session extension** in
`session_store.load_session` (not an OAuth token rotation — that needs a
Keycloak refresh handler that does not exist in v0.2). On every active
`/ui/*` load (each SSE reconnect is one), when the row is within
`UI_SESSION_SLIDING_EXTENSION_SECONDS` of `expires_at`, the load pushes
`expires_at` out to `now + sliding_window` — bounded by an absolute
ceiling `created_at + UI_SESSION_ABSOLUTE_LIFETIME_SECONDS`. This is the
standard idle-vs-absolute session-timeout pairing (OWASP ASVS v4 §3.3):
the sliding window keeps an in-use display alive; the absolute cap
guarantees a daily re-auth even for a permanently-displayed monitor.
`sliding=0` disables the extension. The settings default to a 1h sliding
window and a 12h absolute cap. The mutation is server-side-controlled
(clock + config only, never client-supplied), so it runs safely on every
load.

### Last-24h replay pane (Task #869, work item #6) — `/ui/broadcast/history`

The in-chrome view carries a **Live / Last 24h** tab strip. The Live tab
is the SSE feed; the "Last 24h" tab lazy-loads `/ui/broadcast/history`
the first time it is shown (`hx-trigger="intersect once"` fires the GET
when the initially-hidden pane scrolls into view).

The history route pulls the tenant's last-24h events with a **finite**
`XRANGE` over `meho:feed:{session.tenant_id}` — the opposite shape from
the live feed's BLOCKing `XREAD`: a bounded batch read that returns
immediately (no streaming, no `while True`, so it cannot hang a worker).
The window start is a bare millisecond timestamp `now - retention_hours`
(`BROADCAST_RETENTION_HOURS`, default 24); Valkey `XRANGE` auto-completes
the bare timestamp's sequence to `0`, so the range spans every entry from
that millisecond to `+` (the live tail), bounded by `COUNT = 1000` (the
same in-DOM row budget as the live feed). `XRANGE` returns oldest-first;
the route reverses to newest-first to match the live feed.

The route passes the events as the `history_events` list, which the
fragment renders through Jinja's `| tojson` filter into a
`<script type="application/json" id="broadcast-history-data">` data
island. The **same** `broadcastFeed` controller the live feed uses reads
that island on `init` (`opts.seedFrom` → `seedFromIsland`) and
`JSON.parse`s its `textContent`, so a history row renders through the
same `_event_row.html` partial and opens the same T2 drawer on a click —
a history row is byte-identical to a live row. The `init` op_id re-read
is gated to `#broadcast-feed` (the live feed) so it never leaks the live
filter onto the history pane, which keeps its own filter state. The
history fetch is fail-soft: a transient Valkey error returns an empty
list (the pane shows its empty state) rather than 500-ing the fragment.

**Why a data island, not an `x-data` attribute (B1, PR #1044).** Event
fields (`op_id`, `target_name`, `principal_sub`, payload values) are
operator-controlled within a tenant. Interpolating the serialised array
straight into the double-quoted `x-data="…"` attribute — even via
`| tojson` — is a stored DOM-XSS: `tojson` escapes `<` `>` `&` `'` and
U+2028/U+2029 but **not** `"`, so a double-quote in any event field
breaks out of the attribute and injects live event handlers (and is a
plain render-corruption bug for any legitimate `"`-bearing value).
Emitting the JSON as inert `<script type="application/json">` text and
parsing it client-side keeps the untrusted data out of every attribute
context; `tojson`'s `<`/`>` escaping prevents a `</script>` in a field
from closing the element early.

**The `op_id` scalar sinks (follow-up to B1, same PR #1044).** The same
`tojson`-in-a-double-quoted-`x-data` class shipped on the live feed too:
`_feed.html`'s `opIdFilter: {{ op_id_filter | tojson }}` and
`_filter_bar.html`'s `x-data="{ opId: {{ op_id_filter | tojson }} }"`.
Both echo the reflected `op_id` query param (`GET /ui/broadcast` and
`GET /ui/broadcast/feed`), so a crafted link
(`/ui/broadcast?op_id=" autofocus onfocus=…`) is a **reflected** XSS in
the victim operator's session — not merely self-XSS. Because the seed
here is a flat options object (not the events array the history island
carries), the fix switches the `x-data` **attribute delimiter to single
quotes** rather than adding a second data island: `tojson` escapes `'` to
`'` (it never emits a raw single-quote) and escapes `<`/`>`/`&`, so a
single-quoted attribute is breakout-proof for any scalar while the
value's `"` bytes ride harmlessly inside it. The static config has no raw
`'` (`op_class_badge_json` is `json.dumps` output, double-quoted only), so
the delimiter is unambiguous, and behaviour is byte-identical. The
sibling `value="{{ op_id_filter }}"` is in an autoescaped attribute
(Jinja escapes `"` to `&#34;`) and was already safe. Regression tests in
`test_ui_broadcast_filters.py` drive both routes with a breakout `op_id`
and parse the response to assert no handler grafts onto an `x-data`
element.

**The runbook editor island (G2.x #100, Goal #87 security review).** The
runbook authoring editor (`runbooks/editor.html`) was the lone
data-bearing `x-data` that never received the PR #1044 hardening: it
interpolated `initialSteps: {{ initial_steps_json | safe }}` (the `safe`
filter disables autoescape entirely, and the upstream `json.dumps` leaves
`"` raw) plus `| tojson` scalars (`slug` / `title` / `description` /
`target_kind`) inside a **double-quoted** `x-data="…"`. Any runbook field
(`OperationCallStep`/`ManualStep` `title`/`body`, `RunbookTemplateBody`
`title`/`description`) containing a `"` broke out — a stored XSS on
`GET /ui/runbooks/{slug}/edit` and a reflected XSS on the
`POST /ui/runbooks/new` 422 re-render, both in an authenticated
`tenant_admin`'s session. The fix is the canonical one: single-quote the
attribute (`x-data='runbookEditor({…})'`) and render the step tree with
`{{ form_steps | tojson }}` over the raw list (`build_editor_context` no
longer `json.dumps`-es it), so every field — `"` bytes included — stays
inert inside the value. This was the last `| safe` in an HTML-attribute
context under `ui/templates/`; the single-quoted `x-data` + `| tojson`
pair is now the canonical UI output-encoding rule for any template
binding untrusted data into Alpine. Regression coverage lives in
`test_ui_runbooks_editor.py` (stored + reflected paths), reusing the
`_assert_no_xss_breakout` parser harness from `test_ui_broadcast_filters.py`.

### Cross-tenant isolation

The page carries no tenant data beyond the operator's own identity; live
events arrive over the tenant-scoped bridge whose stream key is
`meho:feed:{session.tenant_id}`. The target dropdown and the event
drawer both scope to `session.tenant_id` at the SQL `WHERE` clause —
never a query parameter — so a tenant-A operator can never surface
tenant-B targets or audit rows. The history pane keys the same way: its
`XRANGE` reads `meho:feed:{session.tenant_id}`, taken from the validated
session, never a query parameter — so a tenant-A operator's replay can
never surface tenant-B events. The wall view reuses the live feed
fragment, so it inherits the live bridge's tenant scoping unchanged. The
[T1 broadcast suite](../../backend/tests/test_ui_broadcast_feed.py) pins
the generator's stream-key derivation per tenant; the
[T2 filters suite](../../backend/tests/test_ui_broadcast_filters.py)
pins the dropdown + drawer tenant scoping; the
[T3 wall/replay suite](../../backend/tests/test_ui_broadcast_wall_replay.py)
pins the history pane's per-tenant `XRANGE` key + the wall layout + the
sliding-session extension.

### Known limits

* **op_id filtering is client-side only** — it narrows the in-DOM
  `events` (bounded by the 1000-row cap), not the server stream. An op
  that fired before the operator typed an op_id substring, and has since
  scrolled past the cap, is not retroactively matched. This is the
  intended scope (op_id is a live-narrowing convenience, not a history
  query — that is G8 audit-query territory).
* **Reconnect replay is exercised at the feed-endpoint layer** — the
  `Last-Event-Id` cursor resolver + validator are reused from
  `/api/v1/feed` (covered by `test_api_v1_feed`); the UI suite asserts
  the bridge forwards an explicit cursor to `xread` and rejects a
  malformed one with 400. A browser-level Playwright reconnect test is
  out of scope (the repo ships no Node/browser test harness).

## Topology surface (Task #880)

Initiative [#342](https://github.com/evoila/meho/issues/342) (G10.5
Topology UI), Task [#880](https://github.com/evoila/meho/issues/880)
(G10.5-T1) replaces the chassis stub at `/ui/topology` with the real
**tabular view** + **per-node detail drawer**.

### Routes

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/ui/topology` | Full-page tabular surface. Accepts `?sort=`, `?direction=`, `?kind=`, `?q=` (name substring), and `?view=` (reserved for G10.5-T2's graph mode). Returns the full page on a normal browser navigation and the `_table_rows.html` fragment when the request carries `HX-Request: true` (HTMX sort / filter swap). |
| `GET` | `/ui/topology/node/{node_id}` | Per-node detail drawer fragment. Returns `_drawer.html` on a happy path; `_drawer_not_found.html` with HTTP 404 when the node id is unknown or belongs to another tenant. |

### Module layout

| Module | Purpose |
| ------ | ------- |
| `meho_backplane.ui.routes.topology` | Aggregate router. `build_router()` includes the table and detail sub-routers; mounted **before** the chassis stubs in `meho_backplane.ui.routes.build_router` so the real route wins FastAPI's first-match-wins path lookup. |
| `meho_backplane.ui.routes.topology.table` | `GET /ui/topology` handler. Pulls the tenant inventory via `meho_backplane.topology.query.list_nodes` and branches on `HX-Request` to render either `topology/table.html` (full page) or `topology/_table_rows.html` (fragment). Sort + filter inputs are echoed back into the template context so the rendered HTML preserves operator selection. |
| `meho_backplane.ui.routes.topology.detail` | `GET /ui/topology/node/{node_id}` handler. Resolves the node tenant-scoped, then pulls incoming + outgoing edges via a SQLite-portable ORM query (the substrate `list_edges` uses PG-only `jsonb_typeof` for its conflict filter; the drawer doesn't need conflict info, so the local helper is the right factoring) and the recent `audit_log` rows for the node's `target_id`. |

### Substrate primitive

`meho_backplane.topology.query.list_nodes` was added by T1 as the
tenant-scoped paginated node listing every UI / CLI / MCP layer can
share. ORM-based (not raw `text()`) so the helper runs on SQLite (the
unit-test fixture) as well as Postgres (production). Closed sort-column
enum (`name | kind | first_seen | last_seen`) is the SQL-injection
guard -- the route layer validates the same set at the HTTP boundary,
but the substrate refuses defensively because a non-route caller (CLI /
MCP / REPL) may not have the same validation. Tenant scoping is
unconditional: every statement starts with
`WHERE graph_node.tenant_id = :tenant_id`; no filter combination
overrides it. Cross-tenant isolation is enforced at the SQL layer, not
the template.

### Templates

| Template | Purpose |
| -------- | ------- |
| `topology/table.html` | Full-page surface. Extends `base.html`; renders the filter bar + sortable column headers + the multi-row checkbox `<tbody>` + the `#node-drawer` slot. Alpine.js holds the selection state (`x-data="{ selected: new Set() }"`) so a future bulk-action button reads selection without a template rewrite. |
| `topology/_table_rows.html` | `<tbody>` fragment swapped in by HTMX on a sort / filter change. Idempotent shape — re-rendered standalone on an HTMX request or included from the full page on a browser nav. |
| `topology/_drawer.html` | Drawer fragment with node properties (id, kind, name, target_id, first_seen, last_seen, discovered_by, raw JSON), outgoing + incoming edges, recent operations on the node's target (empty for inner graph nodes with no `target_id`), and the "Show dependents" link handing off to T2/T3's graph view. |
| `topology/_drawer_not_found.html` | 404 drawer fragment for an unknown / cross-tenant node id. Same outer `aside#node-drawer` shape so the HTMX `outerHTML` swap remains semantically consistent. |

### HTMX wiring

* **Sort + filter on `/ui/topology`** — the filter form uses
  `hx-get="/ui/topology"` + `hx-target="#topology-table-body"` +
  `hx-trigger="input changed delay:300ms from:find input, change from:find select"`
  so a typed search character or a dropdown change triggers a single
  debounced fetch returning only the `<tbody>` partial.
  `hx-push-url="true"` updates the browser URL so a copy/paste
  reproduces the filtered view. Column headers use the same shape:
  the `href` is the next sort URL the `next_direction_for` Jinja
  closure computes.
* **Row "view" buttons on each row** — `hx-get="/ui/topology/node/{id}"`
  + `hx-target="#node-drawer"` + `hx-swap="outerHTML"` swaps the
  drawer fragment in place without a full-page reload.
* **HX-Request header branch** — the table handler reads
  `request.headers["hx-request"]` to decide between the full page
  and the fragment template. Per the
  [HTMX 2 reference](https://htmx.org/reference/#request_headers),
  HTMX sets `HX-Request: true` on every fetch its directives drive.

### Cross-tenant isolation

Every UI route reads `tenant_id` from the session-bound
`UISessionContext`; no query parameter or path segment ever carries a
tenant id. The substrate `list_nodes` and the local detail-route helpers
all start with `WHERE graph_node.tenant_id = :tenant_id` or its edge /
audit-log counterpart. A cross-tenant node id surfaces as 404, never as
a render of the other tenant's data — the tenant boundary is opaque to
the caller. The
[chassis smoke suite](../../backend/tests/test_ui_chassis_smoke.py) and
the [topology suite](../../backend/tests/test_ui_topology_table.py)
pin both invariants.

### Known limits

* **Parents column is a placeholder** — T1 ships the column header +
  empty cell so the table reserves the space; the immediate-parent
  projection wires when the substrate helper grows a `parents`
  projection or T3's dependents overlay lands.
* **Multi-row select is client-side only** — Alpine.js holds the
  selection set. T1 doesn't ship a bulk-action endpoint; the selection
  state is exposed for a future button to consume.

## Topology graph view (Task #881)

Initiative [#342](https://github.com/evoila/meho/issues/342) (G10.5
Topology UI), Task [#881](https://github.com/evoila/meho/issues/881)
(G10.5-T2) layers an interactive Cytoscape.js graph view on top of the
same `/ui/topology` path via the `?view=graph` branch. The same
`GET /ui/topology` handler serves both surfaces; one of three response
shapes is selected per request:

| Query | Header | Response |
| ----- | ------ | -------- |
| `view=table` (default) | _none_ | `topology/table.html` full page |
| `view=table` (default) | `HX-Request: true` | `topology/_table_rows.html` fragment |
| `view=graph` | _none_ | `topology/graph.html` full page (Cytoscape island) |

The graph view does not have an HTMX-fragment variant — layout
switches happen client-side via `cy.layout({name}).run()`, and filter
changes round-trip to the server as a full-page navigation so the URL
captures the active mode for copy/paste.

### Module layout

| Module | Purpose |
| ------ | ------- |
| `meho_backplane.ui.routes.topology.table` | `GET /ui/topology` handler. Branches on `view=` and delegates the `graph` branch to `topology.graph.render_graph`; the `table` branch keeps the T1 tabular rendering. |
| `meho_backplane.ui.routes.topology.graph` | The `?view=graph` render. Pulls nodes via the substrate `list_nodes` capped at 500 (`GRAPH_NODE_CAP`) and edges via a local SQLite-portable ORM query (the substrate `list_edges` relies on PG `jsonb_typeof` for its conflict predicate; the graph view does not need conflict info, so the local helper is the right factoring). Emits node + edge JSON as a `<script type="application/json">` data island the `topology-graph.js` controller reads on init. |

### Vendored JS

The graph view adds four vendored files alongside the chassis bundle
(`htmx`, `sse`, `alpine`, `cytoscape`, `daisyui`). All four are
SHA256-pinned in
[`backend/src/meho_backplane/ui/static/src/vendor/VENDOR.md`](../../backend/src/meho_backplane/ui/static/src/vendor/VENDOR.md).

| File | Library | Version | Notes |
| ---- | ------- | ------- | ----- |
| `layout-base.js` | layout-base | 2.0.1 | Shared layout primitive; exposes `window.layoutBase`. |
| `cose-base.js` | cose-base | 2.2.0 | Consumes `layoutBase`, exposes `window.coseBase`. |
| `cytoscape-cose-bilkent.js` | cytoscape-cose-bilkent | 4.1.0 | Default organic layout. Consumes `coseBase`; exposes `window.cytoscapeCoseBilkent`. |
| `cytoscape-dagre.js` | cytoscape-dagre | 3.0.0 | Hierarchical layout; bundles dagre internally (the 2.x line required a separate `dagre` vendored file). |

Script load order in `graph.html` is **load-bearing** (the UMD
wrappers consume globals exposed by their predecessors):

```
cytoscape.min.js → layout-base.js → cose-base.js
                → cytoscape-cose-bilkent.js
                → cytoscape-dagre.js
                → topology-graph.js   (the per-page controller)
```

All carry `defer` so they execute in document order after the HTML is
parsed — the chassis CSP posture (zero inline JS) is unchanged.

### Controllers

| File | Purpose |
| ---- | ------- |
| `static/src/app/topology-graph.js` | Cytoscape init for the graph view. Registers the two layout plugins once (`cytoscape.use(...)`), reads the elements + selected-id data islands, mounts `<div id="cy">`, wires node-tap → HTMX drawer swap + URL sync, and drives the `cy.layout(...).run()` switcher off the `<select id="topology-graph-layout">` change event. |
| `static/src/app/topology-table.js` | Cross-link helper for the tabular view. On `DOMContentLoaded` it finds any `<tr data-selected="true">` row (rendered by `_table_rows.html` when the route received `?selected=<id>`), scrolls it into view, and applies a brief outline pulse so the operator's eye catches it. No-op when no row matches. |

### Cross-link contract (table ↔ graph)

The `?selected=<uuid>` query param round-trips selection between the
two surfaces.

* **Graph → table**: `cy.on('tap', 'node', ...)` opens the drawer
  (HTMX swap of `/ui/topology/node/<id>` into `#node-drawer`),
  `history.replaceState`s `?selected=<id>` into the URL, and updates
  the header's "Show in table" link `href` so a click takes the
  operator to `/ui/topology?view=table&selected=<id>`. The table page
  marks the matching row `data-selected="true"`, and
  `topology-table.js` scrolls + highlights it.
* **Table → graph**: each row carries a "Graph" link to
  `/ui/topology?view=graph&selected=<id>`. The graph route emits the
  id into `#topology-graph-selected`; `topology-graph.js` reads it,
  centers the matching node on the first `layoutstop`, selects it,
  and opens the same drawer.

Both directions preserve active filters (`kind`, `q`) so the toggle
keeps operator state.

### 500-node render cap

Per Initiative #342 work item #6 ("Performance discipline.
Cytoscape handles ~1k nodes; v0.2 caps frontend-side rendering at
500 nodes"), `graph.GRAPH_NODE_CAP = 500` and the route passes
`limit=500` into `list_nodes`. When the returned list saturates the
cap, the page surfaces a truncation banner ("Capped at 500 — narrow
the filter or use the dependents query (T3) for larger sets"). Larger
sets reach the operator via T3 (#882)'s subgraph query.

### Templates

| Template | Purpose |
| -------- | ------- |
| `topology/graph.html` | Full-page Cytoscape surface. Extends `base.html`; renders the view toggle, filter bar, status row + layout `<select>`, the `<div id="cy">` mount, the `#node-drawer` slot (shared with the table view), and two `<script type="application/json">` data islands carrying the elements + selected-id payloads. |

### Cross-tenant isolation

Same posture as the tabular surface: every read goes through
`list_nodes` (substrate-level `WHERE graph_node.tenant_id = :tenant_id`
first clause) plus a local `_fetch_edges_for_nodes` that joins both
endpoints with explicit `tenant_id` predicates (defence-in-depth
matching the T1 drawer's `_fetch_edges` shape). Cross-tenant nodes /
edges cannot surface even if a future invariant violation introduced
one.

## Known issues / open items

- **`static/dist/tailwind.css` is missing on first uvicorn start in
  a fresh clone**. T5 (#866) added `ensure_static_dist_dir()` in
  the lifespan so the mount no longer crashes on construction, but
  the request for the compiled stylesheet still 404s until the
  operator runs `tailwindcss` once. Local dev must run
  `tailwindcss --watch` once before the rendered UI is fully styled.
- **CSP not configured yet**. The `<head>` deliberately avoids inline
  script so a future nonce-based CSP can land without template
  rewrites. T5 #866 / a follow-up Initiative wires the header.
- **No dark-mode toggle UI** even though DaisyUI's `dim` theme is
  enabled — out of scope per Initiative #337.

## Topology query overlays + 30s polling-refresh (Task #882)

Initiative [#342](https://github.com/evoila/meho/issues/342) (G10.5
Topology UI), Task [#882](https://github.com/evoila/meho/issues/882)
(G10.5-T3) layers dependents/dependencies subgraph + shortest-path
overlays on the existing `?view=graph` surface (T2 / #881) plus a
30-second HTMX polling refresh so the rendered view stays in sync
with the live graph without losing the operator's pan/zoom.

### URL contract (graph branch additions)

| Query | Render |
| --- | --- |
| `?view=graph&from=<name>[&from_kind=<kind>][&depth=N]` | Dependents subgraph rooted at `<name>` |
| `?view=graph&from=<name>&direction=dependencies[&from_kind=<kind>][&depth=N]` | Dependencies subgraph rooted at `<name>` |
| `?view=graph&from=A&to=B[&from_kind=...&to_kind=...&max_hops=N]` | Shortest path between `A` and `B` (highlighted edges) |

`depth` defaults to `3` (operator-friendly v0.2 value); `max_hops`
defaults to `8`. `direction` is dual-purpose by branch (sort
direction on the table, overlay direction on the graph) -- see
`backend/src/meho_backplane/ui/routes/topology/table.py` module
docstring for the resolution rules.

### Substrate split (UI-facing helpers, dialect-portable)

| Module | Responsibility |
| --- | --- |
| `meho_backplane.ui.routes.topology.queries` | `fetch_dependents_subgraph` / `fetch_dependencies_subgraph` -- bounded BFS over the closed neighbour set, returns `SubgraphResult(nodes, edges, truncated)`. Public `resolve_anchor` resolves `(tenant_id, name [, kind])` into a `GraphNode` row (raises `NodeNotFoundError` / `AmbiguousNodeError`). |
| `meho_backplane.ui.routes.topology.path_queries` | `fetch_path_subgraph` -- bidirectional BFS shortest-path. Returns `PathSubgraphResult(path_node_ids, nodes, edges, highlighted_edge_ids, total_hops)`. |
| `meho_backplane.ui.routes.topology.graph` | `render_graph` dispatcher: dispatches to `_render_full_inventory`, `_render_dependents_or_dependencies_overlay`, or `_render_path_overlay` based on `?from=` + `?to=` presence; serves either the full page or the HTMX data-island fragment (on `HX-Request`). |

The UI-facing helpers are intentionally separate from the substrate
`meho_backplane.topology.query.find_dependents` /
`find_dependencies` / `find_path` verbs:

1. **Dialect portability** -- the chassis unit-test fixture uses
   SQLite; the substrate verbs are PG-only (PG `WITH RECURSIVE ...
   CYCLE`). The UI helpers use plain SQLAlchemy ORM `select(...)`
   so the unit suite covers the route end-to-end.
2. **`tenant_id` + `db_session` shape** matches `list_nodes` rather
   than the substrate's `Operator` requirement.
3. **Subgraph emission** -- the substrate returns flat closure
   lists; Cytoscape needs the edges between visited nodes too.

The substrate verbs stay the source of truth for the REST + CLI +
MCP surfaces.

### Substrate-parity invariants

Every UI BFS module mirrors the substrate's edge-traversal
predicates so the same closure is reported on both surfaces:

* **Soft-delete (not filtered)** -- the BFS modules do *not* filter
  `last_seen`, matching the substrate's `_TRAVERSAL_SQL` (which also
  does not): a soft-deleted node stays reachable on both surfaces
  (last-refresh-wins; point-in-time visibility is the separate G9.3
  history/diff/timeline verbs, not a traversal filter). Regression
  test: `test_dependents_overlay_includes_soft_deleted_node`. (The BFS
  previously applied `last_seen IS NOT NULL` on the mistaken belief the
  substrate did too; #584 removed it to restore real parity. Note the
  *full-inventory* graph/table/drawer queries still exclude
  soft-deleted rows -- those mirror `list_nodes` / `list_edges`, which
  do filter, not the traversal verbs.)
* **Superseded-edge exclusion** -- edges carrying
  `properties->>'superseded_by' IS NOT NULL` are dropped from
  both the dependents/dependencies BFS and the bidirectional
  path BFS. Mirrors Initiative #364 §6 / Task #595 on the
  substrate. The portable SQLAlchemy idiom uses
  `or_(GraphEdge.properties["superseded_by"].is_(None),
  GraphEdge.properties["superseded_by"] == JSON.NULL)` because
  PG returns SQL NULL on a missing JSON key while SQLite's JSON1
  returns the JSON NULL token. Regression tests:
  `test_dependents_overlay_excludes_superseded_edges` /
  `test_path_overlay_excludes_superseded_edges`.

A divergence here is a substrate-vs-UI bug: an operator who
curates supersede annotations on the substrate REST/CLI API would
see edges in the UI overlay that the API hides, with no way to
reconcile.

### Tenant-scoping defense in depth

Every overlay edge query enforces the tenant boundary at three
points: the edge row itself + both endpoint joins (aliased
`from_alias` / `to_alias` on `GraphNode`). Same posture
`...graph._fetch_edges_for_nodes` and `...detail._fetch_edges`
established for the existing surfaces. The cross-tenant isolation
acceptance criterion (`test_dependents_overlay_isolates_other_tenants_graph`,
`test_path_overlay_isolates_other_tenants_graph`) covers each
overlay flavour.

### 30s polling-refresh wiring

The graph view's data-island wrapper carries:

```html
<div id="topology-graph-data-wrapper"
     hx-get="{{ refresh_url }}"
     hx-trigger="every 30s"
     hx-swap="outerHTML"
     hx-headers='{"X-CSRF-Token": "..."}'>
  <script type="application/json" id="topology-graph-data">[...]</script>
  <script type="application/json" id="topology-graph-selected">"..."</script>
  <script type="application/json" id="topology-graph-path-nodes">[...]</script>
</div>
```

On every 30s tick HTMX issues `GET /ui/topology?view=graph...`
with `HX-Request: true`; the route returns the
`topology/_graph_data_island.html` fragment only. The
`topology-graph.js` controller listens for `htmx:afterSwap` on the
wrapper, replaces the elements via `cy.batch(...)`, then re-runs
the active layout with `preserveViewport: true` (the second
argument to `layoutOptions(name, preserveViewport)`). Passing
`true` injects `fit: false` and `randomize: false` into the
layout options so:

* `cose-bilkent` does NOT zoom-to-fit on layout-stop (the default
  `fit: true` would override any synchronous pan/zoom restore and
  was the root cause of PR #1049's B2 finding); and
* node positions are NOT re-randomised every 30s, preserving the
  operator's mental map of the graph across refreshes.

Initial render and the user-driven layout-switcher pass
`preserveViewport: false` (re-fit + re-randomize) because both are
deliberate viewport-changing actions.

### Error surface

| Status | Reason | Template |
| --- | --- | --- |
| 404 | Unknown name (or kind-pinned anchor missing in this tenant) | `topology/_graph_overlay_error.html` (full page) / `topology/_graph_overlay_error_fragment.html` (HX-Request) |
| 409 | Ambiguous bare name resolves to multiple kinds | Same templates as 404 with a kind-disambiguation hint |

The error fragment keeps the polling trigger active so a transient
hard-delete + re-create recovers automatically on the next tick.

Both statuses are declared on the `GET /ui/topology`
`router.add_api_route(..., responses={404: ..., 409: ...})` so
the generated CLI client + downstream OpenAPI consumers see the
real response set rather than a 200/422-only contract. The
`direction` query param's OpenAPI schema carries
`pattern: ^(asc|desc|dependents|dependencies)$` (the union of
table-sort + graph-overlay values) so the same client generator
sees the dual-purpose contract instead of a free-form `string`.

### Tests

`backend/tests/test_ui_topology_queries.py` covers every acceptance
criterion: dependents/dependencies subgraph rendering,
depth-bounding, path overlay + highlighted edges (with ordered
path-id assertion), polling endpoint shape (HTMX fragment),
cross-tenant isolation (both overlay flavours), 404 unknown-name,
409 ambiguous-name, drawer cross-link to `?from=`, substrate-
parity superseded-edge exclusion on both overlay flavours, and
two source-anchored assertions on `topology-graph.js` (polling
viewport-preservation + `node.highlight` style rule). All pass
against the SQLite fixture.

## References

- v0.2 decisions [#9 / #10 / #11](../planning/v0.2-decisions.md).
- HTMX 2 — https://htmx.org/
- HTMX `hx-trigger="every Ns"` — https://htmx.org/attributes/hx-trigger/
- HTMX `htmx:afterSwap` event — https://htmx.org/events/#htmx:afterSwap
- Jinja2 — https://jinja.palletsprojects.com/
- Tailwind CSS 4 — https://tailwindcss.com/blog/tailwindcss-v4
- Tailwind 4 standalone CLI install — https://tailwindcss.com/docs/installation
- DaisyUI 5 install — https://daisyui.com/docs/install/
- Alpine.js — https://alpinejs.dev/
- Cytoscape.js — https://js.cytoscape.org/
- Cytoscape `cy.pan` / `cy.zoom` — https://js.cytoscape.org/#cy.pan
- Cytoscape selector classes — https://js.cytoscape.org/#selectors/class
- FastAPI `StaticFiles` (consumed by T5) — https://fastapi.tiangolo.com/tutorial/static-files/
- markdown-it-py — https://markdown-it-py.readthedocs.io/en/latest/
- pygments `HtmlFormatter` — https://pygments.org/docs/formatters/

## Memory surface (Task #877)

Initiative [#341](https://github.com/evoila/meho/issues/341) (G10.4
Memory UI), Task [#877](https://github.com/evoila/meho/issues/877)
(G10.4-T1) replaces the chassis stub at `/ui/memory` with the real
**scope-aware list** + **per-memory detail/edit view** + **delete with
confirm modal** + **tag autocomplete**. Sibling Tasks T2
([#878](https://github.com/evoila/meho/issues/878)) and T3
([#879](https://github.com/evoila/meho/issues/879)) layer create+promote
and expiry-viz+bulk on top of the same router.

### Routes

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/ui/memory` | List page or HTMX card-list fragment. Accepts `?scope=` (one of the five `MemoryScope` values or `"all"`) and `?tag=` (equal-match against `metadata.tags`). HTMX fragment when `HX-Request: true`. |
| `GET` | `/ui/memory/tags` | Tag-autocomplete datalist fragment. Returns `<option>` rows for the union of tags the operator can see, sorted, capped at 200. |
| `GET` | `/ui/memory/<scope>/<slug>` | Detail page (server-rendered Markdown body) or HTMX body fragment. |
| `GET` | `/ui/memory/<scope>/<slug>/edit` | HTMX edit-form fragment. 403 when RBAC denies the write. |
| `PATCH` | `/ui/memory/<scope>/<slug>` | Save the edited body (HTMX form post). Returns the re-rendered body view. CSRF-enforced. |
| `DELETE` | `/ui/memory/<scope>/<slug>` | Delete + re-render the card list with a flash banner. CSRF-enforced. |

### Module layout

* `backend/src/meho_backplane/ui/routes/memory/__init__.py` — exports
  `build_memory_router`.
* `backend/src/meho_backplane/ui/routes/memory/routes.py` — thin
  FastAPI handlers that resolve session + operator deps and delegate
  to the render helpers in `views`.
* `backend/src/meho_backplane/ui/routes/memory/views.py` — render
  functions + projection helpers (`render_index`, `render_detail`,
  `render_edit_form`, `patch_entry`, `delete_entry`, `render_tags`).
  Pulled out of `routes` so the render logic is unit-testable
  without an HTTP fixture and so each module fits the chassis-wide
  ~600-line cap.
* `backend/src/meho_backplane/ui/routes/memory/render.py` — Markdown
  → HTML renderer (`markdown-it-py` commonmark + `pygments`). Mirrors
  the precedent the KB UI sets in `kb/render.py`
  (G10.2-T1 #870); the two modules will dedupe once both PRs land on
  `main`.
* `backend/src/meho_backplane/ui/routes/memory/operator.py` —
  `resolve_ui_operator` FastAPI dependency that lifts a full
  `Operator` (carrying `tenant_role`) from the BFF session by
  re-verifying the stored access token. Used by every write handler.
  Read handlers use `build_read_operator` which synthesises an
  `OPERATOR`-role operator without a JWT round-trip (the read RBAC
  matrix only consults the per-row `user_sub`, never the role).
* `backend/src/meho_backplane/ui/templates/memory/` — Jinja2
  templates: `index.html` (full list page), `_cards.html` (HTMX list
  fragment), `detail.html` (full detail page), `_body_view.html` /
  `_body_edit.html` (HTMX swap targets on Edit / Save / Cancel),
  `_tags_options.html` (autocomplete datalist).

### Markdown rendering

`render_markdown` constructs a process-wide `MarkdownIt("commonmark",
{"html": False, "linkify": True, "highlight": _highlight_code})` and
enables `table` + `strikethrough`. The `html=False` override is
load-bearing — `markdown-it-py` 4.2.0's `commonmark` preset has
`html` defaulted to `True`, which would render raw `<script>` /
`<iframe>` in a memory body as live HTML. With `html=False`, raw HTML
is rendered as escaped text and Markdown structure (headings, links,
code blocks) still parses.

Code blocks pass through `pygments`'s `HtmlFormatter(nowrap=True,
cssclass="memory-code")` so each token is a bare `<span>` annotated
with a class; the highlight callback wraps the spans in
`<pre class="memory-code"><code class="language-{lang}">`. Unknown
languages fall back to `TextLexer` (no decoration) rather than
guessing.

`MarkdownIt.render` mutates internal parser state, so the singleton
is guarded by a `threading.Lock`. The lock-contention cost is
negligible compared to the surrounding DB read at realistic UI QPS.

### RBAC posture

Read paths (list / detail / tags) build a synthesised `Operator` with
`TenantRole.OPERATOR` and rely on `MemoryRbacResolver.can_read`'s
per-row `user_sub` gate for cross-user isolation. Write paths
(edit-form GET, PATCH, DELETE) re-verify the BFF session's access
token through the chassis JWT chain (`verify_jwt_for_audience`) to
produce a fully-validated `Operator` carrying the live `tenant_role`.
The matrix is re-checked at the service layer
(`MemoryService.remember` / `forget` call `can_write`); the
route-side check is for the UX "show / hide Edit button" decision
and a quick 403 on the edit-form GET so the operator doesn't load
the textarea for an action the save would reject anyway. The
edit-form GET surfaces RBAC denial as **403** (not 404, the read
posture) — mirroring `/api/v1/memory`'s write-side posture and
pinned by `test_edit_form_tenant_scoped_as_operator_returns_403`.

The 404-vs-403 collapse on detail / edit-form / PATCH / DELETE for
non-existent slugs is the info-leak avoidance the `/api/v1/memory`
surface holds: a caller cannot distinguish "no such memory" from
"you can't read it" by the response status. PATCH / DELETE on a
tenant-scoped row by an `operator` role do surface as 403 (the
matrix mismatch is honest feedback — the alternative would be a
silent no-op that audits worse).

### HTMX conventions

* `hx-get` for scope tabs + tag filter (idempotent reads).
* The tag datalist (`#memory-tag-options`) pins `hx-target="this"`
  on its `hx-trigger="load"` options fetch. htmx resolves
  `hx-target` closest-wins up the ancestor chain, so without the
  local override the datalist inherits the filter form's
  `hx-target="#memory-cards"` and the `<option>` fragment replaces
  the card grid on page load (#1695).
* `hx-patch` for the edit-in-place save; the form's
  `id="memory-body"` is the swap target so Save replaces the form
  with the rendered body view in place.
* `hx-delete` for the delete-with-confirm-modal; the modal's
  Confirm button swaps the full `<body>` with the re-rendered list
  page (`hx-target="body" hx-push-url="/ui/memory"`) so the
  operator lands on the list with the deleted row absent.
* The page-level `hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'`
  directive on `detail.html` echoes the chassis double-submit token
  on every HTMX request from that page; the index page sets the
  same directive so future state-changing actions on the list (T3's
  bulk delete) inherit the token without per-element wiring.
* `render_index` sets the `meho_csrf` cookie on the **full-page
  render only**, never on the HTMX card-list fragment (#1754). The
  `_cards.html` wrapper polls the same handler every 60s
  (`hx-trigger="every 60s"`); minting + `Set-Cookie`-ing a fresh
  token on each poll rotated the cookie out from under any open
  create modal, whose render-time echo (#1693) then failed the
  middleware's cookie/header match and 403'd the create POST. The
  poll now **reuses** the token carried by the request's existing
  `meho_csrf` cookie (validated via `verify_csrf_token`, so a
  tampered value is never echoed back) and leaves the cookie
  untouched, so the modal's token — and the cards fragment's own
  bulk-action echo — stay valid across polls. A fragment fetched
  with no prior cookie (defensive: no full-page load happened first)
  falls back to a fresh mint + `Set-Cookie`. This mirrors the
  inline-refresh path's zero-`Set-Cookie` posture (G0.25 #1694) that
  keeps in-flight pages' CSRF tokens stable across a token rotation.

### Tests

The full suite lives in
`backend/tests/test_ui_memory_list.py`. It pins:

* the auth boundary (unauthenticated requests 302 to the BFF login),
* the list view (empty inventory + populated cards with scope badge
  + 200-char preview + tag chips + scope tab + tag filter +
  `/ui/memory/tags` autocomplete),
* the detail view (Markdown → HTML rendering, raw `<script>` stripped
  to escaped text, 404 on missing slug, cross-user 404 on another
  operator's user-scoped slug, cross-tenant 404 on another tenant's
  tenant-scoped slug),
* the edit-in-place flow (textarea fragment renders for own
  user-scoped, 403 for tenant-scoped under `operator` role, textarea
  for tenant-scoped under `tenant_admin`, PATCH save persists +
  returns the rendered body view, empty body 422),
* the delete flow (DELETE removes the row + re-renders the empty
  list with a flash banner, 403 on tenant-scoped under `operator`,
  404 on missing slug),
* the stub-retirement (the chassis "Coming soon" stub no longer
  renders for `/ui/memory`).

The PATCH happy-path mocks `meho_backplane.retrieval.indexer.get_embedding_service`
because `MemoryService.remember`'s re-index path calls `encode_one`
on the new body. Read paths bypass embedding entirely.

## Memory create modal + scope-promotion (Task #878)

Initiative [#341](https://github.com/evoila/meho/issues/341) (G10.4
Memory UI), Task [#878](https://github.com/evoila/meho/issues/878)
(G10.4-T2) layers the **create modal** + the **scope-promotion flow**
onto the T1 router. The "+" button on `/ui/memory` opens an
HTMX-loaded `<dialog>` with an RBAC-filtered scope selector, slug
input (optional, auto-generated when blank), Markdown body
textarea + debounced server-side preview, expiry picker, and a
comma-separated tags input. Submit calls `MemoryService.remember`
and HTMX redirects back to the list. The detail-page Promote
button (rendered only when the source scope has at least one legal
ladder step) opens a second HTMX-loaded modal and submits through
the same G5.2 `MemoryService.promote` the REST surface uses; the
chassis `AuditMiddleware` writes one `memory.promote` audit row
per request because the handler binds `operator_sub` + `tenant_id`
+ `audit_op_id` + `audit_op_class` + `audit_scope` + `audit_slug`
+ `audit_promotion_target_scope` to the structlog contextvars
before calling the service.

### Routes

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/ui/memory/create` | HTMX-loaded create modal fragment. Scope selector filtered to scopes the operator can write to via `MemoryRbacResolver.can_write`. |
| `POST` | `/ui/memory/create` | Submit handler. Form-encoded body shape mirrors `/api/v1/memory`'s `RememberBody`. Blank `expires_at` runs through the shared #624 default-TTL resolver (see "Default TTL on create" below). Returns 204 + `HX-Redirect: /ui/memory`. |
| `POST` | `/ui/memory/preview` | Debounced server-side Markdown preview. Returns the `_body_preview.html` fragment; same `<article>` shape `_body_view.html` uses on the detail page so styling matches. |
| `GET` | `/ui/memory/<scope>/<slug>/promote` | HTMX-loaded promote modal. Legal targets derived from `PROMOTE_TARGETS_BY_SOURCE`; terminal scopes (`tenant` / `target`) return 400. |
| `POST` | `/ui/memory/<scope>/<slug>/promote` | Submit handler. Calls G5.2's `MemoryService.promote`. Returns 204 + `HX-Redirect: /ui/memory/<target-scope>/<slug>`. |

### Module layout (additions to T1)

* `backend/src/meho_backplane/ui/routes/memory/create.py` —
  render + submit helpers for the create modal
  (`render_create_modal`, `render_body_preview`, `create_entry`).
  Split out of `views.py` so each module stays under the
  chassis-wide ~600-line cap.
* `backend/src/meho_backplane/ui/routes/memory/promote.py` —
  render + submit helpers for the scope-promotion modal
  (`render_promote_modal`, `promote_entry`, `_map_promote_error`).
  Sibling of `create.py`; both call into G5.2's `MemoryService`.
* `backend/src/meho_backplane/ui/routes/memory/_modal_shared.py` —
  shared helpers + constants for both modals: scope-selector
  vocabulary (`writable_scopes_for`, `scope_label`), the
  double-submit CSRF cookie set (`set_csrf_cookie`,
  `build_common_template_context`), the form-encoded tags parser
  (`parse_tags`), and the form-field sizing constants
  (`TAGS_MAX_LENGTH`, etc.).
* `backend/src/meho_backplane/ui/routes/memory/routes.py` — adds
  `_register_static_prefix_routes` which registers `/ui/memory/create`
  + `/ui/memory/preview` (+ T1's `/ui/memory/tags`) ahead of the
  parametrised `/ui/memory/{scope}/{slug}` routes. Registration
  order is load-bearing: a request to `/ui/memory/create` would
  otherwise be matched by `/ui/memory/{scope}/{slug}` with
  `scope="create"`, producing a 422 from the `MemoryScope` enum.
* `backend/src/meho_backplane/ui/templates/memory/_create_modal.html` —
  the HTMX-loaded create modal with the RBAC-filtered scope selector,
  Markdown textarea with debounced preview wiring, and a tiny inline
  script that toggles the `target_name` row visibility based on the
  selected scope. Empty-state renders an alert when
  `writable_scopes_for(operator)` is empty (read-only role).
* `backend/src/meho_backplane/ui/templates/memory/_promote_modal.html` —
  the HTMX-loaded promote modal. Legal targets are rendered as a
  `<select>`; the same inline-script pattern as the create modal
  toggles `target_name` when the selected target is
  target-flavoured.
* `backend/src/meho_backplane/ui/templates/memory/_body_preview.html` —
  Markdown-rendered preview fragment returned by `POST /ui/memory/preview`.
* `backend/src/meho_backplane/ui/templates/memory/index.html` — adds
  the "+" Create button (`hx-get="/ui/memory/create"`) and a stable
  `#memory-modal-container` mount point that persists across HTMX
  swaps.
* `backend/src/meho_backplane/ui/templates/memory/detail.html` — adds
  the Promote button (only rendered for non-terminal source scopes)
  and a second `#memory-modal-container` mount point.

### HTMX modal conventions

* Modals are loaded into a stable `#memory-modal-container` mount
  point via `hx-get` with `hx-target="#memory-modal-container"
  hx-swap="innerHTML"`. The inserted `<dialog>` element carries the
  `modal-open` class (DaisyUI v5) so it opens immediately without a
  client-side `showModal()` call.
* Submit forms inside the modals use `hx-post` with `hx-target="this"
  hx-swap="none"` — the 204 + `HX-Redirect` response shape means
  HTMX navigates the whole page rather than swapping the form into a
  rendered fragment. Same convention T1's delete-confirm modal uses.
* Form encoding is `application/x-www-form-urlencoded`. The chassis
  `CSRFMiddleware` accepts the double-submit token from either the
  `X-CSRF-Token` header or the `csrf_token` form field. The create
  form declares its own `hx-headers` echo of the token its render
  minted (#1693): each modal GET re-mints + re-sets the `meho_csrf`
  cookie, so the token inherited from the page-level directive is
  stale by the time the operator submits. htmx 2 does inherit
  `hx-headers`, and a child declaration overrides a parent one
  (https://htmx.org/attributes/hx-headers/), so the form-level echo
  keeps the double-submit pair aligned — and the body textarea's
  debounced preview `hx-post` inherits the fresh token from the form.
  (The cookie-rotation half of the same desync class — a background
  list poll rotating the cookie *after* the modal renders — is fixed
  separately in `render_index`; see the T1 "HTMX conventions" note on
  #1754.)
* The create form surfaces a failed submit instead of dropping it.
  Because the form posts with `hx-swap="none"`, a non-2xx response —
  most importantly the chassis CSRF middleware's
  `403 {"detail":"csrf_token_invalid"}` — would otherwise be
  swallowed silently (the operator clicks Create and nothing visibly
  happens). The form carries
  `hx-on::response-error="window.memoryCreateShowError(this, event)"`
  (htmx 2.x double-colon shorthand for the `htmx:responseError`
  event) which un-hides an `alert alert-error` banner above the form
  fields with a status-tailored message read off
  `event.detail.xhr.status` — a 403 reads as an expired session
  token with a retry hint, a network error (`status === 0`) as a
  connectivity message, any other status surfaces the server's
  `detail` string. The form body is never swapped, so the operator's
  input survives for an immediate retry; `hx-on::before-request`
  hides the banner so a retry starts clean (#1754).
* `hx-trigger="keyup changed delay:300ms"` on the create modal's
  body textarea drives the debounced server-side preview — see
  https://htmx.org/attributes/hx-trigger/. `delay:300ms` matches
  the convention T1's tag-filter input uses for the list page's
  type-ahead.
* The create + promote modals each carry a tiny inline `<script>`
  that toggles the `target_name` field's visibility based on the
  selected scope. Pulled inline (not into a separate `<script src>`)
  because the modal is HTMX-injected and a fresh request for the
  external JS file would race the `<dialog>` insertion. The script
  reads the target-scoped scope values off a `data-target-scoped-values`
  attribute on the form so the enum is not re-hardcoded on the
  client.

### Default TTL on create (#1697)

The create submit handler routes the parsed `expires_at` form value
through the shared G5.2-T2 (#624) resolver
(`memory/ttl.py:resolve_default_expires_at`) before calling
`create_entry` — the same seam the REST `remember` route and the MCP
`add_to_memory` tool consume, so the form's "Leave blank to use the
scope's default TTL" hint is backed by the one canonical policy: a
blank picker on a `user`-scope create injects `now(UTC) +
Settings.memory_user_default_ttl_days` (default 7 days), non-`user`
scopes persist `expires_at = null`, and an explicit timestamp is
honoured verbatim.

The surface-native "field was set" discriminant is `expires_at is
not None` on the parsed form value: a blank
`<input type="datetime-local">` submits `expires_at=` (empty string),
and FastAPI coerces that to `None` exactly like an absent field
(verified against FastAPI 0.136.3). A browser form cannot express
the REST surface's explicit-`null` opt-out (the CLI `--persist`
shape), so set-vs-unset collapses to datetime-vs-`None` here. See
`docs/codebase/memory.md` ("Write path — default-TTL contract") for
the cross-surface discriminant table.

### Idempotency + audit trail

`MemoryService.promote` is idempotent (G5.2 contract): a re-promotion
to the same target returns the existing target row, no insert, no
`promote_target_conflict` 409. The UI mirrors that contract — a
double-click on Promote redirects to the same URL twice; the second
audit row reflects a successful repeat (status 204) and the row
count in the target scope stays at one.

The promote handler explicitly binds `operator_sub` + `tenant_id`
+ `audit_op_id="memory.promote"` + `audit_op_class="write"` +
`audit_scope` + `audit_slug` + `audit_promotion_target_scope` to the
structlog contextvars before calling the service so the chassis
`AuditMiddleware` writes the audit row with the canonical op id.
Without this binding the UI session middleware leaves the contextvars
unset and the chassis middleware skips the audit write — the T1 read
+ edit + delete handlers run without an audit row today for the same
reason. T2's create handler also binds `audit_op_id="memory.remember"`
+ `audit_op_class="write"` for parity with the `/api/v1/memory` POST
route, so any future audit-query consumer can correlate a UI-driven
write with the same op-id literal a CLI / MCP-driven write produces.

### Tests

The full suite lives in
`backend/tests/test_ui_memory_create_promote.py`. It pins:

* the create-modal render (RBAC-filtered scope selector for
  operator, tenant-admin sees TENANT, read-only sees the empty state),
* the create submit (persists the row + HX-Redirects to the list,
  blank slug auto-generates, tenant scope as operator 403s, empty
  body 422s, target-scoped without `target_name` 422s, missing CSRF
  cookie 403s, and the real modal cookie/header pair round-trips to
  204 — the #1693 desync regression),
* the create-submit default-TTL injection (#1697: blank `expires_at`
  on a USER-scope create persists `metadata.expires_at ≈ now + 7d`,
  blank on TENANT scope persists `null`, an explicit
  `datetime-local` timestamp persists verbatim — asserted off the
  persisted row's `doc_metadata`),
* the Markdown preview (renders Markdown to HTML, empty body returns
  the placeholder, raw `<script>` escapes),
* the promote-modal render (USER source lists USER_TENANT +
  USER_TARGET; terminal TENANT source 400s; cross-user 404),
* the promote submit (USER -> USER_TENANT persists the target +
  HX-Redirects to the new detail page; the chassis audit row commits
  with op id `memory.promote` and the right payload fields; re-promote
  is idempotent at the row count; operator -> TENANT 403s with
  `insufficient_promotion_authority`; tenant_admin USER_TENANT ->
  TENANT succeeds; cross-ladder USER -> TENANT 400s; cross-user 404;
  cross-tenant 404),
* the UI integration (list page renders the Create button + modal
  container; detail page renders the Promote button only for
  non-terminal source scopes).

The create + promote tests stub the embedding service for the same
reason the T1 PATCH test does — `index_document` (called by both
`MemoryService.remember` and `MemoryService.promote`) computes a
new embedding for the inserted row.

### Expiry visualisation + bulk actions (Task #879)

Initiative [#341](https://github.com/evoila/meho/issues/341) (G10.4
Memory UI), Task [#879](https://github.com/evoila/meho/issues/879)
(G10.4-T3) layers two surfaces on top of T1's list:

* **Server-rendered countdown badges** — every memory with
  `expires_at` shows an `"expires in 3d 4h"` cue, formatted by
  `bulk.format_countdown`. The cards block wrapper carries
  `hx-trigger="every 60s"` (mirrors topology graph's poll at #882's
  30-second cadence) so the badge re-renders without a client-side
  timer. The refresh URL preserves the active scope + tag so a poll
  mid-page-stay stays aligned with the operator's filter state.
* **Recently expired section** — expired-but-unswept rows render in a
  greyed `<ul>` below the active cards. The bucket is naturally
  bounded by the G5.2 sweeper window
  ([#623](https://github.com/evoila/meho/issues/623)) — between
  expiry and the next sweeper tick (default 24 h via
  `memory_expiry_tick_interval_seconds`), expired rows are still in
  the documents table. `MemoryService.list_memories` is called with
  `include_expired=True` and the partition runs in
  `bulk.partition_expired`.
* **Bulk select + actions** — writable cards carry a checkbox (form
  association via the HTML5 `form="memory-bulk-form"` attribute so
  the checkbox participates in the bulk form regardless of DOM
  nesting); the toolbar posts the selected `Document.id` UUIDs to
  `POST /ui/memory/bulk` via HTMX. Two actions: `delete` (calls
  `MemoryService.forget` per row) and `extend` (calls
  `MemoryService.remember` with the existing body + a fresh
  `expires_at = now + duration`; durations are pre-canned at 1d /
  7d / 30d to prevent "extend by 10 years" footguns).

#### Bulk RBAC posture

The route resolves IDs to entries via a tenant-scoped + `source='memory'`
filter so an operator can't smuggle a knowledge-base or audit-row UUID
through the form. Per row, `MemoryRbacResolver.can_write` is re-checked
before dispatch; the service's own `can_write` re-check still runs
inside `forget` / `remember`. The route-side check exists so the
result counts (`succeeded` / `denied` / `missing`) are honest in the
flash banner.

The flash message follows the shape `"Bulk: N deleted, M denied
(RBAC), K not found."` with the denied / not-found terms suppressed
when their count is zero. Cross-tenant IDs fall silently into the
`missing` bucket — the row is real in another tenant, but invisible
to this operator.

#### Routes (T3 additions)

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/ui/memory/bulk` | Bulk delete or bulk extend-expiry. Form fields: `action` (`delete` \| `extend`), `ids` (multi-value `Document.id`), `extend_duration` (one of `1d` / `7d` / `30d`, required when `action=extend`), `scope` + `tag` (echoed back so the post-bulk render preserves the operator's filter state). CSRF-enforced. Returns the re-rendered `_cards.html` partial with a flash banner. |

#### Files

* `backend/src/meho_backplane/ui/routes/memory/bulk.py` — countdown
  formatter, partition helper, form parsers, and `apply_bulk_action`.
  Pulled out of `views.py` so the T3 code lands in one cohesive
  module without growing the T1 render file past its cap.
* `backend/src/meho_backplane/ui/routes/memory/views.py` (extended) —
  `render_index` partitions the entries and passes the active +
  recently-expired buckets to the template; `render_bulk_action`
  dispatches the bulk handler.
* `backend/src/meho_backplane/ui/routes/memory/routes.py` (extended) —
  registers `POST /ui/memory/bulk` ahead of the parameterised
  PATCH/DELETE so the literal path segment is unambiguous.
* `backend/src/meho_backplane/ui/templates/memory/_cards.html`
  (extended) — countdown badge, checkbox column on writable rows,
  bulk-action toolbar, recently-expired section, and the
  `hx-trigger="every 60s"` poll attribute on the wrapper.
* `backend/tests/test_ui_memory_expiry_bulk.py` — countdown
  formatting, partition split, parser guards, badge + recently-
  expired rendering, bulk delete + extend, RBAC denial path,
  cross-tenant safety, CSRF gate.

## Connectors surface (Task #873)

Initiative #340 (G10.3 Connectors + Targets UI). Task #873 (T1)
ships the **read** surface — the targets list + per-target detail
page. T2 (#874) layers create / edit forms; T3 (#875) layers bulk
import. The connectors stub registered in T5 #866 is retired by
this task — the real router is included ahead of the chassis stubs
the same way broadcast / topology / memory retired theirs.

### Routes

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/ui/connectors` | Sortable + filterable targets list. URL: `?sort=name|product|host|last_probed_at|status&dir=asc|desc&product=<slug>`. Full-page render or `_table_rows.html` fragment (HX-Request branch). |
| `GET` | `/ui/connectors/{name}` | Per-target detail. Renders properties + fingerprint card + recent-ops card (SSE-live) + available-operations matrix. Alias-aware target resolution via `resolve_target`. |
| `POST` | `/ui/connectors/{name}/probe` | Re-probe action. Tenant_admin RBAC gated server-side (the template hides the button optimistically; the handler is the authority). Persists the new fingerprint and swaps the refreshed `_fingerprint_card.html` fragment. |
| `GET` | `/ui/connectors/create` | (T2 #874) HTMX-loaded create-target modal. Tenant_admin gated. Product `<select>` server-rendered from `registered_product_tokens()`; auth_model `<select>` from the `AuthModel` enum. |
| `POST` | `/ui/connectors/create` | (T2 #874) Create submit. Builds a `TargetCreate` and delegates to the REST `create_target` handler in-process. Success → 204 + `HX-Redirect: /ui/connectors`; validation failure → 422 + modal re-rendered with per-field errors. |
| `GET` | `/ui/connectors/{name}/edit` | (T2 #874) HTMX-loaded edit-target modal, pre-populated server-side from the resolved target. Tenant_admin gated; alias-aware 404 on cross-tenant. |
| `PATCH` | `/ui/connectors/{name}` | (T2 #874) Edit submit. Builds a `TargetUpdate` and delegates to the REST `update_target` handler in-process. Same success / failure shapes as create. |
| `GET` | `/ui/connectors/{name}/delete` | (G0.15-T10 #1218) HTMX-loaded delete-confirm modal. Tenant_admin gated; pre-checks the `graph_node.target_id` cascade count so the modal surfaces the impact and pre-sets `?force=true` on the submit URL when refs exist (mirrors the REST 409+force flow). |
| `POST` | `/ui/connectors/{name}/delete` | (G0.15-T10 #1218) Delete submit. Delegates to the REST `delete_target` handler in-process — same soft-delete (`deleted_at` stamp) + cascade-count + audit code path. Success → 204 + `HX-Redirect: /ui/connectors`. |
| `GET` | `/ui/connectors/import` | (T3 #875) Full bulk-import page: paste box + file upload. Tenant_admin gated. |
| `POST` | `/ui/connectors/import` | (T3 #875) Parse the pasted / uploaded `targets.yaml` (`yaml.safe_load`) and render the CREATE-vs-UPDATE preview table; parse errors render inline (422, no 500). Read-only — no writes. |
| `POST` | `/ui/connectors/import/confirm` | (T3 #875) Re-parse + re-classify against the tenant's current targets, validate the whole plan up front (schema-invalid entry → inline 422, no partial write), then apply it in-process via `create_target` (new) + `update_target` (existing); renders the result summary (N created, M updated). |

### Module layout

* `backend/src/meho_backplane/ui/routes/connectors/__init__.py` — the umbrella `build_router()` aggregating the list / detail / probe routers in registration order.
* `backend/src/meho_backplane/ui/routes/connectors/list_view.py` — the list handler. Sort enum, direction enum, status freshness classifier (24 h threshold), distinct-products query for the filter dropdown, Python-side re-sort layered on top of the SQL `ORDER BY` so status sorts honour the `never < stale < ok` ladder.
* `backend/src/meho_backplane/ui/routes/connectors/detail.py` — the detail handler. Resolves the target via `targets.resolver.resolve_target`, projects it via `_project_target` (shared with probe), resolves the connector via `resolve_connector_or_label` (the same helper `/api/v1/targets/{name}/probe` uses), loads the operations matrix grouped by `operation_group` with the same tenant scoping shape `list_operation_groups` uses, loads the last 10 audit rows for the target. SSE bridge URL is the existing `/ui/broadcast/stream?target=<name>` (G10.1 already supports the `target` filter; piggy-backing keeps the SSE plumbing single-sourced).
* `backend/src/meho_backplane/ui/routes/connectors/operator.py` — `resolve_role_probe` (fails soft to `is_tenant_admin=False` on any JWT hiccup so a transient JWKS outage doesn't 5xx the read surface) + `resolve_operator_or_403` (the rigorous write-gate dep used by the probe handler).
* `backend/src/meho_backplane/ui/routes/connectors/probe.py` — the re-probe POST handler. Uses the same `resolve_connector_or_label` path the REST `/api/v1/targets/{name}/probe` route uses so the two surfaces stay byte-compatible. Maps `no_connector` → 501 + alert fragment, `ambiguous_connector` → 409 + alert fragment.

### Connector resolution

The detail page's available-operations matrix and the re-probe action both consume `resolve_connector_or_label(target)` — the same helper the `/api/v1/targets/{name}/probe` REST route uses (G0.14-T1 #1142). The detail page then resolves the matching v2-registry entry to produce the canonical `"<impl_id>-<version>"` `connector_id` the operations meta-tool query reads (`list_operation_groups`'s shape). When the registry has the chosen class under multiple keys (the K8s pattern, both a v1 wildcard and a v2 versioned key), the detail page picks the versioned key — same precedence the G0.6 dispatcher's lookup uses.

### SSE wiring for recent ops

The recent-ops card on the detail page is live-driven by the existing G10.1 broadcast SSE bridge: the card embeds `sse-connect="/ui/broadcast/stream?target=<urlencoded-name>"` plus `sse-swap="broadcast"`. An Alpine controller (`connectorsRecentOps`, `backend/src/meho_backplane/ui/static/src/app/connectors-feed.js`) hooks `htmx:sse-before-message`, parses each `event: broadcast` frame's JSON, and prepends a row to its bounded `events` array (cap 50; older rows trim). The card is initially seeded by the server-rendered last-10 audit rows so the page is useful even before any new event streams in.

### RBAC posture for re-probe

The re-probe button is rendered only when the page-load role probe returned `is_tenant_admin=True`. The button hides for operators who can't use it. The server-side authority is `resolve_operator_or_403` on the POST handler — a crafted POST hitting the endpoint with a stolen / forged form still hits the 403 gate. The probe handler also re-validates the target tenant-scoped via `resolve_target` so a cross-tenant target name never resolves on the write surface.

### Cross-tenant isolation

Every list / detail / probe path filters on `session_ctx.tenant_id`:

* List: `WHERE targets.tenant_id = :tenant_id` is the first clause in the substrate query.
* Detail: `resolve_target` raises 404 on a cross-tenant name.
* Probe: same `resolve_target` gate before the write.
* Recent ops: `WHERE audit_log.tenant_id = :tenant_id AND audit_log.target_id = :target_id` — defense in depth even though the soft-FK on `audit_log.target_id` makes a cross-tenant target_id structurally impossible.
* Ops matrix: `(operation_group.tenant_id IS NULL OR operation_group.tenant_id = :tenant_id)` — same shape `list_operation_groups` uses for the agent surface.
* Recent-ops SSE: the underlying `/ui/broadcast/stream` bridge takes the tenant from the session row, never from a query parameter, so the per-target filter cannot leak across tenants.

### Create / edit forms (Task #874)

T2 layers the **write** surface on the T1 read surface. The DaisyUI
modal forms HTMX-submit into the REST `POST` / `PATCH`
`/api/v1/targets` *handlers* in-process (`create_target` /
`update_target` imported directly), so the UI and REST surfaces share
one validation + product-registry-check + audit-binding code path —
the same posture T1's re-probe handler uses by sharing
`resolve_connector_or_label` with the REST probe route. The
form-field strings are fed into `TargetCreate` / `TargetUpdate`, so
Pydantic runs the identical coercion + validation the REST body runs
(port 1-65535, name `min_length=1`, `auth_model` enum membership).

* **Success** → the handler returns 204 + `HX-Redirect: /ui/connectors`
  (the canonical HTMX post-mutation navigation); the list re-renders
  with the new / edited row.
* **Validation failure** → the handler catches the `ValidationError`,
  projects it to a `{field → message}` map, and re-renders the modal
  fragment (422) with the messages under each field and the operator's
  typed values echoed back. The form targets itself
  (`hx-target="this"`, `hx-swap="outerHTML"`) so HTMX swaps the
  errored form in place without losing input.

Client-side validation (HTML5 `required` / `minlength` / numeric
`min`/`max` with DaisyUI `input-error` styling) gives immediate
feedback; the server-side Pydantic pass is authoritative.

**Product dropdown source.** The create dropdown is rendered from
`registered_product_tokens()` — the canonical set `create_target`
validates `product` against — rather than from the raw
`GET /api/v1/connectors` ingest list, so a selectable product is
always an acceptable product (no dropdown / validator drift, no
surprise 422 on a product the operator could pick).

**RBAC posture.** All four routes depend on `resolve_operator_or_403`
(the same rigorous tenant_admin write-gate T1's probe uses): a
non-admin GET (modal) or POST/PATCH (submit) hits 403 server-side. The
list / detail templates additionally hide the "Create target" / "Edit"
buttons from non-admins (UX); the hide is not the security boundary.
Because the in-process REST handler's own `Depends(require_role(...))`
is not re-run on a direct function call, the 403 gate lives on the UI
route deps and the lifted `Operator` is passed into the handler so it
runs under the caller's tenant scope.

**Cross-tenant isolation.** The edit modal + PATCH resolve the target
via `resolve_target(db_session, session_ctx.tenant_id, name)` → 404 on
a cross-tenant or unknown name, so a tenant_admin in tenant B can
neither load nor PATCH tenant A's target. Create writes
`tenant_id = operator.tenant_id` (the REST handler never trusts a
body-supplied tenant).

**CSRF.** `POST` / `PATCH` under `/ui/` are gated by the chassis
`CSRFMiddleware` (signed double-submit) before the handler runs. The
modal form inherits the `X-CSRF-Token` header from the page-level
`hx-headers` directive; each modal render re-mints + re-sets the
`meho_csrf` cookie so the double-submit pair lines up.

### Detail page UX fixes (G0.15-T10 #1218 — v0.7.0 dogfood signal #6 closure follow-up)

`claude-rdc-hetzner-dc#753` flagged three UX clusters on the connector
detail surface that didn't exist when v0.7.0 cut:

1. **"Re-probe vs PATCH vs DELETE" verb confusion on the
   `no_connector` resolver verdict.** Two visually-identical cases —
   "fingerprint not cached yet" and "product slug doesn't match any
   registered connector" — both rendered the same "Re-probe" call-to-
   action, but only the first case is actually fixable by re-probing.
   Re-probing the second case dispatches through the same resolver
   with the same `(product, version)` tuple and fails the same way.
   The fix at `detail.py:_classify_no_connector_cause` classifies the
   verdict into `missing_fingerprint` (target's product **is**
   registered, but `fingerprint IS NULL` — Re-probe is the right verb)
   vs `product_mismatch` (target's product slug is **not** in the
   `registered_product_tokens()` set — Edit-product or Delete is the
   right verb). The template branches on the cause and surfaces the
   `valid_products` enum (the same source `TargetCreate.product`
   validates against — G0.14-T3 #1166) when the cause is
   `product_mismatch`, so the operator knows what values are
   acceptable for a PATCH.

2. **No Delete button on the detail page.** The REST DELETE shipped
   at v0.7.0 (G0.14-T4 #1145) but the UI didn't expose it; an
   operator hitting the unrecoverable `product_mismatch` case in
   cluster 1 had to drop into CLI / REST to recover. The fix adds a
   `Delete` button top-right of the detail page alongside `Edit`
   (tenant_admin-gated, same server-side `resolve_operator_or_403`
   posture as Edit + Re-probe). Clicking it loads a confirm modal
   (`_delete_modal.html`) via `GET /ui/connectors/{name}/delete` that
   pre-checks the `graph_node.target_id` cascade count. When the
   count is non-zero, the modal surfaces the impact ("N topology rows
   reference this target — they survive the delete with target_id =
   NULL per the ON DELETE SET NULL FK") and pre-sets `?force=true` on
   the submit URL so the typical confirmed-by-the-operator submit
   lands a clean 204 rather than the REST 409+force handshake. Submit
   delegates to `delete_target` in-process — same soft-delete
   (`deleted_at` stamp), same cascade-count, same audit row
   (`op_id='targets.delete'`) the REST surface writes.

3. **Connectors-vs-Targets taxonomic drift.** The sidebar /
   page-title / page-subtitle used "Connectors" while the URL +
   backend table is `targets`; the listed entities are **targets**
   (per-tenant deployed instances) not **connectors** (typed connector
   classes like `k8s-1.x`, `vault-1.x`). Picked Option B from the
   issue body — the lower-friction split: the sidebar label and URL
   path stay `Connectors` / `/ui/connectors` (parity, no route
   rename), the list-page `<h1>` + the detail-page breadcrumb +
   page-title use "Targets" instead. An operator scanning the list
   page now reads "Targets" and clicking into a row sees the
   breadcrumb "Targets / `<name>`" — matching what they actually
   manipulate. The two-page split (separate `/ui/connectors` catalog
   page for the actual connector classes) is left as future scope.

### Files

* `backend/src/meho_backplane/ui/routes/connectors/__init__.py` — umbrella router factory.
* `backend/src/meho_backplane/ui/routes/connectors/list_view.py` — list view handler + freshness classifier (T2 adds the `is_tenant_admin` role probe for the create button).
* `backend/src/meho_backplane/ui/routes/connectors/detail.py` — detail handler + connector_id resolution + ops matrix query + recent-ops query.
* `backend/src/meho_backplane/ui/routes/connectors/operator.py` — role-probe (read) + operator-403 (write).
* `backend/src/meho_backplane/ui/routes/connectors/probe.py` — re-probe POST handler.
* `backend/src/meho_backplane/ui/routes/connectors/forms.py` — (T2 #874) create / edit render + submit helpers; builds `TargetCreate` / `TargetUpdate`, delegates to the REST handlers, maps `ValidationError` to per-field form errors. (G0.15-T10 #1218) adds `render_delete_modal` + `submit_delete` helpers delegating to `delete_target`.
* `backend/src/meho_backplane/ui/routes/connectors/forms_router.py` — (T2 #874) create / edit route registration (thin FastAPI wrappers; tenant_admin-gated deps). (G0.15-T10 #1218) adds `GET`/`POST` `/ui/connectors/{name}/delete` routes.
* `backend/src/meho_backplane/ui/templates/connectors/_target_form_fields.html` — (T2 #874) shared form fields for both modals.
* `backend/src/meho_backplane/ui/templates/connectors/_create_modal.html` — (T2 #874) create modal.
* `backend/src/meho_backplane/ui/templates/connectors/_edit_modal.html` — (T2 #874) edit modal.
* `backend/src/meho_backplane/ui/templates/connectors/_delete_modal.html` — (G0.15-T10 #1218) delete-confirm modal; shows the cascade-count warning when `graph_node.target_id` references exist + pre-sets `?force=true` on the submit URL in that case.
* `backend/tests/test_ui_connectors_forms.py` — (T2 #874) create / edit form tests: modal render (admin) + 403 (operator), create success + Pydantic validation errors (port out of range, empty name), edit pre-population + PATCH, cross-tenant isolation, CSRF enforcement. (G0.15-T10 #1218) adds delete tests: modal render + cascade-count surface, RBAC + cross-tenant + CSRF gating, soft-delete on success, 409-without-force vs 204-with-force on a referenced target.
* `backend/src/meho_backplane/ui/templates/connectors/list.html` — full-page list template.
* `backend/src/meho_backplane/ui/templates/connectors/_table_rows.html` — list rows fragment (HTMX swap target).
* `backend/src/meho_backplane/ui/templates/connectors/detail.html` — full-page detail template.
* `backend/src/meho_backplane/ui/templates/connectors/_fingerprint_card.html` — fingerprint card (also returned by the re-probe POST).
* `backend/src/meho_backplane/ui/templates/connectors/_recent_ops.html` — SSE-live recent-ops card.
* `backend/src/meho_backplane/ui/templates/connectors/_ops_matrix.html` — grouped operations matrix.
* `backend/src/meho_backplane/ui/templates/connectors/_probe_alert.html` — failure alert fragment (no_connector / ambiguous).
* `backend/src/meho_backplane/ui/static/src/app/connectors-feed.js` — `connectorsRecentOps` Alpine controller.
* `backend/tests/test_ui_connectors_view.py` — auth boundary, list (full + fragment + sort + filter + cross-tenant), detail (properties + alias + 404 + cross-tenant + recent ops + ops matrix + ambiguous / no-connector branches), fingerprint card (present / never), re-probe button visibility (operator vs tenant_admin), SSE wiring + URL-encoding, re-probe (success + 403 operator + 501 no-connector + 404 unknown).

### Bulk import (Task #875)

T3 layers a **bulk `targets.yaml` import** surface on top of the T1 / T2
surfaces. The operator pastes or uploads a `targets.yaml`; the server
parses it, classifies every entry CREATE-vs-UPDATE against the caller's
tenant, and renders a preview table; on confirm the route applies the
plan **in-process** via the existing target CRUD handlers
(`create_target` for new names, `update_target` for existing ones).

**No `/api/v1/targets/import` endpoint exists.** This UI mirrors the
client-orchestrated CRUD the `meho targets import` CLI tool (G0.3-T6
#257, `cli/internal/cmd/targets/import.go`) performs: parse → list
existing names → classify CREATE vs UPDATE → POST new / PATCH existing.
The key-mapping + classification logic in `import_view.py` is a
server-side port of that CLI's `mapEntry` / `buildLivePlan`, so the web
import and the CLI import produce byte-identical writes for the same
YAML.

**Mapping rules (parity with `import.go`).**

* **Known top-level keys** — `name`, `aliases`, `product`, `host`,
  `port`, `fqdn`, `secret_ref`, `auth_model`, `vpn_required`, `notes`,
  `preferred_impl_id`, `extras` — map 1:1 to `TargetCreate` /
  `TargetUpdate` fields.
* **Unknown keys** spill into the `extras` JSONB column (merged with an
  explicit `extras:` block when one is present).
* **`fingerprint`** is server-managed (probe verb is the only writer;
  the write schemas reject it via `extra='forbid'`) → dropped with a
  preview warning.
* **CREATE vs UPDATE** is decided by an existing-name lookup scoped to
  the caller's tenant (excluding soft-deleted rows — the same filter the
  `/api/v1/targets` list route the CLI calls applies).
* On **UPDATE** the body is **sparse** (only YAML-present keys); `name`
  and `product` are stripped (the PATCH route rejects `name`; `product`
  is not patched on the CLI update path). The sparse shape means
  re-importing a YAML that omits some fields does not wipe those columns
  — the same load-bearing contract PR #362's review on #257 pinned.

**Stateless preview → confirm.** The server holds no plan between the
two requests: the preview echoes the submitted YAML into a hidden field
and the confirm route re-parses + re-classifies it. Re-classifying on
confirm keeps the server the source of truth — a target created between
preview and confirm is correctly PATCHed rather than re-CREATEd into a
409.

**RBAC + CSRF + isolation.** All three routes depend on
`resolve_operator_or_403` (tenant_admin only; operators 403 server-side).
CSRF is enforced by the chassis `CSRFMiddleware` on the two `POST`
routes. Cross-tenant isolation is two-layer: the existing-name lookup
filters on `session_ctx.tenant_id`, and the in-process `create_target` /
`update_target` handlers write / resolve under `operator.tenant_id`, so
an import can only ever land in the caller's own tenant.

**Validate the whole plan before any write (no partial import).**
`build_plan` constructs *and* schema-validates every `TargetCreate` /
`TargetUpdate` body up front — before the confirm route's write loop
runs. A structurally-valid YAML carrying a schema-invalid value (a bad
`auth_model` enum, an out-of-range `port`, etc.) therefore fails the
*whole* plan (`ImportParseError`, rendered as the inline 422 fragment)
and writes nothing, rather than committing the rows ahead of the bad
entry and then 500-ing mid-loop. This mirrors the CLI's no-partial-write
contract (`import.go` builds the full plan before any API call fires).
The same pre-validation runs in `render_preview`, so the preview never
green-lights a plan the confirm step would reject.

**Errors render inline.** `yaml.safe_load` failures, a non-mapping root,
a missing / empty `targets:` list, per-entry required-field violations
(`name` / `product` / `host`), and Pydantic schema-validation failures
all render an inline error in the preview fragment with HTTP 422 — never
a 500.

#### Files (Task #875)

* `backend/src/meho_backplane/ui/routes/connectors/import_view.py` — parse (`yaml.safe_load`) + key-mapping + CREATE/UPDATE classification (port of `import.go`'s `mapEntry` / `buildLivePlan`) + up-front Pydantic body validation (whole plan validated before any write — no partial import) + render helpers + in-process confirm.
* `backend/src/meho_backplane/ui/routes/connectors/import_router.py` — thin FastAPI route wrappers (multipart paste + upload parse; tenant_admin-gated deps); registered before the detail route so the literal `/ui/connectors/import` paths win the first-match lookup over `/ui/connectors/{name}`.
* `backend/src/meho_backplane/ui/templates/connectors/import.html` — full-page paste-box + upload form.
* `backend/src/meho_backplane/ui/templates/connectors/_import_preview.html` — preview table fragment (CREATE/UPDATE badges + per-entry warnings + confirm form, or an inline parse error).
* `backend/src/meho_backplane/ui/templates/connectors/_import_result.html` — result summary fragment (N created, M updated).

## Agents surface (G10.8-T1 #1825)

Initiative [#1824](https://github.com/evoila/meho/issues/1824) (G10.8
Agents console). The console surface over the G11.1 agent-definition
layer (`api/v1/agents.py`, `agents/service.py`, `agents/schemas.py`).
Task #1825 stands up `/ui/agents` as a **top-level sidebar surface** and
the anchor scaffold subsequent agent-console Tasks hang off (run console
T2 #1829, run history T3 #1830, principals T4 #1831, grants T5 #1832).

`meho_backplane.ui.routes.agents` ships:

- `GET /ui/agents` — the per-tenant agent-definitions list. One handler
  serves both shapes (branch on `HX-Request`): the full `agents/index.html`
  page on a browser nav, the `agents/_cards.html` fragment on a re-render
  swap. One DaisyUI card per definition: name → detail link, `model_tier`
  badge, `enabled` pill, `identity_ref`, `turn_budget`, tool count,
  created-by, updated-at, and a **first-line-only** system-prompt summary.
  The sensitive `system_prompt` / `toolset` are never dumped here — only
  summarised — mirroring the audit trail keeping them out (`api/v1/agents.py`).
- `GET /ui/agents/{name}` — the per-agent detail page (or HTMX body
  fragment). Renders the full `AgentDefinitionRead`: metadata header, the
  read-only `system_prompt` in a monospace block, and `toolset` /
  `output_schema` as collapsible pretty-printed JSON. A non-existent /
  cross-tenant name → 404 (the service returns `None` for both — the
  existence-leak collapse the REST surface holds). A "Recent runs →" link
  in the breadcrumb row points at the runs list (T3 #1830); the Run entry
  point (T2) lands here once that Task ships.
- `POST/GET /ui/agents/create`, `GET/PATCH /ui/agents/{name}` (edit),
  `POST /ui/agents/{name}/toggle` (enable/disable), `GET/POST
  /ui/agents/{name}/delete` — the write surface, **tenant_admin only**.

### RBAC posture

The role split mirrors the connectors surface (`connectors/operator.py`):

- **Read** (`resolve_role_probe`) — fails *soft*: a JWT-validation hiccup
  projects to a "no privileges" probe rather than 5xx-ing the page. The
  probe's `is_tenant_admin` flag is the *UX hint* that hides the create /
  edit / toggle / delete affordances from operators who can't use them.
- **Write** (`resolve_operator_or_403`) — the *hard* gate: lifts the full
  `Operator` from the BFF session, re-validates the access token through
  the chassis JWT chain, and raises 403 for any non-`tenant_admin` caller.
  A crafted POST/PATCH that bypasses the hidden affordance still hits the
  403; the template hiding is never the security boundary.

The write handlers delegate to `AgentDefinitionService` **in-process**
(the same pattern the memory surface uses for `MemoryService`) rather than
to the REST routes, because the service is RBAC-free by design (the caller
gates the role) — so the UI write and the REST write share one validation
+ identity-ref-check + persist code path, and the 403 gate lives on the
UI route deps.

### Create / edit error surfacing

Both modals are HTMX-injected native `<dialog class="modal">` fragments
opened by the shared app-shell controller (`app/modal-dialogs.js`) on
`htmx:afterSwap` (#1803). The submit handler builds an
`AgentDefinitionCreate` / `AgentDefinitionUpdate` and persists via the
service; failures re-render the **same modal inline** (not a generic
error page) with per-field messages and the operator's typed values
echoed back:

- Pydantic `ValidationError` (e.g. `turn_budget` outside 1..1000) → 422
  with the field error under the offending input.
- `AgentDefinitionExistsError` (duplicate `(tenant, name)`) → 409 with a
  `name` field error.
- `AgentIdentityRefInvalidError` (unknown / revoked / cross-tenant
  `identity_ref`) → 422 with an `identity_ref` field error.

The `identity_ref` field is **free-text** for this scaffold; the picker
over registered non-revoked principals (from `api/v1/agent_principals.py`)
is T4 (#1832). Until it lands, the free-text field with inline 422
surfacing is the accepted shape per the #1825 issue body.

### Files

* `backend/src/meho_backplane/ui/routes/agents/routes.py` — thin FastAPI
  route wrappers (path / method / dependency wiring); the static-prefix
  `/ui/agents/create` route registers before `/ui/agents/{name}`.
* `backend/src/meho_backplane/ui/routes/agents/views.py` — read renders
  (list + detail) + the row projections (system-prompt summary, tool
  count, pretty-printed JSON).
* `backend/src/meho_backplane/ui/routes/agents/forms.py` — write renders
  (create / edit / delete modal) + submit handlers (create / edit /
  toggle / delete), with the 409 / 422 inline error mapping.
* `backend/src/meho_backplane/ui/routes/agents/operator.py` — the role
  lift: `resolve_role_probe` (soft, read) + `resolve_operator_or_403`
  (hard tenant_admin gate, write).
* `backend/src/meho_backplane/ui/templates/agents/index.html`,
  `_cards.html`, `detail.html`, `_detail_body.html`,
  `_agent_form_fields.html`, `_create_modal.html`, `_edit_modal.html`,
  `_delete_modal.html` — the surface templates.
* Nav + dashboard wiring: the `('agents', '/ui/agents', 'bot', 'Agents')`
  entry in `base.html`'s `nav_surfaces`, the `bot` icon in `_icons.html`,
  and the Agents tile in `dashboard.py`'s `_SURFACE_TILES`.

This surface ships no `api/v1` schema, but its `/ui/*` routes still
register into the app's route table, so the CLI OpenAPI snapshot
(`cli/api/openapi.json`) and the generated Go client
(`cli/internal/api/client.gen.go`) gain the new paths — re-snapshot +
re-generate (`cd cli && make snapshot-openapi && make generate`) when
adding or changing any `/ui/*` route, or the "CLI API snapshot freshness"
CI lane fails.

## Agent-runs surface (G10.8-T3 #1830)

Initiative [#1824](https://github.com/evoila/meho/issues/1824). The
read-only console face of the agent run-history read path
(`api/v1/agent_runs.py` — `GET /api/v1/agents/runs` +
`GET /api/v1/agents/runs/{handle}`): before this Task an operator could
see run history / poll a run's durable status only from the CLI / REST.
Hung off the `/ui/agents` scaffold (#1825) as a **Runs sub-tab** rather
than a new sidebar entry (`active_surface` stays `agents`); a
Definitions/Runs tab strip on the index + detail pages switches between
the two agents surfaces, and the agent detail page carries a
"Recent runs →" link.

`meho_backplane.ui.routes.agents.runs` ships:

- `GET /ui/agents/runs` — the cross-agent run list, newest-first. One
  handler serves both shapes (branch on `HX-Request`): the full
  `agents/runs/list.html` page on a browser nav, the
  `agents/runs/_table_rows.html` `<tbody>` fragment on a filter swap.
  Filters: `status` (the runtime `AgentRunStatus` `StrEnum` used directly
  as the `Query` type — an out-of-enum value 422s) + `work_ref` (exact
  match). Both ride `hx-push-url` so the filtered view is bookmarkable.
  Columns: run-id prefix, status pill, trigger provenance, model +
  provider + tier, turns, work_ref chip, relative created, duration.
  An `awaiting_approval` row deep-links to `/ui/approvals` (T7).
- `GET /ui/agents/runs/{handle}` — the per-run detail. The status panel
  (`agents/runs/_status_panel.html`) **polls after the fact** (not the
  live SSE run console — that is the sibling #1829): while the run is
  non-terminal it carries `hx-get` + `hx-trigger="every Ns"` so HTMX
  re-fetches it on a timer; once the run is terminal (`succeeded` /
  `failed` / `cancelled`) the panel drops those directives so the
  load-poll cycle ends naturally (the
  [HTMX "stop returning the polling element"](https://htmx.org/docs/#polling)
  pattern — no JS, no `286`-response plumbing). Renders status, turns,
  provider + model, the pretty-printed `output` JSON, or the `error`
  reason on a failed run. A cross-tenant / absent handle → 404; a
  non-UUID handle → 422.

### RBAC posture + tenant scoping

Reads are **operator** (Initiative #1824: reads operator, writes
tenant_admin — there are no writes on this surface). The two reads call
the in-process `AgentInvoker` (`list_runs` / `poll`) — the same read path
the Bearer REST routes use — because a browser carrying only the BFF
session cookie cannot authenticate the Bearer route. The invoker takes a
full `Operator` and tenant-scopes every query on `operator.tenant_id`, so
`ui/routes/agents/runs/operator.py`'s `resolve_run_reader` synthesises a
tenant-scoped `OPERATOR` from the BFF session context (the no-JWT-round-
trip read path the memory surface's `build_read_operator` established,
#877) — sound because the chassis session middleware already
authenticated the request and tenant isolation is the invoker's job. A
cross-tenant run is invisible on the list and 404 on the detail. The
surface never exposes `system_prompt` / `toolset` / approval params — the
invoker's summary + poll projections carry none of them.

### Agent ↔ run correlation gap

`AgentRunSummary` carries no `agent_definition_id` back-link (a documented
substrate gap — same one the scheduler detail's "Recent fires" panel
handles), so the runs list cannot filter by a specific agent. The agent
detail page's "Recent runs →" link therefore lands on the unfiltered
cross-agent list (filterable there by `status` / `work_ref`) rather than
fabricating an agent filter the read path cannot serve. The only
correlation key across triggers and runs is `work_ref`.

### Files

* `backend/src/meho_backplane/ui/routes/agents/runs/list_view.py` — the
  list handler (full page + `<tbody>` fragment).
* `backend/src/meho_backplane/ui/routes/agents/runs/detail.py` — the
  detail handler (full page + self-polling status-panel fragment).
* `backend/src/meho_backplane/ui/routes/agents/runs/views.py` — the
  row-to-view projections + status-badge map + UTC coercion +
  terminal-status predicate (shared by both handlers).
* `backend/src/meho_backplane/ui/routes/agents/runs/operator.py` — the
  read-path `Operator` lift + the re-exported role probe.
* `backend/src/meho_backplane/ui/templates/agents/runs/list.html`,
  `_table_rows.html`, `detail.html`, `_status_panel.html`.

The `build_runs_router()` aggregate is included in `ui/routes/__init__`'s
`build_router()` **before** `build_agents_router()` so the literal
`/ui/agents/runs` + `/ui/agents/runs/{handle}` routes win the
first-match-wins lookup against the definition surface's
`/ui/agents/{name}` (which would otherwise bind `"runs"` as a name). Like
every `/ui/*` surface this changes the CLI OpenAPI snapshot — re-snapshot
+ re-generate when touching it.

## Agent principals surface (G10.8-T4 #1831)

Initiative [#1824](https://github.com/evoila/meho/issues/1824). The
agent-identity inventory and the **Keycloak kill switch**, hung off the
T1 agents scaffold as the `/ui/agents/principals` sub-surface. The console
over the G11.2 agent-principal lifecycle (`api/v1/agent_principals.py`,
`auth/agent_principals.py`, `auth/keycloak_admin.py`). Registered inside
`build_agents_router` **before** the `/ui/agents/{name}` read route so the
literal `principals` segment is not swallowed as an agent `{name}` (the
same first-match discipline `/ui/agents/create` follows).

`meho_backplane.ui.routes.agents.principals_*` ships:

- `GET /ui/agents/principals` — the per-tenant principals list (operator).
  One handler serves both shapes (branch on `HX-Request`): the full
  `agents/principals/index.html` page on a browser nav, the
  `agents/principals/_table.html` fragment on the `include_revoked` toggle
  swap. One row per principal: name, `keycloak_client_id`, a revoked /
  active pill, `owner_sub`, `created_by_sub`, `created_at`. The internal
  `keycloak_internal_id` is never surfaced. The `include_revoked` toggle
  flips the service query so revoked rows join the inventory for audit.
- `GET/POST /ui/agents/principals/register` — register a new principal
  (**tenant_admin**). An upstream side-effecting op: the service creates a
  Keycloak client + writes its generated credential to Vault before the DB
  row lands. Failures re-render the modal inline (not a generic error):
  empty / bad-character `name` → 422 `name` field error; duplicate
  `(tenant, name)` → 409 `name` field error; **Keycloak unconfigured →
  503** banner carrying the gold-standard `KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL`
  three-clause text; other Keycloak API failure → 502 `keycloak_admin_error`
  banner; Vault credential-write failure → 502 `scheduler_vault_write_error`
  banner. The actionable backend detail is rendered verbatim, never
  flattened to "something went wrong".
- `GET/POST /ui/agents/principals/{name}/revoke` — revoke = the **Keycloak
  kill switch** (**tenant_admin**): disables the Keycloak client, which
  blocks all new token grants for the identity (tokens already minted stay
  valid until their `exp`). It is terminal — there is no un-revoke. Because
  a too-easy kill switch is a footgun (#1831 risk note), the confirm modal
  demands a **type-to-confirm of the principal name**: an inline Alpine
  `x-data` gate keeps the destructive submit `:disabled` until the typed
  value equals the name, and the handler **re-checks the typed value
  server-side** (a crafted POST cannot skip the confirm — a mismatch
  re-renders the modal with a 422 banner and makes no service call). 404
  on an absent / cross-tenant / already-revoked name; Keycloak
  unconfigured / API failures render the same 503 / 502 actionable banners
  as register.

RBAC + service-delegation + CSRF posture is identical to the
agent-definition surface: `resolve_role_probe` (soft, read) +
`resolve_operator_or_403` (hard tenant_admin gate, write) from
`agents/operator.py`; the write handlers call `AgentPrincipalService`
**in-process** (the same path the REST routes + the `meho agent-principal`
CLI use); `POST` is CSRF-double-submit-gated by the chassis
`CSRFMiddleware`, with each modal render re-minting + re-setting the
`meho_csrf` cookie.

### Files

* `backend/src/meho_backplane/ui/routes/agents/principals_views.py` — the
  read render (`render_principals_index`) + the row projection +
  `validate_principal_name` + `fetch_principal_or_404`.
* `backend/src/meho_backplane/ui/routes/agents/principals_forms.py` — the
  register + revoke modal renders + submit handlers, with the 503 / 502 /
  422 / 409 inline error mapping and the server-side type-to-confirm check.
* `backend/src/meho_backplane/ui/routes/agents/routes.py` —
  `_register_principals_routes`, wired into `build_agents_router` before
  the `/ui/agents/{name}` read route.
* `backend/src/meho_backplane/ui/templates/agents/principals/index.html`,
  `_table.html`, `_register_modal.html`, `_revoke_modal.html` — the
  surface templates. The principals surface keeps `active_surface="agents"`
  (it is a sub-surface; the sidebar highlights Agents) and is reached via a
  "Principals" link on the agents list header.

## Agent grants surface (G10.8-T5 #1832)

Initiative [#1824](https://github.com/evoila/meho/issues/1824) (G10.8
Agents console). The console surface over the G11.2 agent permission-grant
layer (`api/v1/agent_grants.py`, `agents/grants.py`,
`agents/grant_schemas.py`). Task #1832 layers `/ui/agents/grants` onto the
agents console (`/ui/agents`, #1825) as a **tenant_admin-only** governance
surface: which principal may run which op pattern with which verdict.

### RBAC — the whole surface is tenant_admin, reads included

Unlike the agent-definitions surface (where an `operator` can read and only
writes are admin-gated), **every** grants route — list, detail, create,
elevate, revoke — is `tenant_admin`. A grant listing reveals the tenant's
least-privilege posture, so it is governance data, not operator data. This
mirrors the REST surface (`api/v1/agent_grants.py`), whose every route
carries `require_role(TenantRole.TENANT_ADMIN)`. The gate is
`resolve_grants_admin_or_403` (`ui/routes/agents/grants/operator.py`),
which reuses the shared operator lift the agents console ships
(`ui/routes/agents/operator.py::_lift_operator`) and wires it onto the read
paths too; a non-admin caller gets a 403 the page cannot bypass. The
agents list links to `/ui/agents/grants` only for admins (a non-admin would
403 the route).

### Routes

`meho_backplane.ui.routes.agents.grants` ships:

- `GET /ui/agents/grants` — the per-tenant grants table (or the HTMX
  `_rows.html` tbody fragment on a filter swap). Filters: `principal_sub`
  (exact match) + `include_expired` (off by default, so expired elevations
  are hidden). The verdict renders as a colour-coded badge whose colour AND
  label both derive from the same verdict string
  (`views.verdict_badge_class`): `auto-execute` = `badge-success`,
  `needs-approval` = `badge-warning`, `deny` = `badge-error` — so a `deny`
  grant can never read as an allow. The expiry column distinguishes a
  permanent grant from a time-bounded elevation that auto-expires at T.
- `GET /ui/agents/grants/{grant_id}` — the per-grant detail page (or HTMX
  body fragment). An absent / malformed / cross-tenant id renders 404
  (existence-leak avoidance, mirroring the REST 404 collapse).
- `GET/POST /ui/agents/grants/create` — create a permanent or time-bounded
  grant. The `verdict` `<select>` defaults to the conservative `deny`. A
  Pydantic `ValidationError` or a service `GrantValidationError` (past /
  naive `expires_at`, bad `target_scope` UUID, duplicate grant) re-renders
  the modal inline with the matching field error + 422.
- `GET/POST /ui/agents/grants/elevate` — create a time-bounded elevation;
  `expires_at` is **required** (the `AgentElevationCreate` schema makes the
  field non-optional, so an omitted value surfaces as the inline
  `expires_at` field error).
- `GET/POST /ui/agents/grants/{grant_id}/revoke` — revoke a grant. Revoke
  is destructive (it drops the principal's explicit permission), so a
  native-`<dialog>` confirm gates the submit. 204 + `HX-Redirect` on
  success; 404 on an absent / cross-tenant id.

All writes go through the chassis CSRF double-submit (`CSRFMiddleware` gates
every non-safe method under `/ui/`; each modal re-mints + re-sets the
`meho_csrf` cookie and echoes the matching `X-CSRF-Token` header). The
writes call `AgentGrantService` in-process so the UI and REST surfaces share
one validation + persist code path.

The `principal_sub` field is free-text for now (the picker over registered
principals is T4 #1831; until it lands a free-text field is the accepted
shape — the same precedent #1825 set for `identity_ref`). The grant service
does not validate `principal_sub` against registered principals, so a grant
may be issued ahead of a principal's first login — the UI mirrors that
backend contract.

### Routing order

`build_agent_grants_router()` is mounted **before** `build_agents_router()`
in `build_router()` so the literal `/ui/agents/grants` wins the
first-match-wins lookup against the agents surface's `/ui/agents/{name}`
(which would otherwise bind `name="grants"`). Inside the grants router the
literal `create` / `elevate` routes register before the `{grant_id}` detail
route for the same reason.

### Files

* `backend/src/meho_backplane/ui/routes/agents/grants/routes.py` — thin
  FastAPI handlers + the load-bearing registration order.
* `.../grants/views.py` — read renders (table + detail), row projections,
  the verdict-badge colour mapping, and the `grant_id` parse-or-404.
* `.../grants/forms.py` — write renders (create / elevate / revoke modals)
  + submit handlers; maps `GrantValidationError` messages to the offending
  field for inline surfacing.
* `.../grants/operator.py` — the all-paths `tenant_admin` gate.
* `backend/src/meho_backplane/ui/templates/agents/grants/index.html`,
  `_rows.html`, `detail.html`, `_detail_body.html`,
  `_grant_form_fields.html`, `_create_modal.html`, `_elevate_modal.html`,
  `_revoke_modal.html` — the surface templates.

Unlike the agent-definitions surface's doc note above, the `/ui/*` routes
**do** appear in the CLI OpenAPI snapshot (`cli/api/openapi.json`) — the
FastAPI app the snapshot is generated from mounts the UI router, so a new
`/ui/agents/grants*` path lands in the snapshot and the typed Go client.
`cd cli && make snapshot-openapi && make generate` regenerates both after a
route change.

## Runbooks surface (G10.6)

Initiative [#1381](https://github.com/evoila/meho/issues/1381) (G10.6
Runbooks UI). The console surface over the G12.2 runbook template layer —
the catalog, the opacity-floor-aware detail view, the `tenant_admin`
authoring editor, and the publish / deprecate lifecycle controls.
Landed across three tasks: T1 (#1382) the read surface, T2 (#1383) the
authoring editor, T3 (#1384) the lifecycle controls. The surface consumes
the REST runbook-template layer (`/api/v1/runbooks/templates/*`,
`backend/src/meho_backplane/api/v1/runbook_templates.py`) through the same
`RunbookTemplateService` the REST routes and the `meho runbook` CLI use —
no parallel data path. The runbooks stub registered in the T5 #866
chassis is retired by this surface the same way broadcast / topology /
memory / connectors retired theirs (the real router is mounted ahead of
the stubs aggregate, first-match-wins).

### Routes

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| `GET` | `/ui/runbooks` | `require_ui_session` | Catalog page. Lists the latest version of each template slug for the operator's tenant (slug / version / title / status / target_kind / edited_at) as DaisyUI rows with status badges. `HX-Request: true` returns only the `runbooks/_list.html` fragment; a direct navigation returns the full page. |
| `GET` | `/ui/runbooks/list` | `require_ui_session` | HTMX filter partial. Same projection as the catalog, parameterised by the `status` (`draft` / `published` / `deprecated`, a closed `Literal`) and `target_kind` query params the filter controls carry. An out-of-vocab `status` trips a clean 422 at the query boundary. Registered **before** `/ui/runbooks/{slug}` so the literal `list` segment is not swallowed as a slug. |
| `GET` | `/ui/runbooks/{slug}` | `require_ui_session` | Template detail. Renders title / description / target_kind / status + the ordered steps (`manual` vs `operation_call`, op_id / params) and verify gates (`confirm` prompt vs `operation_call` op_id / params / expect). Step bodies are server-rendered Markdown via the shared KB renderer. Opacity-floor-gated (see below). `?version=<n>` pins a specific version. |
| `GET` | `/ui/runbooks/new` | `require_ui_admin` | (T2 #1383) Blank-draft editor page. |
| `POST` | `/ui/runbooks/new` | `require_ui_admin` | (T2 #1383) Create a draft from the editor form (mirrors REST `POST /api/v1/runbooks/templates`). Success → 204 + `HX-Redirect: /ui/runbooks/<slug>`; a duplicate slug / invalid body re-renders the editor inline (422) preserving entered data. |
| `POST` | `/ui/runbooks/preview` | `require_ui_admin` | (T2 #1383) HTMX Markdown live-preview partial for one step's `body` (max 64 KiB), rendered via the shared KB renderer. |
| `GET` | `/ui/runbooks/{slug}/edit` | `require_ui_admin` | (T2 #1383) Editor pre-loaded with the template's latest version. Missing / cross-tenant slug → 404. |
| `POST` | `/ui/runbooks/{slug}/edit` | `require_ui_admin` | (T2 #1383) Edit-in-place (draft) / fork-on-edit (published → new draft), mirroring REST `PATCH`. The detail page surfaces how many runs are still pinned to the source version (the fork leaves them bound) via `count_in_flight_runs`. |
| `POST` | `/ui/runbooks/{slug}/publish` | `require_ui_admin` | (T3 #1384) Promote `(slug, version)` draft → published (mirrors REST publish: 200 idempotent / 400 not-draft / 404). The body carries the integer `version` (the slug is the URL's job) so a stale catalog row acts on the version it was rendered against, not the latest. |
| `POST` | `/ui/runbooks/{slug}/deprecate` | `require_ui_admin` | (T3 #1384) Retire `(slug, version)` published → deprecated (200 idempotent / 400 not-published / 404). Same version-in-body posture as publish. |
| `GET` | `/ui/runbooks/runs` | `require_ui_session` | (G10.11-T1 #1884) Runs list — the "Runs" sub-tab. Role-scoped + **service-enforced**: an `OPERATOR` only ever sees their own runs (the service forces `assignee=caller_sub` even on a forged `?assignee=`); a `TENANT_ADMIN` sees every tenant run and may filter by `assignee`. `status` (`in_progress` / `completed` / `abandoned`, a closed `Literal`) + admin-only `assignee` filters. `HX-Request: true` returns only the `runbooks/_runs_list.html` fragment. Each row links to the run driver. |
| `GET` | `/ui/runbooks/runs/start` | `require_ui_session` | (G10.11-T1 #1884) HTMX start-run modal fragment. Pre-populates a `<datalist>` of the tenant's published template slugs (free text still allowed). Registered **before** `/ui/runbooks/runs/{run_id}` so the literal `start` segment is not swallowed by the `{run_id}` param route. CSRF re-mint + cookie refresh on render. |
| `POST` | `/ui/runbooks/runs` | `require_ui_session` | (G10.11-T1 #1884) Start handler. Operator floor (the service auto-assigns the caller). Success → 204 + `HX-Redirect: /ui/runbooks/runs/{run_id}` (drops the operator into the driver). The typed start errors (`DeprecatedTemplateError` / `TemplateNotFoundError` / `MissingParamsError`) map to an inline modal `alert-error` (HTTP 200 fragment, not 500); a non-object `params` JSON → 422. |
| `GET` | `/ui/runbooks/runs/{run_id}` | `require_ui_session` | (G10.11-T2 #1893) Run **driver** page. Calls the opacity-safe `RunbookRunService.get_current_step` (the SAME single-step `CurrentStepResponse` projection `start_run` / `next_step` return — never `template.steps`) + `get_run_assignee`. Renders run coordinates + `position` ("step n of total") + the **single current** `StepBody` (Markdown body via the shared KB renderer). `RunNotFoundError` → 404 page. **Advance** shown ONLY when `session.operator_sub == assigned_to`; **Reassign** shown ONLY for a `tenant_admin` (soft probe). |
| `POST` | `/ui/runbooks/runs/{run_id}/next` | `require_ui_session` | (G10.11-T2 #1893) Advance one step. Calls `next_step`; re-renders the step fragment from the returned `CurrentStepResponse` (or the completed banner on `RunCompletedResponse`). The assignee gate is **service-enforced, fail-closed**: a non-assignee (INCLUDING a `TENANT_ADMIN`) gets `NotRunAssigneeError` → an inline "reassigned away from you" message at HTTP 200 (never a 500). A `confirm` answered `no`/`escalate` → step `failed` → `PreviousStepFailedError` → a dead-end banner (Advance hidden, only Abort forward). |
| `POST` | `/ui/runbooks/runs/{run_id}/abort` | `require_ui_session` | (G10.11-T2 #1893) Abort with a **required** non-empty reason (`AbortRunRequest.reason` is `min_length=1`, persisted to the abort audit row). Empty reason guarded client-side (HTMX `required`) AND handled server-side (a tampered empty reason → inline alert, not 500) so the audit guarantee holds. `caller_is_admin = probe.is_tenant_admin` (service allows `{assignee, tenant_admin}`). Success → the abandoned banner. |
| `POST` | `/ui/runbooks/runs/{run_id}/reassign` | `require_ui_admin` | (G10.11-T2 #1893) Transfer ownership. **`require_ui_admin` hard gate** — an `OPERATOR` gets 403 at the dependency, before the body / service runs (the service itself does no role check; the gate is the sole authority). After the flip, the prior assignee's open page shows "reassigned away from you" on their next Advance. |

**Route ordering.** The literal `list` / `new` / `preview` / `publish` /
`deprecate` / `runs` / `runs/start` segments are registered **before**
`/ui/runbooks/{slug}` in `build_runbooks_router()` so FastAPI's
first-match-wins routing does not bind them to the slug path parameter. The
run-surface ordering is load-bearing in two places: `register_runs_routes`
(T1) registers the literal `/ui/runbooks/runs/start` **before**
`register_driver_routes` (T2) registers the `/ui/runbooks/runs/{run_id}`
param route, so `start` is not bound as a `run_id`; and both run-surface
registrations precede the `/ui/runbooks/{slug}` catch-all so `runs` is not
bound as a `slug`. The factory's call order is therefore: editor →
lifecycle → runs (T1) → driver (T2) → `{slug}`. A grep-proof test
(`test_route_ordering_start_before_run_id_before_slug` in
`test_ui_runbook_driver_opacity.py`) asserts the index ordering by building
the router.

### Module layout

* `backend/src/meho_backplane/ui/routes/runbooks/__init__.py` — exports the `build_runbooks_router` factory the umbrella `build_router()` mounts.
* `backend/src/meho_backplane/ui/routes/runbooks/routes.py` — the read surface (T1). The catalog + filter-fragment + opacity-floor detail handlers, the `_resolve_role` / `_is_admin` role-lift helpers, and the factory itself (which calls `register_editor_routes` → `register_lifecycle_routes` → `register_runs_routes` → `register_driver_routes` before the `{slug}` catch-all).
* `backend/src/meho_backplane/ui/routes/runbooks/editor.py` — the authoring form (de)serialisation + service calls (T2): `build_editor_context`, `handle_editor_submit`, `template_to_form_steps`, the CSRF cookie helper.
* `backend/src/meho_backplane/ui/routes/runbooks/editor_routes.py` — thin FastAPI wiring for the T2 editor routes (`require_ui_admin`-gated). Split from `editor.py` so neither file crosses the code-quality size gate.
* `backend/src/meho_backplane/ui/routes/runbooks/lifecycle.py` — the T3 publish / deprecate handler bodies + route wiring (`require_ui_admin`-gated). Maps the typed service errors (`TemplateNotDraftError` / `TemplateNotPublishedError`) to inline DaisyUI alerts, a missing version to 404, and a tampered `version` field to 422.
* `backend/src/meho_backplane/ui/routes/runbooks/runs.py` — the run-surface list + start modal + start handler (G10.11-T1 #1884), `register_runs_routes`. A pure UI/BFF build over `RunbookRunService` (`list_runs` / `start_run`); operator-floor reads with the visibility split service-enforced.
* `backend/src/meho_backplane/ui/routes/runbooks/driver.py` — the run-driver route wiring + action orchestration (G10.11-T2 #1893), `register_driver_routes`. Maps each service / engine outcome to an operator-facing fragment (`NotRunAssigneeError` → "reassigned away" inline; `PreviousStepFailedError` → dead-end banner; terminal / verify `ValueError`s → inline alert). The reassign route declares the `require_ui_admin` hard gate.
* `backend/src/meho_backplane/ui/routes/runbooks/driver_render.py` — the T2 driver's Jinja render primitives (`StepView`, `step_view`, `render_driver_page`, `render_not_found`, `render_step_fragment`) + the CSRF mint/cookie-refresh. Split from `driver.py` so neither file crosses the code-quality size gate. **Opacity**: every render surfaces only the one current step — there is no `template.steps` loop here.
* `backend/src/meho_backplane/ui/templates/runbooks/` — `index.html` (catalog page), `_list.html` (filter fragment), `detail.html` + `_detail_actions.html` (detail page + lifecycle action row), `editor.html` + `_step_fields.html` + `_editor_preview.html` (authoring editor), `_row_alert.html` (catalog row-action alert slot), `runs.html` + `_runs_list.html` + `_start_modal.html` + `_tabs.html` (T1 runs list + start modal + Templates/Runs sub-tab nav), `run_driver.html` + `_run_step.html` + `run_not_found.html` (T2 driver page + single-current-step fragment + 404 page).

### RBAC gating (`require_ui_session` vs `require_ui_admin`)

The read routes gate on `require_ui_session` (any authenticated operator)
— `UISessionContext` carries `operator_sub` + `tenant_id` only, so it
omits the tenant role to keep read paths free of a JWT-decode round-trip.
The write routes (author / edit / preview / publish / deprecate) chain
`require_ui_admin`, which loads the `DecryptedSession`, re-verifies the
session's access token via `verify_jwt_for_audience`, and raises HTTP 403
(`detail="tenant_admin_required"`) for `operator` / `read_only`. The
server is the single source of truth for the authoring privilege: the
client-side controls are hidden for non-admins optimistically, but a
forged POST still hits the 403 at the dependency before the handler body
runs. A forged POST missing the double-submit CSRF token is blocked
earlier still, by `CSRFMiddleware` (403 at the CSRF gate).

The detail render needs the admin-vs-operator distinction the opacity
floor turns on, even though its route only requires a session. It
resolves it via `_resolve_role` — the same access-token re-verification
`require_ui_admin` performs, but **fails soft**: any hiccup (the session
row vanished mid-request, JWKS transiently unreachable, a token/session
identity mismatch) returns `None`, and the caller treats the request as a
plain operator. The restricted-detail render is the safe default; an
unavailable role lift never 5xx-es the read surface. This mirrors
`connectors.operator.resolve_role_probe`.

### Opacity floor (restricted detail, not a raw 403)

The G12.3-T4 (#1309) carve-out the REST `show` route enforces is mirrored
on the console. A `tenant_admin` always sees the full steps. An
`operator` sees the full steps only when they have a `completed` or
`abandoned` run against the resolved `(slug, version)` — the
`RunbookRunService.can_show_template_post_completion` predicate. An
operator with no such run (or only an in-flight run) is **not** shown a
raw 403: the detail view renders the catalog-level summary (title /
description / target_kind / status / step count) plus a clear "step
details are restricted until you complete a run of this template" notice,
withholding the step internals. This matches the REST surface's
`403 detail="opacity_floor"` posture while keeping the console a navigable
page rather than an error.

A missing / cross-tenant slug collapses to that same restricted page for
an operator (anti-enumeration — existence never leaks via a status-code
differential), and to a 404 for an admin. The restricted placeholder
carries only the slug the operator typed and epoch-sentinel timestamps —
no real template metadata leaks.

### Lifecycle fragment + HTMX wiring

Each publish / deprecate action posts via HTMX. On the **detail** page it
targets `#runbook-lifecycle` and swaps the re-rendered
`runbooks/_detail_actions.html` fragment — which carries the action row
valid for the new state, an `hx-swap-oob` copy of the status badge (so the
header badge flips without a full-page reload), and an inline alert region
(a typed 400 renders as `alert-error`; an idempotent re-action just
refreshes the badge). On the **catalog** row the action targets a per-row
`#runbook-row-alert-…` slot and gets the minimal `_row_alert.html`
fragment instead (the detail-shaped action row + OOB badge would be
nonsense swapped into a list row); the handler branches on the
`HX-Target` header. The CSRF cookie is re-minted + refreshed on every
fragment render so the next action carries a token whose cookie still
matches — a missing refresh would 403 the second interaction at the
double-submit check.

### Cross-tenant isolation

Every handler derives tenant identity from `UISessionContext`; no query
parameter or form field overrides it. The `RunbookTemplateService` tenant
filter makes another tenant's row invisible, so a cross-tenant slug probe
surfaces as the restricted state (operator) or a 404 (admin / lifecycle /
editor) — the same anti-enumeration posture the `/api/v1/runbooks/templates`
surface uses.

### Tests

* `backend/tests/test_ui_runbooks_list.py` — the T1 read surface: auth boundary, catalog render + status badges, empty state, HTMX fragment branch, `status` / `target_kind` filters, the 422 on an out-of-vocab status, admin full-step detail (both step + verify kinds, Markdown-rendered), the opacity-floor branches (no run / in-progress run → restricted; completed run → full steps; missing slug → restricted for operator, 404 for admin), cross-tenant isolation, dashboard tile + sidebar link.
* `backend/tests/test_ui_runbooks_editor.py` — the T2 authoring editor: admin renders (new / edit), operator 403, draft create round-trip (both step + verify kinds), server-side validation re-renders (duplicate slug, bad step id, disallowed substitution) preserving entered data, fork-on-edit from published, in-place edit of a draft, the Markdown live-preview (admin-only).
* `backend/tests/test_ui_runbooks_lifecycle.py` — the T3 lifecycle controls: admin publish / deprecate flips status + swaps the OOB badge, operator forged POST → 403, missing-CSRF → 403, the typed-400 inline alerts (publishing a non-draft, deprecating a non-published version), idempotent re-actions, the catalog-row vs detail-page fragment branch, version-in-body targeting.
* `backend/tests/test_ui_runbooks_acceptance.py` — the **cross-cutting** end-to-end acceptance (T4 #1385): one test exercises the surface as a whole against a single app instance — operator browse (catalog + filters) → operator opacity-floor restricted-detail (not a raw 403) → `tenant_admin` author (draft) → publish → deprecate round-trip with status transitions read back through the read surface → operator blocked (403) from author / publish / deprecate → sidebar link + dashboard tile render. A companion test pins the post-completion operator crossing the floor (completed run → full steps).
* `backend/tests/test_ui_connectors_import.py` — `build_plan` mapping / classification unit tests (extras spill, explicit-extras merge, fingerprint drop, sparse UPDATE) + behavioural tests: auth / RBAC (403 operator, 403 missing CSRF), preview (paste + upload + parse error + missing-field error + schema-invalid-value inline error), confirm (in-process create + update, sparse PATCH preserves omitted columns, malformed-YAML no-write, schema-invalid entry 422 + zero writes), cross-tenant isolation (import lands in caller tenant only; same-name in another tenant does not flip CREATE→UPDATE).
