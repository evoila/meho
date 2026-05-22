# Releasing MEHO

The precise, step-ordered runbook for cutting a MEHO release. Read it
top to bottom the first time; use the [checklist](#checklist) thereafter.
The `/release` skill walks these same steps interactively.

> **Why this exists.** v0.5.0 was tagged without rolling the CHANGELOG,
> so its release notes omitted ~24 shipped PRs; v0.3.2 was skipped
> entirely. Both failures are a missing *roll-the-changelog-before-you-tag*
> step. This runbook makes that step load-bearing.

## What a release is

A release is **a `v*` git tag**. The tag is the single source of truth
for the version — three artefacts derive from it, all published
automatically on the tag push:

| Artefact | Where | Workflow | Version source |
|---|---|---|---|
| Backplane image | `ghcr.io/evoila/meho:vX.Y.Z` | [`image.yml`](../.github/workflows/image.yml) | git tag |
| Helm chart | `oci://ghcr.io/evoila/meho-chart` | [`chart.yml`](../.github/workflows/chart.yml) | chart workflow sets `version` + `appVersion` |
| CLI tarballs + GitHub Release | [releases](https://github.com/evoila/meho/releases) | [`cli-release.yml`](../.github/workflows/cli-release.yml) | GoReleaser bakes `{{.Tag}}` into `version.Version` |

All three trigger on `tags: ['v*']` (image/chart have **no** path filter
on tags — a tag always publishes). `cli-release.yml` is **tag-only**: a
main push never cuts a release.

## Versioning

[SemVer](https://semver.org). The version lives **only in the git tag** —
do not hand-edit version files:

- `backend/pyproject.toml` stays `0.1.0-dev` (the image is tagged from the
  git tag, not from pyproject).
- `deploy/charts/meho/Chart.yaml` `version`/`appVersion` are
  **calver-bumped by `chart.yml`** at publish time.
- The CLI version is baked from `{{.Tag}}` by GoReleaser
  ([`cli/.goreleaser.yaml`](../cli/.goreleaser.yaml)).

Choose the bump from what's in `[Unreleased]`:

- **patch** (`x.y.Z`) — fixes / docs only.
- **minor** (`x.Y.0`) — new features, new connectors, additive API.
- **major** (`X.0.0`) — breaking-change-heavy (pre-1.0 we still ship
  breaking changes in minors with a migration recipe; see the CHANGELOG
  `Breaking changes` convention).

## The runbook

### 1. Pre-flight

- [ ] The release Initiative's Tasks are merged and closed.
- [ ] CI is green on `main`.
- [ ] Pick `vX.Y.Z`.

### 2. Roll the CHANGELOG — the load-bearing step

This is the step that keeps getting skipped. Do it in the
**release-cutting PR**, before the tag.

1. **Audit completeness.** Every merged PR since the last tag must have a
   bullet. Cross-check:

   ```bash
   PREV=$(git describe --tags --abbrev=0 HEAD)   # last tag
   # PRs merged since the last tag:
   git log "$PREV"..main --pretty=format:'%s' | grep -oiE '#[0-9]+' | sort -un > /tmp/shipped.txt
   # PR/issue numbers already cited in [Unreleased]:
   awk '/^## \[Unreleased\]/{u=1;next} /^## \[/{u=0} u' CHANGELOG.md \
     | grep -oiE '#[0-9]+' | sort -un > /tmp/cited.txt
   comm -23 /tmp/shipped.txt /tmp/cited.txt   # numbers with NO bullet — backfill these
   ```

2. **Backfill** the missing bullets under the right category
   (Added / Changed / Fixed / Security / Breaking changes). Apply the
   **connector ship-state rubric** — a connector line must state its
   *exact* state (skeleton / dispatch+catalog / loader-wired /
   production), never the next state up
   ([`docs/codebase/connector-release-readiness.md`](codebase/connector-release-readiness.md)).

3. **Roll.** Move every `[Unreleased]` bullet that ships in this release
   under a new `## [X.Y.Z] - YYYY-MM-DD` heading; leave a fresh empty
   `## [Unreleased]`. **Leave behind** any bullet whose commit is *not*
   in this tag (post-tag work stays in `[Unreleased]`).

### 3. Release-cutting PR → merge

Open the PR (CHANGELOG roll + any release-only edits), get it reviewed,
merge to `main`.

### 4. Tag + push

```bash
git checkout main && git pull
git tag vX.Y.Z            # on the merge commit
git push origin vX.Y.Z
```

The push fans out to `cli-release.yml`, `image.yml`, `chart.yml`.

### 5. Verify the artefacts

- [ ] **GitHub Release** exists and its body is the `[X.Y.Z]` CHANGELOG
  section — **not** the empty `[Unreleased]` fallback (the v0.5.0 failure
  mode). A hand-curated narrative body is fine, but it must cover the
  release; keep `CHANGELOG.md` as the authoritative detail.
- [ ] Image `ghcr.io/evoila/meho:vX.Y.Z` published (`image.yml` green).
- [ ] Chart published to `oci://ghcr.io/evoila/meho-chart` (`chart.yml` green).
- [ ] CLI tarballs attached to the Release; `meho version` prints `vX.Y.Z`.

### 6. Deploy + smoke

- [ ] Deploy to `rke2-infra`.
- [ ] Run smoke; confirm **smoke-green** (same pattern as
  v0.2.0 / v0.3.0 / v0.3.1).

### 7. Post-release

- [ ] Close the release Initiative; update the board / MVP roadmap.
- [ ] Announce to consumers (e.g. `evoila-bosnia/claude-rdc-hetzner-dc`).

## Checklist

```
[ ] 1. Tasks merged + CI green on main; version picked
[ ] 2. CHANGELOG: completeness audited, missing bullets backfilled,
       [Unreleased] rolled to [X.Y.Z] (post-tag work left behind)
[ ] 3. Release-cutting PR merged to main
[ ] 4. Tagged vX.Y.Z + pushed
[ ] 5. GH Release notes correct (not [Unreleased] fallback); image, chart,
       CLI tarballs all published
[ ] 6. Deployed to rke2-infra; smoke-green
[ ] 7. Initiative closed; board/roadmap updated; consumers notified
```

## Failure modes seen (do not repeat)

- **v0.3.1 → v0.5.0:** `v0.3.2` skipped, and the CHANGELOG was never
  rolled, so v0.5.0's notes omitted ~24 PRs. Fixed retroactively; step 2
  prevents recurrence.
- **Empty release notes:** `cli-release.yml` falls back to `[Unreleased]`
  when no `## [X.Y.Z]` section exists at tag time. Always roll (step 2)
  *before* tagging (step 4).
