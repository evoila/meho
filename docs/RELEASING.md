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
- [ ] **Release only from a confirmed-green `main` HEAD.** Every publish
  workflow (`image.yml` / `cli-release.yml` / `chart.yml`) fires on the
  `v*` tag with **no** other gate, so the tag publishes whatever `main`
  is at — a red `main` ships a broken or incomplete image, chart, and
  CLI. Confirm the **tagged commit's** `main` CI run is green before
  step 4:

  ```bash
  gh run list --repo evoila/meho --branch main --limit 5 \
    --json headSha,conclusion,status,name,url
  ```

- [ ] **"cancelled" ≠ "green".** A cancelled CI run is *inconclusive*,
  not a pass. Merge-storm concurrency cancellation (a later push
  cancels an in-flight run on the same ref) is the common cause, and a
  cancelled run reads green-ish in a glance — it is not. If the latest
  `main` run for the tagged commit is `cancelled`, **re-run it and wait
  for a real `success`** before tagging:

  ```bash
  gh run rerun <run-id> --repo evoila/meho   # then re-check conclusion
  ```

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

Before merging, run the **release-body path-freshness gate**. The same
recurring class of defect that motivated #928 at PR time (snapshot
drifts from the route table) shows up at release time as paths cited
in the release body that don't exist as written. Three consecutive
releases shipped with this drift (v0.5.0 missing notes entirely,
v0.5.1 catalog-vs-dispatch mismatch, v0.6.0 audit/replay +
tenant_conventions mismatch — see
[`docs/codebase/release-body-freshness.md`](codebase/release-body-freshness.md)).

```bash
# Extract the candidate release body to a file (the
# cli-release.yml workflow uses the same awk shape).
PREV=$(git describe --tags --abbrev=0 HEAD)
VERSION=X.Y.Z   # the version you picked in step 1
awk -v pat="${VERSION//./\\.}" '
  BEGIN { in_section = 0 }
  $0 ~ "^## \\[" pat "\\]" { in_section = 1; next }
  in_section && /^## / { exit }
  in_section { print }
' CHANGELOG.md > /tmp/release-body.md

# Assert every cited path resolves in the published OpenAPI snapshot.
cd backend
uv run python ../scripts/release/check_release_body_paths.py \
  --release-body /tmp/release-body.md \
  --openapi-snapshot ../cli/api/openapi.json
```

Exit 0 → proceed. Exit 1 → the script lists each unresolved citation
plus the closest matching snapshot path; amend the release body to
cite the shipped path (or pass `--allow-path` if the citation is
intentionally outside the snapshot's surface). The script tolerates
concrete IDs in citations (UUIDs / digits resolve against the
matching templated form) so example URLs in prose don't trip the
gate.

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
- [ ] Audit replay (G8.2) auto-lights-up when MCP clients send
  `Mcp-Session-Id` — no env var required after G0.14-T6 #1147.
  Older clients that don't send the header continue to work; audit
  rows just won't carry `agent_session_id`. To confirm the deploy is
  in the expected capture mode (`always` by default; `enforced` when
  `MCP_REQUIRE_SESSION_ID=true`), inspect
  `GET /api/v1/health`'s `mcp_session_id_capture` field.

### 7. Post-release

- [ ] Close the release Initiative; update the board / MVP roadmap.
- [ ] Announce to consumers (e.g. `evoila-bosnia/claude-rdc-hetzner-dc`).

## Checklist

```
[ ] 1. Tasks merged; main CI GREEN on the tagged commit (cancelled ≠ green —
       re-run + wait for success); version picked
[ ] 2. CHANGELOG: completeness audited, missing bullets backfilled,
       [Unreleased] rolled to [X.Y.Z] (post-tag work left behind)
[ ] 3. Release-body path-freshness gate green
       (scripts/release/check_release_body_paths.py — sister to #928);
       release-cutting PR merged to main
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
- **Tagging off a red / cancelled `main` (v0.5.0):** the publish
  workflows have no green-main gate — the `v*` tag publishes whatever
  `main` is. During the v0.5.0 cut, merge-storm concurrency cancellation
  made a red `main` read as "cancelled" rather than "failed", and a
  cancelled run was treated as good enough to tag. A cancelled run is
  inconclusive, not a pass. Step 1 now makes both gates explicit:
  confirm the tagged commit's `main` run is a real `success`, and
  re-run any `cancelled` run before trusting it.

## v0.7 follow-ups (deprecation removals)

When cutting v0.7, drop these v0.6.x compatibility shims:

- **MCP `add_to_memory.content` -> `body` alias shim** (G0.13-T4,
  #1134). Remove the `content` field from
  `backend/src/meho_backplane/mcp/tools/memory.py`'s `inputSchema`
  `properties`; drop the `anyOf` clause; restore
  `required: ["body", "scope"]`. Drop the body/content resolution
  branch + the `add_to_memory_field_deprecated` log emission from
  `_add_to_memory_handler`. Update
  `backend/tests/test_mcp_tools_memory.py` to assert `content` is
  rejected by the JSON-Schema gate (re-introduce a variant of the
  deleted `test_tools_call_add_to_memory_rejects_legacy_content_field`
  test). CHANGELOG `[0.7.0]` entry under **Removed** naming the shim
  and pointing at this paragraph.
