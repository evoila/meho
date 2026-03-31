# Features

> Last verified: v2.3 (Phase 101)

MEHO ships a comprehensive set of capabilities for AI-powered infrastructure diagnostics and operations. This page organizes features by capability area, not by release version.

## Cross-System Investigation

MEHO's core value: diagnose problems across your entire stack in a single conversation.

- **Unified SpecialistAgent** with injectable markdown skills -- new system expertise is a markdown file, not Python code
- **ReAct reasoning loop** as the primary execution engine -- preserves raw connector data at every step, never summarizes it away
- **Cross-system entity resolution** linking entities across connectors (deterministic-first with LLM fallback)
- **Cross-layer diagnostic traversal** via SAME_AS topology edges -- a Kubernetes pod maps to a VM maps to a hypervisor host
- **Hypothesis tracking** with citations, confidence levels, and investigation breadcrumbs
- **Routing classification** -- the orchestrator classifies every query as quick, standard, or deep in its existing routing LLM call (zero extra latency), adapting specialist count and step budgets per complexity tier (v2.3)
- **Investigation plan visibility** -- before dispatching specialists, the orchestrator emits a visible plan showing classification, reasoning, planned systems, and estimated calls via a new `orchestrator_plan` SSE event (v2.3)
- **Memory-as-tool** -- auto-extracted operator memories are removed from the specialist system prompt and available on-demand via a `recall_memory` tool, reducing prompt bloat while preserving memory access when needed. Operator-provided memories remain always in the prompt (v2.3)
- **Budget-aware specialists** -- specialists self-regulate across 4 budget regimes based on remaining steps, balancing thoroughness with efficiency (v2.3)
- **Investigation visualization** with three-layer density control, hypothesis display, and topology path animation
- **Error classification system** for systematic categorization of infrastructure issues

## Connector Framework

Connect to any system in your infrastructure through typed connectors or generic specs.

- **20 connectors** across infrastructure (Kubernetes, VMware, Proxmox, GCP, AWS, Azure), observability (Prometheus, Loki, Tempo, Alertmanager), CI/CD (ArgoCD, GitHub), collaboration (Jira, Confluence, Email, Slack), integration (MCP Client, MCP Server), and generic (REST, SOAP) categories
- **Shared base classes** for connector families -- ObservabilityHTTPConnector, AtlassianHTTPConnector, ArgoHTTPBase, GitHubHTTPBase
- **Auto-generated markdown skills** from OpenAPI specs -- MEHO learns a system's API surface automatically
- **Connector-scoped knowledge and memory** with auto-extraction from conversation context
- **Connector-type operation sharing** with instance-level overrides for customization
- **Orchestrator dispatches** queries to multiple connectors in parallel
- **MCP Client connector** -- connect to any MCP server and dynamically discover its tools at runtime (v2.3)
- **MCP Server endpoint** -- expose MEHO's investigation tools to other AI systems via the Model Context Protocol at `/mcp` (v2.3)
- **VMware Cloud Foundation (VCF)** -- 209 operations across 10 categories including vSAN health, NSX micro-segmentation, SDDC lifecycle, and capacity planning with separate API credentials per subsystem (v2.3)
- **Generic REST connector** via OpenAPI spec ingestion -- connect to any REST API
- **Generic SOAP connector** via WSDL service definitions -- connect to legacy enterprise systems

## Trust & Safety Model

Every action MEHO takes is classified, controlled, and auditable.

- **Three-tier trust classification**: READ (safe, always allowed), WRITE (requires confirmation), DESTRUCTIVE (requires explicit approval)
- **Approval modal** for write and destructive operations with full context display
- **Complete audit trail** of every action taken, every approval granted, every operation executed
- **Keycloak JWT auth** on all API routes with role-based access control
- **Credential encryption at rest** -- connector credentials are never stored in plaintext

See [Trust & Safety](trust-and-safety.md) for full details.

## Data Pipeline

MEHO doesn't do naive LLM calls. The JSONFlux pipeline processes infrastructure data intelligently so the reasoning engine works with precise, relevant data instead of drowning in raw JSON.

- **Shape detection** -- automatically classifies API responses (single object, list-of-dicts, wrapped collection, nested structures)
- **Arrow table conversion** with smart column typing and conflict resolution
- **Parquet caching** for efficient multi-turn conversations
- **DuckDB SQL reduction** -- the LLM queries cached data with SQL to get exactly what it needs
- **Token-aware tiering** -- small responses go inline, large responses require SQL (prevents context overflow and hallucination)

The full technical story: **[How MEHO Handles Data](how-it-works.md)**

## Topology Auto-Discovery

Every connector query enriches a live topology graph that MEHO uses for cross-system reasoning.

- **Automatic entity extraction** from connector responses -- pods, VMs, hosts, services, and more
- **Deterministic entity resolution**: providerID matching (GCE, AWS, Azure), IP address matching, hostname normalization
- **LLM fallback** for ambiguous entity resolution cases
- **Cross-connector linking** -- entities from different systems connected via SAME_AS edges
- **React Flow visualization** with elkjs layout, swim lanes by connector, and investigation path animation
- **Entity types** tracked across Kubernetes (10+ types), VMware (6 types), Proxmox (4 types), GCP (6 types), ArgoCD, GitHub, and Prometheus

## Knowledge Architecture

Three-tier knowledge system with hybrid search and AI-powered reranking.

- **Three-tier scoping**: global knowledge, connector-type knowledge, and connector-instance knowledge
- **Day-one value** -- upload documentation and it's immediately available to all connectors of that type
- **Hybrid search**: BM25 full-text + pgvector semantic search (Voyage AI voyage-4-large, 1024D embeddings)
- **Voyage AI rerank-2.5** post-retrieval reranking for 15-30% precision improvement
- **Docling-powered document processing** -- structure-aware chunking that preserves headings, sections, and tables with TOC filtering and heading path enrichment (v2.3)
- **Lightweight CPU-only pipeline** -- `MEHO_FEATURE_USE_DOCLING=false` activates a PyTorch-free pipeline (pymupdf4llm + pdfplumber + RapidOCR) producing the same output shape as Docling, enabling ~500MB Docker images without GPU (v2.3)
- **Ephemeral ingestion worker** -- optionally offload heavy Docling PDF processing to short-lived cloud workers (Cloud Run, K8s Jobs) while keeping the main MEHO container lightweight (v2.3)
- **PDF and URL ingestion** -- upload PDFs or point to URLs for automatic knowledge extraction
- **Auto-extraction** from conversation context -- MEHO learns from every interaction

## Network Diagnostics

Built-in SRE tools for connectivity troubleshooting, used automatically by the agent during investigations.

- **Four diagnostic tools**: `dns_resolve` (DNS records including SRV/MX/CNAME), `tcp_probe` (port connectivity + latency), `http_probe` (full HTTP/HTTPS endpoint check), `tls_check` (certificate chain inspection)
- **Topology-first discovery** -- probe results emit topology entities (ExternalURL, IPAddress, TLSCertificate) for cross-system correlation
- **Feature flag controlled** via `MEHO_FEATURE_NETWORK_DIAGNOSTICS`
- **Zero connector configuration required** -- the agent probes endpoints directly from the MEHO server

See [Network Diagnostics](features/network-diagnostics.md) for full documentation.

## CI Quality Gates

Automated code quality and security enforcement for the open-source codebase.

- **SonarCloud** quality gate blocks PRs introducing new issues, with "Clean as You Code" approach
- **Semgrep SAST** with Python and TypeScript rulesets, SARIF results in GitHub Code Scanning
- **Dependency vulnerability scanning** via pip-audit (Python) and npm audit (frontend)
- **Codecov** coverage tracking with 80% patch coverage requirement on PRs
- **License compliance** checking for AGPL-3.0 compatibility across all dependencies
- **SPDX headers** on all Python source files, enforced by pre-commit hook
- **Ruff quality rules**: C901 complexity (max 15), PERF, ERA, TCH rule sets

See [CI Quality Gates](configuration/ci-quality-gates.md) for full documentation.

## Chat Experience

Purpose-built chat interface for infrastructure investigation.

- **Dual-mode chat**: Ask mode for instant knowledge Q&A, Agent mode for full connector-backed investigations
- **@connector mentions** -- direct queries to specific connectors inline (e.g., `@kubernetes list pods in production`)
- **/recipe slash commands** -- pre-built investigation templates for common scenarios
- **Context monitoring** with token budget visualization and automatic compaction when context grows large
- **SSE streaming** of agent events to the frontend with automatic reconnection
- **Markdown rendering** with syntax highlighting, tables, and structured investigation output
- **Message persistence** -- conversations survive page reloads and browser sessions

## Session Management

Collaborative investigation sessions with real-time state.

- **Group sessions** -- multiple operators can participate in the same investigation
- **War rooms** for incident response collaboration
- **Session state management** with entity tracking, UUID correction, and error learning
- **Chat session management** with full message history and transcript persistence

## Proactive Capabilities

MEHO doesn't just respond -- it can act on triggers.

- **Events system** -- receive HTTP events from any connected system and trigger investigations, with optional response channels to post results back (renamed from webhooks in v2.3)
- **Scheduled tasks** with cron-based triggers -- MEHO creates sessions with prompts, the agent decides the action
- **Agent-driven change correlation** -- skill-guided analysis, no separate correlation engine
- **Feature flags** for module control -- disable optional modules (knowledge, topology, events, memory, scheduled tasks) at startup via environment variables

## Security

Enterprise-grade security posture.

- **Content Security Policy (CSP)** in report-only mode for safe rollout
- **HTTP Strict Transport Security (HSTS)** enforced
- **CORS lockdown** to trusted origins only
- **Memory-only auth tokens** -- keycloak-js with direct integration, no tokens in localStorage (XSS mitigation)
- **Keycloak OIDC** for multi-tenant authentication with RBAC
- **Credential encryption at rest** for all connector credentials
- **OTEL observability** with full transcript persistence for debugging and audit

See [Security](security.md) for full details.

## Accessibility

Baseline accessibility for keyboard and screen reader users.

- **ARIA landmarks** on all major page sections
- **Focus traps** for modals and dialogs
- **Keyboard navigation** throughout the application
- **jsx-a11y lint rules** enforced at build time

## Frontend Architecture

Modern React frontend optimized for real-time investigation workflows.

- **Zustand v5** with 4 typed slices replacing 15 useState hooks and 6 useRef stale-closure hacks
- **React Flow** for topology graph visualization with elkjs layered layout
- **TanStack Table** for data grid display and **TanStack Query** for server state management
- **motion/react** for investigation animations and UI transitions
- **use-stick-to-bottom** for proper streaming scroll behavior with pause-on-user-scroll

## Token Optimization

81% cumulative token reduction across four optimization layers.

- **Server-side data reduction** -- only relevant data reaches the LLM
- **Token-aware response tiering** -- inline for small data, SQL-required for large
- **Smart schema summaries** -- LLM sees structure, not raw data
- **Layered optimization**: 50% initial reduction, compounding through context management to 81% total
