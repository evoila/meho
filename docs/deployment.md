# Deployment

> Last verified: Goal #294 (post-bootstrap-consolidation)

This guide covers running MEHO in Docker for development and testing. MEHO ships as a Docker Compose stack with a unified backend service, React frontend, and supporting infrastructure (PostgreSQL, Redis, MinIO, Keycloak, Seq).

> **What changed in Goal #294.** The nine per-module Alembic trees were merged into a single tree at `meho_app/alembic/`. `docker-compose.yml` is now an authoritative base file with two thin overrides (`docker-compose.override.yml` for development, `docker-compose.test.yml` for integration tests). The bash `dev-env.sh` wrapper has been replaced by a Typer CLI shipped with the wheel as `meho-dev`. `scripts/dev-env.sh` is kept as a thin compatibility shim that delegates to `meho-dev`.

## Prerequisites

- **Docker** 24+ with Docker Compose v2
- **Git** for cloning the repository
- An **Anthropic API key** (Claude Opus 4.6 / Sonnet 4.6)
- A **Voyage AI API key** (for embeddings and reranking)

## Quick Start

```bash
# Clone the repository
git clone <repository-url>
cd MEHO.X

# Copy environment file and set required secrets
cp env.example .env

# Edit .env -- set these three required values:
#   ANTHROPIC_API_KEY=sk-ant-your-key-here
#   VOYAGE_API_KEY=your-voyage-key-here
#   CREDENTIAL_ENCRYPTION_KEY=$(./scripts/generate-encryption-key.sh --raw)
#
# Or append the full assignment directly to .env (no substitution needed):
#   ./scripts/generate-encryption-key.sh >> .env

# Start everything (builds images, starts services, runs migrations)
docker compose up
```

`docker compose up` automatically loads `docker-compose.override.yml`, which switches the `meho` service to the `debug` build target and exposes the debugpy port (5678). To run the production target locally without those, opt out explicitly: `docker compose -f docker-compose.yml up`.

After startup completes, you will see:

| Service | URL | Credentials |
|---------|-----|-------------|
| Frontend | [http://localhost:5173](http://localhost:5173) | Keycloak login |
| Backend API | [http://localhost:8000](http://localhost:8000) | JWT required |
| API Docs (Swagger) | [http://localhost:8000/docs](http://localhost:8000/docs) | -- |
| Keycloak Admin | [http://localhost:8080](http://localhost:8080) | admin / admin |
| MinIO Console | [http://localhost:9001](http://localhost:9001) | minioadmin / minioadmin |
| Seq (Logs/Traces) | [http://localhost:5341](http://localhost:5341) | -- |
| PostgreSQL | localhost:5432 | meho / password |
| Redis | localhost:6379 | -- |

> **`meho-dev` vs raw Docker Compose.** `docker compose up` is enough for first-run. `meho-dev` (also reachable as `./scripts/dev-env.sh` for muscle-memory) wraps Compose with type-checking before build, automatic Keycloak realm import, an explicit `alembic upgrade head` step against the unified migration tree, and a `local` mode that runs uvicorn + vite on the host. Use raw `docker compose` for one-off operations and `meho-dev` for the standard inner loop.

## Development Modes

### Full Docker (CI/Testing)

```bash
meho-dev up        # Build and start everything (uses docker-compose.yml + override.yml)
meho-dev down      # Stop everything (forwards extra args, e.g. `down --volumes`)
meho-dev restart   # Restart everything
meho-dev logs      # Tail all logs
meho-dev logs meho # Tail backend logs only
meho-dev status    # Show service status
meho-dev test      # Run smoke + contract tests
meho-dev test-all  # Run all tests
```

`meho-dev` is the new Typer CLI introduced in Goal #294 (#310). It ships with the wheel via the `[project.scripts]` entry in `pyproject.toml` and is implemented in `meho_app/tools/dev.py`. The previous `scripts/dev-env.sh` is now a one-line shim that runs `uv run meho-dev "$@"` so existing automation, the Makefile, and `preflight.sh` keep working unchanged.

### Local Hot-Reload (Development)

For active development with instant feedback on code changes:

```bash
meho-dev local
```

This mode runs:

- Infrastructure (PostgreSQL, Redis, MinIO, Keycloak, Seq) in Docker
- Backend locally with `uvicorn --reload` for hot-reload on Python changes
- Frontend locally with `vite dev` for hot-reload on TypeScript/React changes
- Alembic migrations on `localhost:5432` against the unified tree at `meho_app/alembic.ini`

Requires Python 3.13+ (managed by `uv`) and Node.js 20+ installed locally.

## Environment Variables

### Required Secrets

These must be set in `.env` -- Docker Compose will refuse to start without them.

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude models (Opus 4.6, Sonnet 4.6) |
| `VOYAGE_API_KEY` | Voyage AI API key for embeddings (voyage-4-large) and reranking (rerank-2.5) |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet symmetric key for encrypting connector credentials at rest |

### LLM Models (Anthropic)

Two-tier model configuration: Opus 4.6 for heavy reasoning, Sonnet 4.6 for utility tasks.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_MODEL` | `anthropic:claude-opus-4-6` | General reasoning model |
| `STREAMING_AGENT_MODEL` | `anthropic:claude-opus-4-6` | Streaming investigation agent |
| `PLANNER_MODEL` | `anthropic:claude-opus-4-6` | Investigation planner |
| `EXECUTOR_MODEL` | `anthropic:claude-opus-4-6` | Operation executor |
| `INTERPRETER_MODEL` | `anthropic:claude-opus-4-6` | Result interpreter |
| `CLASSIFIER_MODEL` | `anthropic:claude-sonnet-4-6` | Query classifier |
| `DATA_EXTRACTOR_MODEL` | `anthropic:claude-sonnet-4-6` | Data extraction |
| `WORKFLOW_BUILDER_MODEL` | `anthropic:claude-sonnet-4-6` | Workflow generation |
| `TRANSFORM_GENERATION_MODEL` | `anthropic:claude-sonnet-4-6` | Transform generation |
| `WORKFLOW_LLM_REPORT_MODEL` | `anthropic:claude-sonnet-4-6` | Workflow reports |
| `EMBEDDING_MODEL` | `voyage-4-large` | Voyage AI embedding model (1024D) |

### Database (PostgreSQL)

Defaults work with the Docker Compose stack. Override only for external databases.

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_USER` | `meho` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `password` | PostgreSQL password |
| `POSTGRES_DB` | `meho` | Database name |
| `DATABASE_URL` | `postgresql+asyncpg://meho:password@postgres:5432/meho` | Full connection string |

### Redis

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection URL |

### Keycloak (Identity Provider)

| Variable | Default | Purpose |
|----------|---------|---------|
| `KEYCLOAK_ADMIN_PASSWORD` | `admin` | Keycloak admin console password |
| `KEYCLOAK_CLIENT_ID` | `meho-api` | Backend client ID in Keycloak |
| `KEYCLOAK_URL` | `http://localhost:8080` | Keycloak base URL (frontend) |
| `KEYCLOAK_REALM` | `example-tenant` | Keycloak realm name |
| `VITE_KEYCLOAK_URL` | `http://localhost:8080` | Keycloak URL for Vite build |
| `VITE_KEYCLOAK_REALM` | `example-tenant` | Realm for Vite build |
| `VITE_KEYCLOAK_CLIENT_ID` | `meho-frontend` | Frontend client ID |

### Object Storage (MinIO / S3)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OBJECT_STORAGE_BUCKET` | `meho-dev-data` | S3 bucket name |
| `OBJECT_STORAGE_ENDPOINT` | `http://minio:9000` | S3-compatible endpoint |
| `OBJECT_STORAGE_ACCESS_KEY` | `minioadmin` | S3 access key |
| `OBJECT_STORAGE_SECRET_KEY` | `minioadmin` | S3 secret key |
| `OBJECT_STORAGE_USE_SSL` | `false` | Enable HTTPS for S3 |

### Observability (OpenTelemetry)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OTEL_SERVICE_NAME` | `meho` | Service name in traces |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://seq:80/ingest/otlp` | OTLP collector endpoint |
| `OTEL_CONSOLE` | `true` | Also log to console |
| `MEHO_LOG_LEVEL` | `INFO` | Application log level |
| `OTEL_TRACE_LEVEL` | `full` | Trace detail: full, truncated, summary |

### Application Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_ENVIRONMENT` | `dev` | Environment name (dev, staging, production) |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | Allowed CORS origins (JSON array) |

### Feature Flags

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_RATE_LIMITING` | `true` | API rate limiting |
| `ENABLE_MEMORY_EXTRACTION` | `true` | Auto-extract memories from conversations |
| `ENABLE_TRANSCRIPT_PERSISTENCE` | `true` | Persist agent transcripts |
| `ENABLE_DETAILED_EVENTS` | `true` | Send detailed SSE events to frontend |
| `ENABLE_OBSERVABILITY_API` | `true` | Enable observability endpoints |

### Orchestrator Tuning

| Variable | Default | Purpose |
|----------|---------|---------|
| `ORCHESTRATOR_MAX_ITERATIONS` | `3` | Max orchestrator retry iterations |
| `ORCHESTRATOR_AGENT_TIMEOUT` | `30.0` | Per-agent timeout (seconds) |
| `ORCHESTRATOR_TOTAL_TIMEOUT` | `120.0` | Total orchestrator timeout (seconds) |

### Document Ingestion

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEHO_FEATURE_USE_DOCLING` | `true` | Document ingestion backend. `true` = Docling (ML-powered, needs PyTorch/GPU, ~4 GB image). `false` = lightweight pipeline (pymupdf4llm + pdfplumber + RapidOCR, CPU-only, ~500 MB image). |
| `MEHO_FEATURE_EPHEMERAL_INGESTION` | `false` | When `true`, large PDF ingestion is offloaded to ephemeral cloud workers (e.g., Cloud Run) instead of running in-process. Requires coordinator backend configuration. |

## Service Architecture

The Docker Compose stack runs these services:

| Service | Image | Purpose |
|---------|-------|---------|
| **meho** | Custom (Dockerfile.meho) | Unified FastAPI backend -- API, agents, connectors, knowledge, topology |
| **meho-frontend** | Custom (Dockerfile.meho-frontend) | React/TypeScript frontend with Vite |
| **postgres** | pgvector/pgvector:pg15 | PostgreSQL with pgvector extension for hybrid search |
| **redis** | redis/redis-stack-server | Redis with RediSearch and RedisJSON modules |
| **keycloak** | quay.io/keycloak/keycloak:24.0 | Identity provider (OIDC, RBAC, multi-tenant) |
| **minio** | minio/minio | S3-compatible object storage for Parquet cache and documents |
| **seq** | datalust/seq | Log aggregation and OTLP trace ingestion |
| **pgadmin** | dpage/pgadmin4 | Database admin UI (optional, `--profile tools`) |

### Port Map

| Port | Service | Protocol |
|------|---------|----------|
| 5173 | Frontend (Vite) | HTTP |
| 8000 | Backend API (FastAPI) | HTTP |
| 8080 | Keycloak | HTTP |
| 5432 | PostgreSQL | TCP |
| 6379 | Redis | TCP |
| 9000 | MinIO S3 API | HTTP |
| 9001 | MinIO Console | HTTP |
| 5341 | Seq (Logs UI + OTLP) | HTTP |
| 5050 | pgAdmin (tools profile) | HTTP |

## Database Migrations

MEHO uses **Alembic** with a single unified migration tree at `meho_app/alembic/`. All schema changes (agents, connectors, knowledge, topology, memory, ingestion, scheduled tasks, orchestrator skills, audit) live in one linear history with one `alembic_version` table.

| Path | Purpose |
|------|---------|
| `meho_app/alembic.ini` | Authoritative Alembic config -- the only `alembic.ini` in the repo |
| `meho_app/alembic/env.py` | Loads the shared `meho_app.database.Base`; imports all modules' models |
| `meho_app/alembic/versions/` | Linear migration history starting at `0001_init` |
| `scripts/run-migrations-monolith.sh` | Container entrypoint shim: `alembic -c meho_app/alembic.ini upgrade head` |
| `scripts/migrate_to_unified_alembic.py` | One-time rescue for deployments that ran the old per-module trees (see below) |

The pre-Goal-#294 layout shipped nine separate `alembic.ini` files at `meho_app/modules/*/alembic.ini` and nine `alembic_version_meho_*` tables. Those are gone. Anything you read in older docs about cross-module ordering or per-module heads is obsolete.

### Automatic migrations

`docker compose up` starts the `meho` container, which runs `scripts/run-migrations-monolith.sh` as part of its startup. The script is one line:

```bash
uv run alembic -c meho_app/alembic.ini upgrade head
```

`meho-dev up` does the same after a build, and `meho-dev local` runs the equivalent against `localhost:5432`. All three paths fail loud -- a non-zero `alembic` exit aborts startup and `meho_app/main.py` lifespan refuses to serve traffic against a stale schema (#313).

### Manual migrations

```bash
# Inside the Docker container
docker compose exec meho uv run alembic -c meho_app/alembic.ini upgrade head

# On the host (against localhost:5432)
DATABASE_URL=postgresql+asyncpg://meho:password@localhost:5432/meho \
  uv run alembic -c meho_app/alembic.ini upgrade head

# Inspect the current head
uv run alembic -c meho_app/alembic.ini current

# Generate a new migration after editing a model
uv run alembic -c meho_app/alembic.ini revision --autogenerate -m "describe change"
```

There is one head and one revision graph -- no module disambiguation flags.

### Upgrading existing deployments (rescue script)

If you are upgrading a MEHO instance that ran the per-module Alembic layout, you have nine `alembic_version_meho_*` tables and no `alembic_version` row. Running `alembic upgrade head` against that database is a hard error: it does not know what state it is in.

Use the rescue script **once before** starting the new container:

```bash
# Stop the old stack but leave Postgres running
docker compose stop meho meho-frontend

# Run the rescue script against the existing database
DATABASE_URL=postgresql://meho:password@localhost:5432/meho \
  uv run python scripts/migrate_to_unified_alembic.py
```

The script:

1. Verifies all nine legacy `alembic_version_meho_*` tables exist and hold the expected revision IDs.
2. Stamps the new unified `alembic_version` table with the head revision (`0009_doc_family`).
3. Drops the nine legacy tables.

It is idempotent (re-running on a stamped database is a no-op) and fails loud on any unexpected state. Fresh installs skip it entirely. `docker/docker-entrypoint.sh` references the script in a comment but does **not** auto-invoke it -- the rescue is an explicit, operator-driven action.

For the authoring workflow ("how do I add a new migration?") see [docs/contributing/migrations.md](contributing/migrations.md).

## Production Considerations

The Docker Compose development setup is not production-ready as-is. For production deployment, address these areas:

### Secrets Management

- Move `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, and `CREDENTIAL_ENCRYPTION_KEY` to a secrets manager (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault).
- Replace the default PostgreSQL password (`password`) and Keycloak admin password (`admin`).
- Do not use the default MinIO credentials in production.

### HTTPS Termination

- Place a reverse proxy (nginx, Traefik, Caddy, or a cloud load balancer) in front of MEHO to terminate TLS.
- Update `CORS_ORIGINS` to match your production frontend domain.
- Configure HSTS headers at the proxy level for strict transport security.

### Persistent Storage

- Use external managed PostgreSQL (RDS, Cloud SQL, Azure Database) instead of the Docker volume.
- Use external managed Redis (ElastiCache, Memorystore) for production reliability.
- Use external S3-compatible storage instead of local MinIO for Parquet cache durability.

### Keycloak Configuration

- Configure a production Keycloak realm with proper client settings, redirect URIs, and user federation.
- The development setup auto-imports a realm configuration from `config/keycloak/`. Production deployments should manage Keycloak configuration through its admin API or admin console.
- Enable HTTPS for Keycloak and set `KC_HOSTNAME_STRICT: "true"`.

### Scaling

- The MEHO backend is a single-process FastAPI application. For higher throughput, run multiple replicas behind a load balancer.
- The in-process approval flow (asyncio.Event) requires sticky sessions when scaling to multiple workers. For multi-worker deployments, migrate to Redis pub/sub for approval signaling.

### Observability

- Replace Seq with your preferred observability stack. Point `OTEL_EXPORTER_OTLP_ENDPOINT` to your OTLP collector (Jaeger, Grafana Tempo, Datadog, New Relic).
- Set `OTEL_TRACE_LEVEL=summary` in production to reduce trace payload size.
- Set `MEHO_LOG_LEVEL=WARNING` for reduced log volume.

## Slim Docker Image

MEHO supports a slim Docker build that excludes PyTorch and Docling, reducing image size from ~4 GB to ~500 MB. The slim image uses the lightweight document ingestion pipeline (pymupdf4llm, pdfplumber, RapidOCR) instead of Docling.

### When to Use Slim

- **Open-source deployments** without GPU resources
- **Development environments** where fast builds and startup matter
- **Resource-constrained environments** (4-8 GB pod memory limits)
- **CI/CD pipelines** where image pull time impacts deployment speed

### Building the Slim Image

```bash
# Build slim image (no PyTorch, no Docling) — INCLUDE_DOCLING defaults to false
docker build -f docker/Dockerfile.meho --target prod -t meho:slim .

# Build full image (adds docling-group: docling-core, docling-parse, PyTorch)
docker build -f docker/Dockerfile.meho --target prod --build-arg INCLUDE_DOCLING=true -t meho:full .
```

The `INCLUDE_DOCLING` build arg controls what ships in the image:

- **Default (`INCLUDE_DOCLING=false`)** — slim image, ~500 MB. No PyTorch, Docling, or ML model dependencies. Ingestion falls back to the lightweight `pymupdf4llm` + `pdfplumber` path. Runtime env is `MEHO_FEATURE_USE_DOCLING=false`. Suitable for open-source deployments, CI pipelines, and resource-constrained hosts.
- **Opt-in (`INCLUDE_DOCLING=true`)** — full image, ~4 GB. Adds the `docling-group` dependency group (docling, docling-core, docling-parse, PyTorch, torchaudio, torchvision) and sets `MEHO_FEATURE_USE_DOCLING=true` at runtime. Required for Docling-based PDF layout parsing and heading-aware chunking.

GPU builds opt in additionally with `--build-arg TARGETBASE=base-gpu --build-arg CUDA_ENABLED=true` (CUDA 12.4.1 / Ubuntu 22.04). GPU and Docling are orthogonal — any combination is valid.

### Slim vs Full Comparison

| Aspect | Full Image | Slim Image |
|--------|-----------|------------|
| Image size | ~4 GB | ~500 MB |
| PyTorch included | Yes | No |
| PDF extraction | ML-powered (Docling) | Rule-based (pymupdf4llm) |
| Table extraction | Deep learning | Rule-based (pdfplumber) |
| OCR capability | Docling built-in | RapidOCR (CPU-only) |
| Memory (idle) | ~500 MB | ~200 MB |
| Memory (PDF processing) | 6-22 GB peak | ~250 MB |
| All other features | Full | Full |

!!! note "Slim image is fully functional"
    The slim image has all MEHO features except ML-powered document ingestion. Connectors, agent investigation, topology, knowledge search, and all other capabilities work identically.

## Ephemeral Ingestion Worker

For environments that need Docling's ML-powered quality but cannot run PyTorch in the main MEHO container, an ephemeral ingestion worker can offload PDF conversion to a separate, short-lived process (e.g., Google Cloud Run job, Kubernetes Job).

Enable with `MEHO_FEATURE_EPHEMERAL_INGESTION=true`. See [Document Ingestion Worker Architecture](architecture/document-ingestion-worker.md) for the full design.

The worker:

- Runs Docling with PyTorch in a dedicated container with 16-32 GB memory
- Receives PDFs via object storage (MinIO/S3), converts them, writes chunks back
- Terminates after completion (no persistent resource cost)
- Uses Bearer token authentication for coordinator communication
