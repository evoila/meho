<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# SonarCloud — how it's wired, how to read it, how to keep it honest

MEHO is analysed by [SonarCloud](https://sonarcloud.io/project/overview?id=evoila_meho)
(project `evoila_meho`, organisation `evoila`, **public** project — read APIs
need no token). This doc is the operator runbook: what the analysis covers, the
one project setting that currently makes the dashboard misleading, and how to
triage results without drowning in false positives.

## How analysis runs

```
push / PR ──► CI workflow ──► uploads python-coverage artifact (backend/coverage.xml)
                  │
                  └─ on success ──► Quality Gate workflow (workflow_run)
                                       ├─ downloads python-coverage into backend/
                                       └─ SonarSource/sonarqube-scan-action  ──► SonarCloud
```

| Concern | Where |
|---|---|
| Project + scope config | [`sonar-project.properties`](../../sonar-project.properties) |
| Scan trigger + coverage handoff | [`.github/workflows/quality-gate.yml`](../../.github/workflows/quality-gate.yml) |
| Coverage production | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — `Pytest (unit + coverage)` |
| Coverage path portability | `[tool.coverage.run] relative_files` in [`backend/pyproject.toml`](../../backend/pyproject.toml) |
| Auth | `SONAR_TOKEN` repo secret (write/scan only) |

Coverage is the **unit sweep only** (`--cov=meho_backplane`); integration tests
run in a separate job without `--cov`. The gate is `continue-on-error: true` — it
is **advisory**, never blocks merges, until the baseline below is fixed.

## ⚠️ The new-code baseline is broken — fix it once

SonarCloud's "Clean as You Code" model is supposed to gate only **changed** code.
Right now it gates the **entire 186k-line repo** because the orphan-commit history
reset left the new-code baseline predating every commit. The tell:

- `new_lines` (~296k) **exceeds** total `ncloc` (~187k), and
- every `new_*` metric exactly equals its all-time twin (`new_bugs == bugs`, etc.).

Until this is fixed the Quality Gate verdict is **meaningless** (it fails the whole
codebase as if it were one giant PR), which is why it's kept advisory.

**Fix (one-time, requires project admin — it is a SonarCloud setting, not a file):**

1. UI: **Project → Administration → New Code → Reference branch → `main`**, *or*
2. API:
   ```bash
   curl -s -u "$SONAR_TOKEN:" -X POST \
     "https://sonarcloud.io/api/new_code_periods/set" \
     -d project=evoila_meho -d type=REFERENCE_BRANCH -d value=main
   ```

After this, PRs are diffed against `main` and the gate scores only the real diff.
Coverage/duplication/ratings on new code then become trustworthy signals.

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

## Real backlog (not noise)

- **Go CLI dispatch boilerplate** — extract a shared `CallOperation` helper so the
  ~18 per-vendor command files stop re-implementing the HTTP-to-`/operations/call`
  plumbing. Compatible with CLAUDE.md postulate 5 (transport DRYing ≠ thin wrapper).
- **K8s resource requests/limits** (`kubernetes:S6870`) on the chart deployments +
  migration job.
- **Dockerfile hardening** (`docker:S8541`).
- *(optional)* **`S3776` cognitive-complexity** pass on the worst offenders.
