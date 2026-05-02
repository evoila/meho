# Changelog

All notable changes to MEHO are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No unreleased changes yet. New entries land here under Keep a Changelog
subsections (Added, Changed, Deprecated, Removed, Fixed, Security) and
graduate to a versioned section at the next release._

## [0.1.0] - 2026-05-02

Initial public release under AGPLv3. MEHO (Machine Enhanced Human Operator) is
an AI-powered diagnostic and operations platform that reasons across an entire
infrastructure stack through typed connectors and an LLM-powered ReAct agent.
This entry is the OSS baseline — it describes what the product does at launch
rather than enumerating every private milestone that preceded it.

### Added

#### Agent and investigation engine

- ReAct-loop agent with orchestrator → specialist → tool hierarchy, real-time
  streaming of reasoning via SSE, hypothesis tracking, and citation maps
- Cross-system entity resolution with deterministic `SAME_AS` topology edges
  linking Kubernetes nodes, cloud VMs, and observability targets without
  LLM involvement
- Automated identity delegation — investigations run with the triggering
  user's credentials via a deterministic resolver (service → delegated → fail)
  with full audit trail
- Per-connector knowledge base and memory system with Voyage AI embeddings,
  cosine-similarity deduplication, and automatic post-conversation memory
  extraction
- Investigation skills platform — tenant-scoped playbooks, AI-generated skills
  from OpenAPI specs, and two-panel CRUD UI
- Evaluation framework with 4-dimension scoring rubric, 7 anti-pattern
  catalog, and guided TDD workflows

#### Connectors (15+)

- **Observability**: Prometheus, Loki, Tempo, Alertmanager
- **Container platforms**: Kubernetes, VMware (vSphere / NSX / SDDC / vSAN)
- **Cloud providers**: GCP, AWS, Azure
- **CI/CD**: GCP Cloud Build + Artifact Registry, ArgoCD, GitHub
- **Collaboration**: Jira, Confluence, Slack, Email (SMTP / SendGrid /
  Mailgun / SES / generic HTTP)
- **Protocols**: Model Context Protocol (MCP) client for consuming external
  tools, MCP server for exposing investigation tools to IDE extensions and
  external agents
- Built-in network diagnostic tools (`dns_resolve`, `tcp_probe`, `http_probe`,
  `tls_check`) with topology entity emission as persistent breadcrumbs

#### Operator experience

- Group investigation sessions with real-time SSE fan-out via Redis pub/sub,
  private/group/tenant visibility, and collaborative war-room mode
- Bidirectional event system with HMAC-SHA256 signature verification, Jinja2
  prompt templating, per-connector response-channel formatters, and
  auto-session creation
- Scheduled investigations via APScheduler + PostgreSQL with
  natural-language-to-cron translation
- Topology graph with `elkjs` layout, tiered swim lanes, interactive
  `SAME_AS` suggestion review, and investigation-path animation
- Real-time investigation journey UI — streaming orchestrator narrative,
  sticky narrative timeline, connector cards with operation-aware labels,
  three-layer response density (summary / reasoning / data)
- `@connector` mentions, `/recipe` slash commands, and context-monitoring
  compaction triggers in the chat input

#### Platform and operations

- Open-core licensing with Ed25519-signed keys gating enterprise features;
  edition-aware frontend
- Zero-config single-user quickstart with a pre-configured Keycloak realm
  and guided tour
- Multi-LLM routing — Anthropic Claude, OpenAI, and Ollama via a centralized
  model-settings factory
- Local embeddings via Text Embeddings Inference (`bge-m3`) and a local
  reranker — no cloud dependency required
- Ephemeral cloud ingestion worker for Docling PDF conversion, offloaded via
  GCS-mediated Cloud Run Jobs to reclaim PyTorch memory reliably
- Three-tier health monitoring (`/health`, `/ready`, `/status`) with
  connector reachability badges
- Business-logic observability via OpenTelemetry span hierarchy with
  end-to-end trace correlation and token-usage telemetry
- MkDocs Material documentation site with per-connector setup guides, an
  architecture overview, and a full OpenAPI reference

### Changed

- LLM runtime switched to Anthropic Claude with prompt caching, stateful
  message history, and dynamic step budgets (~78 % cumulative token reduction
  per investigation versus the earlier stateless baseline)
- Data pipeline is Arrow/DuckDB end-to-end; the legacy `pandas`
  data-reduction path and dependency have been removed
- 40+ Alembic migrations squashed to one per module, with two-path
  fresh/existing detection and an operator stamp script
- Webhook subsystem renamed to events, with a `response_config` column and
  per-connector response formatters for bidirectional event processing

### Security

- AGPLv3 license text at repo root; SPDX license headers on every source file
- Multi-tenant isolation hardened across every API layer — JWT authentication,
  authorization, tenant scoping, IDOR protection, and SSRF guards
- Semgrep SAST integrated with triaged findings and documented false-positive
  suppressions
- Secret scanning (gitleaks) and Python + npm license inventories enforced
  in CI
- Graduated trust model — READ operations run automatically, WRITE operations
  require explicit approval, with full audit trail
- Memory-only auth tokens (no `localStorage`), CSP, HSTS, and standard
  security headers on the frontend
- HMAC-SHA256 signature verification on inbound event endpoints

[Unreleased]: https://github.com/evoila/meho/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/evoila/meho/releases/tag/v0.1.0
