# Feature Flags

> Last verified: v2.3 (Phase 101)

MEHO uses feature flags to enable or disable optional modules at startup. Most flags default to `true` (enabled). Set a flag to `false` in your `.env` file or environment to disable the corresponding module entirely.

Disabling a module prevents its API routes from being registered, its background tasks from starting, and its lifespan hooks from running. The module's database tables remain but are not accessed.

## Module Feature Flags

| Flag | Default | Controls |
|------|---------|----------|
| `MEHO_FEATURE_KNOWLEDGE` | `true` | Knowledge base: document ingestion, embedding, and hybrid search |
| `MEHO_FEATURE_TOPOLOGY` | `true` | Topology discovery, entity graph, and cross-system resolution |
| `MEHO_FEATURE_SCHEDULED_TASKS` | `true` | Cron-based scheduled task execution |
| `MEHO_FEATURE_EVENTS` | `true` | Event-driven triggers (webhooks, scheduled, external) |
| `MEHO_FEATURE_MEMORY` | `true` | Operator memory extraction and injection |
| `MEHO_FEATURE_SLACK` | `true` | Slack connector and `/meho` slash command bot |
| `MEHO_FEATURE_NETWORK_DIAGNOSTICS` | `true` | Built-in SRE network tools (dns_resolve, tcp_probe, http_probe, tls_check) |
| `MEHO_FEATURE_MCP_CLIENT` | `true` | MCP client connector for external tool servers |
| `MEHO_FEATURE_MCP_SERVER` | `true` | MCP server exposing MEHO tools to external clients |
| `MEHO_FEATURE_EPHEMERAL_INGESTION` | `false` | Offload large PDFs to ephemeral cloud workers (requires backend config) |
| `MEHO_FEATURE_USE_DOCLING` | `true` | Document ingestion backend: `true` = Docling (GPU/PyTorch), `false` = lightweight CPU-only pipeline |

## How Feature Flags Work

Feature flags are implemented using [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) with the `MEHO_FEATURE_` environment variable prefix. At startup:

1. The `FeatureFlags` class reads all `MEHO_FEATURE_*` environment variables.
2. Values are parsed as booleans (`true`/`false`, `1`/`0`, `yes`/`no`).
3. The resulting `FeatureFlags` instance is frozen (immutable) and cached for the lifetime of the process.
4. Module registration checks the cached flags -- disabled modules are skipped entirely.

Changing a feature flag requires a restart. There is no hot-reload mechanism.

```python
# Internal implementation (meho_app/core/feature_flags.py)
class FeatureFlags(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEHO_FEATURE_", frozen=True)

    knowledge: bool = Field(default=True)
    topology: bool = Field(default=True)
    scheduled_tasks: bool = Field(default=True)
    events: bool = Field(default=True)  # env var: MEHO_FEATURE_EVENTS
    memory: bool = Field(default=True)
    slack: bool = Field(default=True)
    network_diagnostics: bool = Field(default=True)
    mcp_client: bool = Field(default=True)
    mcp_server: bool = Field(default=True)
    ephemeral_ingestion: bool = Field(default=False)
    use_docling: bool = Field(default=True)
```

## Usage

Set flags in your `.env` file:

```bash
# Disable the knowledge base module
MEHO_FEATURE_KNOWLEDGE=false

# Disable scheduled tasks
MEHO_FEATURE_SCHEDULED_TASKS=false
```

Or override in `docker-compose.yml`:

```yaml
services:
  meho:
    environment:
      MEHO_FEATURE_KNOWLEDGE: "false"
      MEHO_FEATURE_EVENTS: "false"
```

## Effect of Disabling

| Flag | When Disabled |
|------|---------------|
| `MEHO_FEATURE_KNOWLEDGE` | Knowledge API routes (`/api/knowledge/`) not registered. Document ingestion and search unavailable. The agent cannot query the knowledge base. |
| `MEHO_FEATURE_TOPOLOGY` | Topology routes not registered. Entity extraction from connector results is skipped. The topology graph page shows no data. Cross-system entity resolution is disabled. |
| `MEHO_FEATURE_SCHEDULED_TASKS` | Scheduled task execution does not start. Existing cron configurations are preserved in the database but not executed. |
| `MEHO_FEATURE_EVENTS` | Event routes (`/api/events/`) not registered. External systems cannot trigger investigations via webhooks or other event sources. Existing event registrations are preserved. |
| `MEHO_FEATURE_MEMORY` | Memory extraction from conversations is disabled. The agent does not inject operator memory into investigation context. Existing memories remain in the database. |
| `MEHO_FEATURE_SLACK` | Slack connector not loaded, `/meho` slash command unavailable. Existing Slack connector configurations are preserved in the database. |
| `MEHO_FEATURE_NETWORK_DIAGNOSTICS` | Network diagnostic tools (dns_resolve, tcp_probe, http_probe, tls_check) removed from agent toolkit. Network diagnostic topology schema not registered. |
| `MEHO_FEATURE_MCP_CLIENT` | MCP client connector not available. Cannot connect to external MCP servers. Existing MCP connector configurations are preserved. |
| `MEHO_FEATURE_MCP_SERVER` | MCP server endpoint (`/mcp`) not registered. External tools cannot trigger MEHO investigations via MCP protocol. |
| `MEHO_FEATURE_EPHEMERAL_INGESTION` | Large PDF ingestion runs in-process instead of offloading to ephemeral workers. This is the default behavior when disabled. |
| `MEHO_FEATURE_USE_DOCLING` | Document ingestion uses lightweight CPU-only pipeline (pymupdf4llm, pdfplumber, RapidOCR) instead of Docling. No PyTorch or GPU required. Image quality slightly reduced but adequate for most documents. |

!!! warning "Non-standard defaults"
    `MEHO_FEATURE_EPHEMERAL_INGESTION` defaults to `false` (unlike all other flags which default to `true`). Ephemeral ingestion requires a configured cloud coordinator backend -- it is opt-in, not opt-out.

!!! note "Core modules cannot be disabled"
    The connector framework, agent system, chat sessions, and authentication are core modules that are always loaded. Feature flags only control optional modules that provide additional capabilities.

## Operational Flags

Separate from module feature flags, MEHO has operational `ENABLE_*` flags for fine-grained control of specific behaviors:

- `ENABLE_RATE_LIMITING` -- Toggle API rate limiting
- `ENABLE_MEMORY_EXTRACTION` -- Toggle automatic memory extraction from conversations
- `ENABLE_TRANSCRIPT_PERSISTENCE` -- Toggle full transcript storage
- `ENABLE_DETAILED_EVENTS` -- Toggle detailed SSE event streaming
- `ENABLE_OBSERVABILITY_API` -- Toggle the observability debug API

These are documented in `env.example`. Unlike module flags, operational flags control individual behaviors within always-loaded modules.
