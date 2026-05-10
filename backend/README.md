# meho-backplane

The MEHO governance-layer backplane (Python / FastAPI). v0.1 ships a
minimum-viable chassis only — health, version, readiness, metrics, and
authn/authz federation are layered on by Tasks #19, #20 and the G2.2 /
G2.3 Initiatives.

Stack choices are locked in [ADR
0004](https://github.com/evoila-bosnia/meho-internal/issues/13)
(Python / FastAPI / Pydantic v2 / SQLAlchemy 2.x async / Alembic).

## Layout

```text
backend/
├── pyproject.toml              # uv-managed; locks deps + tool configs
├── Dockerfile                  # multi-stage uv build, non-root uid 1001
├── .dockerignore
├── src/
│   └── meho_backplane/
│       ├── __init__.py         # __version__ marker
│       └── main.py             # FastAPI `app`, root identity route
└── tests/
    └── test_app_starts.py
```

The `src/`-layout convention prevents tests from accidentally
importing the in-tree source — they only resolve the installed
package.

## Run locally

Requires [uv](https://docs.astral.sh/uv/) ≥ 0.4 and Python 3.12.

```bash
cd backend/
uv sync                                              # resolves deps from uv.lock
uv run ruff check src/ tests/                        # lint
uv run ruff format --check src/ tests/               # format check
uv run mypy src/                                     # type-check
uv run pytest -x                                     # unit tests

uv run uvicorn meho_backplane.main:app --port 8000   # boot the app
curl -s localhost:8000/ | jq .                       # → {"name":"meho-backplane","version":"0.1.0-dev"}
```

## Build and run the container

```bash
cd backend/
docker build -t meho-backplane:dev .
docker run --rm -d -p 8000:8000 --name meho-backplane meho-backplane:dev
curl -s localhost:8000/ | jq .
docker exec meho-backplane id -u                     # → 1001
docker rm -f meho-backplane
```

The image runs as **uid 1001** in the root group (gid 0) so the
runtime filesystem can stay read-only when the orchestrator sets
`readOnlyRootFilesystem: true` (configured in the Helm chart in
G2.5). The base image is `python:3.12-slim`; the runtime stage
contains only the locked virtualenv, no build tools.

## What this skeleton intentionally omits

| Surface          | Lands in       |
| ---------------- | -------------- |
| `/healthz` / `/version` / `/ready` | Task #19   |
| Prometheus `/metrics`              | Task #20   |
| structlog JSON logs + middleware   | Task #20   |
| Keycloak JWT validation            | Initiative G2.2 |
| Vault OIDC, static-cred read       | Initiative G2.2 |
| SQLAlchemy + Alembic               | Initiative G2.3 |
| Multi-arch image, GHCR, cosign     | Initiative G2.4 |
| Helm chart deploying this image    | Initiative G2.5 |
| CI workflow exercising this tree   | Initiative G2.7 |

## References

- Goal #11 — Deployable v0.1
- Initiative #17 — G2.1 Backplane chassis
- Task #18 (this) — backplane Python source-tree bootstrap
- ADR 0003 — SPDX header convention (every authored Python file)
- [FastAPI tutorial](https://fastapi.tiangolo.com/tutorial/)
- [uv project guide](https://docs.astral.sh/uv/concepts/projects/)
- [uv Docker pattern](https://docs.astral.sh/uv/guides/integration/docker/)
