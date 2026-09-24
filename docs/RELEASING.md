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
for the version — five artefacts derive from it, all published
automatically on the tag push:

| Artefact | Where | Workflow | Version source |
|---|---|---|---|
| Backplane image | `ghcr.io/evoila/meho:vX.Y.Z` | [`image.yml`](../.github/workflows/image.yml) | git tag |
| Helm chart | `oci://ghcr.io/evoila/meho-chart` | [`chart.yml`](../.github/workflows/chart.yml) | chart workflow sets `version` + `appVersion` |
| CLI tarballs + GitHub Release | [releases](https://github.com/evoila/meho/releases) | [`cli-release.yml`](../.github/workflows/cli-release.yml) | GoReleaser bakes `{{.Tag}}` into `version.Version` |
| Claude Desktop `.mcpb` bundle | [releases](https://github.com/evoila/meho/releases) | [`cli-release.yml`](../.github/workflows/cli-release.yml) | `clients/claude-desktop-mcpb/build.sh` stamps the tag version |
| Docs site version | <https://evoila.github.io/meho/> | [`docs-site.yml`](../.github/workflows/docs-site.yml) | mike publishes the tag's `MAJOR.MINOR` + moves `latest` |

The Desktop `.mcpb` bundle rides `cli-release.yml`, attached to the same
GitHub Release as the CLI tarballs (four workflows, five artefacts). All
four workflows fire on `tags: ['v*']` (image/chart have **no** path filter
on tags — a tag always publishes). `cli-release.yml` is **tag-only**: a
main push never cuts a release. Docs versions are cut **per minor** — a
patch tag republishes its minor's docs in place.

## Supply-chain and CI baseline

A release is only as trustworthy as the pipeline that built it. Two
supply-chain controls are baselines, not per-release choices — they hold
across every cut:

- **Build → quarantine → scan → promote → deploy by digest, and verify
  the signature.** `image.yml` builds to a quarantine digest, runs the
  required scans, and only then promotes and cosign-signs the **approved
  digest** — so a failing scan can never leave a publishable release
  candidate advertised as approved. The deploy side completes the chain:
  pin the chart's `image.digest` (`sha256:…`, the content-addressed digest
  the pipeline promoted) rather than deploying a mutable tag, and
  `cosign verify` that digest against the keyless workflow identity before
  `helm upgrade` — ideally enforced at admission (a Sigstore
  policy-controller / Kyverno rule). CLI tarballs carry `SHA256SUMS` plus
  cosign signatures for the same reason. Full recipes:
  [`docs/codebase/devops.md` § Image reference / Verifying provenance](codebase/devops.md)
  and the [repository README](https://github.com/evoila/meho#verify-image--chart--cli-signatures).

- **Untrusted PR code never runs on the internal runner pool.** The
  repository's fork-PR approval policy is **`all_external_contributors`**
  (a maintainer must "Approve and run" any external fork PR before any
  workflow runs), and every job that checks out and executes PR-controlled
  code routes fork PRs to a **disposable GitHub-hosted runner**, never the
  internal `meho-runners-ci` pool — with signing, deployment, and build-cache
  identities kept off the PR-triggered path. Org-level runner-group access
  restricts which workflows may schedule the internal pool as the durable
  backstop a PR cannot edit. The full enforcement map is in
  [`docs/codebase/devops.md` § Untrusted pull-request isolation](codebase/devops.md).

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
- **release candidate** (`X.Y.Z-rc.N`) — a SemVer pre-release of the
  final `X.Y.Z`, used for the v1.0.0 series; the same tag-driven
  publish, marked pre-release. See
  [Release candidates — the rc series](#release-candidates--the-rc-series).

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
  cancelled run reads green-ish in a glance — it is not. (A hung
  `python-coverage` job is *no longer* a cause: since #2800 a pytest
  hang trips the step-level timeout and degrades to an absorbed job
  failure with run conclusion `success`, so a `cancelled` main run
  means a genuine cancel.) If the latest
  `main` run for the tagged commit is `cancelled`, **re-run it and wait
  for a real `success`** before tagging:

  ```bash
  gh run rerun <run-id> --repo evoila/meho   # then re-check conclusion
  ```

- [ ] **Security-advisory reconciliation (every rc and GA tag).** Every
  `### Security` entry in `CHANGELOG.md` must have a row in the advisory
  ledger, and at a GA tag no row may still be undecided:

  ```bash
  grep -cE "^### Security" CHANGELOG.md                                              # headings
  grep -cE '^\| [0-9]+ \|' docs/security/advisory-ledger.md                          # ledger rows — must be equal
  grep -cE '^\| [0-9]+ \|.*Owed — decision pending' docs/security/advisory-ledger.md # must be 0 at a GA tag
  gh api /repos/evoila/meho/security-advisories --jq 'length'                        # published GHSAs — record in the ledger snapshot
  ```

  A heading without a row means a security fix shipped with no
  disclosure decision: add the row (class + disposition) in the
  release-cutting PR. A MEHO-code vulnerability row is closed either by
  a published GitHub repository security advisory (GHSA) linked on the
  row or by an exemption on the policy's sole ground (the entry predates `SECURITY.md`, 2026-05-09 — applies to no current row) — steps in
  [`SECURITY.md`](../SECURITY.md#coordinated-disclosure-steps), ledger
  at [`docs/security/advisory-ledger.md`](security/advisory-ledger.md).
  This is v1.0 gate 2 (Goal #2661).

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

4. **Update the public "What's new" page.** In the same release-cutting
   PR, add a `## [vX.Y.Z](release-link) — YYYY-MM-DD` section to
   [`docs-site/reference/whats-new.md`](../docs-site/reference/whats-new.md) —
   one plain-language, bold-lead bullet per notable change, in the page's
   existing format, linking the GitHub release. The docs site publishes on
   the `v*` tag via [`docs-site.yml`](../.github/workflows/docs-site.yml) (the
   docs-site artefact above), so a release cut without this ships a stale
   public "What's new" — it silently missed eight consecutive releases
   (v0.34.2 through v0.35.5, backfilled in #3782).

### 3. Release-cutting PR → merge

Open the PR (CHANGELOG roll + any release-only edits), get it reviewed,
merge to `main`.

- [ ] **Upgrade-notes discipline.** When the release carries an
      upgrade-relevant change (a chart field operators may have hand-patched,
      a new default-on guard, a schema-breaking value, a forward-only
      migration with an operator caveat), append a row to the
      version-specific upgrade-notes table in
      [`deploying.md`](deploying.md) in this same PR. This is the discipline
      that #2393's `startupProbe` (the Helm-4 SSA field-conflict caveat)
      would have triggered.

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
gate. A citation that spells out an HTTP method
(`POST /api/v1/operations/search`) is also held to that path's
actual verbs — a method the route doesn't expose fails the gate even
when the path itself resolves (#1914).

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
- [ ] Claude Desktop bundle `meho-claude-desktop-X.Y.Z.mcpb` attached to
  the Release (fifth asset, uploaded by `cli-release.yml` after GoReleaser).

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
  `agent_session_id: null` regardless of the `capture_mode: "when_negotiated"`
  advertisement; v0.7.1+ closes this gap. To confirm the deploy is
  in the expected capture mode (`when_negotiated` by default; `enforced` when
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
    "approval_queue": {"configured": false, "depends_on": "agent_runtime", "effective_posture": "four_eyes_enforced"}
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
  `"when_negotiated"` post-T6. Operators tracking the audit-replay readiness
  signal off `/ready` get a stable contract across the T6 transition.
  G0.15-T4 (#1213) added the **issuance** half — the server now
  emits `Mcp-Session-Id` on `initialize` per MCP 2025-06-18
  §"Session Management" rule 1, so clients have something to echo
  back on the capture side. Without issuance (≤ v0.7.0), spec-
  conforming MCP clients never sent the header and every row landed
  with `agent_session_id: null` despite `capture_mode` advertising
  `"when_negotiated"`; v0.7.1+ closes the inert-promise regression.

- [ ] **Approval queue** (agent-grant approval surface;
  `POST /api/v1/agents/grants` and the agent grant lifecycle). No
  separate env vars — the queue activates automatically once
  **agent runtime** above is configured. The `/ready` block exposes
  `approval_queue.depends_on: "agent_runtime"` so operators know
  there is no second admin client to provision. The block also
  surfaces `effective_posture` (#2087): `"four_eyes_enforced"` on the
  default fail-closed posture, `"single_operator_break_glass"` when
  the deploy set the emergency `APPROVAL_ALLOW_SELF_APPROVAL=true`
  escape (see `docs/codebase/approvals.md` — the endorsed
  single-operator answer is an agent-requester, not the flag).

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
`"capture_mode": "when_negotiated"` for `audit_replay` post-T6) when fully
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

## Release candidates — the rc series

The road to v1.0.0 ([#2661](https://github.com/evoila/meho/issues/2661),
milestone "v0.29 — contract freeze (rc series)") is the first time MEHO
ships release candidates. An rc is a **real release** — the same `v*`
tag, the same four publish workflows, the same five artefacts — whose
version carries a SemVer pre-release identifier. Everything in steps
1–7 applies; this section carries only the deltas. No rc tag has ever
been pushed (`git tag -l 'v*-rc.*'` is empty as of v0.35.12), so every
workflow behaviour below is read from the YAML, cited by file and line,
and must be confirmed on the first rc — the unconfirmed points are
collected under [Known gaps](#r9--known-gaps-rc-tags-in-the-pipeline).

### R1 — Version scheme

`vX.Y.Z-rc.N`, `N` starting at 1 — SemVer 2.0 pre-release with a
dot-separated numeric counter (`v1.0.0-rc.1`, `v1.0.0-rc.2`, …):

- The rc names the final version it is a candidate **for**:
  `v1.0.0-rc.N` promotes to `v1.0.0`, never to `v1.0.1`.
- `N` only increments. A re-cut is always `rc.N+1`, never a moved tag:
  `cli-release.yml` tolerates a re-pushed tag (concurrency group,
  L63–69), but a moved tag leaves the image alias, chart version and
  Release assets pointing at bytes no tag describes any more.
- No other identifiers (`-beta`, `-alpha`, `-rc1`). Every
  tag-consuming workflow derives the version by stripping the leading
  `v` and nothing else, so the identifier lands verbatim in the chart
  version, image alias, tarball names and `meho version` (see R4).
- Once an rc exists, `git describe --tags --abbrev=0` returns it.
  Wherever this runbook (steps 2.1 and 3) or the `/release` skill
  derives `PREV` and the question is "since the last **final**
  release", exclude rcs:

  ```bash
  PREV=$(git describe --tags --abbrev=0 --exclude='v*-rc.*' HEAD)   # last final tag
  RC_PREV=$(git describe --tags --abbrev=0 HEAD)                    # last tag of any kind
  ```

### R2 — When an rc is cut

- **`rc.1`** is cut when round 1 of the acceptance evaluation
  ([#2665](https://github.com/evoila/meho/issues/2665)) has delivered
  its matrix and the round-1-recommended GA set is encoded in the
  maturity registry (`backend/src/meho_backplane/features.py`, the
  single source of truth per
  [`docs/codebase/feature-maturity.md`](codebase/feature-maturity.md)).
  Round 2 ([#3379](https://github.com/evoila/meho/issues/3379)) hard-
  depends on both the matrix and the first rc tag — the rc is the
  build it measures.
- **`rc.N+1`** is cut only when the rule in R7 says so — never on a
  schedule.
- No rc for a `v0.x` release. Pre-1.0 minors keep shipping directly
  (breaking changes with migration recipes, per
  [Versioning](#versioning)).
- Nothing is cut from an rc branch. `main` is the only release line;
  an rc tags a confirmed-green `main` commit, exactly like a final tag.

### R3 — Before cutting an rc (pre-flight deltas)

Pin the candidate first — every check in R3 is bound to this one SHA,
and R4 tags exactly it:

```bash
git fetch origin main
RC_SHA=$(git rev-parse origin/main)   # the candidate; do not recompute it later
RC_PREV=$(git describe --tags --abbrev=0 "$RC_SHA")   # previous tag of any kind, relative to the candidate (not to your checkout)
git log -1 --format='%H %s' "$RC_SHA"
gh run list --repo evoila/meho --commit "$RC_SHA" --branch main   # the CI run you are verifying
```

If `main` moves while you work through R3, you have two honest
choices: tag `$RC_SHA` anyway (it is the commit the checks were run on),
or restart R3 with the new tip. Never tag a SHA that did not receive the
checks below — that is the moving-branch race the final-release
procedure avoids by selecting its merge commit explicitly.

Run step 1 as written (Tasks closed; **real** `success` on the
`$RC_SHA` `main` CI run; cancelled ≠ green). Then:

- [ ] **Contract gates green on the candidate commit.** Gate 4 of
  #2661 — "contract snapshots + compat gates green across the rc
  series" — is what the gates #2662 left in CI can check today. Confirm
  each on the run you are about to tag:
  - `CLI API snapshot freshness` (`ci.yml` job
    `cli-api-snapshot-freshness`, L1791–1875): `cli/api/openapi.json`,
    `cli/internal/api/client.gen.go` and `docs-site/reference/cli.md`
    regenerate to a clean tree.
  - The contract test modules in the Python lanes:
    `backend/tests/test_api_v1_list_envelope_contract.py` (the
    `{items, next_cursor}` envelope over the governed list registry
    **and** the OpenAPI schema the CLI is generated from),
    `backend/tests/test_mcp_tools_list_shape_conventions.py`
    (`tools/list` `inputSchema` parity pins), and
    `backend/tests/test_maturity_surface_drift.py` (#2678: every MCP
    tool / public REST operation / CLI command / `/ui` area resolves to
    a maturity tier, and `docs-site/reference/maturity.md` regenerates
    clean).
  - `migration-compat.yml` (additive-only `upgrade()`). It is
    PR-triggered, so check it on the PRs that carried migrations since
    the previous tag rather than on the `main` run.
- [ ] **Contract diff since the previous tag is classified.** There is
  no automated breaking-vs-additive gate yet (Known gap 3), so do it by
  hand and record the classification on #2661:

  ```bash
  git diff "${RC_PREV}".."${RC_SHA}" -- cli/api/openapi.json docs-site/reference/cli.md docs-site/reference/maturity.md
  ```

  Anything that is not additive — removed path / operation / field,
  renamed key, tightened enum, changed envelope, dropped MCP tool or
  renamed argument, removed CLI verb or flag, dropped Helm value or env
  var — is a **break**: it needs a `### Breaking changes` bullet with a
  migration recipe in `[Unreleased]`, and, if it lands after `rc.1`, an
  explicit decision on #2661. For `rc.1` the diff spans the whole
  `v0.35.x → 1.0` gap and is expected to be large; the classification
  is what matters, not the size.
- [ ] **CHANGELOG completeness audit** (step 2.1) with `PREV` replaced
  by `RC_PREV` — every PR merged since the previous tag of any kind has
  a bullet in `[Unreleased]`. **Do not roll** `[Unreleased]` into a
  `## [1.0.0-rc.N]` heading: the rc Release body is the audited
  `[Unreleased]` block (R4), and the whole series rolls once, at
  promotion (R8).
- [ ] **Release-body path-freshness gate** (step 3) against the
  `[Unreleased]` block, since that is the body the rc Release carries:

  ```bash
  VERSION=Unreleased   # instead of X.Y.Z; the awk + check_release_body_paths.py lines from step 3 are unchanged
  ```

- [ ] **Security-advisory reconciliation** (step 1, #3380, 1.0 gate 2)
  — run the step-1 commands as written: `### Security` headings in
  `CHANGELOG.md` == rows in
  [`docs/security/advisory-ledger.md`](security/advisory-ledger.md), and
  the published-GHSA count recorded in the ledger snapshot. The rc
  difference is the third command: an **rc** may carry rows still at
  "Owed — decision pending" (the rc is how testers get a fix whose
  advisory is still a private draft, and the row records that), so a
  non-zero count blocks nothing at an rc but must be listed on #2661;
  at the **final** tag it must be `0` — every MEHO-code row closed by a
  published GHSA or an exemption on the policy's sole ground (the entry predates `SECURITY.md`, 2026-05-09 — applies to no current row) — and the dated
  gate-2 statement ("advisories … published" / "no pending advisories
  as of YYYY-MM-DD") is on #2661 (R8).

### R4 — Tag + push, and what the rc tag publishes

There is no release-cutting PR for an rc (nothing rolls). Tag the
candidate pinned at the top of R3 — `$RC_SHA`, unchanged — never a
recomputed `main` tip:

```bash
git fetch origin main
[ -n "${RC_SHA:-}" ] || { echo "RC_SHA is unset — re-run the R3 pin block (a new shell since R3 loses it)"; exit 1; }
git merge-base --is-ancestor "$RC_SHA" origin/main \
  || { echo "$RC_SHA is no longer on main (force-push or revert) — repeat R3 for the current tip"; exit 1; }
[ "$(git rev-parse origin/main)" = "$RC_SHA" ] \
  || echo "main moved past the verified candidate: tag $RC_SHA as verified, or repeat R3 for the new tip"
git tag v1.0.0-rc.N "$RC_SHA"
git push origin v1.0.0-rc.N
```

The push fans out to the same four workflows as a final tag (all four
match `tags: ['v*']`: `cli-release.yml` L54, `image.yml` L96,
`chart.yml` L70, `docs-site.yml` L66). What each one does with a
pre-release identifier, read from the YAML:

| Artefact | What `v1.0.0-rc.N` produces | Where |
|---|---|---|
| GitHub Release | Created **auto-published and marked pre-release**: `prerelease: auto` flips the flag on the hyphen in the tag. Body = the `## [1.0.0-rc.N]` CHANGELOG section if one exists, else the `[Unreleased]` block — the documented pre-release fallback, and with R3's no-roll rule the intended path. `[Unreleased]` must therefore be audited and non-empty; an empty body here is the step-2 failure mode, not a quirk of rcs. | [`cli/.goreleaser.yaml`](../cli/.goreleaser.yaml) L264–269; [`cli-release.yml`](../.github/workflows/cli-release.yml) L164–172, L204–208 |
| CLI tarballs | `meho_1.0.0-rc.N_<os>_<arch>.tar.gz` (`{{ .Version }}` strips the `v`); `meho version` prints `v1.0.0-rc.N` (`{{.Tag}}` keeps it). The cosign identity regex `refs/tags/v.+` covers rc tags, so the step-5 verify recipe is unchanged. | `.goreleaser.yaml` L104, L117, L157; `cli-release.yml` L366 |
| `.mcpb` bundle | `meho-claude-desktop-1.0.0-rc.N.mcpb`, attached as the fifth asset. `mcpb pack` accepts a pre-release semver — the PR dry-run already packs `0.0.0-pr<N>` on every bundle PR. | `cli-release.yml` L285–292; [`mcpb-bundle.yml`](../.github/workflows/mcpb-bundle.yml) L74–78 |
| Backplane image | Alias `ghcr.io/evoila/meho:v1.0.0-rc.N` — `type=semver,pattern={{raw}}` is the only tag-push pattern; `:main` is not emitted on a tag (`enable={{is_default_branch}}`). Quarantine build → trivy gate → promote → cosign sign → SBOM attest run unchanged, so the rc's promoted **digest** is what the lab deploy pins (R5). | [`image.yml`](../.github/workflows/image.yml) L160–163, L235–269, L305–335 |
| Helm chart | Version `1.0.0-rc.N` (`VERSION="${REF#refs/tags/v}"`), `appVersion` = the git SHA; pushed to `oci://ghcr.io/evoila/meho-chart:1.0.0-rc.N` and anonymously pulled back by exact version. Hyphenated chart versions are already the norm for `main` pushes (`0.1.<date>-<sha>`), so the push path is proven. Always pass the exact `--version 1.0.0-rc.N` when installing. | [`chart.yml`](../.github/workflows/chart.yml) L1247–1265, L1309, L1440 |
| Docs site | Publishes under **`1.0`** and **moves `latest` + the site default to it**: the `v[0-9]*.[0-9]*` case matches, `sed` truncates to MAJOR.MINOR, then `mike deploy --push --update-aliases 1.0 latest` and `mike set-default --push latest`. Deploying under the minor is intended ("the rc docs are that minor's docs in progress"); moving `latest` before GA is **Known gap 1**. | [`docs-site.yml`](../.github/workflows/docs-site.yml) L60–66, L156–173 |

Verify per step 5, with these substitutions: the Release **is** marked
pre-release; `meho version` → `v1.0.0-rc.N`; image alias
`:v1.0.0-rc.N`; chart `1.0.0-rc.N`; bundle
`meho-claude-desktop-1.0.0-rc.N.mcpb`; docs at
`https://evoila.github.io/meho/1.0/`. Do **not** add a `whats-new.md`
section for an rc — the single `[v1.0.0]` section at promotion covers
the series (the rc docs publish whatever `whats-new.md` holds at the
tagged commit, which is fine).

### R5 — Deploy the rc to the lab

Step 6 as written, against `rke2-infra`, with the rc pins:

- [ ] `helm upgrade … oci://ghcr.io/evoila/meho-chart --version 1.0.0-rc.N`
  (exact version) with `image.digest` set to the digest the rc's
  `image.yml` run promoted — the supply-chain baseline above (deploy by
  digest, `cosign verify` first) applies to an rc exactly as to a
  final; the rc is not a shortcut around it.
- [ ] Smoke; **smoke-green** with evidence. A red smoke on an rc is
  cheaper than on a final, but it is still a release incident: record
  it on #2661 and it forces `rc.N+1` (R7).
- [ ] 6a post-deploy enablement gates (`GET /ready` `features`).
- [ ] 6b rollback drill when the series carries a migration: the rc
  is the natural `v(n+1)` for the drill, with `v(n)` = the last
  **final** release (`PREV` from R1). Run it on the first rc that
  carries the migration and re-run only if the migration Job or the
  `db` probe changes in a later rc.

### R6 — Acceptance round 2 against the rc (#3379)

Only after R5 is smoke-green. Round 2 is the release-candidate
regression run of the self-run acceptance evaluation:

- [ ] The operator re-runs the round-1 scenario set (S0–S14, rubric
  unchanged, `evoila-bosnia/meho-internal#184` execution kit reused
  as-is) against the R5 deploy.
- [ ] Deliverable: the round-2-vs-round-1 matrix diff — per-feature
  usefulness / correctness deltas, new friction events, every
  regression tagged **blocker / major / minor**.
- [ ] Blocker / major regressions are filed into the per-cycle
  hardening line under #221 and either **fixed on `main`** (→ `rc.N+1`,
  R7) or **explicitly waived on #2661 with rationale**. Minors are
  filed and do not block.
- [ ] The GA set is confirmed or revised from the round-2 evidence and
  recorded on #2661. A revision is a `features.py` edit — a contract
  change (tool descriptions, OpenAPI `x-maturity`, CLI labels, the
  generated index all derive from it), so it lands on `main` **before
  the last rc**, never between the last rc and the final tag.
- [ ] Record on #2661: rc tag, run date, matrix-diff link, verdict.

### R7 — `rc.N+1` or promotion?

Cut **`rc.N+1`** (back to R3) when any of these holds after `rc.N`:

- Round 2 on `rc.N` found a blocker or major that was fixed rather than
  waived — the fix is on `main` and must be re-measured on a build that
  contains it.
- Any commit on `main` after `rc.N` touches a contract surface (the R3
  diff command run as `git diff v1.0.0-rc.N..main -- …` is non-empty),
  including the R6 maturity reclassification.
- A `### Security` fix landed after `rc.N`.
- `rc.N`'s smoke, 6a or 6b was red, or any of its four publish
  workflows was red. A red rc is not evidence of anything.

**Promote** `rc.N` to the final tag when all of these hold:

- Round 2 completed on `rc.N` with zero unresolved blocker / major
  regressions (#3379 DoD), and the final GA / Beta / Experimental scope
  is recorded on #2661 and encoded in `features.py` at `rc.N`.
- The R3 contract gates were green on `rc.N`'s commit, and nothing but
  Markdown changes `main` between `rc.N` and the release-cutting merge
  commit — the release-cutting PR is the **only** commit in that range:

  ```bash
  REL_SHA=$(gh pr view <release-cutting-pr#> --repo evoila/meho --json mergeCommit --jq .mergeCommit.oid)
  git log --oneline "v1.0.0-rc.N..${REL_SHA}"                 # exactly one entry: the release-cutting merge
  git diff --stat "v1.0.0-rc.N..${REL_SHA}" -- . ':!*.md'      # empty: no non-Markdown change since the last rc
  ```

  A non-empty second command means the final tag would ship code no rc
  measured. Cut `rc.N+1` instead.
- Gate 2 holds: the advisory ledger shows every owed GHSA published (R3)
  and the dated statement is on #2661.

### R8 — Promotion to the final tag

Steps 2–7 as written, with `PREV` = the last **final** tag (R1
`--exclude`), plus:

- [ ] **Roll the whole series once.** `[Unreleased]` → `## [1.0.0] -
  YYYY-MM-DD` covers everything since `PREV`, i.e. every rc's content.
  The completeness audit (step 2.1) runs from `PREV`, not from the last
  rc.
- [ ] **State the stability scope in the release notes (1.0 gate 5).**
  Open the `[1.0.0]` section with a `### Stability scope` block listing,
  by feature key, what ships **GA** (carries the 1.0 stability promise),
  **Beta** (with its target-GA milestone and tracking issue — its road
  to GA) and **Experimental** (outside the promise). Copy it from
  `docs-site/reference/maturity.md` at the tagged commit — the page is
  generated from `features.py` and freshness-gated by
  `test_maturity_surface_drift.py`, so it cannot disagree with what the
  build advertises — and link the published page. `cli-release.yml`
  makes this section the GitHub Release body, which is where gate 5
  is checked.
- [ ] `docs-site/reference/whats-new.md`: one `## [v1.0.0]` section
  for the series (step 2.4).
- [ ] `deploying.md` upgrade-notes row for v1.0.0 (step 3), covering
  every upgrade-relevant change since `PREV`.
- [ ] Tag `v1.0.0` on `REL_SHA` per step 4. Verify per step 5 — the
  Release is **not** marked pre-release; `meho version` → `v1.0.0`;
  chart `1.0.0`; docs republish `1.0` in place (`latest` already points
  there since `rc.1`).
- [ ] #2661 definition of done: all five gates recorded as holding on
  the promoted rc; the rc tags stay (they are the audit trail of what
  was measured).

### R9 — Known gaps: rc tags in the pipeline

Read from the YAML; nothing here is fixed by this document. Each is a
follow-up task.

1. **Docs site moves `latest` to the rc's minor.**
   `.github/workflows/docs-site.yml` L162–163: `mike deploy --push
   --update-aliases "${VERSION}" latest` then `mike set-default --push
   latest` run for any `v*` tag, so `v1.0.0-rc.1` re-points the public
   site root at the `1.0` docs before GA. The header (L63–65) intends
   the minor publish; it does not intend the alias move. Suggested
   fix: when `GITHUB_REF_NAME` contains `-`, deploy the version without
   the `latest` alias and skip `set-default`.
2. **`git describe --tags --abbrev=0` returns the rc.** Used by this
   runbook's step 2.1 (`PREV`) and step 3, the `/release` skill's
   Phase 0, and `.github/workflows/readme-version-check.yml` L103. This
   section fixes the runbook (R1 `--exclude`); the skill needs the same
   edit. The README guard is harmless today — `README.md` L11 carries
   no `vX.Y` token and the self-updating badge (L6) short-circuits the
   check at L93–97 — but reintroducing a `**Status:** vX.Y` token while
   an rc is the latest tag would fail it on every merge-queue admission.
3. **No automated compatibility-diff gate.** #2662 closed 2026-08-30
   ("All children shipped; contract-freeze gates live in CI"), but the
   mechanisms its body lists — an oasdiff-class compat gate on the
   public OpenAPI snapshot, a committed `tools/list` snapshot, an
   env-var registry snapshot, Helm golden renders, an
   upgrade-from-previous-tag job, and a `breaking-change` label (no such
   label exists in `evoila/meho`) — are not in the tree; the CI gates
   that exist are the ones R3 lists, and
   `docs-site/project/index.md` L30–33 still says the promise "will be
   documented here when it lands". Until they exist, gate 4 is the R3
   hand-classification plus those gates. Suggested fix: file the
   missing children (at minimum the compat-diff gate) before `rc.1`.
4. **Image alias for a pre-release tag is not asserted in the YAML.**
   `.github/workflows/image.yml` L160–163 configures
   `type=semver,pattern={{raw}}`; the header (L9, L153) documents only
   the `v<x.y.z>` shape. The expected alias is `:v1.0.0-rc.N`; confirm
   it on `rc.1`'s run and, if `docker/metadata-action` drops
   pre-releases from the `semver` type, add `type=raw,value={{ref}}`
   (or equivalent) for tag pushes.
5. **The rc Release body is the raw `[Unreleased]` block.**
   `.github/workflows/cli-release.yml` L204–208 — by design, but
   nothing labels it as cumulative rc notes, and the GoReleaser git-log
   fallback (L216) kicks in silently if `[Unreleased]` is empty.
   Suggested fix: prepend a one-line "Release candidate `rc.N` for
   1.0.0 — cumulative notes since `PREV`" header when the tag contains
   a hyphen, and fail the step (not warn) when both sections are empty.

## Checklist

```
[ ] 1. Tasks merged; main CI GREEN on the tagged commit (cancelled ≠ green —
       re-run + wait for success); version picked
[ ] 1a. Advisory ledger reconciled: `### Security` headings == ledger rows;
        GHSA count recorded; at a GA tag zero "Owed — decision pending"
        rows (docs/security/advisory-ledger.md; SECURITY.md disclosure steps)
[ ] 2. CHANGELOG: completeness audited, missing bullets backfilled,
       [Unreleased] rolled to [X.Y.Z] (post-tag work left behind);
       docs-site/reference/whats-new.md section added for [X.Y.Z]
       (publishes with the v* tag — skipping it ships a stale What's new)
[ ] 3. Release-body path-freshness gate green
       (scripts/release/check_release_body_paths.py — sister to #928);
       upgrade-relevant change → deploying.md version-specific notes row
       appended; release-cutting PR merged to main
[ ] 4. Tagged vX.Y.Z + pushed
[ ] 5. GH Release notes correct (not [Unreleased] fallback); image, chart,
       CLI tarballs all published
[ ] 6. Deployed to rke2-infra; smoke-green
[ ] 6a. Post-deploy enablement — for each gate in /ready features:
        configure the env vars per the cited Vault doc; verify gate
        flips to configured (or capture_mode=when_negotiated for audit_replay
        post-T6)
[ ] 6b. Rollback drill (releases carrying a migration): broken-readiness
        upgrade auto-rolls-back; rolled-back pods reach Ready with
        db_ahead=true and no manual alembic (#1607)
[ ] 7. Initiative closed; board/roadmap updated; consumers notified

[ ] rc. Release candidates (vX.Y.Z-rc.N, the v1.0.0 series — section
        "Release candidates"): contract gates green on the candidate
        commit + contract diff since the previous tag classified;
        [Unreleased] audited (PREV = last tag of any kind) but NOT rolled;
        path-freshness gate run against [Unreleased]; advisory ledger
        reconciled per 1a ("Owed — decision pending" rows allowed on an
        rc, listed on #2661; zero at GA); RC_SHA pinned BEFORE the
        checks and tagged unchanged (R4 aborts if it left main); Release
        marked pre-release, image :vX.Y.Z-rc.N, chart X.Y.Z-rc.N; lab
        deploy pinned to exact chart version + promoted image digest,
        smoke-green; round-2 run (#3379) -> rc.N+1 on a fixed
        blocker/major, any contract-surface or Security change, or a red
        rc; promote via steps 2-7 with PREV excluding rcs, only Markdown
        between the last rc and the release-cutting merge, and a
        "Stability scope" (GA / Beta / Experimental) block opening [X.Y.Z]
```

## Failure modes seen (do not repeat)

- **v0.3.1 → v0.5.0:** `v0.3.2` skipped, and the CHANGELOG was never
  rolled, so v0.5.0's notes omitted ~24 PRs. Fixed retroactively; step 2
  prevents recurrence.
- **Empty release notes:** `cli-release.yml` falls back to `[Unreleased]`
  when no `## [X.Y.Z]` section exists at tag time. For a final tag,
  always roll (step 2) *before* tagging (step 4); an rc deliberately does
  not roll and rides this fallback (R3), so for an rc the audit of
  `[Unreleased]` is the guard.
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
