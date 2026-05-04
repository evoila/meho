# CI Quality Gates

MEHO's CI pipeline enforces code quality, security, and compliance standards on every pull request. All checks run automatically via GitHub Actions.

## Overview

| Gate | Tool | Blocks PR? | What It Checks |
|------|------|-----------|----------------|
| Code Quality | SonarCloud | Yes (new issues) | Bugs, vulnerabilities, code smells, duplications |
| SAST | Semgrep | Yes (ERROR findings) | Security vulnerabilities in Python and TypeScript |
| Python Deps | pip-audit | Yes (any CVE) | Known vulnerabilities in Python dependencies |
| Frontend Deps | npm audit | Yes (HIGH/CRITICAL) | Known vulnerabilities in npm dependencies |
| Coverage | Codecov | Yes (< 80% patch) | Test coverage on changed lines |
| License | License check | Warn mode | Dependency license compatibility with AGPL-3.0 |
| Complexity | Ruff mccabe | Yes | Function complexity > 15 |
| Code Style | Ruff | Yes | 16 rule sets including PERF, ERA, TCH |
| SPDX | Pre-commit hook | Yes | SPDX-License-Identifier header on all Python files |

## SonarCloud

SonarCloud provides continuous code quality analysis with a "Clean as You Code" approach -- the quality gate applies only to new code, so existing issues do not block PRs.

### Configuration

- **Workflow**: `.github/workflows/quality-gate.yml`
- **Trigger**: Runs via `workflow_run` after CI completes (no test duplication)
- **Quality gate**: Blocks PRs that introduce new bugs, vulnerabilities, or security hotspots
- **Coverage**: Receives Python coverage from CI workflow via artifact download; generates frontend coverage inline
- **Test identification**: `sonar.tests=tests` (Python); frontend test files identified by `sonar.test.inclusions` patterns (`**/test_*.py`, `**/*.test.ts`, `**/*.test.tsx`, `**/*.spec.ts`, `**/__tests__/**`)
- **Sources**: `meho_app`, `meho_frontend/src`, `meho_mcp_server`
- **Exclusions**: Alembic migrations, node_modules, `__pycache__`, dist, mock systems

### Required Setup

1. Create a SonarCloud organization and project at sonarcloud.io
2. Add `SONAR_TOKEN` to GitHub repository secrets
3. SonarCloud PR decoration requires `pull-requests: write` permission (configured in workflow)

### SonarCloud Project Properties

Configuration is defined in `sonar-project.properties` at the repository root:

```properties
sonar.projectKey=evoila_meho
sonar.organization=evoila
sonar.python.coverage.reportPaths=coverage.xml
sonar.typescript.lcov.reportPaths=meho_frontend/coverage/lcov.info
```

## Semgrep SAST

Static Application Security Testing using Semgrep with a curated, vendored snapshot of the Python, TypeScript, security-audit, and OWASP Top Ten rule packs.

- **Workflow**: `.github/workflows/security-scan.yml`
- **Trigger**: Every push to main and all PRs targeting main
- **Rulesets**: vendored under `.semgrep/rules/` — three files (`python.yml`, `frontend.yml`, `cross-cutting.yml`) holding a deduplicated, curated subset of `p/python` + `p/typescript` + `p/security-audit` + `p/owasp-top-ten`. Rules whose first-segment language is one MEHO does not use (Java, Go, Ruby, Scala, Kotlin, Swift, C/C++, C#, PHP, Terraform, Solidity, Clojure, Rust, OCaml, Dart, Elixir) are filtered out by `scripts/refresh-semgrep-rules.py`.
- **Severity filter**: Only ERROR-level findings block PRs (WARNING-level reported but non-blocking)
- **SARIF output**: Always uploaded as a `semgrep-sarif` job artifact (14-day retention) for offline diagnosis. Also uploaded to GitHub Code Scanning for inline annotations when supported (best-effort in forks)
- **Per-finding exemptions**: Inline `# nosemgrep: <full-rule-id>` comment on the match line or the line above. Rationale lives in a regular comment on the line above the suppression — never on the same line as the rule ID, because trailing text after the ID has historically broken matching in some Semgrep versions. Rule IDs must be fully namespaced — e.g. `python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text`. Pack-level rule disables require editing the curation script's KEEP/DROP filters and re-running it; never silently delete vendored YAML.
- **Container**: Runs in `semgrep/semgrep:1.160.0`, pinned in `.github/workflows/security-scan.yml`. Numeric version tags on `semgrep/semgrep` are immutable per the Docker Hub repo policy ("Tags cannot be overwritten in this Repository"), so the pin is reproducible without an additional digest pin.
- **`.semgrepignore`**: excludes `.semgrep/` itself (the vendored rule files contain example payloads that would otherwise self-match), `.venv/` and `node_modules/` (third-party code), `_archive/`, `meho-claude/`, `tests/fixtures/`.

### Why rules are vendored, not pulled from the registry

`--config p/<pack>` resolves the rule pack from the Semgrep registry **at scan time**. The image pin only freezes the binary — registry-side rule changes go right past it. This produced a recurring drift episode across 2026-04 and the start of 2026-05:

1. The `# nosemgrep` annotations triaged in commit `e11d3dd9` (closing #333) were placed on the line preceding the matched call. That form was honored by the `:latest` image at the time.
2. Around Semgrep 1.79, preceding-line annotations with trailing rationale text after the rule ID stopped suppressing multi-line matches in some configurations. PRs #475 / #476 / #496 / #498 each carried inline workarounds; PR #501 consolidated them.
3. Before #501 merged, the `:latest` image moved again — to 1.160.0 — and *re-honored* the original preceding-line form. Commit `cd8ca8df` reverted PR #501's defensive workarounds after empirical re-test against 1.160.0 returned exit 0.
4. After PR #559 landed (audit-log integration test), CI started failing again with 4 ERROR findings on `main` — same pre-existing annotations, same pinned image, but the registry packs had drifted server-side. The image pin alone could not prevent this.

Vendoring the rule packs into the repo (`scripts/refresh-semgrep-rules.py` + `.semgrep/rules/`) removes the registry as a moving target. Suppressions verified working at refresh time stay working until the next deliberate refresh.

### Refresh procedure

Refresh on a maintainer's machine; commit the resulting diff:

```bash
uv run python scripts/refresh-semgrep-rules.py
# Refresh is non-zero if the registry has added a rule family with an
# unrecognized first-segment prefix -- handle the listed IDs (extend
# KEEP_LANGS / DROP_LANGS / ROUTE in the script) and re-run.

git add .semgrep/rules/
git diff --stat HEAD~1     # sanity-check the size
docker run --rm -v "$PWD:/src" -w /src semgrep/semgrep:1.160.0 \
  semgrep scan --config .semgrep/rules/ --error --severity ERROR
# Expect: exit 0. If new findings appear, triage each with an inline
# `# nosemgrep` annotation + rationale comment, OR drop the offending
# rule family by editing the curation script's language-level filter
# constants below — never edit the vendored YAML by hand.
```

**Cadence**: review monthly or on a Semgrep advisory. The refresh PR is its own scope — never bundled with feature work or refactors. Bundling tempts out-of-scope reverts (see the 4 `revert(ci): drop out-of-scope semgrep fixes from <X>` commits on `main` for prior art).

**Filter constants** in [`scripts/refresh-semgrep-rules.py`](../../scripts/refresh-semgrep-rules.py) are the contract for what the snapshot includes — adjust them, not the vendored YAML directly:

- `KEEP_LANGS` — first-segment language prefixes to keep (`python`, `javascript`, `typescript`, `generic`, `yaml`, `dockerfile`, `bash`, `json`, `html`).
- `DROP_LANGS` — first-segment prefixes to drop (Java, Go, Ruby, Scala, etc.).
- `DROP_SUBLANG_TOKENS` — sub-language tokens that mark a `problem-based-packs.*` rule for drop.
- `DROP_GENERIC_SUBLANGS` — narrows the broad `generic.*` keep filter for technologies MEHO does not use (e.g. `generic.visualforce.*`).
- `ROUTE` — maps each kept language prefix to its output file (`python.yml`, `frontend.yml`, `cross-cutting.yml`).

These are language- or sub-namespace-level filters. Per-rule disables (single rule from an otherwise-kept family) are not supported by them; suppress at every call site with `# nosemgrep` instead, or curate a custom rule pack outside this snapshot.

The script regenerates `.semgrep/rules/` wholesale: it deletes every pre-existing `*.yml` in that directory before writing, so renamed or retired buckets cannot linger as orphans CI would still load via `--config .semgrep/rules/`. Each output file's rules are sorted by ID so the registry's own iteration order cannot produce large reorder-only diffs.

### Bumping the container image

Same upgrade cadence: monthly or on advisory. Run the candidate image against the *current* vendored rules:

```bash
docker run --rm -v "$PWD:/src" -w /src semgrep/semgrep:<new-version> \
  semgrep scan --config .semgrep/rules/ --error --severity ERROR
```

Exit 0 with every existing `# nosemgrep` annotation honored is the green light. If a suppression stops working on the new tag, fix the suppression on the same PR as the bump (use the inline-on-matched-line form, rule ID with no trailing text). Never silently delete an annotation.

## Dependency Vulnerability Scanning

Both Python and frontend dependency audits run as separate jobs in `.github/workflows/security-scan.yml`, in parallel with Semgrep.

### Python (pip-audit)

- Scans installed production dependencies (`uv run --no-dev`) for known CVEs
- Installs the project with production dependencies before auditing
- **Will block PRs** with any identified vulnerability once #343 strips
  `continue-on-error: true` from the audit job (PR #463). Currently the job
  still soft-fails so the gate can be brought to clean before flipping the
  switch — this Task (#468) is the last prerequisite.
- Uses `--desc` flag to include vulnerability descriptions in output
- Individual findings can be excluded with `--ignore-vuln <ID>`, but only under the
  load-bearing-rationale policy below

#### Suppression policy

Every `--ignore-vuln` flag in the audit step must be backed by an inline rationale
comment containing all four of:

1. **What's wrong.** A one-sentence summary of the CVE.
2. **Why we can't bump.** Either upstream has no fix, or the fix exists but is
   unreachable through dependency resolution (cite the blocking constraint).
3. **Reachability evidence.** A concrete demonstration that the vulnerable
   codepath is not exercised by MEHO. Prefer a `file:line` pointer or a grep
   result. For CVEs in tooling that's not part of MEHO's runtime/process
   surface (e.g. CVEs in `pip` itself, or in a server endpoint MEHO never
   starts), an explicit "not in MEHO's runtime/process surface because
   <reason>" sentence backed by an absence-of-import grep (e.g. `rg -l
   "from <package>|import <package>" meho_app/` returning zero hits) is
   acceptable. "We probably don't use that" is not evidence.
4. **Revisit trigger.** A semantic condition that, if it changes, should
   prompt re-evaluation (e.g. *"revisit when crawl4ai supports lxml 6.x"*,
   *"revisit if MEHO adopts ES256 JWTs"*).

A `--ignore-vuln` for a CVE whose codepath is reachable in MEHO is signal decay
and is not allowed. The correct action there is to switch the underlying library
or accept the audit failure as a hard block — not to suppress the warning.

The current set of suppressions and their rationale lives inline in
[`.github/workflows/security-scan.yml`](https://github.com/evoila-bosnia/MEHO.X/blob/main/.github/workflows/security-scan.yml)
under the *Audit Python dependencies* step. Each one cites a specific MEHO file
and line, plus the upstream condition that would resolve it.

### Frontend (npm audit)

- Scans `package-lock.json` for known vulnerabilities
- Blocks PRs with HIGH or CRITICAL severity (`--audit-level=high`)
- MODERATE and LOW severity findings are reported but do not block

## Code Coverage (Codecov)

Coverage is collected from both Python and frontend test suites during the CI workflow (`.github/workflows/ci.yml`).

- **Python**: `pytest --cov=meho_app --cov-report=xml` produces `coverage.xml`
- **Frontend**: Vitest with coverage configuration
- **Upload**: Codecov action with `fail_ci_if_error: false` (graceful degradation for fork PRs without `CODECOV_TOKEN`)
- **Patch coverage requirement**: 80% on changed lines
- **Project coverage**: Auto baseline (compares to base commit, no fixed target)
- **PR comments**: Coverage diff posted automatically
- **Coverage scope**: `meho_app/` source paths
- **Coverage flags**: `backend-unit` flag for Python coverage

### Configuration

The `CODECOV_TOKEN` secret is optional -- coverage upload degrades gracefully for fork PRs that don't have access to the token. The coverage artifact is also uploaded to GitHub Actions for reuse by the SonarCloud quality gate workflow.

## License Compliance

Checks that all Python and npm dependencies are compatible with AGPL-3.0-only licensing.

- **Workflow**: `.github/workflows/license-check.yml`
- **Trigger**: Push to main or PR, only when `pyproject.toml`, `package.json`, or `package-lock.json` changes
- **Current mode**: **Warn mode** (`continue-on-error: true`) pending initial audit
- **Will be enforced** once baseline is clean and allow-list is finalized

### Python License Allow-List

Compatible licenses: MIT, BSD (all variants), Apache-2.0, ISC, PSF, LGPL (all), AGPL-3.0, MPL-2.0, Unlicense, CC0, Zlib, HPND, Public Domain.

Uses `pip-licenses --allow-only` with the full allow-list.

### Frontend License Allow-List

Compatible licenses: MIT, ISC, BSD-2-Clause, BSD-3-Clause, Apache-2.0, 0BSD, CC0-1.0, Unlicense, CC-BY-4.0, CC-BY-3.0, Python-2.0, BlueOak-1.0.0.

Uses `npx license-checker --onlyAllow` with the allow-list.

## Ruff Code Quality Rules

Ruff configuration is defined in `pyproject.toml` under `[tool.ruff]` and `[tool.ruff.lint]`.

### Enabled Rule Sets

| Rule Set | Purpose |
|----------|---------|
| E | pycodestyle errors |
| W | pycodestyle warnings |
| F | pyflakes |
| I | isort import ordering |
| B | flake8-bugbear |
| C4 | flake8-comprehensions |
| UP | pyupgrade |
| S | flake8-bandit (security) |
| ASYNC | flake8-async |
| PT | flake8-pytest-style |
| DTZ | flake8-datetimez |
| SIM | flake8-simplify |
| RUF | Ruff-specific rules |
| PERF | Perflint performance anti-patterns |
| ERA | eradicate (commented-out code detection) |
| TCH | flake8-type-checking (TYPE_CHECKING import optimization) |

### Complexity Control

Cyclomatic complexity is enforced via McCabe's C901 rule:

```toml
[tool.ruff.lint.mccabe]
max-complexity = 15
```

Functions exceeding complexity 15 will fail the lint check.

### Per-File Ignores

Configured for practical development without blocking on legitimate patterns:

| Pattern | Ignored Rules | Reason |
|---------|---------------|--------|
| `__init__.py` | F401 | Unused imports are expected in `__init__.py` barrel files |
| `**/alembic/env.py` | E402 | Imports after `sys.path` manipulation |
| `tests/**/*.py` | S101, S105, S106, S311, PERF | Asserts, test passwords, non-crypto random, and perf rules are standard in tests |

### Line Length

Configured to 100 characters (`line-length = 100`). Line length enforcement is handled by the Ruff formatter, not the E501 linter rule.

## SPDX License Headers

All Python files must include the AGPL-3.0 license identifier:

```python
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
```

Enforced by a pre-commit hook that runs on every commit. Files missing the header will fail the hook and block the commit.

## CI Workflow Summary

The full CI pipeline consists of 5 workflows with 10 total jobs:

| Workflow | Jobs | Trigger |
|----------|------|---------|
| CI (`.github/workflows/ci.yml`) | Python Lint, TypeScript Lint, Type Check, Python Tests, Frontend Tests | Push to main, all PRs |
| Quality Gate (`.github/workflows/quality-gate.yml`) | SonarCloud Analysis | After CI completes (workflow_run) |
| Security Scan (`.github/workflows/security-scan.yml`) | Semgrep SAST, pip-audit, npm audit | Push to main, all PRs |
| License Check (`.github/workflows/license-check.yml`) | Python License Check, Frontend License Check | Dependency file changes only |
| PR Checks (`.github/workflows/pr-checks.yml`) | PR Title Check, Code Review Checklist | PR opened/updated |

All third-party actions in security-critical workflows are pinned to commit SHAs per supply chain best practice.

## README Badges

The repository README displays quality signal badges:

| Badge | Source |
|-------|--------|
| CI Status | GitHub Actions CI workflow |
| Coverage | Codecov |
| Quality Gate | SonarCloud |
| License | AGPL-3.0-only |
| Python | Version requirement from pyproject.toml |
