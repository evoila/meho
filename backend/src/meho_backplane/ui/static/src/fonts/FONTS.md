# Vendored webfonts — Operator Console brand typography

Self-hosted woff2 (latin subset only) served by the existing
`/ui/static` StaticFiles mount; loaded via `@font-face` in
[`../styles.css`](../styles.css). No CDN, no `node_modules` — the same
vendoring posture as [`../vendor/VENDOR.md`](../vendor/VENDOR.md).

All three families are licensed under the SIL Open Font License 1.1.
OFL is a file-level license that travels with the font files, not with
the repository's code license — vendoring OFL fonts inside an
Apache-2.0 repo is fine as long as each family's OFL text ships beside
the files, which the `OFL-*.txt` files here do.

| File | Family | Style | Source | SHA256 |
| ---- | ------ | ----- | ------ | ------ |
| `bricolage-grotesque-latin-var.woff2` | Bricolage Grotesque | variable (opsz 12–96, wght 200–800) | fonts.gstatic.com (Google Fonts v9, latin) | `85f55a58a31e61a2e19e8bb25fed503181bf2a6b4cab76c589992cfaac377447` |
| `schibsted-grotesk-latin-var.woff2` | Schibsted Grotesk | variable (wght 400–900) | fonts.gstatic.com (Google Fonts v7, latin) | `e3b56e90510a84ac0ed465b822e112983eaf58e37436bf769681c31f77b1f3a7` |
| `ibm-plex-mono-latin-400.woff2` | IBM Plex Mono | 400 | fonts.gstatic.com (Google Fonts v20, latin) | `c36f509c0a8f9f85f29cb44bc8701d8a9e0b14c499e77a884f789ead7093a7ac` |
| `ibm-plex-mono-latin-500.woff2` | IBM Plex Mono | 500 | fonts.gstatic.com (Google Fonts v20, latin) | `a76f53ca6612e7b3828eec2311098675b7f9849ae4169a8bcef6302aec02a6c0` |

Licenses: `OFL-bricolage-grotesque.txt` (© 2022 The Bricolage Grotesque
Project Authors), `OFL-schibsted-grotesk.txt` (© 2023 The
Schibsted-Grotesk Project Authors), `OFL-ibm-plex-mono.txt` (© 2017 IBM
Corp., Reserved Font Name "Plex") — fetched from the canonical
`google/fonts` OFL directory.

Role mapping (see the `@theme` block in `styles.css`):

- **Bricolage Grotesque** → `--font-display` — wordmark + page titles.
- **Schibsted Grotesk** → `--font-sans` — all UI text.
- **IBM Plex Mono** → `--font-mono` — op ids, UUIDs, paths, payloads,
  timestamps.

MIME note: Starlette serves these via `mimetypes`; even on a platform
that maps `.woff2` to `application/octet-stream`, browsers do not
MIME-block font loads (`nosniff` applies to scripts/styles only).

Refresh policy: bump in a dedicated PR that updates the files AND the
SHA256 rows here, mirroring the Tailwind-CLI refresh policy documented
in `backend/Dockerfile`.
