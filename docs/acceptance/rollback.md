<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `helm rollback meho` — acceptance contract

> The producer-side specification of Goal #11 DoD bullet 3:
>
> > `helm rollback meho` returns to the previous chart version without
> > manual DB intervention (verified end-to-end with a non-trivial
> > schema diff).
>
> This document codifies **what "passing" looks like** at the cluster
> level. The actual rollback exercise runs on the consumer side
> ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc));
> the producer (this repo) owns the acceptance bar + the verifier the
> consumer's exercise script invokes as its last step.

## Tracking issue

This contract closes
[`evoila-bosnia/meho-internal#57`](https://github.com/evoila-bosnia/meho-internal/issues/57)
(parent Initiative
[#54](https://github.com/evoila-bosnia/meho-internal/issues/54),
parent Goal
[#11](https://github.com/evoila-bosnia/meho-internal/issues/11)).

## Two layers of forward-compat assurance

The forward-compat property — "the N image runs cleanly against the
N+1 schema" — is asserted at two layers:

| Layer | Where it lives | What it proves |
| --- | --- | --- |
| **Unit-test** | [`backend/tests/test_migration_rollback.py`](../../backend/tests/test_migration_rollback.py) (Task #30, Initiative #26) | The backplane *code* tolerates a schema ahead of it. testcontainers spin up `pgvector/pgvector:pg16` (image overridable via `MEHO_TEST_PGVECTOR_IMAGE`; pgvector required because migration `0003` runs `CREATE EXTENSION vector`), alembic upgrade head to revision N, apply a synthetic N+1 additive migration ([`backend/tests/fixtures/synthetic_n_plus_1.py`](../../backend/tests/fixtures/synthetic_n_plus_1.py)), make an authenticated `GET /api/v1/health` through the revision-N FastAPI app, assert the audit row landed AND the N+1 columns hold their server-side defaults (the negative assertion: revision-N code did not write them). Runs on every PR via `migration-compat.yml`. |
| **Cluster** | This contract + [`scripts/acceptance/rollback-verify.sh`](../../scripts/acceptance/rollback-verify.sh) (Task #57, Initiative #54) | The *running deployment* tolerates a real `helm rollback` against a Kubernetes cluster after a real `helm upgrade` applied a real additive migration. Verifier asserts `helm history` shows a rollback action, the running Pod is the N image, the schema still carries the N+1 columns, and the public surface (`/healthz`, `/version`, `/api/v1/health`) serves traffic correctly post-rollback. |

The split is intentional: the unit-test gates every PR fast and cheap;
the cluster exercise gates the Goal #11 closing milestone end-to-end.
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

v0.1 deliberately ships **no** down-migrations (Task #29's CI guard
rejects destructive patterns like `DROP COLUMN` / `DROP TABLE` /
`ALTER COLUMN ... TYPE` on every migration after `0001_create_audit_log.py`).
Combined with the additive-only invariant, the property the deploy
contract relies on is:

> The N image runs cleanly against the N+1 schema.

That property is what makes `helm rollback` safe without DB
intervention. The operator runs one command (`helm rollback meho`);
they never run `psql` or `alembic downgrade`. The cluster-level
exercise documented here is the closing-criteria proof that the
property holds against the real lab.

## What "non-trivial schema diff" means

The Goal #11 DoD bullet 3 phrasing requires a *non-trivial* additive
migration — not a no-op deploy that happens to bump the chart
version. The acceptance bar is:

| Property | Bar |
| --- | --- |
| At least one new column on an existing table | The migration is observable from `\d <table>` |
| Column is NULLABLE with a server-side default | PostgreSQL applies the default to existing rows lazily (>= PG 11), keeping the migration O(1) regardless of row count |
| Additive only | No `DROP`, no `ALTER COLUMN ... TYPE`, no `RENAME` — Task #29 CI guard would reject these anyway, but the rollback exercise restates the invariant |
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

1. **Producer-side acceptance smoke** — when the Goal #11 closing
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
| 1 | The backplane Deployment selected by `app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release>` reports `Available=True` post-rollback | `kubectl rollout status deployment/...` with a 60s timeout (the rollback action already used `--wait`) | The rollback Pod is healthy; the kubelet has admitted it to the Service endpoints. Same release-name-agnostic selector as Task #55's verifier |
| 2 | The columns added by the N→N+1 migration are still present in `audit_log` post-rollback | `psql "$DATABASE_URL" -c "SELECT column_name FROM information_schema.columns WHERE table_name='audit_log' AND column_name IN (...)"` matches `--expected-schema-columns` exactly | **Load-bearing forward-compat assertion at the cluster level.** A down-migration would have removed these columns; v0.1 forbids down-migrations, so they stay. Asserting they stay is the cluster-level analogue of the unit-level test's "future columns hold their server-side defaults" check |
| 3 | `GET https://<host>/healthz` returns 200 post-rollback | `curl -sf --cacert <ca-bundle> https://<host>/healthz` | The N image is alive against the N+1 schema. Visible payoff of the forward-compat property: chassis serves traffic, doesn't crash on missing-from-N / extra-from-N+1 columns / type drift |
| 4 | `GET https://<host>/version` returns JSON whose `.git_sha` matches `--n-image-sha` (the SHA of the image the rollback returned to) | `curl -sf https://<host>/version \| jq -e '.git_sha'` then prefix-match against `--n-image-sha` (strip optional `sha-` tag prefix) | THE rollback-specific assertion: catches the case where `helm rollback` exited 0 but the old replicas linger (forgot `--wait`). `/version` reporting the N+1 SHA after a rollback = the rollback didn't actually flip the running image |
| 5 | `GET https://<host>/api/v1/health` (no `Authorization` header) returns 401 post-rollback | `curl -so /dev/null -w '%{http_code}' https://<host>/api/v1/health` returns `401` | Negative auth test. Proves the federation chain (Keycloak JWT middleware) still gates correctly under the N image talking to the N+1 schema. A 200 = middleware regressed open. A 503 = backplane crashed on the extra columns (a real forward-compat regression that would invalidate Goal #11 DoD bullet 3) |

### Deferred-to-operator-side — schema check fallback

Check #2 requires `psql` access to the live database. When the
verifier runs from a host that does not have `DATABASE_URL` set
(operator workstation without DB creds, or the verifier is invoked
from a CI runner that talks to the cluster but not the DB), check #2
becomes a `[note]` line emitting the exact psql command the operator
should run, and the result is recorded in the closing-comment
artefact on issue #57 instead of in the verifier's exit code. Same
posture as `install-verify.sh`'s authenticated audit-row check.

## What failure looks like

The verifier exits non-zero on the first failed check and prints a
diagnostic line. Common failure modes:

| Failure | Surface | First debug step |
| --- | --- | --- |
| Check #1a fails — helm history has only 1 entry | No rollback action recorded against the release. | The consumer-side exercise script didn't run `helm rollback`. Inspect the consumer's transcript; verify `helm rollback meho <revision> -n <ns>` was actually invoked and exited 0 |
| Check #1a fails — latest action has `description="Upgrade complete"` | The operator ran `helm upgrade --install` instead of `helm rollback`. | Re-run with the correct command shape. `helm rollback meho <revision-of-N>` — pass the revision number of the N install, NOT the chart version. `helm history` shows the revision-number column |
| Check #1 fails — Deployment not Available | The rollback rolled the manifest but the new Pods can't start. | `kubectl describe pod -n <ns> -l app.kubernetes.io/instance=<release>` — most likely image pull (N image still on GHCR? deleted by retention policy?), readiness probe failure (Vault/Keycloak reachable from the N image?), or PodSecurity admission |
| Check #2 fails — columns missing | A down-migration was applied somewhere (or the N+1 migration never ran in the first place). | Run `psql "$DATABASE_URL" -c "\d audit_log"` to see the current schema. If the N+1 columns are missing AND the N+1 chart was supposedly installed before the rollback, something is broken in the consumer's exercise script — re-read the consumer's transcript, look for any psql / alembic invocation between the upgrade and the rollback |
| Check #4 fails — `/version` reports N+1 SHA | The rollback action succeeded against the chart, but old N+1 Pods are still serving (rollback didn't wait for the replacement Pods to come up before exiting). | Force a fresh rollout: `kubectl rollout restart deployment -n <ns> -l app.kubernetes.io/instance=<release>` then re-run the verifier. Investigate why `helm rollback --wait` didn't block — wrong release? mismatched `--timeout`? |
| Check #5 fails — `/api/v1/health` returns 503 | The N image **cannot** serve traffic against the N+1 schema. | This is a real forward-compat regression. The audit middleware (`backend/src/meho_backplane/audit/middleware.py`) most likely crashed inserting a row into `audit_log` because the ORM model is incompatible with the actual table schema. Capture `kubectl logs -n <ns> deployment/<release>` and file as a release-blocking issue on `evoila/meho` |
| Check #5 fails — `/api/v1/health` returns 200 | The federation chain regressed open. | Roll *forward* to a known-good revision (`helm rollback <release> <revision-of-N+1> -n <ns>`) immediately; investigate the JWT middleware. This failure is almost certainly unrelated to the rollback exercise (it would also fail a non-rollback `install-verify.sh` run) |

The verifier never hides a failure behind a green check. Each
assertion fails-loud with the exact mismatch.

## Who runs the test

| Role | Action | Cadence |
| --- | --- | --- |
| **RDC operator** | Runs the full deploy → upgrade → rollback exercise from the operator workstation (with VPN + Vault session + kubeconfig). Captures `helm history`, `kubectl describe`, the verifier's transcript. | Per Goal #11 closing-criteria — once for the acceptance milestone. Re-runs after every backplane change that touches the audit middleware or alters `audit_log` |
| **`evoila/meho` maintainer** | Reviews the captured run on issue #57 (or the linked workflow artefact), confirms checks #1a/#1/#2/#3/#4/#5 are green, ticks the DoD bullet on Goal #11 | Once per Goal-closing review |
| **CI (per-PR unit-level)** | Runs `backend/tests/test_migration_rollback.py` on every PR via the central CI matrix. Catches the *code-side* forward-compat regression before the deploy ever happens | Every PR against `main`; gated as a required check by branch protection |

The two layers — unit-level on every PR, cluster-level at the closing
milestone — together compose the forward-compat assurance Goal #11
DoD bullet 3 promises.

## What the consumer-side rollback exercise script needs to do

For the producer-side verifier to be invocable end-to-end, the
consumer's exercise script (lives at
`evoila-bosnia/claude-rdc-hetzner-dc/manifests/meho/rollback-drill.sh`
per Goal #11 cross-repo deps) MUST:

1. **Record the current revision number** as the N install. Pin it:

   ```bash
   N_REV=$(helm history -n meho meho -o json | jq -r '.[-1].revision')
   N_SHA=$(kubectl get deployment -n meho \
     -l app.kubernetes.io/name=meho \
     -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' \
     | awk -F: '{print $2}' | sed 's/^sha-//')
   ```

2. **Apply the non-trivial additive migration**. Either:

    - Apply the sample synthetic migration:
      ```bash
      psql "$DATABASE_URL" -f scripts/acceptance/synthetic-n-plus-1.sql
      ```
    - OR run the consumer's real N→N+1 alembic migration (the
      consumer's own Goal #11 schema bump). The verifier accepts a
      comma-separated `--expected-schema-columns` so the consumer's
      column names land in the assertion.

3. **Run `helm upgrade --install`** to the N+1 chart version. The
   chart's pre-install/pre-upgrade migration Job is a no-op when the
   schema is already at N+1 (alembic detects head). The Deployment
   rolls to the N+1 image.

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
     --host meho.evba.lab \
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

The full set of acceptance criteria on
[issue #57](https://github.com/evoila-bosnia/meho-internal/issues/57)
and where each lands:

| AC | Status at PR-time | Evidence path |
| --- | --- | --- |
| AC1 — Deploy v0.1.A (current state from G2.8-T1) | **deferred-to-consumer-side** | Consumer-side install (the post-Task-#55 lab state); not exercised by the producer's verifier |
| AC2 — Author and merge a PR adding a non-trivial additive migration; v0.1.B chart published | **deferred-to-consumer-side** | The sample synthetic migration at [`scripts/acceptance/synthetic-n-plus-1.sql`](../../scripts/acceptance/synthetic-n-plus-1.sql) ships as a producer-side fixture; the actual "publish v0.1.B" step is the chart-publish workflow running on a future schema-bump PR's merge to `main` |
| AC3 — `helm upgrade --install meho ... --version <v0.1.B>` succeeds; PG schema is at v0.1.B; `meho status` passes | **deferred-to-consumer-side** | The consumer's exercise script drives the upgrade and asserts pre-rollback state. The producer-side verifier is **not** invoked at this stage of the exercise |
| AC4 — `helm rollback meho <N-revision>` returns chart to v0.1.A; PG schema **stays at** v0.1.B | **verifier-asserted** (checks #1a + #2) | `helm history` shape (check #1a) + schema-column persistence via psql (check #2 when `$DATABASE_URL` is set, deferred-to-operator-side otherwise) |
| AC5 — No DB intervention required — operator runs only `helm rollback`, no `psql`, no `alembic downgrade` | **verifier-asserted (negative)** + **deferred-to-consumer-side (positive)** | The producer-side verifier asserts the schema retained the N+1 shape (check #2 — a positive proof that no `psql` deleted columns and no `alembic downgrade` ran). The consumer's exercise transcript proves the negative (operator commands list contains only `helm rollback`) |
| AC6 — v0.1.A image with v0.1.B schema: `meho status` still passes; audit rows written for new requests; no schema-mismatch errors in `kubectl logs` | **verifier-asserted** (checks #1 + #3 + #4 + #5) | Deployment Available (#1), `/healthz` 200 (#3), `/version` reports N image SHA (#4), `/api/v1/health` returns 401 (#5 — proves audit middleware can still chain to the audit table without crashing on extra columns) |
| AC7 — Test exercise captured: PR link, helm history, kubectl describe, log excerpts, timing | **deferred-to-consumer-side** | The captured artefact lands in the closing comment on #57 |

The "deferred-to-consumer-side" status is **expected and correct** —
the producer-side worker for #57 cannot run the deploy/rollback
exercise itself (no VPN session, no Vault credentials, no kubectl
context, no chart-publish dispatch). The producer ships the contract
+ the verifier + the sample synthetic migration; the consumer runs
the exercise + writes the closing artefact.

## References

- Parent Goal:
  [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
  (DoD bullet 3)
- Parent Initiative:
  [#54 — G2.8 Acceptance / dogfood proof](https://github.com/evoila-bosnia/meho-internal/issues/54)
- This task:
  [#57 — helm rollback verified end-to-end with non-trivial schema diff](https://github.com/evoila-bosnia/meho-internal/issues/57)
- Predecessor (unit-level proof):
  [#30 — forward-compat regression test (testcontainers)](https://github.com/evoila-bosnia/meho-internal/issues/30)
  / [`backend/tests/test_migration_rollback.py`](../../backend/tests/test_migration_rollback.py)
- Predecessor (additive-only migration discipline):
  [#29 — migration runner entrypoint + CI guard rejecting destructive migration patterns](https://github.com/evoila-bosnia/meho-internal/issues/29)
- Predecessor (cold-deploy install): [Task #55 — `install.sh` cold-deploy](https://github.com/evoila-bosnia/meho-internal/issues/55) / [PR #189](https://github.com/evoila/meho/pull/189)
- Sibling acceptance tasks:
  [#55 — install.sh cold-deploy](https://github.com/evoila-bosnia/meho-internal/issues/55) (closed),
  [#56 — smoke.sh federation chain](https://github.com/evoila-bosnia/meho-internal/issues/56),
  [#58 — 5-PR green counter](https://github.com/evoila-bosnia/meho-internal/issues/58)
- Producer artefacts the rollback exercise uses:
  - Chart: `oci://ghcr.io/evoila/meho-chart:<version>` ([publish workflow](../../.github/workflows/chart.yml))
  - Migration Job template: [`deploy/charts/meho/templates/migration-job.yaml`](../../deploy/charts/meho/templates/migration-job.yaml)
- [`helm rollback` reference](https://helm.sh/docs/helm/helm_rollback/)
- [Helm chart hooks reference](https://helm.sh/docs/topics/charts_hooks/) — `pre-rollback` / `post-rollback` are documented; the chart deliberately ships neither (v0.1 forbids down-migrations)
- Cross-repo handshake: [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
- Deploy surface deep-dive: [`docs/codebase/devops.md`](../codebase/devops.md)
