# MEHO

**AI-powered diagnostic and operations platform that reasons across your entire infrastructure stack.**

> **Pre-1.0 stability**: MEHO is in the SemVer `0.x` series. Per [Semantic Versioning §4](https://semver.org/spec/v2.0.0.html#spec-item-4), the public API is not yet stable — API endpoints, configuration keys, license-token payload schemas, and image-tag conventions may change between any two `0.MINOR` releases. For production deployments, pin to a specific `<major>.<minor>.<patch>` image tag (e.g. `ghcr.io/evoila/meho-backend:0.1.0`) rather than `latest` or a floating `0.1` tag. Breaking changes are documented in [CHANGELOG.md](CHANGELOG.md).

> **Image provenance**: every published image is signed with cosign keyless OIDC and accompanied by a CycloneDX SBOM. See [Security & Data Handling — Supply chain & image provenance](docs/security.md#supply-chain--image-provenance) for the verify command.

[![CI](https://github.com/evoila/meho/actions/workflows/ci.yml/badge.svg)](https://github.com/evoila/meho/actions/workflows/ci.yml)
[![Security Scan](https://github.com/evoila/meho/actions/workflows/security-scan.yml/badge.svg)](https://github.com/evoila/meho/actions/workflows/security-scan.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=evoila_meho&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=evoila_meho)
[![Coverage](https://codecov.io/gh/evoila/meho/branch/main/graph/badge.svg)](https://codecov.io/gh/evoila/meho)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io%2Fevoila%2Fmeho--backend-blue)](https://ghcr.io/evoila/meho-backend)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)

## What is MEHO?

MEHO (Machine Enhanced Human Operator) connects to your infrastructure -- Kubernetes, VMware, Prometheus, Loki, ArgoCD, Jira, and more -- and uses AI agents to diagnose problems across system boundaries in a single conversation.

Ask MEHO why your pods are crashing and it will trace from application logs through service mesh to Kubernetes events, node resources, underlying VM metrics, and the Git commit that caused it -- connecting the dots you'd normally spend hours on.

Think Claude Code, but instead of reasoning across files in a codebase, MEHO reasons across systems in an infrastructure.

## Quick Start

**Prerequisites:** Docker and Docker Compose, plus an LLM API key (Anthropic recommended; OpenAI and Ollama also supported).

```bash
git clone https://github.com/evoila/meho.git
cd meho
cp env.example .env                       # add your LLM API key + run scripts/generate-encryption-key.sh
docker compose up                         # base + auto-loaded docker-compose.override.yml
```

Open [http://localhost:5173](http://localhost:5173) and start investigating.

Default credentials: **admin / admin** (change immediately in production).

The dev override (`docker-compose.override.yml`) loads automatically. To run the production target locally without the debugger port, opt out explicitly: `docker compose -f docker-compose.yml up`.

For a guided 15-minute walkthrough -- including troubleshooting, hot-reload mode, and the `meho-dev` CLI -- see [docs/getting-started.md](docs/getting-started.md).

### Day-to-day commands

The `Makefile` is a thin discovery layer over the underlying tools (`meho-dev`, `uv`, `npm`). `make help` lists every common workflow:

| Command | What it does |
|---|---|
| `make install` | Install backend dev + test dependencies via `uv` |
| `make dev-up` | Build images, start the full Docker stack, run migrations |
| `make dev-down` | Stop services. Pass `ARGS='--volumes'` to wipe state (`make dev-down ARGS='--volumes'`) |
| `make dev-local` | Run infra in Docker; backend (uvicorn) and frontend (vite) on the host with hot-reload |
| `make logs` / `make status` | Tail logs / `docker compose ps` |
| `make test` / `make test-unit` / `make test-integration` | Pytest suites |
| `make lint` / `make lint-fix` | Ruff + mypy + ESLint (read-only / autofix) |
| `make typecheck` | mypy + tsc |
| `make ci` | Run every gate CI runs locally (lint + typecheck + unit tests + frontend tests + env-example sync) |
| `make verify` | Goal #294 success-signal greps + Alembic head check + backend health probe |
| `make clean` | Remove Python caches and build artifacts |

`make` and `meho-dev` are equivalent surfaces over the same Typer CLI -- use whichever fits muscle memory. `meho-dev --help` lists the same subcommands directly. The legacy `./scripts/dev-env.sh` is now a one-line shim that delegates to `meho-dev`, so existing automation keeps working unchanged.

### Upgrading from the per-module Alembic layout

If you are pulling MEHO over an existing deployment that ran nine `alembic_version_meho_*` tables, run the rescue script **once before** starting the new stack:

```bash
DATABASE_URL=postgresql://meho:password@localhost:5432/meho \
  uv run python scripts/migrate_to_unified_alembic.py
```

The script verifies the legacy revisions, stamps the new unified `alembic_version`, and drops the old tables. Fresh installs skip this entirely.

## Supported Connectors

All 15 connectors are included in the open-source edition.

| Stack | Connectors |
|-------|------------|
| **Infrastructure** | Kubernetes, VMware vSphere, Proxmox VE, Google Cloud Platform |
| **Observability** | Prometheus, Loki, Tempo, Alertmanager |
| **CI/CD** | ArgoCD, GitHub (repos, PRs, Actions, deployments) |
| **Collaboration** | Jira, Confluence, Email (IMAP/SMTP) |
| **Generic** | REST (any OpenAPI spec), SOAP (any WSDL) |

Each connector provides typed operations with trust classification (READ/WRITE/DESTRUCTIVE), topology entity extraction, and natural-language query support.

## Architecture Overview

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.13+, FastAPI, SQLAlchemy, Alembic |
| **Frontend** | React 19, TypeScript, Vite, TailwindCSS, Zustand v5 |
| **AI Agents** | PydanticAI with ReAct loop, cross-system investigation skills |
| **Database** | PostgreSQL 15 with pgvector for hybrid search |
| **Cache** | Redis |
| **Auth** | Keycloak OIDC with JWT validation |
| **Topology** | React Flow + elkjs layout engine |
| **Embeddings** | Local TEI (bge-m3) by default, Voyage AI optional |
| **Storage** | MinIO (S3-compatible) for document uploads |

## LLM Providers

MEHO supports multiple LLM providers. Set `MEHO_LLM_PROVIDER` in your `.env` file:

| Provider | Config | Notes |
|----------|--------|-------|
| **Anthropic Claude** (recommended) | `ANTHROPIC_API_KEY=sk-ant-...` | Best reasoning quality. Opus 4.6 for investigation, Sonnet 4.6 for utility tasks. |
| **OpenAI** | `OPENAI_API_KEY=sk-...` | GPT-4o. Good alternative. |
| **Ollama** | `OLLAMA_BASE_URL=http://host.docker.internal:11434` | Fully local. No API keys needed. See [Ollama setup](https://ollama.com/download). |

See `env.example` for the full configuration reference.

## Key Features

- **Cross-system investigation** -- trace problems across infrastructure, observability, CI/CD, and collaboration tools in one conversation
- **Intelligent data pipeline** -- JSONFlux normalizes large API responses into Arrow tables, caches as Parquet, uses DuckDB SQL reduction to keep LLM context manageable
- **Topology auto-discovery** -- builds a connected infrastructure graph with cross-system entity resolution via SAME_AS edges
- **Trust model** -- three-tier operation classification (READ/WRITE/DESTRUCTIVE) with approval workflows and audit trail
- **Dual-mode chat** -- Ask mode for knowledge base Q&A, Agent mode for cross-system investigation
- **Three-tier knowledge** -- global, connector-type, and connector-instance scoped knowledge with hybrid search (BM25 + semantic) and reranking
- **Local embeddings** -- TEI sidecar with bge-m3 runs locally by default. No cloud embedding service required.

## Community vs Enterprise

| | Community (open-source) | Enterprise |
|---|---|---|
| **Connectors** | All 15 | All 15 |
| **Investigation** | Full cross-system reasoning | Full cross-system reasoning |
| **Topology** | Auto-discovery + entity resolution | Auto-discovery + entity resolution |
| **Knowledge** | Hybrid search + local embeddings | Hybrid search + managed embeddings |
| **Auth** | Single-user (Keycloak) | Multi-tenant SSO/SAML |
| **Audit** | -- | Export + compliance reporting |
| **Collaboration** | -- | Group sessions, team features |
| **Support** | Community | Priority SLA |

The community edition is fully functional for individual operators. Enterprise adds organizational features: multi-tenancy, corporate SSO, audit compliance, and team collaboration.

Learn more at [https://meho.ai](https://meho.ai).

## Documentation

Full documentation is available at [https://docs.meho.ai](https://docs.meho.ai).

To preview locally:

```bash
uv tool install --with neoteroi-mkdocs mkdocs-material
mkdocs serve
```

Then visit [http://localhost:8001](http://localhost:8001).

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on reporting issues, submitting changes, code style, and our Contributor License Agreement. New contributors should also read [docs/getting-started.md](docs/getting-started.md) for the 15-minute onboarding walkthrough and [docs/contributing/migrations.md](docs/contributing/migrations.md) before authoring their first database migration.

MEHO is developed in a private repository and mirrored here on every green build of `main`. Maintainers with access to the private repo should read [docs/development/dual-repo-workflow.md](docs/development/dual-repo-workflow.md) for the mirror pipeline, release procedure, and incident playbook.

## License

[AGPL-3.0-only](LICENSE). Copyright (c) 2026 evoila Group.
