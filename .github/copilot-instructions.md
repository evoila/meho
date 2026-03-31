# GitHub Copilot Instructions for MEHO

All project rules are defined in `AGENTS.md` at the repository root. Read it for the full specification. This file summarizes the critical rules for Copilot.

## Critical Rules

- Never import pandas. Use Apache Arrow and DuckDB for data processing.
- Never use `export default` in TypeScript. Named exports only.
- Never hardcode credentials or secrets.
- Never branch on connector type outside `meho_app/modules/connectors/pool.py`.
- Every new file must start with SPDX license headers:
  - Python: `# SPDX-License-Identifier: AGPL-3.0-only` + `# Copyright (c) 2026 evoila Group`
  - TypeScript: `// SPDX-License-Identifier: AGPL-3.0-only` + `// Copyright (c) 2026 evoila Group`
- All Python functions must have type hints (MyPy strict mode).
- All I/O must be async. Wrap blocking calls with `asyncio.to_thread()`.
- Use `datetime.now(UTC)`, never naive datetimes.
- Use `X | None` union syntax, not `Optional[X]`.
- Use lowercase generics: `list[X]`, `dict[K, V]`, not `List`, `Dict`.

## Code Style

- Python: Ruff (13 rule sets), line length 100, MyPy strict
- TypeScript: ESLint strict, typescript-eslint strict, jsx-a11y errors
- Commits: Conventional Commits with scope (`feat(connectors):`, `fix(knowledge):`)

## Key Architecture

- Backend: FastAPI, Python 3.13+, domain-driven modules under `meho_app/modules/`
- Frontend: React 19, TypeScript strict, Vite, Zustand v5, TailwindCSS v4
- Connectors: `BaseConnector` interface, handler mixins, operation registry, `_handle_{id}` dispatch
- Agent: ReAct loop, PydanticAI, per-connector skill injection

See `AGENTS.md` for the complete specification including directory layout, testing patterns, connector development checklist, and migration rules.
