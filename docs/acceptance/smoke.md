<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `smoke.sh` federation chain — acceptance contract

> The producer-side specification of Goal #11 DoD bullet 2:
>
> > `smoke.sh` passes (login + status + audit-row + Vault +
> > DB-migration state) — the full federation chain works for a real
> > operator with a real Keycloak token against the deployed
> > backplane.
>
> This document codifies **what "passing" looks like** for the
> federation-chain end-to-end smoke. The actual smoke run executes on
> the consumer side
> ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc));
> the producer (this repo) owns the acceptance bar + the verifier the
> consumer's wrapper invokes as its last step.

## Tracking issue

This contract closes
[`evoila-bosnia/meho-internal#56`](https://github.com/evoila-bosnia/meho-internal/issues/56)
(parent Initiative
[#54](https://github.com/evoila-bosnia/meho-internal/issues/54),
parent Goal
[#11](https://github.com/evoila-bosnia/meho-internal/issues/11)).

## Why this lives in `evoila/meho`

The CLI (`meho login`, `meho status`), the authenticated
`/api/v1/health` endpoint, the audit middleware, and the DB-migration
runner are all produced here. When any of those surfaces changes the
shape of "federation-chain proof", this document — and the verifier
it points at — change in lock-step, without the consumer's `smoke.sh`
wrapper needing to know. Same producer-owns-the-contract /
consumer-owns-the-wrapper split as
[`install.md`](./install.md) and [`rollback.md`](./rollback.md).

## What the five legs prove

The Goal #11 DoD bullet 2 phrasing — "login + status + audit-row +
Vault + DB-migration state" — maps to five discrete legs the verifier
asserts in order. Each leg corresponds to a load-bearing Initiative
the federation chain depends on:

| # | Leg | What it proves end-to-end | Predecessor task |
| --- | --- | --- | --- |
| 1 | **login** | `meho login <backplane>` (device-code flow) succeeds; the resulting access token is persisted in the OS keyring (or a 0600-mode file on headless hosts) and is usable by subsequent CLI calls. Verifies the Keycloak → CLI → token-store chain. | [`#44`](https://github.com/evoila-bosnia/meho-internal/issues/44) — `meho login` (G2.6-T2) / [`#42`](https://github.com/evoila-bosnia/meho-internal/issues/42) — CLI binary (G2.6) |
| 2 | **status** | `meho status --json` (or the equivalent `curl -H "Authorization: Bearer ..." /api/v1/health`) returns 200 and the JSON response carries `operator.sub`, `vault.reachable=true`, `vault.read_ok=true`, `db.migrated=true`. Verifies the Status API + JWT middleware + the federation summary surface. | [`#45`](https://github.com/evoila-bosnia/meho-internal/issues/45) — `meho status` (G2.6-T3) |
| 3 | **audit-row** | An authenticated `GET /api/v1/health` writes a synchronous row to `audit_log` (operator_sub, method=GET, path=/api/v1/health, status_code=200) before the response returns. Verifies the audit middleware's "row-before-response" contract end-to-end. | [`#28`](https://github.com/evoila-bosnia/meho-internal/issues/28) — audit middleware (G2.3-T2) |
| 4 | **Vault** | The same `/api/v1/health` call exercises a Vault JWT/OIDC round-trip — the backplane forwards the operator's JWT to Vault, Vault verifies against the configured Keycloak trust, returns a Vault token bound to the operator, and the backplane reads `secret/meho/test/federation` under that token. Verifies the federation chain. | [`#25`](https://github.com/evoila-bosnia/meho-internal/issues/25) — Vault JWT federation (G2.2) |
| 5 | **DB-migration state** | The chassis's `db_migration_probe()` reports `alembic_version == head`. Optional cluster-side cross-check: `kubectl get job -l app.kubernetes.io/component=migrate,app.kubernetes.io/instance=<release>` shows `succeeded=1`, OR the helm release status is `deployed` (when the Job was GC'd by the hook-succeeded delete-policy). Verifies the migration-runner contract. | [`#29`](https://github.com/evoila-bosnia/meho-internal/issues/29) — migration runner + CI guard (G2.3-T3) |

**Role requirement.** `GET /api/v1/health` is gated at
`TenantRole.OPERATOR` (least-privilege hardening: every call
federates a live per-operator Vault credential). The smoke operator's
token must carry `tenant_role` of `operator` or `tenant_admin`; a
`read_only` token receives 403 `insufficient_role` and the smoke
fails at leg #2. Low-privilege monitoring principals that only need a
process/DB liveness signal poll `GET /api/v1/health/live` instead
(valid JWT, any role — no Vault interaction, no `vault.*` fields in
the response).

The five legs share **one** authenticated `/api/v1/health` request —
legs #2, #3, #4, and #5 all parse fields off the response of that
single request. This is deliberate: each smoke run writes exactly one
audit row, keeping the closing-comment artefact small and the
audit-log signal-to-noise ratio high. The cluster-side cross-check
in leg #5 is the only leg that makes a second probe (a kubectl /
helm call), and that probe is read-only.

**Note on script-side leg ordering.** The numbered legs above are the
*contract* order — the narrative order an operator reads. The
verifier (`scripts/acceptance/smoke.sh`) asserts them in a different
order for fail-fast reasons: **#2 status → #4 Vault → #3 audit-row →
#5 db-migration**. Leg #4 is asserted before leg #3 because the
audit-row check needs the `operator_sub` from leg #2's response AND
because a broken Vault chain often correlates with broken audit
writes; failing fast on Vault avoids a flood of misleading audit-row
`[FAIL]` lines that all point at the same root cause. The pass/fail
verdict is identical either way — only the order of the lines in the
verifier's output changes.

## What "passing" means

The deployed system has **passed** the smoke when every required leg
above passes. The producer-side verifier
[`scripts/acceptance/smoke.sh`](../../scripts/acceptance/smoke.sh)
encodes every one of them as a discrete check.

### Required (verifier-asserted) — runs every smoke run

| # | Assertion | How it's checked | Why |
| --- | --- | --- | --- |
| 1 | A bearer token is available to the verifier — either via `MEHO_ACCESS_TOKEN` env var (preferred; exported by the consumer's wrapper after `meho login`) or via the `meho` CLI's keyring lookup | The verifier asserts the precondition; the actual proof of "login produced a working token" is leg #2 (a stored token that doesn't authenticate is a failed login, not a passed one) | The verifier is non-interactive (no TTY). The interactive `meho login` runs in the consumer's wrapper BEFORE the verifier; the verifier just confirms the wrapper produced a usable token |
| 2 | `GET https://<backplane>/api/v1/health` with `Authorization: Bearer <token>` returns 200 AND the response JSON contains a non-empty `.operator.sub` | `curl -sSf` + `jq -e '.operator.sub != null and .operator.sub != ""'` | Federation chain works for at least one operator — JWKS cached, signature verified, audience matched, Vault OIDC role exchange succeeded, operator identity bound into the response body |
| 3 | The authenticated probe writes an `audit_log` row matching the operator's `sub`, `method='GET'`, `path='/api/v1/health'`, `status_code=200`, `occurred_at` within the last minute | `psql "$DATABASE_URL"` against the live DB with a parameterised query; **deferred to operator-side** when `DATABASE_URL` is unset | Audit middleware is synchronous (row written before response yielded). Verifies the v0.1 audit-on-every-authenticated-call contract end-to-end. Scoping by `operator_sub` + `path` + recent timestamp avoids false positives from concurrent operator activity on the same backplane |
| 4 | Same `/api/v1/health` response carries `.vault.reachable=true` AND `.vault.read_ok=true` | `jq -r '.vault.reachable'` + `jq -r '.vault.read_ok'` on the leg #2 response | The federation chain to Vault (JWT/OIDC login + `secret/meho/test/federation` KV read) succeeded. `.vault.detail` carries the KV version on success, the exception class name on failure — never a free-form message (per the v0.2 sensitive-data discipline in `backend/src/meho_backplane/api/v1/health.py`) |
| 5 | Same `/api/v1/health` response carries `.db.migrated=true` | `jq -r '.db.migrated'` on the leg #2 response | The chassis's `db_migration_probe()` reports `alembic_version == head` (per `backend/src/meho_backplane/db/migrations.py`). Verifies the migration-runner contract: the backplane is talking to a DB whose schema matches the deployed image's expected revision |

### Optional (verifier-asserted when context is available)

| # | Assertion | When it runs | Why |
| --- | --- | --- | --- |
| 5b | The migrate Job's status is `succeeded=1`, OR (when the Job has been GC'd by the hook-succeeded delete-policy) the helm release status is `deployed` | When `kubectl` (and optionally `helm`) is on the verifier's PATH. Skipped on operator workstations that probe the public ingress without cluster access | Cross-checks the chassis-side `.db.migrated` flag against the cluster-side migration-Job state. Catches a subtle case where `db_migration_probe()` lies — e.g. the probe reports `migrated=true` because the schema happens to be at head, but the migration Job for the *current* upgrade never ran (someone stamped manually) |
| 6 | Wall-clock duration from wrapper start to verifier exit ≤ 60 seconds | When `SMOKE_START_TS` is set (consumer's wrapper records it as the first action) | Federation chain on a warm cluster is sub-second per leg; bursting past 60s indicates a Vault / Keycloak / PG latency regression worth investigating even when the legs themselves pass. Under `--enforce-budget` this is a hard failure; without it, a warning |

### Deferred-to-operator-side — leg #3 fallback

Leg #3 requires `psql` access to the live database. When the verifier
runs from a host that does not have `DATABASE_URL` set (operator
workstation without DB credentials), leg #3 becomes a `[note]` line
emitting the exact `psql` command the operator should run, and the
result is recorded in the closing-comment artefact on issue #56
instead of in the verifier's exit code. Same posture as
`install-verify.sh`'s authenticated audit-row check (#9) and
`rollback-verify.sh`'s schema check (#2).

## What "60s" means

The 60-second budget is **wall-clock for the verifier itself** — the
five legs against a warm, deployed backplane. Stopwatch semantics:

| Boundary | Event |
| --- | --- |
| **Start** | The consumer's `smoke.sh` wrapper's first instruction. Implementation: `START_TS=$(date -u +%s); export SMOKE_START_TS="$START_TS"` on line 1 (after `set -euo pipefail`). |
| **Stop** | The verifier prints `[ok] federation chain verified end-to-end` and exits 0. |

The budget covers **everything in between**:

- Token retrieval (either CLI keyring lookup or `MEHO_ACCESS_TOKEN`
  env var read)
- One authenticated `GET /api/v1/health` round-trip (legs #2 + #4 +
  #5 parse this single response)
- Optional `psql` round-trip for leg #3 (the actual SELECT against
  `audit_log` is sub-millisecond against a warm PG; the latency floor
  is the TCP+TLS+auth handshake)
- Optional `kubectl` + `helm` round-trips for leg #5b

The budget does **not** cover:

- Interactive `meho login` (device-code flow) — that runs in the
  consumer's wrapper before the verifier is invoked. Operator
  approval in the browser is unbounded.
- Initial chart install / upgrade (covered by Task #55's
  install-verify.sh's 5-minute budget)

### Why 60 seconds is the right bar

The federation chain on a warm cluster is sub-second per leg:

- `curl https://<backplane>/api/v1/health` over the lab's ingress
  controller: ~50ms TLS + ~20-200ms backplane response (JWKS cached,
  Vault token cached or re-issued)
- `psql` against the namespace-scoped PG: ~10ms connect + ~5ms
  parameterised SELECT
- `kubectl get job`: ~50ms against the API server

A budget of 60s allows for one transient retry on each leg without
bursting. If the smoke ever exceeds 60s on a warm cluster without a
known correlate (Vault unsealed during the run, Keycloak realm
reload, PG vacuum-induced lock contention), the regression is in the
federation chain itself, **not** the budget. The fix lands in the
chart / image / Vault role config — not in this document.

## What failure looks like

The verifier exits non-zero on the first failed required leg and
prints a diagnostic line. Common failure modes:

| Failure | Surface | First debug step |
| --- | --- | --- |
| Leg #1 fails — no token and no CLI | `[FAIL] login: no MEHO_ACCESS_TOKEN and meho CLI not on PATH` | The consumer's wrapper didn't run `meho login` or didn't export `MEHO_ACCESS_TOKEN`. Rerun the wrapper, OR run `meho login <backplane>` manually in the same shell |
| Leg #2 fails — 401 | `[FAIL] status: GET .../api/v1/health failed` | Token is expired, the audience in the JWT doesn't match the backplane's configured audience, or the JWKS cache went stale and rejected a valid signature. Rerun `meho login <backplane>` to get a fresh token; if that still 401s, inspect `kubectl logs -n <ns> deployment/<release>` for JWT-validation errors |
| Leg #2 fails — `operator.sub` missing | `[FAIL] status: response missing operator.sub` | The JWT validated but `verify_jwt_and_bind` didn't bind the sub into structlog — middleware regression. Inspect the route handler at `backend/src/meho_backplane/api/v1/health.py` |
| Leg #3 fails — psql returns 0 rows | `[FAIL] audit-row: psql returned 0 rows` | Audit middleware did not write a row, OR the row didn't match the WHERE clause. Most often: path normalisation diverged (trailing slash, query string), the audit middleware's contextvar binding broke (operator_sub null in the row), or the request flow short-circuited before the middleware ran (e.g. a 401 from JWT validation — but then leg #2 would have failed first) |
| Leg #4 fails — `vault.reachable=false` | `[FAIL] vault: not reachable from backplane` | Vault role JWKS URL drift (Keycloak realm renamed?), audience mismatch on the Vault role, network policy blocks egress to Vault, or Vault is sealed. `detail` field carries the exception class name |
| Leg #4 fails — `vault.read_ok=false` | `[FAIL] vault: reachable but read failed` | JWT/OIDC login to Vault succeeded but the subsequent secret read failed. Either the operator's Vault policy doesn't permit reads on `secret/meho/test/federation`, OR the KV mount / path doesn't exist (consumer-side provisioning gap) |
| Leg #5 fails — `db.migrated=false` | `[FAIL] db-migration: chassis reports .db.migrated=false` | DB unreachable from backplane, `alembic_version` table missing, or `alembic_version.version_num != head`. Inspect `kubectl logs -n <ns> deployment/<release>` for connection errors, or run `kubectl exec -n <ns> deployment/<release> -- python -m meho_backplane.db.migrations --check` (when the migration runner contract from Task #29 ships this entry-point) |
| Budget exceeded under `--enforce-budget` | `[FAIL] wall-clock Xs > budget 60s` | Inspect `kubectl get events -n <ns> --sort-by=.lastTimestamp` for slow legs. Most often: a Vault re-seal during the smoke window, a PG vacuum causing lock contention, or a Keycloak realm export running concurrently |

The verifier never hides a failure behind a green check. Each
assertion fails-loud with the exact mismatch (e.g. `expected
operator.sub, got null`).

## Who runs the test

| Role | Action | Cadence |
| --- | --- | --- |
| **RDC operator** | Runs the consumer's `smoke.sh` wrapper from the operator workstation (with VPN + Vault session + a completed `meho login` in the same shell). Captures stdout + stderr + the optional `psql` output (when run from a host with DB access). | Per Goal #11 closing-criteria — once for the acceptance milestone; on every cold-deploy from then on |
| **`evoila/meho` maintainer** | Reviews the captured run on issue #56 (or the linked artefact), confirms legs #1-#5 are green and (when the operator has DB access) the audit-row was found, ticks the DoD bullet on Goal #11 | Once per Goal-closing review |
| **CI (per-PR ephemeral smoke)** | Runs the unauthenticated subset of `install-verify.sh` against the per-PR `meho-ci-<n>` namespace via [`scripts/ci/pr-smoke.sh`](../../scripts/ci/pr-smoke.sh). The PR smoke does **not** run `smoke.sh` — it has no Keycloak token (the device-code flow needs a real browser). The federation chain smoke is a Goal-closing exercise, not a per-PR gate. | Federation-chain smoke does NOT run per-PR; per-PR smoke covers the unauthenticated surface only |

The per-PR smoke and the closing-criteria federation smoke intentionally
do not overlap: the per-PR smoke catches regressions in the
unauthenticated surface fast; this contract closes the federation
chain's behaviour against a real operator + real Vault + real PG at
the closing milestone. Together they're the two sides of the
"every code path closes the real-target feedback loop" discipline
Goal #11 makes non-negotiable.

## What the consumer-side `smoke.sh` wrapper needs to do

For the producer-side verifier to be invocable end-to-end, the
consumer's wrapper (lives at
`evoila-bosnia/claude-rdc-hetzner-dc/manifests/meho/smoke.sh` per
Goal #11 cross-repo deps) MUST:

1. **Record the start timestamp** as the first action:

   ```bash
   START_TS=$(date -u +%s)
   export SMOKE_START_TS="$START_TS"
   ```

2. **Run `meho login` interactively**:

   ```bash
   meho login https://meho.evba.lab
   ```

   The operator completes the device-code flow in their browser. On
   success the CLI persists the token to the keyring (or 0600 file).

3. **Export the access token** for the verifier:

   ```bash
   export MEHO_ACCESS_TOKEN="$(meho token)"
   ```

   When `meho token` is not yet implemented (v0.2 surface), the
   wrapper falls back to extracting the token from the CLI's
   credentials file or asking the operator to paste it.

4. **Invoke the verifier** as the last step:

   ```bash
   export DATABASE_URL='postgresql://meho:...@.../meho'   # optional
   bash scripts/acceptance/smoke.sh \
     --backplane https://meho.evba.lab \
     --namespace meho \
     --release meho \
     --enforce-budget
   ```

   (or fetch the verifier via `curl -sSf
   https://raw.githubusercontent.com/evoila/meho/main/scripts/acceptance/smoke.sh`
   if the consumer prefers not to pin a producer commit).

5. **Propagate the verifier's exit code** as the wrapper's exit code.
   Either the smoke passed wholesale (every required leg green +
   budget met) or it did not.

The verifier's `--skip-login` flag exists for the case where the
operator has already completed `meho login` in a prior shell — the
verifier then trusts the keyring-stored token without re-running
the interactive flow.

## Sensitive-data discipline (Goal #11 / Task #25)

The verifier holds itself to the same JWT-handling discipline the
backplane enforces:

- **Bearer tokens are never echoed.** `curl`'s
  `-H "Authorization: Bearer ..."` is constructed inline; the token
  never lands in any log line, never in `set -x` output (the verifier
  doesn't enable `set -x`), and never in `ps`-visible argv (the
  header travels as a `curl` argument, but the verifier's banner
  prints the host + namespace + release only, never the headers).
- **Operator `sub` is echoed.** The audit-row check needs `sub` for
  the WHERE clause and the closing-comment artefact records it as
  proof of which operator wrote the row. `sub` is a Keycloak-issued
  UUID — not PII in the v0.1 model. `name` and `email` are
  deliberately excluded from any verifier output.
- **`vault.detail`** carries only structured strings the route
  handler constructs (`read_failed: KeyError`, `login_failed:
  VaultClientError`) — never operator-controllable URL substrings
  or exception messages. Same shape per
  `backend/src/meho_backplane/api/v1/health.py`.
- **`psql` query parameters** are passed via `-v sub="$operator_sub"`
  + `:'sub'`, which inlines the value as a single-quoted SQL literal.
  Even though `sub` is a Keycloak UUID (no quotes possible), the
  parameterisation is defensive — a future change of `operator_sub`
  shape can't open a SQL injection path through this verifier.

The verifier's stdout / stderr is safe to capture verbatim into the
closing-comment artefact on issue #56.

## Acceptance-criteria status

The full set of acceptance criteria on
[issue #56](https://github.com/evoila-bosnia/meho-internal/issues/56)
and where each lands:

| AC | Status at PR-time | Evidence path |
| --- | --- | --- |
| AC1 — `meho login https://meho.evba.lab` succeeds; token stored; `meho status` no longer 401s | **deferred-to-consumer-side** | Interactive login requires a real Keycloak realm and a real browser. The producer-side verifier confirms a usable token is available (leg #1) and proves it works (leg #2) but cannot drive the device-code flow itself. Consumer's wrapper drives the login; verifier consumes the resulting token |
| AC2 — `meho status --json` returns JSON with `.operator.sub`, `.operator.email`, `.vault.reachable=true`, `.vault.read_ok=true`, `.db.migrated=true` | **verifier-asserted** (leg #2 + leg #4 + leg #5) | `jq -e` on each field of the `/api/v1/health` response. `.operator.email` is NOT asserted by the verifier (email is PII the verifier deliberately doesn't echo); the consumer's wrapper can run `meho status --json \| jq .operator.email` separately if needed |
| AC3 — PG audit-row check: query against the lab's PG returns a row matching the `meho status` call | **verifier-asserted** (leg #3, when `DATABASE_URL` set) / **deferred-to-operator-side** (otherwise) | `psql` with the parameterised query above. When `DATABASE_URL` is unset on the verifier host, the verifier emits the exact `psql` command for the operator to run, and the operator pastes the output into the closing comment |
| AC4 — Vault audit log shows successful OIDC login + secret read attributed to the operator's identity | **deferred-to-consumer-side** | The producer-side verifier asserts the federation chain WORKED (leg #4) but does not have access to Vault's own audit log (consumer-side Vault deployment; the operator inspects Vault's audit log per the consumer's runbook). Recorded in the closing-comment artefact on #56 |
| AC5 — Test run captured with timestamps, command transcripts, and assertion results | **deferred-to-consumer-side** | The captured artefact lands in the closing comment on #56. The verifier's `[ok]` / `[FAIL]` / `[note]` lines are stable and grep-able for inclusion |
| AC6 — No JWT bytes appear in any captured output (sensitive-data discipline) | **verifier-asserted (by construction)** | The verifier never echoes the token — see "Sensitive-data discipline" above. The captured artefact from the verifier's stdout/stderr is safe to publish verbatim |

The "deferred-to-consumer-side" status is **expected and correct** —
the producer-side worker for #56 cannot drive the interactive
`meho login` (no real Keycloak realm, no operator browser), cannot
inspect Vault's own audit log (consumer-side deployment), and cannot
run the smoke against the live lab (no VPN, no kubeconfig, no DB
credentials). The producer ships the contract + the verifier + the
acceptance bar; the consumer runs the smoke + writes the closing
artefact.

## Static checks the producer side CAN run at PR time

Even though the deployed-system smoke is deferred to the
consumer-side runbook, the producer side asserts at PR time that the
verifier itself is well-formed:

| Check | How | What it proves |
| --- | --- | --- |
| Bash syntax | `bash -n scripts/acceptance/smoke.sh` | The verifier parses without errors |
| `--help` exits 0 | `bash scripts/acceptance/smoke.sh --help` | The self-contained help text renders without breaking on the heredoc |
| Unknown-flag exit 2 | `bash scripts/acceptance/smoke.sh --bogus` returns exit code 2 | The CLI-usage-error path works (matches `install-verify.sh` / `rollback-verify.sh`) |
| Missing-token failure mode | `bash scripts/acceptance/smoke.sh --enforce-budget` (with no token, no DB, no cluster) exits 1 with `[FAIL] login: ...` | Fail-loud on the precondition; budget hard-fail on missing `SMOKE_START_TS` |

These checks run inside the PR's CI on the linter / shell-static-check
job (`scripts/ci/check-scripts.sh` or the equivalent — TBD as part of
Task #29's CI guard expansion). They do **not** prove the smoke
passes against the live deployment; they prove the verifier won't
explode under the operator's hand.

## References

- Parent Goal:
  [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
  (DoD bullet 2)
- Parent Initiative:
  [#54 — G2.8 Acceptance / dogfood proof](https://github.com/evoila-bosnia/meho-internal/issues/54)
- This task:
  [#56 — `smoke.sh` passes (login + status + audit-row + Vault + DB-migration state)](https://github.com/evoila-bosnia/meho-internal/issues/56)
- Predecessors:
  - [Task #42](https://github.com/evoila-bosnia/meho-internal/issues/42) — CLI binary (G2.6)
  - [Task #44](https://github.com/evoila-bosnia/meho-internal/issues/44) — `meho login` device-code flow (G2.6-T2)
  - [Task #45](https://github.com/evoila-bosnia/meho-internal/issues/45) — `meho status` (G2.6-T3)
  - [Task #25](https://github.com/evoila-bosnia/meho-internal/issues/25) — Vault JWT federation (G2.2)
  - [Task #28](https://github.com/evoila-bosnia/meho-internal/issues/28) — audit middleware (G2.3-T2)
  - [Task #29](https://github.com/evoila-bosnia/meho-internal/issues/29) — migration runner entrypoint (G2.3-T3)
  - [Task #55](https://github.com/evoila-bosnia/meho-internal/issues/55) (closed) — `install.sh` cold-deploy contract
- Sibling acceptance tasks:
  [#55](https://github.com/evoila-bosnia/meho-internal/issues/55) (closed),
  [#57](https://github.com/evoila-bosnia/meho-internal/issues/57) (closed),
  [#58](https://github.com/evoila-bosnia/meho-internal/issues/58)
- Producer artefacts the smoke uses:
  - CLI: [`https://github.com/evoila/meho/releases`](https://github.com/evoila/meho/releases) (`meho login`, `meho status`)
  - Authenticated health: [`backend/src/meho_backplane/api/v1/health.py`](../../backend/src/meho_backplane/api/v1/health.py)
  - Audit middleware: [`backend/src/meho_backplane/audit.py`](../../backend/src/meho_backplane/audit.py)
  - DB-migration probe: [`backend/src/meho_backplane/db/migrations.py`](../../backend/src/meho_backplane/db/migrations.py)
- Cross-repo handshake: [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
- Deploy surface deep-dive: [`docs/codebase/devops.md`](../codebase/devops.md)
