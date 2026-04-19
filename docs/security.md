# Security & Data Handling

> Last verified: v2.0

MEHO is designed for production environments where security is non-negotiable. This page documents the security posture hardened across v1.64-v1.72, covering authentication, authorization, transport security, data handling, and credential management.

For the operational trust model (READ/WRITE/DESTRUCTIVE classification and approval flow), see [Trust & Safety](trust-and-safety.md).

## Authentication

MEHO uses **Keycloak** as its identity provider, implementing OpenID Connect (OIDC) for both the backend API and the frontend application.

**Backend (FastAPI):**

- Every API route requires a valid JWT token. Unauthenticated requests receive a 401 response.
- Tokens are validated against the Keycloak server on every request. There is no local token cache that could serve stale permissions.
- The `KEYCLOAK_CLIENT_ID` configuration determines which Keycloak client the backend validates against.

**Frontend (React):**

- Authentication is handled by **keycloak-js** (the official Keycloak JavaScript adapter), replacing the abandoned `@react-keycloak/web` wrapper.
- keycloak-js was chosen specifically because the third-party wrapper was abandoned for 5+ years and introduced unnecessary abstraction over a well-documented official library.

**Token Storage:**

- Auth tokens are stored **in memory only**. They are not persisted to `localStorage`, `sessionStorage`, or cookies.
- This is a deliberate XSS mitigation: even if an attacker achieves script injection, they cannot exfiltrate authentication tokens from browser storage.
- The trade-off is that tokens do not survive page refreshes. keycloak-js handles silent token renewal via iframe-based refresh, so this is transparent to the operator.

## Authorization

**Multi-tenant architecture:**

- Every data model in MEHO is scoped by `tenant_id`. Queries always filter by the authenticated user's tenant.
- Connectors, sessions, knowledge, topology, and audit logs are all tenant-isolated.
- A user in one tenant cannot access another tenant's connectors, investigation sessions, or data.

**Role-based access control (RBAC):**

- Keycloak RBAC manages user roles and permissions.
- Connector access is scoped -- operators can only interact with connectors registered within their tenant.

**Connector-scoped permissions:**

- Each connector instance has its own credentials, operation list, and trust classifications.
- Connector-type-level operations can be shared across instances, with per-instance overrides for customization.

## Transport Security

**HTTPS:**

- MEHO is designed to run behind an HTTPS-terminating reverse proxy in production. The Docker Compose development setup uses HTTP for local development convenience.

**HSTS (HTTP Strict Transport Security):**

- HSTS headers instruct browsers to always use HTTPS for MEHO, preventing protocol downgrade attacks.

**CORS (Cross-Origin Resource Sharing):**

- CORS is locked down to specific allowed origins via the `CORS_ORIGINS` environment variable.
- The default configuration (`["http://localhost:5173"]`) allows only the local frontend. Production deployments must set this to the exact frontend domain.
- Wildcard origins (`*`) are not used.

## Content Security Policy

MEHO ships with a **Content Security Policy (CSP)** header that restricts what resources the browser can load:

- **Script sources** are restricted to prevent inline script injection (XSS).
- **Style sources** are controlled to prevent style-based attacks.
- **Frame ancestors** prevent clickjacking by controlling which sites can embed MEHO in an iframe.

CSP is deployed in **report-only mode** initially to identify violations without breaking functionality. This allows safe rollout before switching to enforcement mode.

!!! info "CSP Report-Only Mode"
    Report-only mode was chosen for the initial rollout to avoid breaking existing functionality. Violations are logged but not blocked, allowing the team to identify and fix any legitimate resources that would be blocked by strict CSP before enabling enforcement.

## Credential Encryption

Connector credentials (API tokens, service account keys, passwords) are **encrypted at rest** using Fernet symmetric encryption:

- The `CREDENTIAL_ENCRYPTION_KEY` environment variable provides the encryption key.
- Credentials are encrypted before writing to PostgreSQL and decrypted only when needed for connector operations.
- The encryption key is never stored in the database -- it exists only as an environment variable.
- Credential decryption happens in-process, in memory. Decrypted credentials are not written to disk or logged.

!!! warning "Encryption Key Management"
    The `CREDENTIAL_ENCRYPTION_KEY` must be kept secret and backed up securely. Losing this key means all stored connector credentials become unrecoverable. In production, use a secrets manager (HashiCorp Vault, AWS Secrets Manager, etc.) to manage this key.

## Data Handling

MEHO processes potentially large datasets from connected systems. The data handling pipeline is designed to minimize exposure:

**Raw data lifecycle:**

1. **Connector response** -- Raw JSON/XML data arrives from a connected system (Kubernetes API, Prometheus query, etc.).
2. **Shape detection** -- The JSONFlux pipeline analyzes the response structure (single object, list of objects, nested collections).
3. **Arrow table conversion** -- Data is converted to Apache Arrow columnar format for efficient processing.
4. **Parquet caching** -- Arrow tables are written to session-scoped Parquet files in object storage (MinIO/S3). These files are tied to the investigation session.
5. **SQL reduction** -- DuckDB runs SQL queries over the Parquet data to extract only the relevant subset. The LLM sees reduced data, not raw dumps.
6. **Session scope** -- Cached data is scoped to the investigation session. It is not shared across sessions or tenants.

**What the LLM sees:**

- The LLM (Claude) never sees raw connector responses directly. All data passes through the reduction pipeline first.
- SQL-based reduction extracts specific columns and rows, reducing token consumption by up to 81% while preserving the information the agent needs to reason.
- This is not just a cost optimization -- it limits the surface area of data exposed to the LLM inference API.

**What is stored permanently:**

- **Stored:** Investigation transcripts (agent reasoning steps and results), approval audit logs, knowledge base documents, topology graph, session metadata.
- **Not stored permanently:** Raw connector responses. These exist only in session-scoped Parquet cache and are tied to session lifecycle.

## Audit Trail

Every modifying operation is logged with a complete audit trail:

- **Who** approved the operation (user ID, IP address, user agent)
- **When** the approval happened (timestamp)
- **What** was executed (tool name, arguments, HTTP method, endpoint)
- **Which** connector was targeted
- **Outcome** of the execution (success or failure)

Audit entries are append-only and immutable. They cannot be modified or deleted through the application.

See [Trust & Safety](trust-and-safety.md) for the full approval flow and audit schema.

## Observability

MEHO integrates with **OpenTelemetry** for distributed tracing and structured logging:

- All backend operations emit OTEL spans with trace context.
- Logs are structured JSON with correlation IDs linking to traces.
- The default development setup ships logs to **Seq** (available at `http://localhost:5341`).
- Production deployments can point `OTEL_EXPORTER_OTLP_ENDPOINT` to any OTLP-compatible collector (Jaeger, Grafana Tempo, Datadog, etc.).

## Security Checklist

| Control | Status | Notes |
|---------|--------|-------|
| JWT authentication on all API routes | Implemented | Keycloak OIDC, validated per-request |
| Memory-only token storage | Implemented | No localStorage/sessionStorage, XSS mitigation |
| keycloak-js (official adapter) | Implemented | Replaced abandoned @react-keycloak/web |
| Multi-tenant data isolation | Implemented | tenant_id on all models, query-level filtering |
| CORS lockdown | Implemented | Explicit origin allowlist, no wildcards |
| HSTS headers | Implemented | Strict transport security |
| Content Security Policy | Implemented | Report-only mode for safe rollout |
| Credential encryption at rest | Implemented | Fernet symmetric encryption |
| Approval flow for writes | Implemented | Three-tier trust model, see Trust & Safety |
| Audit trail | Implemented | Append-only, immutable operation log |
| OpenTelemetry integration | Implemented | Distributed tracing + structured logging |
| Rate limiting | Implemented | Configurable via `ENABLE_RATE_LIMITING` |
| Session-scoped data caching | Implemented | Parquet files tied to session lifecycle |
