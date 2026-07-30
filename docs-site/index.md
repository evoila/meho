# Start here

MEHO is a governance backplane for AI agents acting on infrastructure —
policy-gated, audit-grade, MCP-native, Apache 2.0. It sits between MCP
clients (Claude Desktop, Claude Code, Cursor, custom clients) and the
infrastructure they operate against, so every operation passes through
one governed seam: policy, just-in-time credentials, server-side result
reduction, broadcast, and an immutable audit trail.

!!! note "This site is under construction"

    The scaffold you are looking at is live and versioned, but the
    content is still being migrated. Each section below names what will
    live there and links the task tracking it.

## What this section will cover

Tracked by [evoila/meho#2671](https://github.com/evoila/meho/issues/2671):

- **What MEHO is** — the problem it solves and what it guarantees.
- **Reference architecture** — backplane, Keycloak, Postgres + pgvector,
  Valkey, Vault / Google Secret Manager, targets and connectors, agents
  via MCP, operators via CLI and UI, satellite gateway.
- **What 1.0 promises** — the honestly-scoped stability contract.

## The site at a glance

| Section | What will live there | Tracked by |
|---|---|---|
| [Install & operate](install/index.md) | Prerequisites, Helm install, credential backends, Keycloak realm setup, TLS, upgrades | [#2671](https://github.com/evoila/meho/issues/2671) |
| [Connect clients](clients/index.md) | CLI first, then the MCP client matrix and troubleshooting | [#2672](https://github.com/evoila/meho/issues/2672) |
| [Do real work](guides/index.md) | Task guides: targets & secrets, first operations, sensors | [#2673](https://github.com/evoila/meho/issues/2673) |
| [Reference](reference/index.md) | Generated: MCP tools, REST API, CLI, env vars, Helm values, connector catalog | [#2662](https://github.com/evoila/meho/issues/2662) |
| [Project](project/index.md) | Versioning & deprecation policy, feature maturity, security policy | [#2664](https://github.com/evoila/meho/issues/2664) |

In the meantime, the in-repo material remains the source of truth:
[github.com/evoila/meho](https://github.com/evoila/meho).
