# Contributing to MEHO

Thank you for your interest in contributing to MEHO! This document provides guidelines and information for contributors.

## How to Contribute

### Reporting Issues

- Use the [GitHub Issues](https://github.com/evoila/meho/issues) tab to report bugs or request features
- **Bug reports:** Use the [bug report template](https://github.com/evoila/meho/issues/new?template=bug_report.yml) -- it includes fields for steps to reproduce, expected behavior, environment details, and relevant logs
- **Feature requests:** Use the [feature request template](https://github.com/evoila/meho/issues/new?template=feature_request.yml)
- Check existing issues before creating a new one
- For security vulnerabilities, see [SECURITY.md](SECURITY.md)

### Submitting Changes

1. **Fork** the repository
2. **Create a branch** from `main` for your changes (`feature/your-feature` or `fix/your-fix`)
3. **Make your changes** following the code style guidelines below
4. **Write or update tests** for your changes
5. **Run the test suite** to ensure nothing is broken
6. **Submit a pull request** against the `main` branch
7. **Ensure your PR title** follows [Conventional Commits](https://www.conventionalcommits.org/) format (e.g., `feat(connectors): add Datadog connector`)
8. **Fill out the PR template** checklist -- CI will validate the PR title format automatically

### Development Setup

See the [README](README.md) for full development environment setup instructions.

Quick start:

```bash
git clone https://github.com/evoila/meho.git
cd meho
docker compose up
```

## Code Style

### Python (Backend)

- **Linter:** [Ruff](https://docs.astral.sh/ruff/) with strict configuration (see `pyproject.toml`)
- **Type checking:** [mypy](https://mypy.readthedocs.io/) in strict mode
- **Formatting:** Ruff formatter (line length 100)
- All new files must include SPDX license headers:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-only
  # Copyright (c) 2026 evoila Group
  ```

### TypeScript (Frontend)

- **Linter:** [ESLint](https://eslint.org/) with strict configuration (see `meho_frontend/eslint.config.js`)
- **Type checking:** TypeScript strict mode
- **Named exports only** -- no `export default` for barrel-exported components/hooks
- All new files must include SPDX license headers:
  ```typescript
  // SPDX-License-Identifier: AGPL-3.0-only
  // Copyright (c) 2026 evoila Group
  ```

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` -- new feature
- `fix:` -- bug fix
- `docs:` -- documentation changes
- `test:` -- test changes
- `refactor:` -- code refactoring
- `chore:` -- maintenance tasks

Example: `feat(connectors): add ServiceNow connector with READ operations`

## Testing

### Backend

```bash
# Run unit tests (no external services needed)
pytest tests/ -x -q

# Run with coverage
pytest tests/ --cov=meho_app --cov-report=term-missing
```

### Frontend

```bash
cd meho_frontend
npm run test:run    # Unit tests
npm run typecheck   # Type checking
npm run lint        # Linting
```

## Contributor License Agreement

By submitting a pull request, you agree to the [Contributor License Agreement](CLA.md) (CLA). Copyright is assigned to evoila Bosnia d.o.o. to allow license management of the open-core project.

**How it works:**

1. A CLA bot will automatically comment on your first pull request
2. Sign by replying with: `I have read the CLA Document and I hereby sign the CLA`
3. Your signature is recorded in `signatures/cla.json` and applies to all future contributions
4. See [CLA.md](CLA.md) for the full agreement text

The CLA is a one-time requirement. Once signed, all subsequent PRs are automatically approved.

### Developer Certificate of Origin

In addition to the CLA, we recommend including a `Signed-off-by` line in your commits:

```bash
git commit -s -m "feat(connectors): add Datadog connector"
```

This adds `Signed-off-by: Your Name <your.email@example.com>` to the commit message, certifying that you have the right to submit the contribution. The DCO is informational -- the CLA is the binding agreement.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). By participating, you are expected to uphold this code.

## Questions?

- Open a [Discussion](https://github.com/evoila/meho/discussions) on GitHub
- Join our [Discord community](https://discord.gg/meho)

---

*MEHO is licensed under [AGPLv3](LICENSE). See the license file for details.*
