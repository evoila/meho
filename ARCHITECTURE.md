# MEHO Architecture

**Repository:** `evoila/meho`
**Location:** `meho/`
**License:** Apache 2.0
**Status:** Released (core chassis); selected features are Beta or Experimental — see the maturity table in §6.
**Started:** 2026-05-09 (fresh start; prior codebase deprecated and kept for reference only)

---

## 1. What MEHO is

MEHO is a **governance backplane for AI agents acting on infrastructure**. It sits between any MCP client (Claude Code, Cursor, Cline, Continue, custom) and the infrastructure the agent operates against (Kubernetes, vCenter/VCF, NSX, public cloud, network appliances, secrets stores).

MEHO does **not require or ship a model**. You bring your own agent. MEHO optionally hosts agents on the backplane (experimental), but under the same governance rules as any other caller. MEHO governs what the agent is allowed to do, per call, through one narrow governed seam.

Every interaction through MEHO is guaranteed to be:

| Guarantee | What it means | Scope |
|---|---|---|
| **Policy-gated** | Operations are authorised against the caller's role and per-target grants before they execute. | Calls through MEHO only |
| **Credential-federated** | The **agent** never holds a backend credential; the backplane exchanges a short-lived Keycloak OIDC token for a just-in-time backend credential per operation. The connector holds the JIT credential only for the duration of that operation. | Calls through MEHO only |
| **Server-reduced** | Results are reduced server-side, so the agent sees a compact, relevant view instead of raw firehose output. | Calls through MEHO only |
| **Broadcast** | Every action is published to a Valkey-backed activity feed (at-most-once pub/sub via SSE). Other agents and humans can watch the feed. | Calls through MEHO only |
| **Audited** | Every authenticated interaction lands as an audit row in PostgreSQL, attributed to the calling principal; fail-closed (audit insert error ? HTTP 500). | Calls through MEHO only |
| **Tenant-scoped & version-aware** | Every context lookup is scoped to the caller's tenant and aware of resource versions. | Calls through MEHO only |

> **Assumption:** These guarantees hold only for calls that route through MEHO. If an agent has a direct network path or embedded credentials to a target, it can bypass MEHO entirely. Operators must ensure targets accept connections only from the backplane (network segmentation, target-side allowlists).

---

## 2. High-Level Diagram

```mermaid
flowchart TD
    subgraph Callers["Callers (trusted)"]
        AG["AI agents<br/>(any MCP client)"]
        OP["Operators<br/>(meho CLI · browser console)"]
        HU["Human approver<br/>(four-eyes)"]
    end

    subgraph Backplane["MEHO backplane<br/>(trusted, one FastAPI service)"]
        MCP["/mcp — MCP server<br/>Streamable HTTP + OAuth 2.1"]
        API["/api — REST API v1"]
        POL["Policy gate<br/>+ approvals"]
        DISP["Connector<br/>dispatch"]
        RED["Result<br/>reduction"]
        AUD["Audit middleware<br/>fail-closed, spans whole pipeline"]
    end

    subgraph Backing["Backing services (trusted)"]
        KC["Keycloak<br/>(identity / OIDC)"]
        CB["Credential backend<br/>(OpenBao/Vault or<br/>Google Secret Manager)"]
        PG["PostgreSQL + pgvector<br/>(state · audit · search)"]
        VK["Valkey / Redis<br/>(activity feed, at-most-once)"]
    end

    subgraph Targets["Targets (untrusted responses)"]
        TGT["Registered targets<br/>address + credential ref +<br/>TLS trust settings"]
    end

    subgraph Satellite["Satellite gateway<br/>(isolated network,<br/>outbound poll only)"]
        SAT["Satellite runner<br/>mTLS / attestation<br/>work leases, replay protection"]
    end

    AG -->|"OAuth 2.1 + PKCE"| MCP
    OP -->|"device-code login"| API
    MCP --> POL
    API --> POL
    POL --> DISP
    DISP --> RED
    POL -.->|"JWT validation / readiness"| KC
    DISP -.->|"just-in-time credential"| CB
    RED -.->|"audit rows"| PG
    RED -.->|"broadcast events (SSE)"| VK
    DISP --> TGT
    HU -.->|"approve / reject"| POL
    SAT -->|"outbound poll"| API
    SAT --> TGT
    SAT -.->|"JIT credential via central"| CB

    classDef default fill:#f9f9f9,stroke:#333,stroke-width:2px;
    classDef trusted fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
    classDef untrusted fill:#fff3e0,stroke:#e65100,stroke-width:2px;
    class AG,OP,HU,MCP,API,POL,DISP,RED,AUD,KC,CB,PG,VK,SAT trusted;
    class TGT untrusted;

---

## 3. Who runs what

| Component | Provided by | Maturity | Notes |
|---|---|---|---|
| Backplane container image | **MEHO** (`ghcr.io/evoila/meho`) | GA | One Deployment, rendered by the Helm chart. Multi-arch (linux/amd64 + linux/arm64), non-root uid 1001, cosign-signed. |
| Valkey (activity feed) | **MEHO** — bundled subchart | GA | Installed by the same Helm release. |
| PostgreSQL + pgvector | **MEHO** by default, or **you** | GA | Built-in PostgreSQL with pgvector. Turn off (`corePostgres.enabled: false`) to bring your own. |
| Keycloak | **MEHO** | GA | Built-in Keycloak makes the `meho` realm. Connect your company login (AD/LDAP, Entra ID, authentik, own Keycloak) in front. |
| Credential backend | **MEHO** by default, or **you** | GA | Built-in OpenBao (open-source Vault fork). Turn off to use your own HashiCorp Vault or Google Secret Manager. |
| AI agent / MCP client | **You** | — | MEHO governs agents; it does not require or ship a model. Bring Claude, Cursor, or any MCP client. |
| Hosted agents | **MEHO** (experimental) | Experimental | Optional agents running on the backplane under the same rules. |
| Browser console (`/ui`) | **MEHO** | Beta | Read-heavy operator surface; off by default, enabled all-or-nothing via chart values. |
| Satellite gateway | **MEHO** | Beta | Runs inside isolated networks, polls central API outbound. |
| VMware / VCF connectors | **evoila enterprise pack** | GA (overlay) | Overlay image built on the public image; not in the public tree. |

---

## 4. The four surfaces

### 4.1 Caller surface (outside the backplane)

- **AI agents** connect to `/mcp` via OAuth 2.1 authorization-code + PKCE (the MCP-mandated flow). Default tools only *read* targets (`list_targets`). Target registration is an operator tool requiring `mcp:admin` scope + `tenant_admin` role. There is **no MCP tool to store a secret**.
- **Operators** use the `meho` CLI (static Go binary, device-code login — works from private networks/jump hosts because only the browser reaches Keycloak) or the optional browser console (`/ui`, read-heavy, Beta, off by default).
- **Satellite gateway** runs the same backplane image inside an isolated network, polls the central API for authorized work, executes locally against the same connector surface, and reports results back. Outbound only; cannot self-authorize. `safe` operations by default; `caution` writes need human approval; `dangerous`/`destructive` never run on a satellite (Beta).
- **Human approver** participates in four-eyes approval flows for `dangerous` and `destructive` operations, and for `caution` writes on satellites.

### 4.2 Backplane (Python / FastAPI)

A single Python service exposing two front doors and one governed pipeline behind both:

**Front doors:**
- `/mcp` — MCP server, JSON-RPC 2.0 over Streamable HTTP. OAuth 2.1 resource-server protection (RFC 8707 audience, RFC 9728 `/.well-known/oauth-protected-resource`, `WWW-Authenticate` on 401). Tool + resource registries with RBAC filtering.
- `/api` — REST API v1 (~30 router modules: targets, operations, feed, agents, approvals, audit, runbooks, sessions, tenants, connectors, docs retrieval, etc.)

**Governed pipeline:** `Policy gate + approvals ? Connector dispatch ? Result reduction`

**Audit** spans the whole pipeline (not just result reduction): one row per authenticated request, written synchronously before the response is yielded. Fail-closed: audit insert error ? HTTP 500.

**Key backend domains** (`backend/src/meho_backplane/`):

| Domain | Responsibility |
|---|---|
| `mcp/` | MCP transport skeleton, JWT verification + operator binding, tool/resource registries, per-operation audit rows. |
| `api/v1/` | REST route modules; thin shells over domain services. |
| `auth/` | JWT validation ? `Operator` model, Vault OIDC login per operator, Keycloak/Vault readiness probes, external doc-corpus search. |
| `connectors/` | `Connector` ABC (`fingerprint`/`probe`; `execute` refuses direct calls), registries v1+v2, resolver with tri-state dispatchability, execution profiles, version splitters, auth schemes. |
| `operations/` | Dispatcher, policy engine, approval gates, reducer + result reduction, result delivery/spill, flight recorder, typed operation registration, composite operations, deny reasons. |
| `agents/` | Agent-definition + permission-grant CRUD (tenant-scoped), grant-expiry sweeper. Writes gated to `tenant_admin`; reads to `operator`. |
| `tenancy/` | Every context lookup scoped to caller's tenant. |
| `targets/` | Target CRUD (address, credential-backend reference — never the secret itself, TLS trust settings). |
| `sessions/` | Agent sessions. |
| `broadcast/` | Valkey-backed real-time activity feed + SSE (`/feed`). |
| `memory/` | Agent memory with expiry sweeper. |
| `kb` / `docs_collections` / `docs_search` / `retrieval` | Knowledge base, hybrid retrieval; fastembed ONNX embedding pipeline preloaded at lifespan. |
| `runbooks/` | Runbook templates + runs. |
| `scheduler/` | Scheduled jobs (`croniter`). |
| `sensors/` / `event_source/` / `events/` | Sensor/event ingestion, event drain. |
| `gateway/` | Satellite gateway runner, dead-man switch sweeper, unreported-mint security alarm. |
| `runner/` | Runner workers, queue, repository patterns. |
| `topology/` | Connector topology discovery hints ? graph nodes/edges diffed and applied as insert/update/soft-delete. |
| `flight_recorder/` | Flight recorder. |
| `redaction/` | PII redaction; Presidio Tier-2 NER adapter. Redaction is applied to results before they reach the agent and before broadcast publication. |
| `reflex/` | Reflex rules. |
| `checks/` | Health/checks, dashboards. |
| `audit` / `audit_query/` | Immutable audit rows in PostgreSQL; fail-closed audit middleware. |
| `web_push/` | Web push notifications. |
| `ui/` | Jinja2 templates for the browser console. |
| `db/` | SQLAlchemy 2.x async engine, models, Alembic migrations, pgvector. |
| `core/` | Install-time Helm hook Jobs (secrets, vault-init, etc.). |

### 4.3 CLI (`cli/`, Go)

- Single static binary, multi-platform (linux/amd64 + linux/arm64), cosign-signed releases via GoReleaser.
- Cobra-based; `version`, `login` (device-code), `status` today; additional commands are **server-driven** (manifest fetched at startup).
- OIDC device-code flow; token store = OS keyring by default, file backend available; config at `$XDG_CONFIG_HOME/meho/config.json`.
- Typed API client generated by oapi-codegen v2 from the FastAPI OpenAPI snapshot, with an auth-aware wrapper and lazy 401-retry refresh.

### 4.4 Clients (`clients/`)

- **Claude Code plugin** — skills: `broadcast`, `knowledge`, `memory`, `operations`, `prefer-meho`; hooks + login-guard/signin-lock/token-renewal scripts; `meho-mcp-remote` shim.
- **Claude Desktop MCP bridge** — `server/index.mjs` + manifest.

### 4.5 Deploy (`deploy/`)

- **Helm chart `meho/`** with subcharts: `broadcast`, `keycloak`, `openbao`, `postgres`, `result-cache`; templates for deployment, ingress, networkpolicy, servicemonitor, prometheusrule, PVC (fastembed cache), migration job, core-secrets job, core-vault-init job, update-strategy job; values examples (kind, openshift, bring-your-own, GSM, etc.).
- **VMware appliance** (CIS hardening, firststart scripts, ovf rewrite, tests).
- **Linux server install** (RKE2-based, CIS, tests).
- **Connector overlay** — enterprise pack mechanism for VMware/VCF connectors.

---

## 5. Request lifecycle (how a call flows)

1. **AuthN:** Agent presents a Bearer token (MCP OAuth 2.1) or CLI uses device-code flow against Keycloak; `verify_jwt` / `verify_mcp_jwt_and_bind` validate against Keycloak JWKS and bind an `Operator` (tenant + role).
2. **Policy gate:** The operation is authorized against the caller's role and per-target grants *before* execution (`op_policy`, RBAC floors, `runner_write_allowlist`).
3. **Approval gate (if required):** Write/dangerous ops may route to the approval queue. Approvals are bound to a hash of the exact parameters (TOCTOU prevention), have expiry, and forbid self-approval (four-eyes). The agent sees a `pending` status while waiting.
4. **JIT credential:** The backplane exchanges the Keycloak OIDC token with Vault/OpenBao (or GSM) for a just-in-time backend credential per operation. The connector holds this credential only for the duration of the operation; the **agent never holds it**. The credential is revoked on exit.
5. **Connector dispatch:** The resolver picks the connector (hand-coded beats profiled beats bare), the connector runs against the target via its SDK/protocol/SSH/REST.
6. **Result reduction:** Results are reduced server-side (JSON flux / reducer) so the agent sees a compact view. Large results spill to result handles. Reduction provenance (what was dropped, reducer used, handle) is recorded so audit can reconstruct the agent's context.
7. **Audit:** For state-changing operations, an intent row is written before execution and an outcome row after (write-ahead audit). For reads, one row per authenticated request in PostgreSQL `audit_log`, attributed to the calling principal; fail-closed (audit insert error ? HTTP 500). MCP path writes one row per operation.
8. **Broadcast:** Every action is published to the Valkey-backed activity feed (at-most-once, SSE) watchable by agents and humans. Results are labelled as untrusted data before publication; PII redaction (Presidio) is applied before the agent sees results and before broadcast.

---

## 6. Key design patterns & decisions

### Maturity table

| Feature | Maturity | Notes |
|---|---|---|
| Backplane chassis (auth, policy, audit, connectors) | GA | Shipped in v0.1+ |
| Satellite gateway | Beta | Outbound poll only; `safe` ops by default |
| Browser console (`/ui`) | Beta | Read-heavy; off by default |
| Hosted agents | Experimental | Runs on backplane under same rules |
| VMware / VCF connectors | GA (enterprise overlay) | Not in public tree |
| Result spill / handles | GA | Large results spill to Valkey-backed store |
| PII redaction | Beta | Presidio Tier-2 NER, lazy-loaded |

### Documented decisions

- **Python / FastAPI / Pydantic v2 / SQLAlchemy 2.x async / Alembic** stack for the backplane (ADR 0004).
- **SPDX header convention:** every authored file carries `SPDX-License-Identifier: Apache-2.0`.
- **DCO sign-off, no CLA:** `git commit -s` required; inbound = outbound under Apache 2.0.
- **`src/`-layout** for Python so tests only resolve the installed package.
- **Multi-arch container** (linux/amd64 + linux/arm64), non-root uid 1001, read-only root filesystem support, base image pinned by digest, trivy + SBOM + cosign + Sigstore attestations.
- **Fail-closed defaults:** readiness registry fails closed on empty default; audit middleware fails closed (un-audited request ? HTTP 500); readiness probes flip `not_ready` when Keycloak/Vault unreachable.
- **Connector tri-state:** `none`/`profiled`/`bare` dispatchability so vetted profiles cannot shadow hand-coded connectors. Unclassified operations on profiled and bare connectors default to `deny` (treated as `dangerous`).
- **Satellite gateway:** central auth/approval/audit; satellite cannot self-authorize; outbound poll only; mTLS/attestation for enrollment; work leases and replay protection.
- **Version-aware connectors:** version splitters so connectors are aware of resource versions.
- **Result spill + handles:** results too large for inline delivery get a handle; agent queries pages.
- **Observability:** structlog JSON logs, Prometheus `/metrics`, request-context middleware.
- **Identity:** calling principal is the agent operating under delegated user identity (RFC 8693-style act claims); audit records "agent X on behalf of user Y".

---

## 7. Technology stack

| Layer | Technology |
|---|---|
| Backplane language | Python 3.14+ |
| Backplane framework | FastAPI |
| Validation / models | Pydantic v2 |
| Database | SQLAlchemy 2.x async, asyncpg, PostgreSQL + pgvector, Alembic |
| Logging | structlog (JSON to stdout) |
| Metrics | Prometheus client |
| Auth | Keycloak JWKS, authlib, OIDC device-code flow |
| Credentials | hvac (Vault/OpenBao), Google Secret Manager |
| Cache / feed | Valkey / Redis |
| Embeddings | fastembed (ONNX) |
| AI routing | pydantic-ai (anthropic, bedrock, openai) — hosted agents only, optional |
| Redaction | Presidio (lazy, Tier-2 NER) |
| Scheduling | croniter |
| CLI language | Go |
| CLI framework | cobra |
| API codegen | oapi-codegen v2 |
| Release | GoReleaser, cosign, Sigstore |
| Packaging | Helm chart, multi-arch Docker, trivy, SBOM |
| Docs | mkdocs (`docs-site/`), contributor ADRs (`docs/`) |

---

## 8. What MEHO is not

- **Not an agent runtime first.** MEHO governs what any MCP client is allowed to do. It can optionally run agents on the backplane (hosted agents, experimental), under the same rules.
- **Not a session broker.** Tools like Teleport or StrongDM broker *human* sessions (SSH, database, kubectl). MEHO authorises and audits *individual agent actions*, per call, with infrastructure-aware connectors.
- **Not a SaaS.** You run everything; your audit trail stays in your PostgreSQL.
- **Not a bypass boundary.** MEHO's guarantees apply only to calls that route through it. Agents with direct target credentials or network paths can bypass governance.

---

## 9. Extensibility

- **Connector kit:** `meho_connector_kit` — one module with the names a package may use and its own version number. External vendors ship connectors in their own repos importing only the kit. Start from `templates/connector-package/`. Third-party connector packages run in-process with access to JIT credentials; they are fully trusted by default. Document signing, allowlisting, or sandboxing for third-party connectors is a stated gap.
- **Bring your own parts:** PostgreSQL, Keycloak, and the credential backend can all be replaced with your own (expert paths documented).
- **Server-driven CLI:** commands are discovered from a server-fetched manifest, so new verbs land without CLI recompilation.
