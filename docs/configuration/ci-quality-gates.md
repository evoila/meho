# CI Quality Gates

> Added in v2.3 (Phase 100)

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

Static Application Security Testing using Semgrep with Python, TypeScript, security-audit, and OWASP Top Ten rulesets.

- **Workflow**: `.github/workflows/security-scan.yml`
- **Trigger**: Every push to main and all PRs targeting main
- **Rulesets**: `p/python`, `p/typescript`, `p/security-audit`, `p/owasp-top-ten`
- **Severity filter**: Only ERROR-level findings block PRs (WARNING-level reported but non-blocking)
- **SARIF output**: Results uploaded to GitHub Code Scanning tab for inline annotations
- **Container**: Runs in the official `semgrep/semgrep` container image

## Dependency Vulnerability Scanning

Both Python and frontend dependency audits run as separate jobs in `.github/workflows/security-scan.yml`, in parallel with Semgrep.

### Python (pip-audit)

- Scans installed production dependencies (from `pyproject.toml`) for known CVEs
- Installs the project with production dependencies before auditing
- Blocks PRs with any identified vulnerability
- Uses `--desc` flag to include vulnerability descriptions in output
- Individual false positives can be excluded with `--ignore-vuln PYSEC-XXXX`

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
- **Coverage scope**: `meho_app/` source paths (corrected from `meho_core/` in Phase 100)
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
