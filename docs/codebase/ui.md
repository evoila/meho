# `ui/` — Operator-console chassis

Initiative [#337](https://github.com/evoila/meho/issues/337) (G10.0
Frontend chassis) introduces a server-rendered web UI inside the
backplane FastAPI process at `/ui/*`. This doc covers the **chassis**
that Task [#863](https://github.com/evoila/meho/issues/863) (G10.0-T2)
landed — module layout, template-rendering shape, Tailwind 4 build
pipeline, vendored JS assets. Subsequent Tasks fill in the missing
pieces (`#864` session storage, `#865` auth flow, `#866` FastAPI
mount + dashboard + smoke test); this doc grows with each Task.

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
| `meho_backplane.ui.routes` | Stub package for chassis Task. T5 (#866) lands the `APIRouter` instance + dashboard view + the five surface stub routes. |
| `meho_backplane.ui.auth` | Stub package for chassis Task. T3 (#864) lands `web_session` ORM + encrypted token custody; T4 (#865) lands `/ui/auth/{login,callback,logout}` + session middleware. |

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

## Vendored asset versions

Current as of Task #863 (refresh procedure documented in
`VENDOR.md`):

| Library | Version |
| ------- | ------- |
| Tailwind CSS | 4.3.0 |
| DaisyUI | 5.5.20 |
| HTMX | 2.0.9 |
| Alpine.js | 3.15.12 |
| Cytoscape.js | 3.33.4 |

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
- **No new Python deps** for the chassis. T3 (#864) adds `cryptography`
  (already a dev dep) to runtime; T4 (#865) leans on the existing
  `authlib` dependency.
- **Tailwind standalone CLI** — runtime-of-image-build dependency
  only; never enters the running container or the wheel.

## Known issues / open items

- **`static/dist/tailwind.css` is missing on first uvicorn start in
  a fresh clone**. Local dev must run `tailwindcss --watch` once
  before the FastAPI mount works. T5 (#866) adds a startup hook that
  surfaces the operator-facing remediation rather than 404-ing the
  CSS file silently.
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
