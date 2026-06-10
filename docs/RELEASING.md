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
   git log "$PREV"..main --pretty=format:'%s' | grep -oiE '#[0-9]+' | sort -u > /tmp/shipped.txt
   # PR/issue numbers already cited in [Unreleased]:
   awk '/^## \[Unreleased\]/{u=1;next} /^## \[/{u=0} u' CHANGELOG.md \
     | grep -oiE '#[0-9]+' | sort -u > /tmp/cited.txt
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
# Capture the release-cutting PR's merge commit explicitly — do NOT rely on
# HEAD. A concurrent merge between the Phase-2 merge and the tag push would
# otherwise drag unbulleted work into the tag (the exact failure /release
# exists to prevent). Substitute the PR number:
REL_SHA=$(gh pr view <phase2-pr#> --repo evoila/meho --json mergeCommit --jq .mergeCommit.oid)
git merge-base --is-ancestor "$REL_SHA" main || { echo "merge commit not on main — abort"; exit 1; }
git tag vX.Y.Z "$REL_SHA"   # tag the release-cutting merge commit by SHA, not HEAD
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

#### Maintainer one-time setup — GHCR package visibility

The first time `image.yml` pushes to `ghcr.io/evoila/meho`, GHCR creates
the package as **private**. A maintainer must flip visibility to
**public** once so anonymous `docker pull` works (this is a one-time
action, not per-release):

```bash
gh api --method PATCH /orgs/evoila/packages/container/meho \
  -f visibility=public
```

Or via the UI: GitHub org `evoila` → Packages → `meho` → Package settings →
Change visibility → **Public**.

Verify:

```bash
gh api /orgs/evoila/packages/container/meho --jq '.visibility'   # -> "public"
docker logout ghcr.io && docker pull ghcr.io/evoila/meho:main    # -> succeeds
```

### 6. Deploy + smoke

- [ ] Deploy to `rke2-infra`.
- [ ] Run smoke; confirm **smoke-green** (same pattern as
  v0.2.0 / v0.3.0 / v0.3.1).
- [ ] Audit replay (G8.2) auto-lights-up: the backplane **issues** an
  `Mcp-Session-Id` response header on every `initialize` per MCP
  2025-06-18 §"Session Management" rule 1 (G0.15-T4 #1213), and
  spec-conforming MCP clients (Claude Code, MCP Inspector, Cline)
  echo it on every subsequent POST (rule 2). The capture path then
  lands the id in `audit_log.agent_session_id`. No env var required
  after G0.14-T6 #1147 — capture is unconditional and issuance is
  unconditional. Pre-G0.15-T4 deploys (≤ v0.7.0) captured the header
  but never issued one, so audit rows from MCP clients that wait for
  a server-assigned id (the spec-compliant default) landed
  `agent_session_id: null` regardless of the `capture_mode: "always"`
  advertisement; v0.7.1+ closes this gap. To confirm the deploy is
  in the expected capture mode (`always` by default; `enforced` when
  `MCP_REQUIRE_SESSION_ID=true`), inspect
  `GET /api/v1/health`'s `mcp_session_id_capture` field.

### 6a. Post-deploy enablement — gated features

As of v0.6.0 the backplane ships four feature surfaces that **the
image alone does not light up** — each needs additional deploy
configuration that the smoke pass at step 6 does *not* exercise
(`claude-rdc-hetzner-dc#697` signals 16 + 17). An operator who
expects "smoke green = everything works" hits a 503 (best case)
or a silent NULL column (audit replay, pre-G0.14-T6) the first
time they reach for one of these surfaces.

The state of each gate is visible on `GET /ready`'s `features`
block (G0.14-T7 #1148) — one structured GET answers "which
features will work out of the box?":

```json
{
  "features": {
    "agent_runtime":  {"configured": false, "missing_env": ["KEYCLOAK_ADMIN_URL", "..."], "docs": "..."},
    "ui_surface":     {"configured": false, "missing_env": ["UI_KEYCLOAK_CLIENT_ID", "..."], "docs": "..."},
    "audit_replay":   {"configured": true,  "capture_mode": "enforced", "missing_env": []},
    "approval_queue": {"configured": false, "depends_on": "agent_runtime"}
  }
}
```

Walk the four gates in the order an operator hits them:

- [ ] **UI surface** (Helm `ui.*` values; affects every `/ui/auth/*`
  request). Set `UI_KEYCLOAK_CLIENT_ID` and `UI_KEYCLOAK_CLIENT_SECRET`
  in the backplane pod's environment — the secret renders from Vault
  via the deploy's existing render-into-env chain (the same one that
  lands `DATABASE_URL` / `UI_SESSION_ENCRYPTION_KEY`). Provision the
  confidential `meho-web` Keycloak client per
  [`docs/cross-repo/keycloak-web-client.md`](cross-repo/keycloak-web-client.md).
  Without these, `GET /ui/auth/login` returns 503 `ui_oauth_not_configured`
  and the operator console cannot complete OAuth.

- [ ] **Agent runtime** (`POST /api/v1/agent-principals` lifecycle;
  affects everything downstream that needs an agent identity). Set
  `KEYCLOAK_ADMIN_URL`, `KEYCLOAK_ADMIN_CLIENT_ID`, and
  `KEYCLOAK_ADMIN_CLIENT_SECRET`. Provision the confidential
  admin Keycloak client (with `manage-clients` service-account role
  on the realm) per
  [`docs/cross-repo/keycloak-agent-client.md`](cross-repo/keycloak-agent-client.md).
  Without these, `POST /api/v1/agent-principals` returns 503
  `keycloak_admin_not_configured: KEYCLOAK_ADMIN_URL / KEYCLOAK_ADMIN_CLIENT_ID
  / KEYCLOAK_ADMIN_CLIENT_SECRET are unset.` — the named env vars are
  exactly the three to set.

- [ ] **Audit replay** (MCP session-id capture for the audit log's
  `agent_session_id` column). No env var required for capture once
  G0.14-T6 (#1147) lands — capture is unconditional, no operator
  knob. Until T6 lands, capture is gated on `MCP_REQUIRE_SESSION_ID=true`
  (which also flips a missing header into a `-32600` reject before
  dispatch). The `/ready` `features.audit_replay.capture_mode` field
  exposes the current state — `"enforced"` pre-T6,
  `"always"` post-T6. Operators tracking the audit-replay readiness
  signal off `/ready` get a stable contract across the T6 transition.
  G0.15-T4 (#1213) added the **issuance** half — the server now
  emits `Mcp-Session-Id` on `initialize` per MCP 2025-06-18
  §"Session Management" rule 1, so clients have something to echo
  back on the capture side. Without issuance (≤ v0.7.0), spec-
  conforming MCP clients never sent the header and every row landed
  with `agent_session_id: null` despite `capture_mode` advertising
  `"always"`; v0.7.1+ closes the inert-promise regression.

- [ ] **Approval queue** (agent-grant approval surface;
  `POST /api/v1/agents/grants` and the agent grant lifecycle). No
  separate env vars — the queue activates automatically once
  **agent runtime** above is configured. The `/ready` block exposes
  `approval_queue.depends_on: "agent_runtime"` so operators know
  there is no second admin client to provision.

- [ ] **GitHub `gh-rest-3` connector credential** (optional;
  enables the `gh/3` typed connector landed by Initiative
  [#1220](https://github.com/evoila/meho/issues/1220); the catalog
  ``version`` field was canonicalised from ``v3`` to ``3`` by
  G3.11-T8 #1242 so the dispatcher's tuple lookup resolves cleanly).
  No backplane
  env vars — the credential lives per-target in Vault. Provision a
  GitHub App (preferred) or fine-grained PAT (fallback) per
  [`docs/cross-repo/github-app-credential.md`](cross-repo/github-app-credential.md),
  write `app_id` + `private_key` (App) or `pat` (PAT) to
  `secret/<tenant>/<target>/github-app`, and register the target
  row with `product: gh`, `secret_ref: <vault-path>`,
  `auth_model: shared_service_account`. Without these, `meho
  targets probe <gh-target>` returns 503 `github_app_not_installed`
  or `github_jwt_mint_failed` and no `gh.*` op can dispatch. Once
  the credential side is green, follow
  [`docs/cross-repo/github-connector.md`](cross-repo/github-connector.md)
  for the end-to-end first-day on-ramp (target probe → catalog
  ingest → group enable → write-op annotation → composite smoke-test).

- [ ] **Spec-ingestion grouping** (`meho connector ingest --catalog` /
  `--spec`; affects every connector that becomes dispatchable only
  after the LLM grouping pass — vmware-rest, nsx, sddc-manager, the
  `gh` L2 surface). The grouping pass reuses the agent runtime's
  `ANTHROPIC_API_KEY` (wired at lifespan startup, G3.17 #1407 / #1386).
  The chart renders that env **only under `agent.enabled: true`**, so a
  deploy that left the agent runtime off has no ingest key either and a
  non-dry-run ingest 503s `LlmClientUnavailable` on the grouping step.
  Set `agent.enabled: true` plus an operator-managed Secret or
  `eso.agent.enabled: true` per
  [`docs/cross-repo/ingest-llm-key.md`](cross-repo/ingest-llm-key.md).
  `--dry-run` ingest works without the key (no LLM call); a keyless
  air-gapped deploy keeps the 503 until grouping routes through the
  G11.5 resolver.

Verify the gates by re-hitting `GET /ready` after each provisioning
step and reading the `features` block:

```bash
curl -s "https://<your-meho-host>/ready" | jq '.features'
```

Every gate should read `"configured": true` (or
`"capture_mode": "always"` for `audit_replay` post-T6) when fully
enabled. Any `"configured": false` with a non-empty `missing_env`
list is an unfinished provisioning step — name the listed env vars
into the pod's environment and re-deploy.

### 6b. Rollback drill — the migration ↔ rollback contract

**Contract** (#1607, hardened after the 2026-06-08 outage):
`helm rollback` / `--atomic` reverts **manifests only** — the
`pre-install,pre-upgrade` migration Job's schema commit survives the
rollback (Helm does not track hook side effects). Pods rolled back
across a migration therefore run against a *newer* schema than their
code expects. Two enforced invariants make that safe:

1. **Additive-only `upgrade()`** — `scripts/ci/check_migration_compat.py`
   (CI: `migration-compat.yml`) rejects destructive DDL, so an older
   image can always read a newer schema.
2. **DB-ahead-tolerant readiness** — the `db` probe
   (`backend/src/meho_backplane/db/migrations.py`) reports `ok=true`
   with detail `current=<newer> head=<older> db_ahead=true` when the
   DB revision is unknown to the image. DB *behind* head still fails
   readiness. Full rationale (including why a `pre-rollback`
   `alembic downgrade` hook cannot work):
   [`docs/codebase/migrations.md`](codebase/migrations.md) § "Rollback
   contract".

Never run `alembic downgrade` as part of a deploy or rollback;
`downgrade()` bodies are the manual escape hatch only. **Caveat:** the
tolerance lives in the image being rolled back **to** — rolling back
to a pre-tolerance release (strict `current == head` probe) across a
migration still bricks readiness; recover those by rolling forward.

**Failure-injection drill** — run on a sandbox (kind/minikube or the
staging namespace, never prod) when the release carries a migration,
and whenever the migration Job or the `db` probe changes:

- [ ] 1. Install the prior release `v(n)` (DB at migration `00NN`);
  wait Ready. Per the caveat above, `v(n)` must already carry the
  db-ahead-tolerant probe (#1607) — the drill proves the safety net
  of the release being rolled back *to*.
- [ ] 2. Upgrade to the candidate `v(n+1)` (carrying `00NN+1`) with
  readiness deliberately broken, so the rollout must fail and
  auto-roll-back:

  ```bash
  helm upgrade meho oci://ghcr.io/evoila/meho-chart \
    --version <candidate> --reuse-values --atomic --timeout 5m \
    --set probes.readiness.httpGet.path=/definitely-404
  ```

- [ ] 3. Confirm the upgrade fails and Helm auto-rolls-back:
  `helm history meho` shows a rollback revision; pods run the `v(n)`
  image again.
- [ ] 4. Assert the rolled-back pods reach `READY 1/1` **without any
  manual `alembic` command**, and `GET /ready` returns 200 with the
  `db` check `ok=true`, detail `current=00NN+1 head=00NN
  db_ahead=true`.
- [ ] 5. Clean up by rolling **forward**: re-run the upgrade with
  readiness intact. The DB is already at `00NN+1`; the migration Job
  re-runs as a no-op (`alembic upgrade head` is idempotent).

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
[ ] 6a. Post-deploy enablement — for each gate in /ready features:
        configure the env vars per the cited Vault doc; verify gate
        flips to configured (or capture_mode=always for audit_replay
        post-T6)
[ ] 6b. Rollback drill (releases carrying a migration): broken-readiness
        upgrade auto-rolls-back; rolled-back pods reach Ready with
        db_ahead=true and no manual alembic (#1607)
[ ] 7. Initiative closed; board/roadmap updated; consumers notified
```

## Failure modes seen (do not repeat)

- **v0.3.1 → v0.5.0:** `v0.3.2` skipped, and the CHANGELOG was never
  rolled, so v0.5.0's notes omitted ~24 PRs. Fixed retroactively; step 2
  prevents recurrence.
- **Empty release notes:** `cli-release.yml` falls back to `[Unreleased]`
  when no `## [X.Y.Z]` section exists at tag time. Always roll (step 2)
  *before* tagging (step 4).
- **Strict `current == head` readiness vs pre-upgrade migrations
  (v0.12.0, 2026-06-08, ~2.5h outage):** the migration Job committed
  `0037` before the Deployment rolled; the new release failed
  readiness; `--atomic` rolled the manifests back but not the schema,
  and the restored `v0.11.0` pods failed their own strict `db` probe
  (`current=0037 head=0036`) forever — recovery required rolling
  *forward*. Closed by #1607 (db-ahead-tolerant probe + section 6b's
  drill). Remember: the tolerance must exist in the image being
  rolled back **to**.
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
