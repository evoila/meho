# Claude Code Instructions

Read `AGENTS.md` first. All rules defined there apply to Claude Code sessions.

## Commands

```bash
# Start development environment
docker compose up

# Backend tests
pytest tests/unit/ -x -q                         # Fast unit tests
pytest tests/ --cov=meho_app --cov-report=term    # Full suite with coverage

# Backend lint + format + typecheck
ruff check meho_app/
ruff format --check meho_app/
mypy meho_app/ --ignore-missing-imports

# Frontend tests + lint + typecheck
cd meho_frontend
npm run test:run
npm run lint
npm run typecheck
```

## Key Files

When working on connectors, always read these first:
- `meho_app/modules/connectors/base.py` -- BaseConnector interface
- `meho_app/modules/connectors/pool.py` -- connector dispatch (only switch point)
- `meho_app/core/feature_flags.py` -- feature flag definitions

When working on the agent:
- `meho_app/modules/agents/shared/graph/` -- ReAct loop graph nodes
- `meho_app/modules/agents/factory.py` -- agent + skill wiring
- `meho_app/modules/agents/skills/` -- per-connector skill files

## Notes

- The `.planning/` directory contains private project planning artifacts. Do not reference or modify these in commits intended for the public repository.
- Conventional commits with scope: `feat(connectors):`, `fix(knowledge):`, etc.
- When adding a new connector, follow the 16-step checklist in `AGENTS.md` and the full walkthrough in `docs/architecture/adding-connector.md`.
