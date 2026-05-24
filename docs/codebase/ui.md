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
| `meho_backplane.ui.templating` | Jinja2 `Environment` factory with `FileSystemLoader`, `select_autoescape`, `StrictUndefined`, and the `app_version` global pre-bound from `meho_backplane.__version__`. |
| `meho_backplane.ui.routes` | Aggregate `APIRouter`. `build_router()` aggregates the dashboard (`GET /ui/`), the real broadcast routes (`GET /ui/broadcast` + `/ui/broadcast/stream`, G10.1-T1 #867), the real topology routes (`GET /ui/topology` + node detail, G10.5-T1 #880), and the remaining surface stubs (`GET /ui/{knowledge,connectors,memory}`, `_stub.html` placeholders). Real routers are included **before** the stubs so their concrete paths win the first-match-wins lookup; the replaced surfaces are dropped from the stub enumeration. G10.2-G10.4 replace the remaining stubs the same way. |
| `meho_backplane.ui.csrf` | T5 (#866) double-submit-cookie CSRF middleware on state-changing `/ui/*` requests (POST/PATCH/PUT/DELETE). Signed-double-submit per OWASP -- the token is `hmac_sha256(session_secret, session_id || random) + "." + random`; the cookie is JS-readable (`meho_csrf`) so HTMX can echo it in `X-CSRF-Token`. Mismatch / missing token / forged signature -> 403. Read-only methods + out-of-prefix paths pass through. |
| `meho_backplane.ui.auth` | BFF auth subpackage. T3 (#864) landed `session_store` (encrypted token custody + RFC 9700 refresh-token rotation); T4 (#865) lands `/ui/auth/{login,callback,logout}` + session middleware. |
| `meho_backplane.ui.auth.session_store` | Fernet-encrypted server-side session storage. `create_session`, `load_session`, `revoke_session`, `rotate_refresh` against the `web_session` Postgres table. Replay of a used refresh token revokes the session and writes a `ui.session.refresh_replay` audit row on a dedicated transaction so the security signal survives caller rollback. |
| `meho_backplane.ui.auth.flow` | OAuth 2.1 + PKCE client primitives layered on authlib's `AsyncOAuth2Client`. `build_authorization_request` mints the Keycloak redirect URL (S256 PKCE + RFC 8707 `resource` parameter) and registers the per-flow verifier in a server-side `PKCEVerifierStore`. `exchange_code_for_tokens` pops the verifier and exchanges code+verifier at the token endpoint. `resolve_oidc_endpoints` caches the discovery doc on the same TTL the JWKS cache uses. |
| `meho_backplane.ui.auth.routes` | FastAPI `APIRouter` for `/ui/auth/{login,callback,logout}`. `build_router()` returns the router for T5 to mount. Callback verifies the access token through the chassis JWT chain (`verify_jwt_for_audience`) so the BFF inherits issuer / audience / sub / tenant_id / tenant_role checks. Sets `meho_session` cookie with `HttpOnly; Secure; SameSite=Strict; Path=/`. Logout revokes the session, clears the cookie, and 302s to Keycloak's `end_session_endpoint` (best-effort -- a missing endpoint falls back to a local `/ui/auth/login` redirect). |
| `meho_backplane.ui.auth.middleware` | Pure-ASGI `UISessionMiddleware` for `/ui/*`. Loads operator identity from the session cookie on every request; 302s to login on missing/expired session. Bypasses `/ui/static/*` (chassis assets) and `/ui/auth/*` (the BFF surfaces themselves). Per-request `UISessionContext` (frozen dataclass: `session_id`, `operator_sub`, `tenant_id`) lands on `request.state.ui_session`; route handlers read it via `Depends(require_ui_session)`. |

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
| `meho_session` cookie | Browser, `HttpOnly` + `Secure` + `SameSite=Strict` | Session lifetime (until access-token expiry minus 60s margin) |
| Access + refresh tokens | `web_session` row, Fernet-encrypted | Same session lifetime |

The `code_verifier` deliberately does NOT live in a cookie -- a
verifier alongside the code on the redirect URI would defeat the
property PKCE protects (an attacker capturing one captures both).
Server-side custody is the whole point.

Logout: revoke the row, clear the cookie, 302 to Keycloak's
`end_session_endpoint` with `client_id` + `post_logout_redirect_uri`
pointing back to `/ui/auth/login`. The IdP-side hop is best-effort
-- the local session is already revoked when the redirect fires.

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
core (HTMX 1 bundled it). It is loaded in `base.html` right after
`htmx.min.js` (script order matters — the extension calls
`htmx.defineExtension` at load) and is required by both the dashboard
recent-activity snippet (G10.0) and the broadcast live feed (G10.1):
without it every `hx-ext="sse"` wrapper is inert.

Every bump lands on its own `chore(ui): bump <library> to <version>`
PR so the supply-chain trail records each move (same discipline the
backplane Python base image already follows).

## Control flow (chassis)

`base.html` is the only template Task #863 ships. Its structure:

```html
<html data-theme="corporate">
  <head>
    <link href="/ui/static/dist/tailwind.css">
    <script src="/ui/static/src/vendor/htmx.min.js" defer>
    <script src="/ui/static/src/vendor/alpine.min.js" defer>
  </head>
  <body>
    <div class="drawer lg:drawer-open" x-data="{...}">
      <input id="meho-drawer-toggle" class="drawer-toggle">
      <div class="drawer-content">
        <header class="navbar">…tenant select…user menu…</header>
        <main>{% block content %}{% endblock %}</main>
        <footer>v{{ app_version }} · ready/starting pill</footer>
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
(`/ui/broadcast`, `/ui/knowledge`, `/ui/topology`,
`/ui/connectors`, `/ui/memory`) — T5 (#866) ships stub routes at
each so the chassis renders end-to-end before the surface
Initiatives fill the routes in.

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
* A live "recent activity" snippet wired to `/api/v1/feed` via the
  HTMX 2 SSE extension (`hx-ext="sse"` + `sse-connect="..."` +
  `sse-swap="broadcast"`). The feed endpoint itself runs the chassis
  JWT dependency, but the browser carries the session cookie and the
  same operator's JWT will be issued by Keycloak; G10.1 (`#338`)
  wires the live-token round-trip and the client-side
  trim-to-last-N rendering (the feed endpoint streams the live tail
  unbounded -- it does not accept a `limit` query parameter, so a
  hardcoded `?limit=N` would be a silent no-op given FastAPI's
  unknown-query-param drop semantics).
* A "readiness checks" panel listing every registered probe with a
  green/orange pill matching `/ready`'s shape.

The five surface stubs render `_stub.html` with a "Coming soon"
panel referencing the surface Initiative number (G10.1=#338,
G10.2=#339, G10.3=#340, G10.4=#341, G10.5=#342). A surface
Initiative landing its real view registers a router that overrides
the stub route.

The chassis smoke test
[`backend/tests/test_ui_chassis_smoke.py`](../../backend/tests/test_ui_chassis_smoke.py)
exercises every acceptance criterion: unauth dashboard -> 302
login, login -> 302 Keycloak, callback -> session row + 302 /ui/,
authenticated dashboard render (page title + 5 sidebar links + 6
card cells + HTMX SSE wiring), 5 stub routes -> 200 with placeholder
content, CSRF rejection on missing/mismatched/forged tokens,
positive control on matched token, and middleware-order sanity
(/api/* passes through untouched).

## Broadcast surface (Task #867)

Initiative [#338](https://github.com/evoila/meho/issues/338) (G10.1
Activity broadcast UI), Task [#867](https://github.com/evoila/meho/issues/867)
(G10.1-T1) replaces the chassis stub at `/ui/broadcast` with the real
**live activity feed**: an SSE-streamed, reverse-chronological event
list with an empty state and a 1000-row in-DOM cap. Filters + the
event-detail drawer + the PII visualisation are T2 (#868);
wall-monitor mode + the Last-24h replay tab are T3 (#869).

### Routes

| Route | Renders |
| ----- | ------- |
| `GET /ui/broadcast` | Full-page live-feed view (`broadcast/feed.html`, extends `base.html`). Sidebar active-state on Broadcast. |
| `GET /ui/broadcast/stream` | Session-gated SSE bridge (`text/event-stream`). The feed view's `sse-connect` target. |

### Why a UI-owned SSE bridge instead of subscribing to `/api/v1/feed`

The canonical per-tenant SSE feed is `GET /api/v1/feed` (G6.1-T4 #310),
which authenticates via the `Authorization: Bearer <jwt>` header. The
browser's `EventSource` — which the HTMX `sse` extension uses under the
hood — **cannot set custom request headers** (the WHATWG `EventSource`
constructor exposes only `withCredentials`); it sends cookies, not a
Bearer token. So pointing `sse-connect` at `/api/v1/feed` from a
logged-in operator's browser would be answered with a 401 and the SSE
state machine would tighten into a reconnect loop. (The chassis
dashboard's recent-activity snippet wired `sse-connect="/api/v1/feed"`
directly for the same reason it only shows a "Connecting…" placeholder —
that snippet is inert today; fixing it is tracked separately.)

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
| `broadcast/feed.html` | Full-page view: SSE sink wrapper, column header, empty state, the `<template x-for>` + `<script src>` for the controller. |
| `broadcast/_event_row.html` | Server-authored row markup (timestamp · principal badge · op_id · op_class badge · result_status icon · target · payload summary). Aggregate-only events render `(aggregate-only)`. |
| `static/src/app/broadcast-feed.js` | The `broadcastFeed` Alpine component (registered on `alpine:init`). External deferred script, not inline, to stay CSP-ready. Holds the parse + prepend + 1000-row trim + the badge/timestamp/payload helpers. |

### Performance + empty state

* **1000-row cap** — `IN_DOM_ROW_CAP` is passed into the page; the
  controller `unshift`s each event and trims `events.length` to the cap
  so a sustained stream keeps the DOM bounded (work item #9).
* **Empty state** — shown while `events` is empty (or, in T2, when
  filters match nothing): "No activity matching your filters…" (work
  item #8).

### Cross-tenant isolation

The page carries no tenant data beyond the operator's own identity; live
events arrive over the tenant-scoped bridge whose stream key is
`meho:feed:{session.tenant_id}`. There is no tenant query parameter on
the page route or the stream, so a tenant-A operator can never surface
tenant-B events. The
[broadcast suite](../../backend/tests/test_ui_broadcast_feed.py) pins
the generator's stream-key derivation per tenant.

### Known limits

* **No filters / drawer / wall-monitor yet** — T1 ships the live feed
  core only. op_class/principal/target/op_id filters + the event-detail
  drawer + the PII 🔒 viz are T2 (#868); wall-monitor mode (`?wall=1`) +
  the Last-24h replay tab are T3 (#869).
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
* **No graph view yet** — `view=table` is the only mode T1 honours.
  T2 (#881) adds `view=graph` and switches routing on the query
  parameter.

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

## References

- v0.2 decisions [#9 / #10 / #11](../planning/v0.2-decisions.md).
- HTMX 2 — https://htmx.org/
- Jinja2 — https://jinja.palletsprojects.com/
- Tailwind CSS 4 — https://tailwindcss.com/blog/tailwindcss-v4
- Tailwind 4 standalone CLI install — https://tailwindcss.com/docs/installation
- DaisyUI 5 install — https://daisyui.com/docs/install/
- Alpine.js — https://alpinejs.dev/
- Cytoscape.js — https://js.cytoscape.org/
- FastAPI `StaticFiles` (consumed by T5) — https://fastapi.tiangolo.com/tutorial/static-files/
