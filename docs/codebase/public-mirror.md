<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Public mirror pipeline

## Overview

MEHO is developed in a private repository (`evoila-bosnia/MEHO.X`) and projected to a
public repository (`evoila/meho`) on every green build of `main`. The projection is
driven by a single GitHub Actions workflow that assembles a filtered "staging tree"
from an explicit allowlist, sanity-checks it, and pushes the result to the public
remote. This document explains how that pipeline is built, why each defensive layer
exists, and what any future maintainer needs to know before touching it.

## Key files

- [.github/workflows/mirror-to-public.yml](../../.github/workflows/mirror-to-public.yml) —
  the only authorized path from private to public. Triggered by a successful `CI`
  workflow run on `main`.
- [.github/workflows/public-allowlist.txt](../../.github/workflows/public-allowlist.txt) —
  the single source of truth for what ships publicly. One repo-relative path per line;
  comments start with `#`; blank lines ignored. Directories ship recursively.
- [scripts/assemble-public-tree.sh](../../scripts/assemble-public-tree.sh) —
  the extracted assembly logic the workflow calls to build the staging tree.
  Runs locally (`scripts/assemble-public-tree.sh --dry-run` previews the file
  list without touching disk) so allowlist edits can be validated before merge.
- [scripts/hooks/block-public-remote-push.sh](../../scripts/hooks/block-public-remote-push.sh) —
  developer-side safety hook that blocks direct pushes to `evoila/meho`. Wired as
  a local `pre-push` hook in [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)
  and installed by `scripts/dev-setup.sh`.
- [scripts/dev-setup.sh](../../scripts/dev-setup.sh) — one-time setup that runs
  `pre-commit install --install-hooks` to wire every stage (pre-commit, pre-push,
  commit-msg) into a fresh clone.
- [docs/development/dual-repo-workflow.md](../development/dual-repo-workflow.md) —
  developer- and maintainer-facing operational guide. Explains the workflow, mirror
  modes, external-PR handling, and PAT management.

## Design principle: fail closed

The pipeline uses an **allowlist**, not a blacklist. The cost of shipping a file that
should have been private is much higher than the cost of withholding a file that
should have been public:

- A leaked file propagates through forks, clones, and GitHub's reflog. A force-push
  does not recall it.
- A withheld file is recovered by editing one line in `public-allowlist.txt` and
  merging.

Every design decision in this area cascades from that asymmetry. New top-level files
and directories are private by default; they appear publicly only when someone
explicitly adds them to the allowlist. This makes allowlist diffs the load-bearing
review surface — reviewers check each addition against "is this safe to publish?"
before approving.

The allowlist replaced an earlier strip-list approach because blacklists drift: every
new private directory required a corresponding strip-list update that was easy to
forget, and forgetting was silent.

## Control flow

1. Developer merges to private `main`.
2. The `CI` workflow runs against the merge commit.
3. On `CI` success, `mirror-to-public.yml` is triggered via `workflow_run`.
4. **Checkout**: `actions/checkout` runs with `persist-credentials: false`. This is
   non-obvious but critical. Without it, the ambient `GITHUB_TOKEN` gets written into
   local git config and subsequently overrides the PAT embedded in the public remote
   URL, producing a 403 on push.
5. **Assemble staging tree**: the workflow calls
   [`scripts/assemble-public-tree.sh`](../../scripts/assemble-public-tree.sh),
   which reads `public-allowlist.txt` line by line, validates every entry,
   skips comments and blanks, and copies each listed path into a temp directory
   (`mktemp -d`) while preserving the repo's relative tree shape via
   `mkdir -p $(dirname)` + `cp -a`. The script prints the staging path on
   stdout and sends every diagnostic to stderr, so the workflow captures it
   as `STAGING=$(scripts/assemble-public-tree.sh)`. The same script supports
   `--dry-run` for local previews and `--allowlist PATH` for test fixtures.
6. **Required-path check**: after assembly, the workflow asserts that
   `meho_app`, `pyproject.toml`, `README.md`, and `LICENSE` are all present in the
   staging tree. If any one is missing, the run aborts — the allowlist is considered
   broken. This guards against catastrophic allowlist edits.
7. **Derived denylist defensive check**: `find -mindepth 1 -maxdepth 1` scans the
   *top level* of the staging tree and rejects any entry whose basename is not
   present in the set of top-level components derived from `public-allowlist.txt`.
   The allowed set is built at parse time from the same loop that copies entries
   into staging, so there is no second list to maintain and no drift between a
   denylist and the allowlist. If any staging entry falls outside the derived set,
   the run aborts with an error annotation listing the offending paths. The check
   is scoped to the top level so that nested fixtures inside legitimately-shipped
   directories (e.g. a sample spec that happens to contain a `.vscode/` folder) are
   not false-positives.
8. **Push**: the workflow picks a push strategy based on the `PUBLIC_MIRROR_MODE`
   GitHub Actions variable, defaulting to `orphan`.

## Mirror modes

Two modes share all of the staging logic and differ only in how the resulting tree
is pushed to public `main`:

### Orphan mode (default)

Creates an orphan branch locally (`git checkout --orphan mirror-staging`), wipes the
working tree, overlays the staged allowlist tree with `rsync -a --exclude '.git/'`,
commits once, and force-pushes to public `main`. The public repo ends up with a
single commit whose parent chain does not reach back into private history.

Appropriate when:

- The public repo is a snapshot distribution, not a living history.
- External PRs are not yet accepted (they would be obliterated by the next sync).
- The team wants to avoid committing to specific public SHAs before OSS launch.

The force-push is the most dangerous single operation in this area — a bug in
staging will overwrite the public repo's main branch with whatever broken content
the staging tree contains. The defensive gates earlier in the workflow exist
specifically because this push cannot be undone.

### Incremental mode

Fetches `public/main`, creates a local branch anchored at its tip, overlays the
staged tree with `rsync -a --delete --exclude '.git/'`, and pushes a normal commit
(no force). The `--delete` flag is essential: without it, files removed from the
allowlist would linger on public because rsync would only copy new and changed
files. With `--delete`, the destination exactly mirrors the staged source.

A guard before committing (`git diff --cached --quiet`) skips empty pushes, so
private-only changes (e.g. edits inside `.planning/`) do not produce empty public
commits.

Appropriate when:

- The public repo should have a living history contributors can reason about.
- External PRs are accepted and merged on the public side (then cherry-picked back
  into private).
- Force-push hazards are no longer tolerable.

Mode is toggled via the `PUBLIC_MIRROR_MODE` repository-level variable with no
workflow edits required. The workflow validates the value as either `orphan` or
`incremental` (case-insensitively) and aborts on anything else.

## Authentication

The workflow authenticates to the public repo via a classic GitHub Personal Access
Token stored in `secrets.PUBLIC_REPO_PAT`. The PAT must have `contents: write`
scoped to `evoila/meho` and is embedded in the public remote URL using the
`x-access-token` convention:

```
https://x-access-token:${PAT}@github.com/evoila/meho.git
```

`persist-credentials: false` on the checkout step is required for this to work —
otherwise git's credential helper latches onto the ambient `GITHUB_TOKEN` from the
checkout and ignores the URL-embedded PAT on push.

Classic PATs expire (maximum one-year lifetime). The current mitigation is a manual
calendar reminder. There is no automated probe that warns when the token is about to
expire, so expiry produces a silent mirror failure on the first sync after the
deadline. A scheduled probe or a migration to a GitHub App installation token would
remove this operational risk.

## Defensive layers

The pipeline has three independent defensive gates. Each exists because the others
are not sufficient on their own:

| Gate | Where | What it catches | What it misses |
|---|---|---|---|
| Pre-push hook | Developer machines | Accidental `git push` to a public remote | Devs who clone public directly, or use `--no-verify` |
| Required-paths check | `mirror-to-public.yml` assemble step | Allowlist edits that remove load-bearing paths | Allowlist edits that *add* something harmful |
| Derived denylist check | Same | Unexpected top-level entries in staging that have no corresponding allowlist entry (parse bugs, copy bugs, malformed paths) | A harmful top-level that is *explicitly* on the allowlist; leaks under an allowlisted top-level (e.g. a secret checked into `docs/`) |

The derived denylist is a safety net for the staging-tree assembly step — it
catches the case where a top-level entry ends up in staging without a matching
allowlist line (an allowlist parse bug, a copy-path mistake, or a malformed
entry that resolved to something unexpected on disk). It is computed from the
allowlist itself, so there is no second list to maintain and no drift between
the two.

What it deliberately does *not* catch: an explicit allowlist edit that *adds*
a harmful top-level directory (e.g. someone adds `.planning` to
`public-allowlist.txt`). Because the allowed set is derived from the allowlist,
any such entry is allowed by definition at this stage. Review of allowlist
diffs remains the load-bearing gate against that class of mistake — reinforced
by the private-repo PR-time check that rejects additions under known-private
top-levels, which runs before an allowlist edit can ever be merged.

## Known issues

- **PAT expiration has no monitoring.** Silent failure mode on the first sync after
  expiry. See [Authentication](#authentication).
- **Assembly logic has no tests.** The extracted
  [`scripts/assemble-public-tree.sh`](../../scripts/assemble-public-tree.sh) is
  load-bearing for every public release and still has no automated regression
  coverage — bats tests are tracked separately.

## References

- [.github/workflows/mirror-to-public.yml](../../.github/workflows/mirror-to-public.yml) —
  the workflow itself. Short enough to read top to bottom.
- [.github/workflows/public-allowlist.txt](../../.github/workflows/public-allowlist.txt) —
  the authoritative list of what ships publicly.
- [docs/development/dual-repo-workflow.md](../development/dual-repo-workflow.md) —
  operational guide for developers and release owners.
- [GitHub Actions — `actions/checkout` persist-credentials](https://github.com/actions/checkout#usage) —
  upstream documentation for the auth interaction that makes `persist-credentials: false`
  necessary.
- [Pro Git — Orphan branches](https://git-scm.com/docs/git-checkout#Documentation/git-checkout.txt---orphanltnew-branchgt) —
  reference for `git checkout --orphan` semantics.
- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — format target for the
  repo's `CHANGELOG.md`.
