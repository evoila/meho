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
| `broadcast/feed.html` | Full-page view: header, filter bar include, feed-fragment include, drawer slot, `<script src>` for the controller. |
| `broadcast/_filter_bar.html` | The op_class / principal / target / op_id controls. The three server filters `hx-get` to `/ui/broadcast/feed`; op_id dispatches a `broadcast-op-id-changed` window event for the client-side filter. |
| `broadcast/_feed.html` | The swappable feed fragment: the SSE sink (with the filtered `sse-connect` URL), status bar, column header, empty state, `<template x-for>`. Wraps the `broadcastFeed` Alpine controller so a filter re-render resets the event list and re-subscribes. |
| `broadcast/_event_row.html` | Server-authored row markup (timestamp · principal badge · op_id · op_class badge · result_status icon · target · payload summary). Click opens the drawer; aggregate-only events render the 🔒 marker + placeholder. |
| `broadcast/_event_drawer.html` | Event detail drawer: op identity, operation metadata, identifiers (audit_id / request_id / broadcast event_id), full payload (or the 🔒 placeholder for sensitive ops). Alpine `click.outside` / Escape / Close dismiss. |
| `broadcast/_event_drawer_not_found.html` | The 404 drawer fragment for a missing / cross-tenant audit id. |
| `broadcast/wall.html` | The no-chrome wall-monitor view (Task #869): a standalone document (not `extends base.html`) that drops the sidebar / navbar / filter bar and embeds `_feed.html` with `wall=True` (taller rows + auto-scroll). |
| `broadcast/_history.html` | The Last-24h replay fragment (Task #869): seeds the shared `broadcastFeed` controller with the historical events the `/ui/broadcast/history` route pulled via `XRANGE`, so the rows render through `_event_row.html` and open the same drawer as the live feed. |
| `static/src/app/broadcast-feed.js` | The `broadcastFeed` Alpine component (registered on `alpine:init`). External deferred script, not inline, to stay CSP-ready. Holds the parse + prepend + 1000-row trim, the `visibleEvents` op_id client filter, the `init` re-read of the live op_id input (so the filter survives a server-side fragment swap, gated to `#broadcast-feed` only), the `openDrawer` helper, the badge/timestamp/payload/aggregate-only helpers, the `opts.seedFrom` data-island seed (for the history replay pane — reads + `JSON.parse`s a `<script type="application/json">` block rather than receiving events through the `x-data` attribute, closing B1's stored-XSS hole), and the `opts.autoScroll` wall-monitor behaviour. |

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
| `POST` | `/ui/memory/create` | Submit handler. Form-encoded body shape mirrors `/api/v1/memory`'s `RememberBody`. Returns 204 + `HX-Redirect: /ui/memory`. |
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
  `X-CSRF-Token` header (HTMX inherits it from the page-level
  `hx-headers` directive) or the `csrf_token` form field.
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
  cookie 403s),
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
| `GET` | `/ui/connectors/import` | (T3 #875) Full bulk-import page: paste box + file upload. Tenant_admin gated. |
| `POST` | `/ui/connectors/import` | (T3 #875) Parse the pasted / uploaded `targets.yaml` (`yaml.safe_load`) and render the CREATE-vs-UPDATE preview table; parse errors render inline (422, no 500). Read-only — no writes. |
| `POST` | `/ui/connectors/import/confirm` | (T3 #875) Re-parse + re-classify against the tenant's current targets, then apply the plan in-process via `create_target` (new) + `update_target` (existing); renders the result summary (N created, M updated). |

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

### Files

* `backend/src/meho_backplane/ui/routes/connectors/__init__.py` — umbrella router factory.
* `backend/src/meho_backplane/ui/routes/connectors/list_view.py` — list view handler + freshness classifier (T2 adds the `is_tenant_admin` role probe for the create button).
* `backend/src/meho_backplane/ui/routes/connectors/detail.py` — detail handler + connector_id resolution + ops matrix query + recent-ops query.
* `backend/src/meho_backplane/ui/routes/connectors/operator.py` — role-probe (read) + operator-403 (write).
* `backend/src/meho_backplane/ui/routes/connectors/probe.py` — re-probe POST handler.
* `backend/src/meho_backplane/ui/routes/connectors/forms.py` — (T2 #874) create / edit render + submit helpers; builds `TargetCreate` / `TargetUpdate`, delegates to the REST handlers, maps `ValidationError` to per-field form errors.
* `backend/src/meho_backplane/ui/routes/connectors/forms_router.py` — (T2 #874) create / edit route registration (thin FastAPI wrappers; tenant_admin-gated deps).
* `backend/src/meho_backplane/ui/templates/connectors/_target_form_fields.html` — (T2 #874) shared form fields for both modals.
* `backend/src/meho_backplane/ui/templates/connectors/_create_modal.html` — (T2 #874) create modal.
* `backend/src/meho_backplane/ui/templates/connectors/_edit_modal.html` — (T2 #874) edit modal.
* `backend/tests/test_ui_connectors_forms.py` — (T2 #874) create / edit form tests: modal render (admin) + 403 (operator), create success + Pydantic validation errors (port out of range, empty name), edit pre-population + PATCH, cross-tenant isolation, CSRF enforcement.
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

**Parse errors render inline.** `yaml.safe_load` failures, a non-mapping
root, a missing / empty `targets:` list, and per-entry required-field
violations (`name` / `product` / `host`) all render an inline error in
the preview fragment with HTTP 422 — never a 500.

#### Files (Task #875)

* `backend/src/meho_backplane/ui/routes/connectors/import_view.py` — parse (`yaml.safe_load`) + key-mapping + CREATE/UPDATE classification (port of `import.go`'s `mapEntry` / `buildLivePlan`) + render helpers + in-process confirm.
* `backend/src/meho_backplane/ui/routes/connectors/import_router.py` — thin FastAPI route wrappers (multipart paste + upload parse; tenant_admin-gated deps); registered before the detail route so the literal `/ui/connectors/import` paths win the first-match lookup over `/ui/connectors/{name}`.
* `backend/src/meho_backplane/ui/templates/connectors/import.html` — full-page paste-box + upload form.
* `backend/src/meho_backplane/ui/templates/connectors/_import_preview.html` — preview table fragment (CREATE/UPDATE badges + per-entry warnings + confirm form, or an inline parse error).
* `backend/src/meho_backplane/ui/templates/connectors/_import_result.html` — result summary fragment (N created, M updated).
* `backend/tests/test_ui_connectors_import.py` — `build_plan` mapping / classification unit tests (extras spill, explicit-extras merge, fingerprint drop, sparse UPDATE) + behavioural tests: auth / RBAC (403 operator, 403 missing CSRF), preview (paste + upload + parse error + missing-field error), confirm (in-process create + update, sparse PATCH preserves omitted columns, malformed-YAML no-write), cross-tenant isolation (import lands in caller tenant only; same-name in another tenant does not flip CREATE→UPDATE).
