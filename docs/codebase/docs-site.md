# Docs site (MkDocs Material + mike)

## Overview

MEHO's published documentation site — <https://evoila.github.io/meho/>
— is built with MkDocs Material from sources under `docs-site/` and
versioned with [mike](https://github.com/jimporter/mike) on the
`gh-pages` branch, which GitHub Pages serves. The scaffold landed with
task #2670 (initiative #2663). The *Start here* and *Install &
operate* sections carry real content (#2671: landing page,
`architecture.md`, and the `install/` trail pages); #2672 (connect
clients) and #2673 (do-real-work guides) migrate the remaining
sections. The generated reference section lands with the #2662
contract snapshots.

The site is the *operator/adopter-facing* documentation. The `docs/`
tree (this directory's parent) remains the contributor-facing material
and is deliberately not the site's source: pointing MkDocs at `docs/`
would drag every internal walkthrough through the strict build and
force nav curation of files that were never written for outsiders.

## Key pieces

| Piece | Where | Role |
|---|---|---|
| `mkdocs.yml` | repo root | Site config: Material theme (light/dark toggle), strict mode, nav skeleton, mike version provider |
| `docs-site/` | repo root | Page sources; one directory per top-level nav section, `index.md` each |
| `.github/workflows/docs-site.yml` | workflows | PR check (`mkdocs build --strict`) + tag-push deploy (mike → gh-pages) |
| `backend/pyproject.toml` `[dependency-groups].docs` | backend | Locked toolchain pins (mkdocs-material, mike) riding `uv.lock` + Dependabot |
| `gh-pages` branch | remote only | mike-managed publish target; never edited by hand |

## Versioning model

One docs version **per minor**, with a `latest` alias:

- Tag `v0.26.3` → mike deploys version `0.26` and re-points `latest`
  (`mike deploy --push --update-aliases 0.26 latest`).
- A patch tag republishes its minor's docs in place — no per-patch
  versions.
- `mike set-default --push latest` keeps the site root redirecting to
  the newest docs.
- Material's version selector is enabled via `extra.version.provider:
  mike` in `mkdocs.yml`; it reads mike's `versions.json` on gh-pages.
- Pre-release tags (`v1.0.0-rc.1`) deploy under their minor (`1.0`) —
  the rc docs are that minor's docs in progress.

## Control flow (deploy)

1. `v*` tag push triggers the `deploy` job (`docs-site.yml`).
2. Job fetches `gh-pages` if it exists (mike commits onto the local
   ref), configures the `github-actions[bot]` committer identity, and
   installs the locked toolchain via `uv sync --locked --only-group
   docs` in `backend/`.
3. `mike deploy` builds the site (strict — `strict: true` in
   `mkdocs.yml` applies to mike's internal build too) and pushes the
   version + alias to `gh-pages`.
4. GitHub Pages serves the branch. A job-level concurrency group
   (`docs-site-gh-pages`, no cancel) serialises publishes so two tags
   can't race the branch update.
5. A final **reachability gate** polls the freshly-published
   `https://evoila.github.io/meho/<MAJOR.MINOR>/` (the version path
   this run wrote, passed from the deploy step via a `version` output —
   *not* the site root, whose redirect to `latest` would return 200 and
   mask a failed version publish) until it returns HTTP 200, up to a
   10-minute deadline at 15 s intervals. If the deadline passes the job
   fails with an error naming the one-time Pages setup. This turns a
   publish onto a repo where Pages was never enabled — which used to
   report `success` while the site 404'd (#2741, the v0.26.0 incident)
   — into a red run.

PRs touching the site (or the toolchain lock) get a `mkdocs build
--strict` check instead; nothing deploys from PRs or main pushes.

## Local development

```bash
cd backend && uv sync --locked --only-group docs && cd ..
uv run --project backend --no-sync mkdocs serve   # live-reload preview
uv run --project backend --no-sync mkdocs build --strict
```

`site/` (the build output) is gitignored.

## Embedding contributor docs without moving them

`pymdownx.snippets` is configured with the repo root on its
`base_path`, so a site page can embed a contributor file in place:

```markdown
--8<-- "docs/deploying.md"
```

Use this when promoting existing material (e.g. `docs/deploying.md`,
`docs/cross-repo/mcp-client-setup.md`) so the in-repo file stays the
single source of truth. `check_paths: true` makes a missing snippet a
build failure, not a silent empty include.

## One-time setup

GitHub Pages must be enabled once by a maintainer (CI cannot safely
mutate repo settings — same custody rule as image.yml's GHCR
visibility flip): **Settings → Pages → Deploy from a branch →
`gh-pages` / root**. The branch exists after the first tag-triggered
deploy. The deploy job's reachability gate (control-flow step 5) makes
skipping this setup fail loudly: the run goes red instead of publishing
a site that returns 404 (#2741).

## Known issues / gotchas

- `mkdocs.yml` is strict: a page not referenced in `nav`, or any
  broken internal link, fails the build. Add new pages to `nav`.
- Mermaid diagrams render natively via the Material custom-fence
  config on `pymdownx.superfences` (#2671). That config uses the
  `!!python/name:` YAML tag, which `yaml.safe_load` rejects —
  `mkdocs.yml` is therefore excluded from the pre-commit `check-yaml`
  hook (the strict mkdocs build validates it far harder anyway).
- Cross-page anchor links are not validated by the strict build
  (only page links are). Heading slugs use the default python-markdown
  slugify: punctuation (em-dashes, colons, apostrophes) is stripped,
  whitespace collapses to single `-` — e.g. `## Step 1 — Pre-create…`
  → `#step-1-pre-create…`, never a double hyphen.
- The deploy job needs the `workflow`-scoped credential story only at
  *merge* time (the file under `.github/workflows/` requires the
  `workflow` OAuth scope to push).
- mike owns `gh-pages` entirely — manual commits there will be
  clobbered or cause non-fast-forward failures.

## References

- Initiative #2663 (information architecture), goal #2661.
- `docs/RELEASING.md` — the docs site is the fourth tag-published
  artefact.
- mkdocs-material versioning guide:
  <https://squidfunk.github.io/mkdocs-material/setup/setting-up-versioning/>
