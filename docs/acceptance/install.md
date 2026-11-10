<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# install.sh cold-deploy — acceptance contract

> The producer-side specification of point 1 of the v0.1 "done" list:
>
> > `bash manifests/meho/install.sh --tag <git-sha>`
> > cold-deploy → working MEHO at `https://meho.example.com` in <5 min.
>
> This document codifies **what "passing" looks like** so that the lab
> operator running the cold-deploy from the consumer's deployment repo
> and the maintainer reviewing the result are working from one shared
> definition. The actual cold-deploy runs on the consumer side; the
> acceptance contract lives here so the chart/image producer (this repo)
> owns the "what passing means" half of the handshake.

## Why this lives in `evoila/meho`

The chart, image, and CLI are produced here. The values overlay
template
[`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml),
the chart at
[`deploy/charts/meho/`](../../deploy/charts/meho/), and the operator
CLI shipped on Releases all originate from this repository. The
consumer-side `install.sh` and its values file are
environment-private (real CIDRs, real Keycloak realm, real Vault
address) and live in the consumer's deployment repo.

Splitting the acceptance contract from the install script lets us
audit two questions independently:

1. **Does the install script do the right thing?** — owned by the
   consumer (whether the wrapper invokes `helm upgrade --install`
   with the right flags, picks up the right values file, validates
   pre-flight state).
2. **Does the deployed system pass the acceptance bar?** — owned by
   the producer (what counts as "working MEHO", what the timing
   stopwatch covers, what failure looks like).

If the producer changes the chart in a way that invalidates a
producer-side acceptance bullet (e.g. adds a new probe, raises a
new minimum cluster version), this document is the single place
that records the new bar; the consumer's install script doesn't
need to change in lock-step.

## What "<5 min" means

The 5-minute budget is **end-to-end wall clock** for the cold-deploy
path. Stopwatch semantics:

| Boundary | Event | Notes |
| --- | --- | --- |
| **Start** | The first command in the consumer's `install.sh` returns control to the script's first instruction (after the shebang, before pre-flight). Implementation: `START_TS=$(date -u +%s)` on line 1 of `install.sh` after `set -euo pipefail`. | Wall-clock, not CPU. `time bash install.sh ...` from the operator's shell is the canonical measurement and is what the AC closes on. |
| **Stop** | The verifier (`scripts/acceptance/install-verify.sh`) prints its final `[ok] MEHO is up` line and exits 0. | The verifier runs as the **last step** of the consumer's `install.sh` — that's the contract. |

The 5-minute budget covers **everything in between**:

- Pre-flight checks (VPN reachable, Vault session valid, kubectl
  context set to the target cluster, outbound egress to `ghcr.io` OK)
- Namespace create (if absent)
- `helm upgrade --install meho oci://ghcr.io/evoila/meho-chart`
  pulling the chart, resolving the in-tree `broadcast` subchart,
  rendering manifests
- The database migration (`alembic upgrade head`). With your own
  PostgreSQL it runs in the pre-install/pre-upgrade migration Job.
  With the built-in PostgreSQL (`corePostgres.enabled: true`, the
  chart default) it runs in the `migrate` init container of the
  backplane pod, and there is no Job
  (`deploy/charts/meho/templates/migration-job.yaml`,
  `backend/src/meho_backplane/db/migrate.py`)
- Deployment rollout (image pull, container start, readiness probe
  passing)
- Ingress + cert-manager Certificate issuance (when the certificate
  is freshly minted vs. a cached Secret)
- Verifier probes: `/healthz` 200, `/version` carries the deployed
  git SHA, `/api/v1/health` returns 401 unauthenticated, optional
  authenticated audit-row probe when the operator passes a Keycloak
  access token

The budget does **not** cover:

- Image build (that's `evoila/meho`'s `image.yml` workflow; the
  consumer assumes the image is already pushed to GHCR by the time
  `install.sh` runs)
- Chart publish (same, via `chart.yml`)
- Cluster provisioning (the cluster already exists; bringing up a
  fresh cluster is out of scope)
- Vault / Keycloak / Postgres provisioning when the install uses its
  own (consumer-side prerequisites)
- Operator-side device-code login (`meho login` is exercised by
  the federation-chain smoke in [`smoke.md`](./smoke.md), not this
  one). The cold-deploy
  acceptance does **not** drive `meho login` end-to-end, but the
  realm-side prerequisites the CLI and MCP onramp share — a
  public `meho-cli` Keycloak client (device-grant enabled) and a
  public `meho-mcp` (auth-code + PKCE), each with the same
  5 protocol mappers and 4 default client scopes — are
  consumer-side provisioning steps that must precede `meho login`
  and the MCP-client wire-up. The consolidated recipe lives in
  [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp),
  with a 4-wall symptom→cause→fix matrix that bounds the failure
  modes (notably the `basic`/`sub` gotcha — Keycloak 25+ moved
  `sub` into the `basic` scope, and admin-API-created clients
  don't auto-inherit realm default-default scopes). The CLI-side
  chart value to wire the resulting client_id through is
  `config.keycloakCliClientId` (env `KEYCLOAK_CLI_CLIENT_ID`).
  `meho admin keycloak bootstrap-clients` creates both public
  clients in an existing realm. The chart's built-in Keycloak ships
  a realm that already has `meho-cli` and `meho-mcp`
  (`deploy/charts/meho/charts/keycloak/files/meho-realm.json`).

### Why 5 minutes is the right bar

Empirical comparables for OSS Helm-driven cold-deploys on a warm
cluster:

- ArgoCD `kubectl apply -f install.yaml` — sub-2 minutes typical
- Tekton Pipelines `helm install` — 1-3 minutes typical
- cert-manager `helm install --wait` — 2-4 minutes (issuer
  reconcile is the slow leg)
- SpiceDB `helm install --wait` — 1-2 minutes (no DB migrations)

MEHO's pre-install migration Job + the ingress-controller cert-manager
reconcile push the floor toward 2-3 minutes on warm caches; 5 minutes
covers cold image pulls on the cluster nodes and a freshly-minted
TLS certificate (cert-manager's Let's Encrypt staging issuance is
sub-30s; the lab's internal CA via cert-manager is sub-5s). The
budget has headroom for one transient retry without bursting.

If the cold-deploy ever exceeds 5 minutes on the lab cluster without a
known correlate (full restart of the cluster, GHCR rate limit, etc.),
the regression is a deploy-pipeline bug, **not** a budget bug. The
fix lands in the chart / image / runtime — not in this document.

## What "working MEHO" means

The deployed system is **working** when every assertion in this list
passes. The producer-side verifier
[`scripts/acceptance/install-verify.sh`](../../scripts/acceptance/install-verify.sh)
encodes every one of them as a discrete check.

### Required (verifier-asserted) — runs every cold-deploy

| # | Assertion | How it's checked | Why |
| --- | --- | --- | --- |
| 1 | The backplane Deployment selected by `app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release>` reports `Available=True` with `replicas == readyReplicas` | `kubectl rollout status deployment/$(kubectl get deployment -n <ns> -l app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release> -o jsonpath='{.items[0].metadata.name}') -n <ns> --timeout=60s` (the install already waited with `--wait`) | Pod is up, readiness probe green; the kubelet has admitted the Pod to the Service endpoints. The label-selector form keeps the assertion release-name-agnostic — the chart's `meho.fullname` helper folds a `name`-prefixed release name (`meho`) but prepends release otherwise (`prod` → `prod-meho`) |
| 2 | The migration succeeded: the migration Job (selected by `app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release>`) has `succeeded=1`, or, when there is no Job, the helm release status is `deployed` | `kubectl get job -n <ns> -l app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release> -o jsonpath='{.items[0].status.succeeded}'` returns `1`; with no Job, `helm status` reports `deployed` | Schema is at `head`; the Deployment is rolling against a migrated DB. The Job's delete policy `hook-succeeded` removes a Job that succeeded, and an install with the built-in PostgreSQL has no Job at all (the migration runs in the pod's init container). The verifier then checks that Helm marked the release `deployed`. Same release-name-agnostic discipline as row 1 |
| 3 | `GET https://<host>/healthz` returns 200 | `curl -sf --cacert <ca-bundle> https://<host>/healthz` | Liveness probe contract; the public ingress entry-point is reachable through TLS terminated at ingress-nginx and routed to the Service |
| 4 | `GET https://<host>/version` returns JSON containing the deployed `git_sha`, and that SHA matches the `--tag` value passed to `install.sh` | `curl -sf https://<host>/version \| jq -e '.git_sha == "<expected>"'` | The image that's running is the one the operator asked for. Catches the `helm upgrade --install` no-op case (chart unchanged, image tag unchanged → no rollout, old version still serving) |
| 5 | `GET https://<host>/api/v1/health` (no `Authorization` header) returns 401 | `curl -so /dev/null -w '%{http_code}' https://<host>/api/v1/health` returns `401` | Negative auth test. A 200 here means the federation chain (Keycloak JWT validation middleware) regressed open — the "no anonymous access to authenticated surfaces" invariant. A non-200/non-401 means something else broke (502 → backplane down behind ingress; 503 → readiness flipped after probe) |
| 6 | The `audit_log` table exists in Postgres | A direct `psql` query is operator-cost; the verifier asserts the negative-auth path went through the audit middleware by checking the response carried a freshly-generated `X-Request-ID` header — surrogate for "the middleware chain ran". An inconclusive result is a `[WARN]`, not a `[FAIL]` | Audit middleware reachability — the row write requires an authenticated request, which [`smoke.md`](./smoke.md) covers. Surrogate keeps this verifier authentication-free |
| 7 | Wall-clock duration from `install.sh` start to verifier exit ≤ 300 seconds (5 minutes) | The verifier reads `INSTALL_START_TS` from env (set by the consumer's `install.sh` as the first action) and computes `now - INSTALL_START_TS` | The budget itself. Failing this assertion is a soft fail by default (warning) and a hard fail when the verifier is invoked with `--enforce-budget`. Under `--enforce-budget` the verifier also hard-fails when `INSTALL_START_TS` is unset or non-numeric — a closing run must prove the bar was met, and a missing timestamp can't |

### Optional (operator-asserted) — authenticated probes

These cover the federation chain end-to-end. The verifier accepts a
Keycloak access token via the `MEHO_ACCESS_TOKEN` env var; when set,
two further assertions run.

| # | Assertion | How it's checked | Why |
| --- | --- | --- | --- |
| 8 | `GET https://<host>/api/v1/health` with `Authorization: Bearer <token>` returns 200 | `curl -sf -H "Authorization: Bearer $MEHO_ACCESS_TOKEN" https://<host>/api/v1/health` | Federation chain works for at least one operator — JWKS cached, signature verified, audience matched, Vault OIDC role exchange succeeded |
| 9 | The authenticated probe writes an `audit_log` row | Operator-side: `psql "$DATABASE_URL" -c "SELECT 1 FROM audit_log WHERE operator_sub = '<sub>' AND occurred_at > NOW() - INTERVAL '1 minute' LIMIT 1"` returns 1 row | Audit middleware is synchronous — the row landed before the response. Verifies the v0.1 audit-on-every-authenticated-call contract |

The verifier prints a note pointing at this section when run without
`MEHO_ACCESS_TOKEN`, so the operator sees what they skipped.

## What failure looks like

The verifier runs every check, prints one `[ok]`, `[FAIL]` or
`[WARN]` line per check, and at the end exits 1 when any check failed
(`install-verify.sh: N passed, N failed, N warned`). Common failure
modes the operator should expect:

| Failure | Surface | First debug step |
| --- | --- | --- |
| Migration stuck | Own PostgreSQL: Helm aborts at `pre-install` with `Job has reached the specified backoff limit`, and the Deployment never rolls. Built-in PostgreSQL: the pod stays in `Init` and the `migrate` init container restarts. | Job: `kubectl logs -n <ns> -l app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release>`. Init container: `kubectl logs -n <ns> <pod> -c migrate`. The runner writes the Alembic error to stderr as `migration_failed: <ExcClass>: <msg>` |
| Image pull failure | Pod stays in `ImagePullBackOff` or `ErrImagePull`. `kubectl describe pod` shows the pull error. | Check the image tag matches a published GHCR tag; check `imagePullSecrets` (empty by default — anonymous GHCR pull is expected); check egress from the cluster to `ghcr.io` |
| Probes failing | Pod is `Running` but `Ready=False`. Service has zero endpoints. | `kubectl logs -n <ns> -l app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release> --tail=50` — backplane startup error (most often: Vault unreachable, Keycloak JWKS unreachable, DATABASE_URL malformed) |
| Ingress 502 / 503 | `/healthz` from the operator workstation fails; from inside the cluster it succeeds. | `kubectl describe ingress -n <ns> meho` — check the TLS Secret exists (`cert-manager` Certificate `Ready=True`); from another Pod in the cluster `curl -k http://<svc>.<ns>.svc.cluster.local:8000/healthz` to isolate ingress vs. backplane |
| `/version` git_sha mismatch | Verifier check #4 fails. | The chart/image rolled but the *previous* Pod is still answering. `kubectl rollout status` for a tighter signal; `kubectl get pods -n <ns> -l app.kubernetes.io/name=meho -o yaml \| grep image:` to see which image tag the live Pod is running |
| `/api/v1/health` returns 200 instead of 401 | Verifier check 5 fails. | Federation regression. The Keycloak JWT check (`verify_jwt` in `backend/src/meho_backplane/auth/jwt.py`) does not run on the route. Roll back: `helm rollback meho <prev-revision> -n <ns>` |
| Budget exceeded | Verifier check #7 fails with warning (or hard-fail with `--enforce-budget`). | First inspect `helm history -n <ns> meho` for which revision is in flight, then the slowest leg via `kubectl get events -n <ns> --sort-by=.lastTimestamp` |

The verifier never hides a failure behind a green check. Each
assertion fails-loud with the exact mismatch (e.g. `expected
git_sha=abc1234, got def5678`).

## Who runs the test

| Role | Action | Cadence |
| --- | --- | --- |
| **Lab operator** | Runs `time bash install.sh --tag <fresh-sha>` against a clean `meho` namespace from the operator workstation (with VPN + Vault session). Captures stdout + stderr + `helm history` + `kubectl describe` + the verifier's output. | Once for the acceptance milestone; in addition, on every `repository_dispatch` deploy when the consumer chooses to re-baseline |
| **`evoila/meho` maintainer** | Reviews the captured run (or the linked workflow artefact), confirms the verifier's check 1..7 are green and the wall-clock is ≤ 5 minutes, ticks the "done" point. | Once per milestone review; subsequent runs on `repository_dispatch` are operator-attested |
| **CI (per-PR ephemeral smoke)** | Runs a **subset** of the verifier against the per-PR `meho-ci-<n>` namespace via [`scripts/ci/pr-smoke.sh`](../../scripts/ci/pr-smoke.sh). The PR-smoke is the unauthenticated subset (checks #3, #4 partial, #5); it does **not** assert the budget (PR builds are not Goal-DoD runs). | Every PR against `main` |

The PR-smoke and this verifier intentionally overlap on the
unauthenticated probes — the PR-smoke catches regressions before
they reach `main`; the cold-deploy verifier catches what survives
the smoke (chart-only changes, image-only changes, the cumulative
effect of merged PRs). Together they're the two sides of the
"every code path closes the real-target feedback loop" discipline.

The PR-smoke workflow (`.github/workflows/pr-smoke.yml`) skips its
run when the cluster login secrets are not set. Its recent runs end
as `skipped`, so check the run history before you count on it.

## What the consumer-side `install.sh` needs to do

For the producer-side verifier to be invocable, the consumer's
`install.sh` MUST:

1. **Record the start timestamp** as the first action:

   ```bash
   START_TS=$(date -u +%s)
   export INSTALL_START_TS="$START_TS"
   ```

2. **Pin the image tag.** The image pipeline
   (`.github/workflows/image.yml`) publishes `sha-<40-char-git-sha>`
   for every build, `v<x.y.z>` for a release and `main` for the
   newest `main` build. There is no tag with the bare SHA. So
   `install.sh --tag <git-sha>` resolves to
   `--set image.tag=sha-<40-char-git-sha>`, or use the release tag
   (`--set image.tag=v<x.y.z>`). `:latest` does not exist and `:main`
   is forbidden by the deploy discipline; the chart's
   `values.schema.json` rejects an empty `image.tag` so a forgotten
   `--set` fails-loud. The verifier's `--expected-git-sha` takes the
   commit SHA, bare or with the `sha-` prefix; for a release tag,
   pass the commit SHA of that release.

3. **Run `helm upgrade --install`** against the published chart at
   `oci://ghcr.io/evoila/meho-chart` with the consumer's values file,
   with `--wait --timeout 5m --create-namespace --namespace meho`.

4. **Invoke the verifier as the last step:**

   ```bash
   bash scripts/acceptance/install-verify.sh \
     --host meho.example.com \
     --expected-git-sha "$TAG" \
     --namespace meho
   ```

   (or fetch the script via `curl -sSf
   https://raw.githubusercontent.com/evoila/meho/main/scripts/acceptance/install-verify.sh
   | bash -s -- --host meho.example.com --expected-git-sha "$TAG"
   --namespace meho` if the consumer prefers not to pin a producer
   commit). Always pass `--host`: use your own MEHO host name.

5. **Propagate the verifier's exit code** as `install.sh`'s exit
   code. Either the install passed wholesale (every probe green +
   budget met) or it did not.

A skeleton wrapper showing the contract is in
[`scripts/acceptance/install-verify.sh`](../../scripts/acceptance/install-verify.sh)'s
header comment. The consumer's runbook fleshes it out with the
pre-flight checks (VPN, Vault session) that are environment-specific.

## Acceptance criteria status

The full set of acceptance criteria of this contract and where each
lands:

| AC | Status at PR-time | Evidence path |
| --- | --- | --- |
| AC1 — Cold-install completes in <5 min wall clock | **deferred-to-consumer-side** | The verifier asserts the budget (check 7); the actual cold-deploy runs on the operator workstation against the lab's MEHO host and is captured in the closing comment of the tracking issue |
| AC2 — install.sh pre-flight checks pass (VPN, Vault session, kubectl context, GHCR egress) | **deferred-to-consumer-side** | Pre-flight is `install.sh`'s responsibility — see the consumer's `manifests/meho/install.sh`. The producer-side verifier doesn't re-check VPN / Vault session because that's already verified by the time the `helm upgrade` succeeded. The verifier *does* check GHCR egress implicitly via the chart pull |
| AC3 — `helm upgrade --install` exits 0; backplane Pod Ready | **verifier-asserted** (check #1) | `kubectl rollout status` in the verifier |
| AC4 — Migration Job succeeded; `audit_log` table exists | **verifier-asserted** (check 2 + check 6 surrogate) | `kubectl get job` in the verifier; the table presence is asserted indirectly through the audit middleware's `X-Request-ID` header on probe 5; direct `psql` check lands in the federation-chain smoke ([`smoke.md`](./smoke.md)) |
| AC5 — `curl -sf https://<host>/healthz` 200; `/version` JSON carries deployed `git_sha` | **verifier-asserted** (checks 3 + 4) | curl in the verifier |
| AC6 — TLS works; certificate issued by internal CA via cert-manager | **verifier-asserted** (check #3 with `--cacert`) | The verifier's curl uses the operator-supplied `--cacert` (defaulting to system trust); failure here is either a missing/wrong CA bundle or a cert-manager reconcile that didn't finish in budget |
| AC7 — Test run captured (logs, transcript, timing) | **deferred-to-consumer-side** | The captured artefact lands in the closing comment of the tracking issue |

The "deferred-to-consumer-side" status is **expected and correct** —
the producer-side worker cannot run the cold-deploy itself
(no VPN session, no Vault credentials, no kubectl context). The
producer ships the contract + the verifier; the consumer runs the
deploy + writes the closing artefact.

## References

- Sibling acceptance contracts:
  [`smoke.md`](./smoke.md),
  [`rollback.md`](./rollback.md),
  [`green-counter.md`](./green-counter.md)
- Producer artefacts the cold-deploy uses:
  - Image: `ghcr.io/evoila/meho:<tag>` ([chart `Chart.yaml`](../../deploy/charts/meho/Chart.yaml))
  - Chart: `oci://ghcr.io/evoila/meho-chart:<version>` ([publish workflow](../../.github/workflows/chart.yml))
  - Values overlay template: [`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml)
  - CLI: [`https://github.com/evoila/meho/releases`](https://github.com/evoila/meho/releases)
- PR-smoke (CI subset): [`scripts/ci/pr-smoke.sh`](../../scripts/ci/pr-smoke.sh)
- Deploy surface deep-dive: [`docs/codebase/devops.md`](../codebase/devops.md)
