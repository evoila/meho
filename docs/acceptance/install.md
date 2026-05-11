<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# install.sh cold-deploy — acceptance contract

> The producer-side specification of Goal #11 DoD bullet 1:
>
> > `bash rdc-hetzner-dc/manifests/meho/install.sh --tag <git-sha>`
> > cold-deploy → working MEHO at <https://meho.evba.lab> in <5 min.
>
> This document codifies **what "passing" looks like** so that the RDC
> operator running the cold-deploy on
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> and the maintainer reviewing the result are working from one shared
> definition. The actual cold-deploy runs on the consumer side; the
> acceptance contract lives here so the chart/image producer (this repo)
> owns the "what passing means" half of the handshake.

## Tracking issue

This contract closes
[`evoila-bosnia/meho-internal#55`](https://github.com/evoila-bosnia/meho-internal/issues/55)
(parent Initiative
[#54](https://github.com/evoila-bosnia/meho-internal/issues/54),
parent Goal
[#11](https://github.com/evoila-bosnia/meho-internal/issues/11)).

## Why this lives in `evoila/meho`

The chart, image, and CLI are produced here. The values overlay
template
[`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml),
the chart at
[`deploy/charts/meho/`](../../deploy/charts/meho/), and the operator
CLI shipped on Releases all originate from this repository. The
consumer-side
`claude-rdc-hetzner-dc/manifests/meho/{install.sh,values-rdc.yaml}`
is environment-private (real CIDRs, real Keycloak realm, real Vault
address) and lives on the consumer side per
[Goal #11 cross-repo deps](https://github.com/evoila-bosnia/meho-internal/issues/11).

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
  context set to `rke2-infra`, outbound egress to `ghcr.io` OK)
- Namespace create (if absent)
- `helm upgrade --install meho oci://ghcr.io/evoila/meho-chart`
  pulling the chart, resolving the in-tree `broadcast` subchart,
  rendering manifests
- Pre-install/pre-upgrade migration Job pulling the backplane image,
  running `alembic upgrade head` against PostgreSQL
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
- Cluster provisioning (`rke2-infra` already exists; bringing up a
  fresh cluster is out of scope for Goal #11)
- Vault / Keycloak / Postgres provisioning (consumer-side
  prerequisites — see
  [cross-repo coordination](../cross-repo/rke2-infra-coordination.md)
  Section 4)
- Operator-side device-code login (`meho login` is exercised by
  [acceptance Task #56](https://github.com/evoila-bosnia/meho-internal/issues/56),
  the federation-chain smoke, not this one)

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
covers cold image pulls on the rke2-infra nodes and a freshly-minted
TLS certificate (cert-manager's Let's Encrypt staging issuance is
sub-30s; the lab's internal CA via cert-manager is sub-5s). The
budget has headroom for one transient retry without bursting.

If the cold-deploy ever exceeds 5 minutes on rke2-infra without a
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
| 1 | The backplane Deployment selected by `app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release>` reports `Available=True` with `replicas == readyReplicas` | `kubectl rollout status deployment/$(kubectl get deployment -n <ns> -l app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release> -o jsonpath='{.items[0].metadata.name}') -n <ns> --timeout=5m` | Pod is up, readiness probe green; the kubelet has admitted the Pod to the Service endpoints. The label-selector form keeps the assertion release-name-agnostic — the chart's `meho.fullname` helper folds a `name`-prefixed release name (`meho`) but prepends release otherwise (`prod` → `prod-meho`) |
| 2 | Pre-install migration Job (selected by `app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release>`) exists and exited 0 | `kubectl get job -n <ns> -l app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release> -o jsonpath='{.items[0].status.succeeded}'` returns `1` | Schema is at `head`; the Deployment is rolling against a migrated DB. A failed Job leaves Helm at `pre-install` and the Deployment is never created — but the verifier still re-asserts to catch a subtle "succeeded but didn't actually migrate" case (e.g. stamped without running). Same release-name-agnostic discipline as row 1 |
| 3 | `GET https://<host>/healthz` returns 200 | `curl -sf --cacert <ca-bundle> https://<host>/healthz` | Liveness probe contract; the public ingress entry-point is reachable through TLS terminated at ingress-nginx and routed to the Service |
| 4 | `GET https://<host>/version` returns JSON containing the deployed `git_sha`, and that SHA matches the `--tag` value passed to `install.sh` | `curl -sf https://<host>/version | jq -e '.git_sha == "<expected>"'` | The image that's running is the one the operator asked for. Catches the `helm upgrade --install` no-op case (chart unchanged, image tag unchanged → no rollout, old version still serving) |
| 5 | `GET https://<host>/api/v1/health` (no `Authorization` header) returns 401 | `curl -so /dev/null -w '%{http_code}' https://<host>/api/v1/health` returns `401` | Negative auth test. A 200 here means the federation chain (Keycloak JWT validation middleware) regressed open — the Goal #11 "no anonymous access to authenticated surfaces" invariant. A non-200/non-401 means something else broke (502 → backplane down behind ingress; 503 → readiness flipped after probe) |
| 6 | The `audit_log` table exists in Postgres with the columns Task #29 stamped | A direct `psql` query is operator-cost; the verifier asserts the negative-auth path went through the audit middleware by checking the response carried a freshly-generated `X-Request-ID` header — surrogate for "the middleware chain ran" | Audit middleware reachability — the row write requires an authenticated request which lands in [Task #56](https://github.com/evoila-bosnia/meho-internal/issues/56). Surrogate keeps this verifier authentication-free |
| 7 | Wall-clock duration from `install.sh` start to verifier exit ≤ 300 seconds (5 minutes) | The verifier reads `INSTALL_START_TS` from env (set by the consumer's `install.sh` as the first action) and computes `now - INSTALL_START_TS` | The budget itself. Failing this assertion is a soft fail by default (warning) and a hard fail when the verifier is invoked with `--enforce-budget` |

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

The verifier exits non-zero on the first failed check and prints a
diagnostic line. Common failure modes the operator should expect:

| Failure | Surface | First debug step |
| --- | --- | --- |
| Migration Job stuck | Helm aborts at `pre-install` with `Job has reached the specified backoff limit`. Deployment never rolls. | `kubectl logs -n <ns> -l app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release>` — Alembic error rendered to stderr by the runner as `migration_failed: <ExcClass>: <msg>` |
| Image pull failure | Pod stays in `ImagePullBackOff` or `ErrImagePull`. `kubectl describe pod` shows the pull error. | Check the image tag matches a published GHCR tag; check `imagePullSecrets` (empty by default — anonymous GHCR pull is expected); check egress from `rke2-infra` to `ghcr.io` |
| Probes failing | Pod is `Running` but `Ready=False`. Service has zero endpoints. | `kubectl logs -n <ns> -l app.kubernetes.io/name=meho,app.kubernetes.io/instance=<release> --tail=50` — backplane startup error (most often: Vault unreachable, Keycloak JWKS unreachable, DATABASE_URL malformed) |
| Ingress 502 / 503 | `/healthz` from the operator workstation fails; from inside the cluster it succeeds. | `kubectl describe ingress -n <ns> meho` — check the TLS Secret exists (`cert-manager` Certificate `Ready=True`); from another Pod in the cluster `curl -k http://<svc>.<ns>.svc.cluster.local:8000/healthz` to isolate ingress vs. backplane |
| `/version` git_sha mismatch | Verifier check #4 fails. | The chart/image rolled but the *previous* Pod is still answering. `kubectl rollout status` for a tighter signal; `kubectl get pods -n <ns> -l app.kubernetes.io/name=meho -o yaml | grep image:` to see which image tag the live Pod is running |
| `/api/v1/health` returns 200 instead of 401 | Verifier check #5 fails. | Federation regression. The middleware chain in `backend/src/meho_backplane/app.py` is no longer enforcing the Keycloak JWT validation dependency. Roll back: `helm rollback meho <prev-revision> -n <ns>` |
| Budget exceeded | Verifier check #7 fails with warning (or hard-fail with `--enforce-budget`). | First inspect `helm history -n <ns> meho` for which revision is in flight, then the slowest leg via `kubectl get events -n <ns> --sort-by=.lastTimestamp` |

The verifier never hides a failure behind a green check. Each
assertion fails-loud with the exact mismatch (e.g. `expected
git_sha=abc1234, got def5678`).

## Who runs the test

| Role | Action | Cadence |
| --- | --- | --- |
| **RDC operator** | Runs `time bash install.sh --tag <fresh-sha>` against a clean `meho` namespace from the operator workstation (with VPN + Vault session). Captures stdout + stderr + `helm history` + `kubectl describe` + the verifier's output. | Per Goal #11 closing-criteria — once for the acceptance milestone; in addition, on every `repository_dispatch` deploy when the consumer chooses to re-baseline |
| **`evoila/meho` maintainer** | Reviews the captured run on issue #55 (or the linked workflow artefact), confirms the verifier's check #1..#7 are green and the wall-clock is ≤ 5 minutes, ticks the DoD bullet on Goal #11. | Once per Goal-closing review; subsequent runs on `repository_dispatch` are operator-attested |
| **CI (per-PR ephemeral smoke)** | Runs a **subset** of the verifier against the per-PR `meho-ci-<n>` namespace via [`scripts/ci/pr-smoke.sh`](../../scripts/ci/pr-smoke.sh). The PR-smoke is the unauthenticated subset (checks #3, #4 partial, #5); it does **not** assert the budget (PR builds are not Goal-DoD runs). | Every PR against `main` |

The PR-smoke and this verifier intentionally overlap on the
unauthenticated probes — the PR-smoke catches regressions before
they reach `main`; the cold-deploy verifier catches what survives
the smoke (chart-only changes, image-only changes, the cumulative
effect of merged PRs). Together they're the two sides of the
"every code path closes the real-target feedback loop" discipline
that Goal #11 makes non-negotiable.

## What the consumer-side `install.sh` needs to do

For the producer-side verifier to be invocable, the consumer's
`install.sh` MUST:

1. **Record the start timestamp** as the first action:

   ```bash
   START_TS=$(date -u +%s)
   export INSTALL_START_TS="$START_TS"
   ```

2. **Pin the image tag.** `install.sh --tag <git-sha>` resolves to
   `--set image.tag=<git-sha>` (or `--set
   image.tag=sha-<long-sha>` — both work). `:latest` and `:main` are
   forbidden per Goal #11 deploy discipline; the chart's
   `values.schema.json` rejects an empty `image.tag` so a forgotten
   `--set` fails-loud.

3. **Run `helm upgrade --install`** against the published chart at
   `oci://ghcr.io/evoila/meho-chart` with `values-rdc.yaml`, with
   `--wait --timeout 5m --create-namespace --namespace meho`.

4. **Invoke the verifier as the last step:**

   ```bash
   bash scripts/acceptance/install-verify.sh \
     --host meho.evba.lab \
     --expected-git-sha "$TAG" \
     --namespace meho
   ```

   (or fetch the script via `curl -sSf
   https://raw.githubusercontent.com/evoila/meho/main/scripts/acceptance/install-verify.sh
   | bash -s -- --host meho.evba.lab --expected-git-sha "$TAG"
   --namespace meho` if the consumer prefers not to pin a producer
   commit).

5. **Propagate the verifier's exit code** as `install.sh`'s exit
   code. Either the install passed wholesale (every probe green +
   budget met) or it did not.

A skeleton wrapper showing the contract is in
[`scripts/acceptance/install-verify.sh`](../../scripts/acceptance/install-verify.sh)'s
header comment. The consumer's runbook fleshes it out with the
pre-flight checks (VPN, Vault session) that are environment-specific.

## Acceptance criteria status

The full set of acceptance criteria on
[issue #55](https://github.com/evoila-bosnia/meho-internal/issues/55)
and where each lands:

| AC | Status at PR-time | Evidence path |
| --- | --- | --- |
| AC1 — Cold-install completes in <5 min wall clock | **deferred-to-consumer-side** | The verifier asserts the budget (check #7); the actual cold-deploy runs on the operator workstation against `meho.evba.lab` and is captured in the closing comment on the issue |
| AC2 — install.sh pre-flight checks pass (VPN, Vault session, kubectl context, GHCR egress) | **deferred-to-consumer-side** | Pre-flight is `install.sh`'s responsibility — see the consumer's `manifests/meho/install.sh`. The producer-side verifier doesn't re-check VPN / Vault session because that's already verified by the time the `helm upgrade` succeeded. The verifier *does* check GHCR egress implicitly via the chart pull |
| AC3 — `helm upgrade --install` exits 0; backplane Pod Ready | **verifier-asserted** (check #1) | `kubectl rollout status` in the verifier |
| AC4 — Migration Job succeeded; `audit_log` table exists | **verifier-asserted** (check #2 + check #6 surrogate) | `kubectl get job` in the verifier; the table presence is asserted indirectly through the audit middleware's `X-Request-ID` header on probe #5; direct `psql` check lands in Task #56's federation-chain smoke |
| AC5 — `curl -sf https://meho.evba.lab/healthz` 200; `/version` JSON carries deployed `git_sha` | **verifier-asserted** (checks #3 + #4) | curl in the verifier |
| AC6 — TLS works; certificate issued by internal CA via cert-manager | **verifier-asserted** (check #3 with `--cacert`) | The verifier's curl uses the operator-supplied `--cacert` (defaulting to system trust); failure here is either a missing/wrong CA bundle or a cert-manager reconcile that didn't finish in budget |
| AC7 — Test run captured (logs, transcript, timing) | **deferred-to-consumer-side** | The captured artefact lands in the closing comment on #55 |

The "deferred-to-consumer-side" status is **expected and correct** —
the producer-side worker for #55 cannot run the cold-deploy itself
(no VPN session, no Vault credentials, no kubectl context). The
producer ships the contract + the verifier; the consumer runs the
deploy + writes the closing artefact.

## References

- Parent Goal:
  [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
  (DoD bullet 1)
- Parent Initiative:
  [#54 — G2.8 Acceptance / dogfood proof](https://github.com/evoila-bosnia/meho-internal/issues/54)
- This task:
  [#55 — install.sh cold-deploy → working MEHO at meho.evba.lab in <5 min](https://github.com/evoila-bosnia/meho-internal/issues/55)
- Sibling acceptance tasks:
  [#56 — smoke.sh](https://github.com/evoila-bosnia/meho-internal/issues/56),
  [#57 — helm rollback](https://github.com/evoila-bosnia/meho-internal/issues/57),
  [#58 — 5-PR green counter](https://github.com/evoila-bosnia/meho-internal/issues/58)
- Producer artefacts the cold-deploy uses:
  - Image: `ghcr.io/evoila/meho:<tag>` ([chart `Chart.yaml`](../../deploy/charts/meho/Chart.yaml))
  - Chart: `oci://ghcr.io/evoila/meho-chart:<version>` ([publish workflow](../../.github/workflows/chart.yml))
  - Values overlay template: [`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml)
  - CLI: [`https://github.com/evoila/meho/releases`](https://github.com/evoila/meho/releases)
- Cross-repo handshake: [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
- PR-smoke (CI subset): [`scripts/ci/pr-smoke.sh`](../../scripts/ci/pr-smoke.sh)
- Deploy surface deep-dive: [`docs/codebase/devops.md`](../codebase/devops.md)
