# `backend/` — backplane Python project

> Durable map of the backplane source tree at the chassis stage. Update
> in lock-step with code changes; stale entries are bugs.

## Overview

`backend/` houses the MEHO governance-layer backplane — a FastAPI
service that mediates every operation an AI agent runs against shared
infrastructure (policy gating, audit, federation, observability). At
this Goal-#11 chassis stage it exposes:

* The identity route at `/`.
* The public operator surfaces — `/healthz`, `/version`, `/ready` —
  backed by a pluggable readiness-probe registry that is empty by
  default and **fails closed on `/ready`** (Task #19).
* Observability primitives — Prometheus metrics on `/metrics`,
  structured JSON logs to stdout via structlog, and a `request_id`
  correlation identifier propagated across every HTTP request via the
  request-context middleware (Task #20).

JWT validation, Vault federation, and database persistence land
progressively in subsequent G2.2 / G2.3 Tasks. The stack (FastAPI,
Pydantic v2, SQLAlchemy 2.x async, Alembic, structlog,
prometheus_client) is locked by
[ADR 0004](https://github.com/evoila-bosnia/meho-internal/issues/13).

The project follows the modern src-layout
(`backend/src/meho_backplane/...`) so tests resolve only the installed
package and never the in-tree source — a guardrail against accidental
PYTHONPATH-leak imports.

## Key types

| Symbol | Location | Purpose |
| --- | --- | --- |
| `app` (`fastapi.FastAPI`) | `src/meho_backplane/main.py` | ASGI application instance consumed by uvicorn / k8s probes. Title and `version` are populated from `__version__` so OpenAPI metadata stays in lock-step with the package. |
| `__version__` (`str`) | `src/meho_backplane/__init__.py` | Single source of truth for the running app version. The pyproject `[project].version` field mirrors this constant; the `test_version_constant_matches_pyproject` test acts as a tripwire if the two drift. |
| `root` (route) | `src/meho_backplane/main.py` | `GET /` returning `{"name": "meho-backplane", "version": "<x>"}`. Identity smoke-probe; coexists with `/healthz`. |
| `metrics` (route) | `src/meho_backplane/main.py` | `GET /metrics` returning the Prometheus exposition format from the default registry (process / GC collectors + the `http_requests_total` counter). |
| `lifespan` | `src/meho_backplane/main.py` | FastAPI lifespan async context manager; calls `configure_logging()` once at startup. The yield/return shape leaves room for G2.2 / G2.3 teardown without restructuring. |
| `ProbeResult` (`dataclass`) | `src/meho_backplane/health.py` | Frozen record `(name, ok, detail)` returned by every readiness probe. Surfaced verbatim in the `/ready` response body. |
| `register_probe` / `run_probes` / `clear_probes` | `src/meho_backplane/health.py` | Public registry API. G2.2 (Vault, Keycloak) and G2.3 (DB migrations) call `register_probe` at startup; `clear_probes` is test-only. |
| `health.router` (`/healthz`, `/ready`) | `src/meho_backplane/health.py` | Liveness and readiness endpoints. `/healthz` is unconditional 200; `/ready` aggregates the probe registry and **fails closed on the empty default** (vacuous-truth trap explicitly guarded). |
| `version.router` (`/version`) | `src/meho_backplane/version.py` | Build identity. Reads `GIT_SHA` and `BUILD_DATE` env vars (injected via `docker build --build-arg`); falls back to `"unknown"` when unset or empty. `chart_version` is `None` until G2.5. |
| `configure_logging` | `src/meho_backplane/logging.py` | Configures structlog: `merge_contextvars` → `add_log_level` → `TimeStamper(iso, utc)` → `JSONRenderer`, writing to stdout. Idempotent. |
| `RequestContextMiddleware` | `src/meho_backplane/middleware.py` | Pure-ASGI middleware. Per request: extracts/mints a `request_id`, binds into structlog contextvars, mirrors it onto the `X-Request-Id` response header, increments `http_requests_total{method,path,status}`, emits one `request_completed` JSON log line with method / path / status / duration_ms. |
| `SENSITIVE_HEADERS` | `src/meho_backplane/middleware.py` | `frozenset({b"authorization", b"cookie", b"x-api-key"})`. The middleware never logs the values of these headers; redaction is enforced by *not* logging request headers at all in v0.1, with a `tests/test_observability.py` regression test. |
| `HTTP_REQUESTS_TOTAL` | `src/meho_backplane/metrics.py` | Module-level `prometheus_client.Counter` registered against the default registry. Labels: `method`, `path`, `status`. `path` is the matched FastAPI route template when available, bounding label cardinality. |
| `render_metrics` | `src/meho_backplane/metrics.py` | Returns `(body, content_type)` for the `/metrics` route. Pins `text/plain; version=0.0.4; charset=utf-8` — the legacy Prometheus format every scraper accepts (`prometheus_client>=0.21` advertises 1.0.0 in `CONTENT_TYPE_LATEST`, but 0.0.4 stays universally compatible). |

## Control flow

1. The container's `CMD` invokes
   `uvicorn meho_backplane.main:app --host 0.0.0.0 --port 8000`.
2. uvicorn imports `meho_backplane.main`, which constructs the
   `FastAPI` instance with the `lifespan` async context manager,
   wraps it in `RequestContextMiddleware`, and mounts the `health` and
   `version` routers via `include_router` alongside the inline `root`
   and `metrics` route handlers.
3. uvicorn opens the lifespan context: `configure_logging()` runs,
   pinning structlog's processor chain so every subsequent log line
   is JSON to stdout.
4. uvicorn binds to `:8000` and starts the ASGI event loop.
5. Each HTTP request enters `RequestContextMiddleware.__call__`,
   which:
   - extracts the incoming `X-Request-Id` header value (or mints a
     fresh `uuid4().hex`),
   - clears any leftover contextvars and binds `request_id`,
   - calls the wrapped FastAPI app,
   - on the first `http.response.start` ASGI message, appends
     `X-Request-Id` to the response headers and captures the status
     code,
   - increments `http_requests_total{method,path,status}` and emits
     a single `request_completed` log line with `duration_ms`.
6. The wrapped app dispatches each request to its route handler:
   - `GET /` → `root()` returns the identity dict.
   - `GET /healthz` → `healthz()` returns `{"status": "ok"}` with 200.
   - `GET /version` → reads `GIT_SHA` / `BUILD_DATE` env vars per
     request (cheap; no caching needed).
   - `GET /ready` → calls `run_probes()` and translates the aggregate
     into a 200 / 503 `JSONResponse`.
   - `GET /metrics` → returns the default registry's exposition text
     directly. The middleware still wraps it, so `/metrics` requests
     show up in the counter (under `path="/metrics"`).

The FastAPI
[lifespan](https://fastapi.tiangolo.com/advanced/events/) hook is the
modern replacement for the deprecated `@app.on_event("startup")`
decorator; it currently performs only the structlog configuration but
is the right seam for the G2.2 Vault client and G2.3 SQLAlchemy
engine setup/teardown. Downstream readiness probes will be registered
from those lifespans: `register_probe("vault", check_vault)`.

## Dependencies

Pinned-floor declarations; exact versions resolved into `uv.lock`.

| Library | Floor | Why it's here |
| --- | --- | --- |
| `fastapi` | ≥ 0.110 | Web framework + OpenAPI 3.1 emission (per ADR 0004). |
| `uvicorn[standard]` | ≥ 0.30 | ASGI server with `httptools` / `websockets` extras. |
| `pydantic` | ≥ 2.6 | Pulled transitively by FastAPI; pinned explicitly so v1 can't be substituted. |
| `structlog` | ≥ 24.1 | JSON-to-stdout logging + `contextvars`-based `request_id` propagation. |
| `prometheus-client` | ≥ 0.20 | Default process / GC collectors + the `http_requests_total` counter exposed on `/metrics`. |
| (dev) `pytest` ≥ 8 | | Test runner. |
| (dev) `pytest-asyncio` ≥ 0.23 | | Async test support; `asyncio_mode = "auto"` in pyproject. |
| (dev) `httpx` ≥ 0.27 | | Backend for `fastapi.testclient.TestClient`. |
| (dev) `ruff` ≥ 0.5 | | Lint + format. |
| (dev) `mypy` ≥ 1.10 | | Strict type checking. |

## Known issues

`/ready` returns 503 in the default chassis state because the probe
registry is empty — this is **fail-closed by design**, not a bug. The
chassis flips to readiness-ready only once G2.2 (Vault/Keycloak) and
G2.3 (Alembic migrations) call `register_probe`. Helm charts pointing
their kubernetes readiness probe at `/ready` before those Initiatives
land will see pods stay `NotReady`; that's the intended contract.

The `Authorization` / `Cookie` / `X-API-Key` redaction guarantee in
`RequestContextMiddleware` is enforced *by omission*: the middleware
never logs request headers at all in v0.1. If a future Initiative
adds header logging, it must filter through `SENSITIVE_HEADERS`
explicitly — there is a regression test
(`tests/test_observability.py::test_sensitive_headers_never_leak_into_logs`)
that asserts no header value ever leaks, but the test only catches
the three named headers, so the contract relies on the omission
discipline rather than a denylist.

`prometheus_client>=0.21` exposes `CONTENT_TYPE_LATEST` as
`text/plain; version=1.0.0; charset=utf-8`. The `/metrics` route
intentionally pins `CONTENT_TYPE_PLAIN_0_0_4` instead, because
version 0.0.4 is supported by every Prometheus deployment in the
wild and Goal #11's acceptance criterion specifies it. When the
ecosystem catches up to the 1.0.0 format, the constant in
`metrics.py` is the one knob to turn.

## References

- ADR 0004 — Stack choice (Python backplane + Go CLI)
- Task #18 — this chassis bootstrap
- Task #19 — public health + version + readiness endpoints
- Task #20 — observability primitives (`/metrics`, structlog, middleware)
- [FastAPI tutorial](https://fastapi.tiangolo.com/tutorial/)
- [FastAPI lifespan API](https://fastapi.tiangolo.com/advanced/events/)
- [Starlette middleware (pure ASGI vs BaseHTTPMiddleware)](https://www.starlette.io/middleware/)
- [structlog contextvars](https://www.structlog.org/en/stable/contextvars.html)
- [prometheus_client](https://github.com/prometheus/client_python)
- [uv project structure](https://docs.astral.sh/uv/concepts/projects/)
- [uv production Docker pattern](https://docs.astral.sh/uv/guides/integration/docker/)
