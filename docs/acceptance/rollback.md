<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `helm rollback meho` — acceptance contract

> The producer-side specification of point 3 of the v0.1 "done" list:
>
> > `helm rollback meho` returns to the previous chart version without
> > manual DB intervention (verified end-to-end with a non-trivial
> > schema diff).
>
> This document codifies **what "passing" looks like** at the cluster
> level. The actual rollback exercise runs on the consumer side
> (the lab's deployment repo);
> the producer (this repo) owns the acceptance bar + the verifier the
> consumer's exercise script invokes as its last step.

## Two layers of forward-compat assurance

The forward-compat property — "the N image runs cleanly against the
N+1 schema" — is asserted at two layers:

| Layer | Where it lives | What it proves |
| --- | --- | --- |
| **Unit-test** | [`backend/tests/migrations/test_migration_rollback.py`](../../backend/tests/migrations/test_migration_rollback.py) | The backplane *code* tolerates a schema ahead of it. testcontainers spin up `pgvector/pgvector:pg16` (image overridable via `MEHO_TEST_PGVECTOR_IMAGE`; pgvector required because migration `0003` runs `CREATE EXTENSION vector`), alembic upgrade head to revision N, apply a synthetic N+1 additive migration ([`backend/tests/fixtures/synthetic_n_plus_1.py`](../../backend/tests/fixtures/synthetic_n_plus_1.py)), make an authenticated `GET /api/v1/health` through the revision-N FastAPI app, assert the audit row landed AND the N+1 columns hold their server-side defaults (the negative assertion: revision-N code did not write them). Runs on migration-touching PRs (and every `push`/`merge_group`) via the `python-migration-tests` job in `.github/workflows/ci.yml`. |
| **Cluster** | This contract + [`scripts/acceptance/rollback-verify.sh`](../../scripts/acceptance/rollback-verify.sh) | The *running deployment* tolerates a real `helm rollback` against a Kubernetes cluster after a real `helm upgrade` applied a real additive migration. Verifier asserts `helm history` shows a rollback action, the running Pod is the N image, the schema still carries the N+1 columns, and the public surface (`/healthz`, `/version`, `/api/v1/health`) serves traffic correctly post-rollback. |

The split is intentional: the unit-test gates migration-touching PRs
fast and cheap (a broken chain still fails every PR via the unit lane's
`alembic upgrade head` fixture); the cluster exercise gates the v0.1
closing milestone end-to-end.
A regression in the code's forward-compat property fails CI in seconds
without waiting on the expensive lab deploy.

## Why "schema stays at N+1" is the design

`helm rollback` does **not** invoke pre-install/pre-upgrade hooks for
the previous revision (per [Helm chart hooks reference](https://helm.sh/docs/topics/charts_hooks/)).
The chart's migration Job
([`deploy/charts/meho/templates/migration-job.yaml`](../../deploy/charts/meho/templates/migration-job.yaml))
is annotated `helm.sh/hook: pre-install,pre-upgrade` only — no
`pre-rollback` hook. So:

- A `helm upgrade` from N to N+1 runs the migration Job, which runs
  `alembic upgrade head`. The schema moves N → N+1.
- A `helm rollback` from N+1 back to N **does not** run any migration
  Job. The chart's resources (Deployment, Service, ConfigMap, etc.)
  revert to the N revision's manifests — including the image tag —
  but no migration runs. The schema stays at N+1.

With the built-in PostgreSQL (`corePostgres.enabled: true`, the chart
default) there is no migration Job. Every backplane pod runs the
migration in its `migrate` init container. After a rollback, the N
pod's runner finds the database at a revision its image does not
know. It prints `migration_skipped` and exits 0, so the pod starts
and the schema stays at N+1
(`backend/src/meho_backplane/db/migrate.py`, step 3).

MEHO ships **no** down-migrations. The CI guard
`scripts/ci/check_migration_compat.py` rejects destructive patterns in
the `upgrade()` of every migration: dropped columns and tables,
renamed tables and columns, and a new `NOT NULL`.
Combined with the additive-only invariant, the property the deploy
contract relies on is:

> The N image runs cleanly against the N+1 schema.

That property is what makes `helm rollback` safe without DB
intervention. The operator runs one command (`helm rollback meho`);
they never run `psql` or `alembic downgrade`. The cluster-level
exercise documented here is the closing-criteria proof that the
property holds against the real lab.

## What "non-trivial schema diff" means

The "done" point's phrasing requires a *non-trivial* additive
migration — not a no-op deploy that happens to bump the chart
version. The acceptance bar is:

| Property | Bar |
| --- | --- |
| At least one new column on an existing table | The migration is observable from `\d <table>` |
| Column is NULLABLE with a server-side default | PostgreSQL applies the default to existing rows lazily (>= PG 11), keeping the migration O(1) regardless of row count |
| Additive only | No `DROP`, no `ALTER COLUMN ... TYPE`, no `RENAME` — the CI guard rejects drops and renames anyway, but the rollback exercise restates the invariant |
| Touches a table the N image actively reads or writes | The forward-compat property only matters for the surface the code actually exercises. `audit_log` is the right table — the audit middleware writes to it on every authenticated request |

The sample synthetic migration shipped at
[`scripts/acceptance/synthetic-n-plus-1.sql`](../../scripts/acceptance/synthetic-n-plus-1.sql)
adds two columns to `audit_log`:

- `payload_summary text NULL DEFAULT 'reserved_for_v0.2'` — the
  rollback verifier's default `--expected-schema-columns` target.
- `payload_summary_jsonb jsonb NULL DEFAULT '{}'::jsonb` — companion
  JSONB column mirroring the unit-level fixture's shape; representative
  of the most realistic future v0.2 additive change.

Both are wrapped in a single transaction so a half-applied state can
never confuse the verifier's assertions.

The sample exists for two scenarios:

1. **Producer-side acceptance smoke** — when the v0.1 closing
   exercise runs against the lab, the operator can apply this exact
   SQL as the N+1 step, sidestepping the need to author a one-shot
   alembic migration for the test.
2. **Other consumers** running the same rollback exercise drill in
   their own labs — they can copy + adapt the SQL (or substitute
   their own real N+1 migration). The verifier's
   `--expected-schema-columns` flag accepts a comma-separated list so
   the operator's real migration shape can be asserted directly.

## What "verified" means

The deployed system has **verified** the rollback when every required
assertion below passes. The producer-side verifier
[`scripts/acceptance/rollback-verify.sh`](../../scripts/acceptance/rollback-verify.sh)
encodes every one of them as a discrete check.

### Required (verifier-asserted) — runs every rollback exercise

| # | Assertion | How it's checked | Why |
| --- | --- | --- | --- |
| 1a | `helm history -n <ns> <release>` has >=2 revisions, and the most recent has `description="Rollback to <rev>"` with `status="deployed"` | `helm history ... -o json \| jq '.[-1] \| {description, status}'` | Catches the silent failure mode where the operator ran `helm upgrade --install` (re-deploy of N image) instead of `helm rollback` — same end state superficially, but proves nothing about the rollback path |
| 1 | The backplane Deployment selected by `app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release>` reports `Available=True` post-rollback | `kubectl rollout status deployment/...` with a 60s timeout (the rollback action already used `--wait`) | The rollback Pod is healthy; the kubelet has admitted it to the Service endpoints. Same release-name-agnostic selector as `install-verify.sh` |
| 2 | The columns added by the N→N+1 migration are still present in `audit_log` post-rollback | `psql "$DATABASE_URL" -c "SELECT column_name FROM information_schema.columns WHERE table_name='audit_log' AND column_name IN (...)"` matches `--expected-schema-columns` exactly | **Load-bearing forward-compat assertion at the cluster level.** A down-migration would have removed these columns; MEHO forbids down-migrations, so they stay. Asserting they stay is the cluster-level analogue of the unit-level test's "future columns hold their server-side defaults" check |
| 3 | `GET https://<host>/healthz` returns 200 post-rollback | `curl -sf --cacert <ca-bundle> https://<host>/healthz` | The N image is alive against the N+1 schema. Visible payoff of the forward-compat property: chassis serves traffic, doesn't crash on missing-from-N / extra-from-N+1 columns / type drift |
| 4 | `GET https://<host>/version` returns JSON whose `.git_sha` matches `--n-image-sha` (the SHA of the image the rollback returned to) | `curl -sf https://<host>/version \| jq -e '.git_sha'` then prefix-match against `--n-image-sha` (strip optional `sha-` tag prefix) | THE rollback-specific assertion: catches the case where `helm rollback` exited 0 but the old replicas linger (forgot `--wait`). `/version` reporting the N+1 SHA after a rollback = the rollback didn't actually flip the running image |
| 5 | `GET https://<host>/api/v1/health` (no `Authorization` header) returns 401 post-rollback | `curl -so /dev/null -w '%{http_code}' https://<host>/api/v1/health` returns `401` | Negative auth test. Proves the federation chain (Keycloak JWT middleware) still gates correctly under the N image talking to the N+1 schema. A 200 = middleware regressed open. A 503 = backplane crashed on the extra columns (a real forward-compat regression that would invalidate this contract) |

### Deferred-to-operator-side — schema check fallback

Check #2 requires `psql` access to the live database. When the
verifier runs from a host that does not have `DATABASE_URL` set
(operator workstation without DB creds, or the verifier is invoked
from a CI runner that talks to the cluster but not the DB), check #2
becomes a `[note]` line emitting the exact psql command the operator
should run, and the result is recorded in the closing-comment
artefact of the tracking issue instead of in the verifier's exit code. Same
posture as `install-verify.sh`'s authenticated audit-row check.

## What an update and a rollback do to the operation rows

MEHO keeps the settings of every built-in operation in shared database
rows: which code runs, the parameter rules, the risk level and the
approval flag. All MEHO pods read the same rows. Every start writes them
from the code of the release that starts.

**A start that fails changes no row.** The start writes all
operation rows in one database transaction. It commits that transaction
only as its last step, after every start-up check passed and every
background loop (the sweepers, the scheduler, the check runner) started.
When a check, the write itself, the start of a loop or the final
commit fails, the transaction is undone: the rows stay as the previous release left them,
and the loops that already started are stopped again. The log then shows
`operation_catalog_unchanged`.

**The rows say which release wrote them.** In the same transaction the
start stores its release (the commit hash, `git_sha` of `GET /version`)
in the one-row table `operation_catalog_state`. `GET /api/v1/health`
and the MCP tool `meho_status` show it as `operation_catalog`:

| `state` | Meaning |
| --- | --- |
| `same_release` | This pod's release wrote the rows. All is well. |
| `other_release` | Another release wrote the rows. This pod serves settings that were not made for its code. When the health status is read (`GET /api/v1/health`, `meho_status`), the pod logs `operation_catalog_written_by_other_release`, and `meho status` prints one line. Nothing checks it on its own, so no log line appears until someone reads the health status. |
| `not_recorded` | No start since this check shipped has written the rows yet. |
| `unknown` | The database could not be read. The `db` field says more. |

**The old pod stops before the new pod starts (#5024).** The chart's
update strategy is `Recreate` (value `updateStrategy`). Kubernetes stops
the old pod first and starts the new pod only when the old one is gone.
So old and new code never work on the database at the same time, and no
old pod serves the rows of a new release. The cost is about one to two
minutes without MEHO per update.

How the old pod stops:

1. It keeps serving for `shutdown.preStopSleepSeconds` (default 5), so
   the Service and the ingress stop sending it requests.
2. It gets the stop signal. From then on `/ready` answers 503, and it
   takes no new calls: a new call gets the error `meho_shutting_down`,
   and nothing is sent to the target. The sensor runner and the scheduler
   start no new work.
3. Running operations may finish, for up to `shutdown.drainSeconds`
   (default 60).
4. An operation that still runs at that limit is recorded as unfinished:
   a log line `operation_unfinished_at_shutdown` and an audit row with
   `result_status: unfinished` and `execution_outcome: uncertain`. It may
   have reached the target. MEHO never runs it again by itself. An
   approved request keeps its "started" mark with no result, and an async
   run stays `running` until its lease ends, so the recovery at the next
   start can find both.

Kubernetes kills the pod when its grace period is over: the pre-stop wait
plus the drain plus 30 seconds.

**With `updateStrategy.type: RollingUpdate`** (only for installs with
several pods) the new pod starts while the old pod still serves. When the
new pod has started, it has written its rows, so the old pod serves the
new rows until it stops. This is also true when the new pod then never
becomes ready (for example Keycloak or Vault cannot be reached): its start
did not fail, so its rows stay. `/ready` does not look at the operation
rows.

**When the new release fails to start.** With `Recreate`, the old pod is
already gone, so MEHO is down until you act. A failed start changed no
operation row, so the previous release can simply run again:

1. See why the start failed:
   `kubectl logs -n <ns> deploy/<release> -c backplane --previous`
   (and `-c migrate --previous` with the built-in PostgreSQL).
2. Find the last good revision: `helm history <release> -n <ns>`.
3. Roll back and wait until the pod is ready:
   `helm rollback <release> <revision> -n <ns> --wait --timeout 10m`.
4. Check that MEHO serves again: `GET /healthz` answers 200, and
   `GET /version` shows the previous release's `git_sha`.
5. Check the operation rows: `GET /api/v1/health` shows
   `operation_catalog` with `same_release`. If it shows
   `other_release`, follow the steps below.
6. Check the audit log for rows with `result_status: unfinished` from the
   time of the update, and check their targets before anyone runs them
   again.

CI runs this recovery on every chart change: the `helm-test` job in
`.github/workflows/chart.yml` updates a throwaway install and checks that
the old pod has stopped before the new one starts. Then it starts a
release that fails on purpose right after it wrote its operation rows and
its release stamp (the test-only setting
`MEHO_TEST_FAIL_START_AFTER_REGISTRATION`; the broken release disables
every built-in row and stamps another release name before it fails). It
checks that the rows and the stamp did not change, rolls back, and runs an
operation on the restored release.

**What to do when a pod reports `other_release`:**

1. Check which releases run: `kubectl get pods -n <ns> -l app.kubernetes.io/instance=<release> -o wide`
   and `GET /version` on each pod.
2. While an update is still in progress, wait until it has finished.
   The old pods stop, and the new pods report `same_release`.
3. After a `helm rollback`, or when only pods of one release should run
   but the rows were written by another release: restart the pods of
   the release you want to keep, for example
   `kubectl rollout restart deployment -n <ns> -l app.kubernetes.io/instance=<release>`. Each start
   writes that release's rows again. Then check that `GET /api/v1/health`
   shows `same_release`.

A release from before this check does not write `operation_catalog_state`.
After a rollback to such a release, its restarts rewrite the operation
rows, but the table still names the newer release. Only the newer
release reads the table, so the old release is not affected.

A restart does not remove rows that only the other release knows. They
stay on purpose (an older release must not delete operations a newer
release owns), and calls to them fail with `handler_unreachable` until
that release runs again.

## What failure looks like

The verifier runs every check, prints one line per check, and at the
end exits 1 when any check failed. Common failure modes:

| Failure | Surface | First debug step |
| --- | --- | --- |
| Check #1a fails — helm history has only 1 entry | No rollback action recorded against the release. | The consumer-side exercise script didn't run `helm rollback`. Inspect the consumer's transcript; verify `helm rollback meho <revision> -n <ns>` was actually invoked and exited 0 |
| Check #1a fails — latest action has `description="Upgrade complete"` | The operator ran `helm upgrade --install` instead of `helm rollback`. | Re-run with the correct command shape. `helm rollback meho <revision-of-N>` — pass the revision number of the N install, NOT the chart version. `helm history` shows the revision-number column |
| Check #1 fails — Deployment not Available | The rollback rolled the manifest but the new Pods can't start. | `kubectl describe pod -n <ns> -l app.kubernetes.io/instance=<release>` — most likely image pull (N image still on GHCR? deleted by retention policy?), readiness probe failure (Vault/Keycloak reachable from the N image?), or PodSecurity admission |
| Check #2 fails — columns missing | A down-migration was applied somewhere (or the N+1 migration never ran in the first place). | Run `psql "$DATABASE_URL" -c "\d audit_log"` to see the current schema. If the N+1 columns are missing AND the N+1 chart was supposedly installed before the rollback, something is broken in the consumer's exercise script — re-read the consumer's transcript, look for any psql / alembic invocation between the upgrade and the rollback |
| Check #4 fails — `/version` reports N+1 SHA | The rollback action succeeded against the chart, but old N+1 Pods are still serving (rollback didn't wait for the replacement Pods to come up before exiting). | Force a fresh rollout: `kubectl rollout restart deployment -n <ns> -l app.kubernetes.io/instance=<release>` then re-run the verifier. Investigate why `helm rollback --wait` didn't block — wrong release? mismatched `--timeout`? |
| Check 5 fails — `/api/v1/health` returns 503 | The N image **cannot** serve traffic against the N+1 schema. | This is a real forward-compat regression. The audit middleware (`backend/src/meho_backplane/audit.py`) most likely crashed inserting a row into `audit_log` because the ORM model is incompatible with the actual table schema. Capture `kubectl logs -n <ns> deployment/<release>` and file as a release-blocking issue on `evoila/meho` |
| Check #5 fails — `/api/v1/health` returns 200 | The federation chain regressed open. | Roll *forward* to a known-good revision (`helm rollback <release> <revision-of-N+1> -n <ns>`) immediately; investigate the JWT middleware. This failure is almost certainly unrelated to the rollback exercise (it would also fail a non-rollback `install-verify.sh` run) |

The verifier never hides a failure behind a green check. Each
assertion fails-loud with the exact mismatch.

## Who runs the test

| Role | Action | Cadence |
| --- | --- | --- |
| **Lab operator** | Runs the full deploy → upgrade → rollback exercise from the operator workstation (with VPN + Vault session + kubeconfig). Captures `helm history`, `kubectl describe`, the verifier's transcript. | Once for the acceptance milestone. Re-runs after every backplane change that touches the audit middleware or alters `audit_log` |
| **`evoila/meho` maintainer** | Reviews the captured run (or the linked workflow artefact), confirms checks 1a, 1, 2, 3, 4 and 5 are green, ticks the "done" point | Once per milestone review |
| **CI (unit-level)** | Runs `backend/tests/migrations/test_migration_rollback.py` via the `python-migration-tests` job in `.github/workflows/ci.yml`. Catches the *code-side* forward-compat regression before the deploy ever happens | Migration-touching PRs against `main` (and every `push`/`merge_group`); gated as a required check by branch protection — the job skips (→ success) on non-migration PRs, where the unit lane's `alembic upgrade head` fixture still guards a broken chain |

The two layers — unit-level on migration-touching PRs, cluster-level at
the closing milestone — together compose the forward-compat assurance
this contract promises.

## What the consumer-side rollback exercise script needs to do

For the producer-side verifier to be invocable end-to-end, the
consumer's exercise script (`manifests/meho/rollback-drill.sh` in the
lab's deployment repo) MUST:

1. **Record the current revision number** as the N install. Pin it,
   and read the N image's commit SHA from `GET /version` (the image
   tag can be a release tag such as `v<x.y.z>`, or the image can be
   pinned by digest, so the tag is not always the SHA):

   ```bash
   N_REV=$(helm history -n meho meho -o json | jq -r '.[-1].revision')
   N_SHA=$(curl -sf https://meho.example.com/version | jq -r '.git_sha')
   ```

2. **Apply the non-trivial additive migration**. Either:

    - Apply the sample synthetic migration:
      ```bash
      psql "$DATABASE_URL" -f scripts/acceptance/synthetic-n-plus-1.sql
      ```
    - OR run the consumer's real N→N+1 alembic migration (the
      consumer's own schema bump). The verifier accepts a
      comma-separated `--expected-schema-columns` so the consumer's
      column names land in the assertion.

3. **Run `helm upgrade --install`** to the N+1 chart version. The
   migration (the hook Job, or the `migrate` init container with the
   built-in PostgreSQL) upgrades the schema to the N+1 image's head.
   The Deployment rolls to the N+1 image.

4. **Run `helm rollback`** back to the N revision, waiting for the
   rollback to fully roll the Deployment:

   ```bash
   helm rollback meho "$N_REV" -n meho --wait --timeout 5m
   ```

   `--wait` is non-negotiable — without it the rollback action exits
   as soon as the chart manifest flips, before the new replicas
   (running the N image) replace the old ones. Verifier check #4
   would catch this, but waiting is cheaper than re-rolling.

5. **Invoke the verifier** as the last step:

   ```bash
   export DATABASE_URL='postgresql://meho:...@.../meho'
   bash scripts/acceptance/rollback-verify.sh \
     --host meho.example.com \
     --n-image-sha "$N_SHA" \
     --namespace meho \
     --release meho \
     --expected-schema-columns payload_summary
   ```

   (or fetch the verifier via `curl -sSf
   https://raw.githubusercontent.com/evoila/meho/main/scripts/acceptance/rollback-verify.sh`
   if the consumer prefers not to pin a producer commit).

6. **Propagate the verifier's exit code** as the exercise's exit
   code. Either the rollback passed wholesale (every probe green +
   schema retained the N+1 columns) or it did not.

A skeleton wrapper showing the contract is in
[`scripts/acceptance/rollback-verify.sh`](../../scripts/acceptance/rollback-verify.sh)'s
header comment. The consumer's runbook fleshes it out with the
pre-flight checks (VPN, Vault session) that are environment-specific.

## Acceptance-criteria status

The full set of acceptance criteria of this contract and where each
lands:

| AC | Status at PR-time | Evidence path |
| --- | --- | --- |
| AC1 — Deploy v0.1.A (the state after the [`install.md`](./install.md) run) | **deferred-to-consumer-side** | Consumer-side install; not exercised by the producer's verifier |
| AC2 — Author and merge a PR adding a non-trivial additive migration; v0.1.B chart published | **deferred-to-consumer-side** | The sample synthetic migration at [`scripts/acceptance/synthetic-n-plus-1.sql`](../../scripts/acceptance/synthetic-n-plus-1.sql) ships as a producer-side fixture; the actual "publish v0.1.B" step is the chart-publish workflow running on a future schema-bump PR's merge to `main` |
| AC3 — `helm upgrade --install meho ... --version <v0.1.B>` succeeds; PG schema is at v0.1.B; `meho status` passes | **deferred-to-consumer-side** | The consumer's exercise script drives the upgrade and asserts pre-rollback state. The producer-side verifier is **not** invoked at this stage of the exercise |
| AC4 — `helm rollback meho <N-revision>` returns chart to v0.1.A; PG schema **stays at** v0.1.B | **verifier-asserted** (checks #1a + #2) | `helm history` shape (check #1a) + schema-column persistence via psql (check #2 when `$DATABASE_URL` is set, deferred-to-operator-side otherwise) |
| AC5 — No DB intervention required — operator runs only `helm rollback`, no `psql`, no `alembic downgrade` | **verifier-asserted (negative)** + **deferred-to-consumer-side (positive)** | The producer-side verifier asserts the schema retained the N+1 shape (check #2 — a positive proof that no `psql` deleted columns and no `alembic downgrade` ran). The consumer's exercise transcript proves the negative (operator commands list contains only `helm rollback`) |
| AC6 — v0.1.A image with v0.1.B schema: `meho status` still passes; audit rows written for new requests; no schema-mismatch errors in `kubectl logs` | **verifier-asserted** (checks #1 + #3 + #4 + #5) | Deployment Available (#1), `/healthz` 200 (#3), `/version` reports N image SHA (#4), `/api/v1/health` returns 401 (#5 — proves audit middleware can still chain to the audit table without crashing on extra columns) |
| AC7 — Test exercise captured: PR link, helm history, kubectl describe, log excerpts, timing | **deferred-to-consumer-side** | The captured artefact lands in the closing comment of the tracking issue |

The "deferred-to-consumer-side" status is **expected and correct** —
the producer-side worker cannot run the deploy/rollback
exercise itself (no VPN session, no Vault credentials, no kubectl
context, no chart-publish dispatch). The producer ships the contract
+ the verifier + the sample synthetic migration; the consumer runs
the exercise + writes the closing artefact.

## References

- Unit-level proof (testcontainers forward-compat test):
  [`backend/tests/migrations/test_migration_rollback.py`](../../backend/tests/migrations/test_migration_rollback.py)
- Additive-only migration discipline: the runner
  [`backend/src/meho_backplane/db/migrate.py`](../../backend/src/meho_backplane/db/migrate.py)
  and the CI guard
  [`scripts/ci/check_migration_compat.py`](../../scripts/ci/check_migration_compat.py)
- Sibling acceptance contracts:
  [`install.md`](./install.md),
  [`smoke.md`](./smoke.md),
  [`green-counter.md`](./green-counter.md)
- Producer artefacts the rollback exercise uses:
  - Chart: `oci://ghcr.io/evoila/meho-chart:<version>` ([publish workflow](../../.github/workflows/chart.yml))
  - Migration Job template: [`deploy/charts/meho/templates/migration-job.yaml`](../../deploy/charts/meho/templates/migration-job.yaml)
- [`helm rollback` reference](https://helm.sh/docs/helm/helm_rollback/)
- [Helm chart hooks reference](https://helm.sh/docs/topics/charts_hooks/) — `pre-rollback` / `post-rollback` are documented; the chart deliberately ships neither (MEHO forbids down-migrations)
- Deploy surface deep-dive: [`docs/codebase/devops.md`](../codebase/devops.md)
