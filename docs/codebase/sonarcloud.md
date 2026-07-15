<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# SonarCloud — how it's wired, how to read it, how to keep it honest

MEHO is analysed by [SonarCloud](https://sonarcloud.io/project/overview?id=evoila_meho)
(project `evoila_meho`, organisation `evoila`, **public** project — read APIs
need no token). This doc is the operator runbook: what the analysis covers, how
the new-code period and scan trigger are configured, and how to triage results
without drowning in false positives.

## How analysis runs

```
push to main ──► CI workflow ──► python-coverage job uploads python-coverage
                  │               artifact (backend/coverage.xml)   [push only]
                  │
                  └─ on success ──► Quality Gate workflow (workflow_run)
                                       ├─ downloads python-coverage into backend/
                                       └─ SonarSource/sonarqube-scan-action  ──► SonarCloud
```

The coverage artifact is produced **on push to main only**, by a dedicated
non-required `python-coverage` job — NOT on PRs, and NOT by the required unit
lane. PRs therefore see SonarCloud coverage update "one merge late" (the pre-#771
behaviour). Why: running the unit suite under coverage peaks the pytest tree at
~12.5–14 GiB (vs ~7.9 GiB no-cov), which OOM-killed the memory-limited
`meho-runners-ci-heavy` pod when coverage rode on the required lane (#1982). The
coverage tax is intrinsic to measuring the whole `meho_backplane` package across
the ~8.3k-test suite and barely moves with xdist worker count — see #1987 for the
peak-memory measurement table. The coverage job is **job-level
`continue-on-error: true`** so an OOM degrades to "no coverage for this push" and
never fails the CI run conclusion (which would also suppress this gate's
`workflow_run` trigger).

| Concern | Where |
|---|---|
| Project + scope config | [`sonar-project.properties`](../../sonar-project.properties) |
| Scan trigger + coverage handoff | [`.github/workflows/quality-gate.yml`](../../.github/workflows/quality-gate.yml) |
| Coverage production | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — `python-coverage` job (push to main only, offline `coverage combine`+`xml`) |
| Coverage config (CI offline-combine) | [`backend/.coveragerc.ci`](../../backend/.coveragerc.ci) — `parallel`, `source`, `relative_files` |
| Coverage path portability | `[tool.coverage.run] relative_files` ([`backend/pyproject.toml`](../../backend/pyproject.toml)) + the same key in `.coveragerc.ci` + quality-gate.yml's `Point coverage report at the backend/ source root` step (injects `<source>backend</source>` so paths resolve from the repo root) |
| Auth | `SONAR_TOKEN` repo secret (write/scan only) |

Coverage is the **unit sweep only** (`--cov`-equivalent over `meho_backplane`);
integration tests run in a separate job without coverage. The gate is
`continue-on-error: true` — it is **advisory** and never blocks merges (see the
new-code-period + scan-trigger section below for why it is kept advisory).

## When coverage collapses to ~24% — the invisible-import failure mode

**Symptom.** The SonarCloud overall-coverage chip drops sharply (e.g. from ~66%
to ~24%) and stays there, with no matching drop in real test coverage.

**What it means.** The push-only `python-coverage` job (ci.yml) died before
uploading `backend/coverage.xml`, so the Quality Gate scan ran *without* backend
coverage and SonarCloud imported ~0% for the backend tree — dragging the blended
Python+Go number down to roughly what Go coverage alone contributes. Because both
the coverage job and the artifact download are non-blocking
(`continue-on-error`), the failure produces no red check anywhere; the collapsed
chip is the only symptom.

**Worked example — the 2026-06-20 incident.** Last good import:
2026-06-19T18:55Z at **65.8%**. First collapsed import: 2026-06-20T10:46Z at
**24.9%** — and it stayed collapsed for 3+ weeks. Root cause: the
`python-coverage` job was OOM-killed / evicted (runner lost, null step
conclusions) at ~47 min on the memory-limited `meho-runners-ci-heavy` pool —
*before* its 50-min timeout, so raising `timeout-minutes` is a no-op. The unit
lane had already dropped `--cov` for the same OOM reason (#1982), so this
dedicated job was the sole coverage producer and nothing else caught the gap.

**Where to look.**

1. The most recent `python-coverage` job on a main push (CI workflow) — did it
   reach `Upload Python coverage artifact`, or die/OOM before it?
2. The Quality Gate run for that push — its `Assert python-coverage artifact
   present (fail-visible)` step emits an **error annotation** naming the missing
   artifact whenever the upload is absent (added in #2513). That annotation is
   the loud signal the 2026-06-20 collapse lacked.
3. The artifact list on the CI run — a present `python-coverage` artifact rules
   out this failure mode.

**The durable fix is ops-owned.** The coverage tax is intrinsic (~12.5 GiB peak
at `-n 1`, only ~1.2 GiB under the pod limit) and barely moves with parallelism,
so the job is one suite-growth increment away from OOM again. The durable fix —
named in both the job's own header comment and its `# TODO(ops)` at `runs-on:` —
is a **larger-memory runner pool** (a gha-runner-scale-set on the rke2-ci
cluster, provisioned out of band); once it exists, move the job's `runs-on:` to
it. Until then the job stays non-blocking and the fail-visible annotation above
surfaces every skipped upload instead of letting it rot silently.

## New-code period + scan trigger — how they're configured

SonarCloud's "Clean as You Code" model gates only **changed** code, relative to a
configurable *new-code period*. Two decisions shape what the dashboard shows:

**New-code period = `previous_version` (SonarCloud default — kept deliberately).**
The tempting alternative — New Code = "Reference branch: `main`" — was considered
and rejected: because this project analyzes `main` (see the trigger below), pinning
the reference branch to `main` would diff `main` against **itself**, so main's
new-code window would be permanently ~empty and the CaYC signal would go dark on
the one branch that is actually analyzed. `previous_version` instead resets the
new-code window at each `sonar.projectVersion` bump (the scan stamps it with the
analyzed commit SHA — see `quality-gate.yml`'s `SonarCloud Scan` step), which keeps
"new code" meaningful across merges. The gate is left **advisory**
(`continue-on-error: true`) regardless.

> Historical note: the orphan-commit history reset once left the baseline
> predating every commit, so `new_lines` (~296k) exceeded total `ncloc` (~187k)
> and every `new_*` metric equalled its all-time twin. That artefact clears as
> `previous_version` windows accrue from real merges; no manual reference-branch
> setting is applied.

**Scan trigger = main pushes only.** `quality-gate.yml`'s job carries
`if: … && github.event.workflow_run.event == 'push'`. CI runs on both `push` and
`pull_request`, and SonarCloud stores a single analysis per project keyed by
branch — so before the filter, every PR-branch CI completion triggered a scan that
**overwrote** the main-branch analysis, churning the activity history and
misaligning the new-code window with real merges. With the filter, only `main`
pushes are analyzed; PR completions are skipped (the job shows as skipped in the
Actions run). PR-level decoration is therefore not produced — an accepted
trade-off while the gate is advisory.

## Reading the dashboard without drowning

The single most useful triage axis is **test code vs. production code**. After the
`sonar.tests` split in `sonar-project.properties`, test fixtures no longer inflate
the ratings, but when reading raw issues remember:

- **Bugs**: historically *all* were in `backend/tests/**` (float `==` asserts).
  Production bug count was zero. Reliability E was 100% test noise.
- **Vulnerabilities**: ~87% were test fixtures (fake tokens, `/tmp`). The handful
  in production are reviewed below.
- **Maintainability** is genuinely **A** (debt ratio ~0.1%). Trust it.
- **Duplication** is concentrated in the Go per-vendor CLI command files
  (`cli/internal/cmd/<vendor>/<vendor>.go`, 44–74% each) — real, structural,
  tracked as the dispatch-boilerplate refactor.

The only smell bucket worth actively chasing is **`S3776` cognitive complexity**
(~45 functions, Python + Go); the high-volume buckets (`S7503` async-without-await,
`S1313` hard-coded IPs, `S8410` type-hints) are mostly intentional or test-only.

## Security hotspots — accepted false positives

28 hotspots; 25 are `S5332` clear-text-HTTP in **test** fixtures (bulk-mark *Safe*).
The three that touch production are all reviewed and **accepted**:

| Finding | Verdict |
|---|---|
| `go:S4036` PATH-exec of `gh` — `cli/internal/cmd/retrieval/retire_checklist.go` | **Safe.** Bounded-context `gh` invocation, already `//nolint:gosec`. You can't hardcode the path to `gh`. |
| `python:S5852` ReDoS — `backend/src/meho_backplane/operations/ingest/_llm_grouping_internals.py` | **Low risk.** ```` ```json ```` fence-stripper over bounded LLM output, not adversarial input. |
| `docker:S6504` group-write — `backend/Dockerfile` (`chmod -R g=u /opt/meho`) | **Safe.** Deliberate OpenShift / arbitrary-UID pattern: the container runs as any UID in group 0, and fastembed must *write* the model cache at runtime. Required, not gratuitous. |

Other accepted issue-level false positives: `go:S4830`/`S5527` on
`cli/internal/cmd/admin/keycloak/tls.go` (flag-gated `InsecureSkipVerify`, already
`//nolint:gosec`); `go:S2068` on the `keycloak.go` `--help` example string;
`python:S5443` on `bind9/_atomic.py` (CSPRNG-random `/tmp` name, runs as a remote
shell script). Mark these *Accepted* / *Won't fix* in SonarCloud with the rationale
above so the ratings reflect real risk.

The 5 production CRITICAL TLS findings are **by-design and suppressed in code**
via rule-scoped `# NOSONAR(Sxxxx)` comments (rule-scoped so only the named rule is
muted, not every issue on the line), each pointing at the module docstring that
carries the justification:

| Line | Rule | Why by-design |
|---|---|---|
| `connectors/net/tls.py:439` | S4423 | probe negotiates whatever the appliance offers; protocol version is *reported* output |
| `connectors/net/tls.py:440` | S4830 | inspection-only handshake; verification-off is the operation's purpose |
| `connectors/net/tls.py:441` | S5527 | hostname match is computed and *reported* by the handler, not enforced |
| `connectors/adapters/http.py:125` | S5527 | per-target `verify_tls=false` opt-out (default `True`); WARN-logged + audited; `tls_ca_pin` is the secure supersession |
| `connectors/adapters/http.py:127` | S4830 | same justification; see the module docstring's TLS-trust precedence |

These are deliberately **not** muted project-wide (no source-tree multicriteria on
the TLS rules) so future genuinely-insecure TLS code still gets flagged.

## Real backlog (not noise)

- **Go CLI dispatch boilerplate** — extract a shared `CallOperation` helper so the
  ~18 per-vendor command files stop re-implementing the HTTP-to-`/operations/call`
  plumbing. Compatible with CLAUDE.md postulate 5 (transport DRYing ≠ thin wrapper).
- **K8s resource requests/limits** (`kubernetes:S6870`) on the chart deployments +
  migration job.
- **Dockerfile hardening** (`docker:S8541`).
- *(optional)* **`S3776` cognitive-complexity** pass on the worst offenders.
