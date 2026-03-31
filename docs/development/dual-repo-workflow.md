# Dual-Repo Development Workflow

MEHO uses a dual-repository architecture: a private repo for development and a public repo for the open-source community.

## Repository Structure

| Repository | Visibility | Purpose |
|-----------|-----------|---------|
| `evoila-bosnia/MEHO.X` | Private | Development, planning, CI |
| `evoila/meho` | Public | Open-source distribution |

## How the Mirror Works

1. Developers push to `main` on the **private** repo (`evoila-bosnia/MEHO.X`)
2. CI runs on the private repo
3. If CI passes, the `mirror-to-public` GitHub Action triggers
4. The action strips `.planning/`, `.claude/`, and `.cursor/` directories
5. The cleaned code is force-pushed to the **public** repo (`evoila/meho`)

## Developer Rules

1. **Always push to the private repo** (`origin`)
2. **Never push directly to the public repo** -- the pre-push hook blocks this
3. **Never commit `.planning/` files** to branches that will merge to main
4. **Run `scripts/dev-setup.sh`** after cloning to configure git hooks

## First-Time Setup

After cloning the private repo:
```bash
scripts/dev-setup.sh
```
This configures `core.hooksPath` to `.githooks/`, enabling the pre-push safety hook.

## Handling External PRs

When the community submits a PR to the public repo:
1. Review and approve on the public repo as normal
2. Merge on the public repo
3. Cherry-pick the merge commit back to the private repo:
   ```bash
   git remote add public https://github.com/evoila/meho.git
   git fetch public
   git cherry-pick <merge-commit-sha>
   ```
4. Push to private main -- the next mirror sync will be a no-op (changes already exist)

## Emergency: Direct Push to Public

If the mirror is broken and you must push directly:
```bash
git push --no-verify public main
```
The `--no-verify` flag bypasses the pre-push hook. Use sparingly.

## Secret Management

| Secret | Location | Purpose |
|--------|----------|---------|
| `PUBLIC_REPO_PAT` | Private repo secrets | Mirror workflow pushes to public repo |
| `SONAR_TOKEN` | Both repos | SonarCloud quality gate |
| `CODECOV_TOKEN` | Both repos | Coverage reporting |

The fine-grained PAT (`PUBLIC_REPO_PAT`) must have `contents: write` permission scoped to `evoila/meho`. It has a maximum 1-year expiration -- set a calendar reminder to renew.

## What Gets Stripped

The mirror removes these directories (they exist only in the private repo):
- `.planning/` -- GSD planning artifacts, roadmap, requirements
- `.claude/` -- Claude Code configuration
- `.cursor/` -- Cursor editor configuration
