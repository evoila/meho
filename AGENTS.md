# MEHO -- AI Agent Instructions

MEHO (Machine Enhanced Human Operator) is an AI-powered diagnostic and operations platform for IT infrastructure. It connects to external systems via typed connectors and reasons across them using an LLM-powered ReAct agent. Licensed under AGPLv3.

## Absolute Rules

These are hard constraints. Violating any of them will break the build, introduce security issues, or corrupt architecture invariants.

- NEVER import `pandas`. The data pipeline uses Apache Arrow and DuckDB exclusively. If you see existing pandas code, it is legacy debt being removed -- do not extend it.
- NEVER use `export default` in TypeScript. All exports must be named. ESLint enforces this.
- NEVER hardcode credentials, secrets, or API keys. Credentials are Fernet-encrypted at rest and loaded from environment variables.
- NEVER branch on `connector_type` outside `meho_app/modules/connectors/pool.py`. This is the single dispatch point for all connector instantiation. Use lazy imports.
- NEVER skip SPDX license headers on new files. Every file must start with:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-only
  # Copyright (c) 2026 evoila Group
  ```
  ```typescript
  // SPDX-License-Identifier: AGPL-3.0-only
  // Copyright (c) 2026 evoila Group
  ```
- NEVER omit type hints on function signatures. MyPy strict mode (`disallow_untyped_defs`) is enforced.
- NEVER use synchronous I/O in async code paths. Wrap blocking SDK calls with `asyncio.to_thread()`. Use native async libraries where available (e.g., `azure-mgmt-*.aio`).
- NEVER stream raw connector data to the frontend. Large results go through the JSONFlux QueryEngine (Arrow/DuckDB) and are returned as markdown tables.
- NEVER create `datetime` without timezone. Always use `datetime.now(UTC)`. The `DTZ` Ruff rule enforces this.
- NEVER modify a committed Alembic migration file. Create a new migration instead.

## Architecture

**Backend:** FastAPI on Python 3.13+. Domain-driven module layout under `meho_app/modules/`. Each module (agents, connectors, knowledge, topology, memory, etc.) owns its models, services, repositories, and Alembic migrations.

**Frontend:** React 19 with TypeScript strict mode, Vite bundler, TailwindCSS v4 for styling, Zustand v5 for state management. Feature stores live in `meho_frontend/src/features/`.

**Data pipeline:** PostgreSQL with pgvector for vector search, Redis for caching and BM25 indices, Apache Arrow for in-memory data, DuckDB for analytical queries. No pandas.

**Agent:** A ReAct (Reason-Act-Observe) loop powered by PydanticAI. The specialist agent calls connector operations, searches the knowledge base, and traverses the topology graph to investigate infrastructure issues. Skills (markdown instructions) are injected per-connector.

**Auth:** Keycloak OIDC with JWT validation on all API routes. Three trust tiers for operations: READ (auto-approved), WRITE (requires approval), DESTRUCTIVE (requires approval + confirmation).

**Connectors:** Typed connectors (Kubernetes, VMware, GCP, AWS, Azure, Prometheus, Loki, Slack, etc.) plus a generic REST connector via OpenAPI spec ingestion. All implement `BaseConnector` from `meho_app/modules/connectors/base.py`.

For the full architecture with diagrams, see `docs/architecture/overview.md`.

## Directory Layout

```
meho_app/                       # Backend (FastAPI)
  core/                          # Config, auth, feature flags, OTEL
  api/                           # Route handlers, schemas, middleware
    connectors/                  # Connector CRUD + operation execution
  modules/
    agents/                      # ReAct agent, tools, skills, graph nodes
    connectors/                  # All typed connectors (see below)
      base.py                    # BaseConnector interface (6 abstract methods)
      pool.py                    # Connector dispatch (ONLY place to branch on type)
      {connector}/               # One directory per connector type
    knowledge/                   # Knowledge base, embeddings, hybrid search
    topology/                    # Infrastructure graph, entity resolution
    memory/                      # Session memory, auto-extraction
    ...
  jsonflux/                      # Arrow/DuckDB query engine

meho_frontend/                   # Frontend (React 19 + TypeScript)
  src/
    features/                    # Zustand stores + hooks (chat, connectors, knowledge, ...)
    components/                  # UI components by domain
    pages/                       # Route-level page containers
    lib/                         # API client, auth, utilities

tests/                           # Test suite
  unit/                          # Mocks only, no external services
  integration/                   # Real database (pgvector), fixtures
  e2e/                           # Full stack with real services

docs/                            # MkDocs documentation site
  connectors/                    # Per-connector setup guides
  architecture/                  # System overview, adding-connector walkthrough
```

### Connector Directory Template

Every typed connector follows this structure:

```
meho_app/modules/connectors/{name}/
  __init__.py              # Package exports
  connector.py             # Main class (extends BaseConnector + handler mixins)
  handlers/                # Handler mixins (one per service area)
    __init__.py
    {area}_handlers.py     # e.g., compute_handlers.py, network_handlers.py
  operations/              # OperationDefinition lists
    __init__.py            # Aggregates all category lists into {NAME}_OPERATIONS
    {category}.py          # e.g., compute.py, monitoring.py
  types.py                 # TypeDefinition list for topology entities
  serializers.py           # Raw SDK response -> clean dict
  sync.py                  # Auto-sync operations + topology discovery
  helpers.py               # Shared utilities (optional)
```

For simpler connectors (few operations), flat files instead of subdirectories are acceptable: `handlers.py` and `operations.py` directly in the connector directory.

## Python Code Style

**Linter:** Ruff with 13 rule sets. Configuration in `pyproject.toml`:
- `E`, `W` (pycodestyle), `F` (pyflakes), `I` (isort), `B` (bugbear), `C4` (comprehensions), `UP` (pyupgrade), `S` (bandit/security), `ASYNC` (async), `PT` (pytest-style), `DTZ` (datetimez), `SIM` (simplify), `RUF` (ruff-specific)

**Formatter:** Ruff format, line length 100.

**Type checker:** MyPy strict -- `disallow_untyped_defs`, `disallow_incomplete_defs`, `no_implicit_optional`, `check_untyped_defs`.

**Conventions:**
- Use `X | None` union syntax, not `Optional[X]` (pyupgrade `UP` rule)
- Use `list[X]`, `dict[K, V]` lowercase generics, not `List`, `Dict` from typing
- Use `TYPE_CHECKING` guard for imports that are only needed for type hints (avoids circular imports)
- Async everywhere -- all I/O functions must be `async def`
- Wrap blocking SDK calls: `await asyncio.to_thread(blocking_call, args)`
- Snake_case for functions/variables, PascalCase for classes
- Google-style docstrings on public classes and methods

**Feature flags:** Defined in `meho_app/core/feature_flags.py` using `pydantic-settings`. Env var prefix `MEHO_FEATURE_*`. All default to `True`. Frozen (immutable) after startup. Check with `flags.{name}`.

**SPDX header** (first two lines of every Python file):
```python
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
```

## TypeScript Code Style

**Linter:** ESLint with `typescript-eslint` strict config. `jsx-a11y` at error level (not warn).

**Conventions:**
- Named exports only -- `export function ...`, `export const ...`. No `export default`.
- TypeScript strict mode -- no `any` (use `unknown` if truly untyped)
- Functional components with hooks, no class components
- Zustand v5 stores in `meho_frontend/src/features/{domain}/`
- Props defined as interfaces, destructured in function signature
- camelCase for functions/variables, PascalCase for components and types

**SPDX header** (first two lines of every TypeScript/TSX file):
```typescript
// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
```

## Commits and Pull Requests

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(connectors): add ServiceNow connector with READ operations
fix(knowledge): filter TOC noise from PDF chunk search results
docs(architecture): update connector checklist with frontend steps
test(agents): add specialist agent convergence detection tests
refactor(core): extract credential resolver into standalone service
chore(ci): add mypy strict check to PR pipeline
```

**Scopes:** `connectors`, `knowledge`, `topology`, `agents`, `frontend`, `core`, `api`, `docs`, `ci`, `events`, `memory`

Reference issue numbers where applicable: `fix(knowledge): filter TOC noise (#142)`

## Testing

**Guard:** The root `tests/conftest.py` sets `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` before any imports. This prevents accidental LLM API calls in tests. All agent tests must use mocked model responses.

**Structure:**
- `tests/unit/` -- Mock all external dependencies. No database, no network, no LLM calls.
- `tests/integration/` -- Real PostgreSQL with pgvector. Fixtures in `tests/integration/conftest.py` provide database sessions, tenant context, and knowledge service instances.
- `tests/e2e/` -- Full stack with real services via Docker Compose.

**Backend:** pytest with `asyncio_mode = "auto"` (async tests run without explicit markers).

**Frontend:** Vitest for unit tests, Playwright for e2e tests.

**Commands:**
```bash
# Backend
pytest tests/unit/ -x -q                        # Unit tests (fast, no deps)
pytest tests/ --cov=meho_app --cov-report=term   # With coverage

# Frontend
cd meho_frontend
npm run test:run     # Vitest unit tests
npm run lint         # ESLint
npm run typecheck    # tsc --noEmit
```

**CI runs all of these in parallel. Every PR must pass: Ruff check + format, MyPy, ESLint, tsc, pytest unit, vitest.**

## Adding a Connector

This is the most common type of contribution. Condensed checklist:

1. **Create directory** at `meho_app/modules/connectors/{name}/` following the template above
2. **Define operations** as `OperationDefinition` lists in `operations/`. Each needs: `operation_id`, `name`, `description`, `category`, `parameters`, `response_entity_type`
3. **Implement connector class** extending `BaseConnector` with handler mixins. Must implement 6 abstract methods: `connect()`, `disconnect()`, `test_connection()`, `execute()`, `get_operations()`, `get_types()`
4. **Write handler mixins** with `_handle_{operation_id}` methods matching each operation's ID
5. **Add serializers** that convert raw SDK responses to clean dictionaries with consistent field names (`id`, `name`, `status`)
6. **Define topology types** as `TypeDefinition` lists in `types.py`
7. **Implement sync** in `sync.py` -- auto-sync operations on startup, discover topology entities
8. **Register in pool.py** with a lazy import in `get_connector_instance()`
9. **Add feature flag** in `meho_app/core/feature_flags.py`
10. **Create API schema** in `meho_app/api/connectors/schemas.py` (Create request + Response)
11. **Create API endpoint** in `meho_app/api/connectors/operations/{name}.py`
12. **Wire in main.py** -- add sync function tuple to lifespan, check feature flag
13. **Add skill** -- create `meho_app/modules/agents/skills/{name}.md` and add to `TYPE_SKILL_MAP` in `meho_app/modules/agents/factory.py`
14. **Frontend** -- add connector form fields in `CreateConnectorModal.tsx`
15. **Documentation** -- write `docs/connectors/{name}.md` following existing template, add to `mkdocs.yml` nav
16. **Tests** -- unit tests in `tests/unit/connectors/{name}/`

**Trust level assignment:** READ for list/get/query operations, WRITE for create/update/scale, DESTRUCTIVE for delete/destroy.

For the full walkthrough with code examples, see `docs/architecture/adding-connector.md`.

## Database Migrations

Each module maintains its own Alembic migration chain with a custom version table:

```
meho_app/modules/{module}/alembic/
  env.py                    # Module-specific config with VERSION_TABLE
  versions/
    0001_initial.py         # Squashed base migration
    0002_add_field.py       # Incremental changes
```

**Rules:**
- Never modify a committed migration. Create a new one.
- New migrations must be additive (handle both fresh installs and upgrades).
- Alembic files are exempt from MyPy type checking.
- Test both upgrade and downgrade paths.
- Use the existing module's `alembic.ini` as reference when adding a new module.

## Key Patterns

**Connector dispatch:** `pool.py` is the ONLY switch on connector type. Use lazy imports to avoid loading SDKs unnecessarily:
```python
if connector_type == "your_connector":
    from meho_app.modules.connectors.your_connector import YourConnector
    return YourConnector(connector_id, config, credentials)
```

**Operation dispatch:** The `execute()` method routes by naming convention. `operation_id="list_pods"` calls `self._handle_list_pods()`. No routing table needed.

**Trust tiers:** 5-level classification hierarchy: per-endpoint override > typed connector registry > operation name heuristic (`list_` = READ, `delete_` = DESTRUCTIVE) > HTTP method heuristic > default WRITE.

**Data pipeline:** Connector results are cached in Redis. Large datasets go through the JSONFlux QueryEngine (DuckDB) for aggregation. The agent sees markdown tables or schema previews, never raw rows for large data.

**Credential handling:** Fernet symmetric encryption. Key from `CREDENTIAL_ENCRYPTION_KEY` env var. Never log credentials -- use `CredentialMasker` for sensitive output. Credential resolution chain: user-owned > service > delegated (with Keycloak check).

**Frontend state:** Zustand v5 stores in feature directories. Named hooks: `useChat()`, `useConnectors()`, etc. API client at `meho_frontend/src/lib/api-client.ts` with automatic 401 token refresh.

## References

- System architecture: `docs/architecture/overview.md`
- Adding a connector (full guide): `docs/architecture/adding-connector.md`
- Contribution workflow: `CONTRIBUTING.md`
- Ruff + MyPy config: `pyproject.toml`
- ESLint config: `meho_frontend/eslint.config.js`
- Feature flags: `meho_app/core/feature_flags.py`
- BaseConnector interface: `meho_app/modules/connectors/base.py`
- Connector pool: `meho_app/modules/connectors/pool.py`
