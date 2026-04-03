# Roadmap

> Last verified: v2.0

## Current State: v2.0

MEHO v2.0 represents a comprehensive platform with cross-system diagnostic intelligence, 15 connector types, and a production-ready deployment model.

### What's Shipped

MEHO has gone through extensive development across 10 milestones, delivering:

- **Cross-system reasoning** -- trace problems across Kubernetes, cloud infrastructure, observability stacks, CI/CD pipelines, and collaboration tools in a single conversation
- **15 connector types** -- Infrastructure (Kubernetes, VMware, Proxmox, GCP), Observability (Prometheus, Loki, Tempo, Alertmanager), CI/CD (ArgoCD, GitHub), Collaboration (Jira, Confluence, Email), and Generic (REST via OpenAPI, SOAP via WSDL)
- **Trust model** -- three-tier classification (READ/WRITE/DESTRUCTIVE) with approval workflows and audit trail for every operation
- **Intelligent data pipeline** -- JSONFlux engine with Arrow tables, Parquet caching, and DuckDB SQL reduction that keeps LLM context manageable even with large datasets
- **Topology auto-discovery** -- automatic entity extraction and cross-system resolution (providerID, IP, hostname matching) that builds a connected infrastructure graph
- **Dual-mode chat** -- Ask mode for knowledge base Q&A, Agent mode for cross-system investigation
- **Three-tier knowledge architecture** -- global, connector-type, and connector-instance scoping with hybrid search (BM25 + semantic) and Voyage AI reranking
- **Token optimization** -- 81% cumulative reduction through observation compression, sliding windows, stateful loop management, and step budgets
- **Security hardening** -- Content Security Policy, HSTS, CORS lockdown, memory-only auth tokens, Keycloak OIDC integration
- **Investigation visualization** -- hypothesis tracking, citations, breadcrumb navigation, and topology animation showing investigation paths

### v2.0 Stabilization

The current stabilization milestone focused on hardening what's built:

- **Repository cleanup** -- archived planning artifacts, resolved all ESLint and Ruff warnings, updated pre-commit hooks
- **Testing infrastructure** -- layered test conftest, PydanticAI test model integration, verified test suites
- **Bug fixes** -- resolved critical and major bugs, implemented AWS/Azure providerID parsing, removed dead code and stale references
- **Documentation** -- complete documentation rewrite covering all features, all connectors, and deployment guides

---

## What's Next

### Testing Expansion

Expanding automated test coverage beyond the current baseline:

- **End-to-end test suite** -- automated browser tests covering all user journeys (currently 5 Playwright specs, expanding to full coverage)
- **CI pipeline** -- GitHub Actions with automated test gates, coverage thresholds, and deployment checks
- **LLM evaluation** -- automated assessment of agent response quality using LLM-as-judge evaluation pipelines
- **Performance baselines** -- regression testing for critical paths (agent response time, data pipeline throughput)

### Internal Documentation

Engineering reference material for the development team:

- **Architecture decision records** -- extracting and documenting the key decisions made during development
- **Connector development guide** -- step-by-step guide for adding new connector types to the platform
- **Agent prompt engineering guide** -- best practices for authoring skills and tuning agent behavior

### Performance Optimization

Fine-tuning the platform for production workloads:

- **Query optimization** -- database query analysis and indexing improvements
- **Caching strategy** -- intelligent cache invalidation and pre-warming for frequently accessed data
- **Streaming performance** -- SSE connection management and message delivery optimization

### New Connector Types

Expanding platform coverage based on customer needs:

- Additional cloud providers (AWS, Azure native connectors)
- Database connectors (PostgreSQL, MySQL, MongoDB)
- APM integrations (Datadog, New Relic, Dynatrace)
- Container orchestration (Docker Swarm, Nomad)

Each new connector type follows the established pattern: typed connector class, operation definitions with trust classification, topology entity extraction, and auto-generated skills.

### Platform Features

Capabilities under consideration for future milestones:

- **Investigation replay** -- post-investigation timeline scrubber for reviewing past diagnostic sessions
- **Scheduled reports** -- automated periodic investigations with report delivery
- **Team collaboration** -- shared investigation sessions with real-time presence
- **Custom dashboards** -- saved query templates for recurring diagnostic patterns

---

## Contributing

MEHO's roadmap is driven by real-world operational needs. If you have suggestions for new connector types, features, or improvements, open an issue on GitHub or join our Discord community.
