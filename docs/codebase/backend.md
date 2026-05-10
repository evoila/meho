# `backend/` — backplane Python project

> Durable map of the backplane source tree at the chassis stage. Update
> in lock-step with code changes; stale entries are bugs.

## Overview

`backend/` houses the MEHO governance-layer backplane — a FastAPI
service that mediates every operation an AI agent runs against shared
infrastructure (policy gating, audit, federation, observability). At
this Goal-#11 chassis stage it serves only a single identity route on
`/`; health, readiness, metrics, structured logging, JWT validation,
Vault federation, and database persistence land progressively in
subsequent G2.1 / G2.2 / G2.3 Tasks. The stack (FastAPI, Pydantic v2,
SQLAlchemy 2.x async, Alembic, structlog, prometheus_client) is locked
by [ADR 0004](https://github.com/evoila-bosnia/meho-internal/issues/13).

The project follows the modern src-layout
(`backend/src/meho_backplane/...`) so tests resolve only the installed
package and never the in-tree source — a guardrail against accidental
PYTHONPATH-leak imports.

## Key types

| Symbol | Location | Purpose |
| --- | --- | --- |
| `app` (`fastapi.FastAPI`) | `src/meho_backplane/main.py` | ASGI application instance consumed by uvicorn / k8s probes. Title and `version` are populated from `__version__` so OpenAPI metadata stays in lock-step with the package. |
| `__version__` (`str`) | `src/meho_backplane/__init__.py` | Single source of truth for the running app version. The pyproject `[project].version` field mirrors this constant; the `test_version_constant_matches_pyproject` test acts as a tripwire if the two drift. |
| `root` (route) | `src/meho_backplane/main.py` | `GET /` returning `{"name": "meho-backplane", "version": "<x>"}`. Smoke-probe surface until `/healthz` is wired in Task #19. |

## Control flow

1. The container's `CMD` invokes
   `uvicorn meho_backplane.main:app --host 0.0.0.0 --port 8000`.
2. uvicorn imports `meho_backplane.main`, which constructs the
   `FastAPI` instance and registers the `root` coroutine.
3. uvicorn binds to `:8000` and starts the ASGI event loop.
4. Each `GET /` request is dispatched to `root()`, which returns a
   plain `dict`; FastAPI serialises it to JSON.

There is no startup or shutdown machinery yet — the FastAPI
[lifespan](https://fastapi.tiangolo.com/advanced/events/) hook is
introduced when DB/Vault wiring lands (G2.2 / G2.3).

## Dependencies

Pinned-floor declarations; exact versions resolved into `uv.lock`.

| Library | Floor | Why it's here |
| --- | --- | --- |
| `fastapi` | ≥ 0.110 | Web framework + OpenAPI 3.1 emission (per ADR 0004). |
| `uvicorn[standard]` | ≥ 0.30 | ASGI server with `httptools` / `websockets` extras. |
| `pydantic` | ≥ 2.6 | Pulled transitively by FastAPI; pinned explicitly so v1 can't be substituted. |
| `structlog` | ≥ 24.1 | Reserved for Task #20 (JSON logs); declared now to keep `uv.lock` stable across the Initiative. |
| `prometheus-client` | ≥ 0.20 | Reserved for Task #20 (`/metrics`); same reason. |
| (dev) `pytest` ≥ 8 | | Test runner. |
| (dev) `pytest-asyncio` ≥ 0.23 | | Async test support; `asyncio_mode = "auto"` in pyproject. |
| (dev) `httpx` ≥ 0.27 | | Backend for `fastapi.testclient.TestClient`. |
| (dev) `ruff` ≥ 0.5 | | Lint + format. |
| (dev) `mypy` ≥ 1.10 | | Strict type checking. |

structlog and prometheus_client are intentionally listed at the
chassis stage even though they have no call sites yet — pinning them
in this Task means Tasks #19 / #20 don't have to relock the project on
top of their own changes, which keeps the per-Task PR diffs focused.

## Known issues

None at the chassis stage. `/ready` does not exist yet, so kubernetes
readiness probes pointed at it during initial helm-chart development
will 404 until Task #19 lands; that's by design and tracked in #19's
acceptance criteria.

## References

- ADR 0004 — Stack choice (Python backplane + Go CLI)
- Task #18 — this chassis bootstrap
- Task #19 — public health + version + readiness endpoints
- Task #20 — observability primitives (`/metrics`, structlog, middleware)
- [FastAPI tutorial](https://fastapi.tiangolo.com/tutorial/)
- [uv project structure](https://docs.astral.sh/uv/concepts/projects/)
- [uv production Docker pattern](https://docs.astral.sh/uv/guides/integration/docker/)
