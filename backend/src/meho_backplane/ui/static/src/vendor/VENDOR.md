# Vendored UI dependencies — pinned SHA256 manifest

This directory contains the **vendored** JavaScript and CSS-plugin
dependencies the MEHO Operator Console (Initiative #337, Task #863)
serves verbatim from `/ui/static/src/vendor/*`.

Per [v0.2-decisions.md #9 + #10](../../../../../../../docs/planning/v0.2-decisions.md):
the UI ships with **zero `node_modules`**, **no `npm` in CI**, and **no
runtime CDN fetches**. Every browser-bound asset is committed to this
repo and its content is pinned by SHA256 below. The Dockerfile and any
future verification step assert the recorded SHA256 still matches the
committed file (tamper-evident vendoring).

Tailwind 4 is **not** in this directory because Tailwind 4 itself is a
build-time-only dependency (the standalone CLI binary, pinned by
SHA256 in [`backend/Dockerfile`](../../../../../Dockerfile), produces
`static/dist/tailwind.css` at image build). Only DaisyUI 5 ships as a
loaded plugin (`@plugin "./vendor/daisyui.js"` in
[`../styles.css`](../styles.css)).

## Pinned assets

| File | Library | Version | SHA256 | Source |
| ---- | ------- | ------- | ------ | ------ |
| `htmx.min.js` | HTMX | 2.0.9 | `57d9191515339922bd1356d7b2d80b1ee3b29f1b3a2c65a078bb8b2e8fd9ae5f` | https://github.com/bigskysoftware/htmx/releases/download/v2.0.9/htmx.min.js |
| `sse.min.js` | htmx-ext-sse | 2.2.4 | `98a46496de0c3605fbffdce9167ba427bdd9553184f83f149c261891a92c0136` | https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/dist/sse.min.js |
| `alpine.min.js` | Alpine.js | 3.15.12 | `57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f` | https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js |
| `cytoscape.min.js` | Cytoscape.js | 3.33.4 | `bcd83f0e31eb175026a811db6dc1f24b4326000edffa402a10d0748c5be557b4` | https://cdn.jsdelivr.net/npm/cytoscape@3.33.4/dist/cytoscape.min.js |
| `daisyui.js` | DaisyUI | 5.5.20 | `a92e663a1f150d6db47920967b0485ee34f87bfe74d0a80045c3a3a73afbc657` | https://github.com/saadeghi/daisyui/releases/download/v5.5.20/daisyui.js |

## Why these sources

HTMX and DaisyUI publish browser-ready minified bundles as **GitHub
release assets** — preferred per the Initiative #337 work-item 1 brief
("GitHub release tarballs"). Alpine.js, Cytoscape.js, and the
`htmx-ext-sse` extension do not publish minified bundles as GitHub
release assets (their GitHub releases ship source tarballs only); their
canonical `dist/*.min.js` artifacts are published to npm and mirrored
byte-for-byte through jsDelivr at the pinned `@<version>` path. The
`htmx-ext-sse` byte sequence was cross-checked identical between
jsDelivr and unpkg before pinning. The SHA256 below covers the same
byte sequence either way — vendoring + pinning is the actual security
boundary, not the URL scheme.

`sse.min.js` is the SSE extension HTMX 2 split out of core (HTMX 1
bundled it; HTMX 2 ships it as a separate `hx-ext="sse"` plugin). It is
co-required by the dashboard recent-activity snippet (G10.0) and the
broadcast live feed (G10.1) — without it both `sse-connect` wrappers are
inert. The `2.2.x` line targets HTMX `2.0.x`.

## Verification

To re-verify any pinned file against this manifest:

```bash
cd backend/src/meho_backplane/ui/static/src/vendor/
sha256sum -c <(awk -F'`' '/^\|/ && NF >= 8 {print $4 "  " $2}' VENDOR.md \
              | grep -v '^SHA256')
```

## Refresh procedure

When a vendored library needs a version bump (security fix, feature
the UI now depends on), file a dedicated PR titled
`chore(ui): bump <library> to <version>` containing:

1. The new minified file replacing the current one in this directory.
2. The updated row in the table above (version, SHA256, source URL).
3. The smoke-test note in the PR body confirming
   `python -m pytest backend/tests/test_ui_templates.py` still passes.

Never bump a vendored library in a feature PR — the supply-chain
audit trail wants every input movement on its own commit, mirroring
the `chore(backend): bump python:3.12-slim base digest to <new>`
discipline `backend/Dockerfile` already follows.
