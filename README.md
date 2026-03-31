# MEHO

**AI-powered diagnostic and operations platform that reasons across your entire infrastructure stack.**

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
cp env.example .env   # Add your LLM API key
docker compose up
```

Open [http://localhost:5173](http://localhost:5173) and start investigating.

Default credentials: **admin / admin** (change immediately in production).

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
pip install mkdocs-material neoteroi-mkdocs
mkdocs serve
```

Then visit [http://localhost:8001](http://localhost:8001).

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on reporting issues, submitting changes, code style, and our Contributor License Agreement.

## License

[AGPL-3.0-only](LICENSE). Copyright (c) 2026 evoila Group.
