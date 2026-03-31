# MEHO for Claude

Lightweight DevOps intelligence engine that runs as a Claude Code extension. Same core capabilities as MEHO.X — connectors, topology, memory, diagnostics, knowledge — but delivered as a Python CLI with local state instead of a cloud-native platform.

## Inspiration

This project is modeled after the [GSD (Get Shit Done) framework](https://github.com/gsd-build/get-shit-done) — a Claude Code extension that uses Python CLI tools, markdown workflow templates, and a local state directory (`~/.claude/get-shit-done/`) to give Claude Code structured planning and execution capabilities for software engineering.

MEHO for Claude applies the same pattern to DevOps: instead of GSD's software planning workflows, we provide DevOps workflows — connector management, infrastructure topology discovery, cross-system diagnostics, knowledge ingestion, and memory-powered context. The delivery model is identical: Python CLI + markdown templates + local state directory, all orchestrated by Claude Code.

**Study GSD's architecture closely when building this.** Key patterns to replicate:
- Workflow templates in markdown that Claude Code reads and executes (see `~/.claude/get-shit-done/workflows/`)
- CLI tools that handle state management, commits, and structured operations (see `~/.claude/get-shit-done/bin/`)
- Skill registration so commands appear as `/meho:*` slash commands in Claude Code
- Local directory structure for persistent state across sessions
- Agent role definitions for specialized subagents (see `~/.claude/agents/`)

## Architecture

- **CLI**: Python + Typer — no web server, no FastAPI, no MCP
- **State**: `~/.meho/` directory (workflows, config, skills)
- **Relational data**: SQLite (connectors, topology, entities, relationships)
- **Vectors**: ChromaDB (knowledge embeddings, memory)
- **Workflows**: Markdown templates executed by Claude Code (modeled after GSD)

## Multimodal Input

Claude Code is multimodal — users can provide visual and document input directly in the terminal:

- **Screenshots**: Paste or drag-drop screenshots of Grafana dashboards, cloud console alerts, Kubernetes error pages, monitoring UIs. MEHO reasons about them in context with the topology and memory it already has.
- **Documents**: Drop PDF runbooks, architecture diagrams, incident reports. MEHO ingests them into the knowledge base.
- **Log files**: Reference any text file by path for analysis.

This is a major differentiator over traditional CLI tools. A DevOps engineer can paste a screenshot of a failing deployment, and MEHO cross-references it against known topology, past incidents in memory, and relevant documentation — all without leaving the terminal.

Workflows should be designed to accept and leverage visual input where it makes sense (e.g., `/meho:diagnose` could prompt "paste a screenshot of the error or describe it").

## Relationship to MEHO.X

| | MEHO.X | MEHO for Claude |
|---|--------|-----------------|
| **Delivery** | Cloud-native platform | Claude Code extension |
| **Database** | PostgreSQL | SQLite |
| **Vectors** | pgvector | ChromaDB |
| **Auth** | Keycloak + JWT | Local (single user) |
| **Frontend** | React SPA | Terminal + Claude Code |
| **Deployment** | Docker Compose / K8s | pip install |
| **Multi-tenant** | Yes | No (single user) |
| **Target** | Enterprise teams | Individual DevOps engineers |

## Status

**Pre-development** — this is a new initiative. See `.planning/todos/pending/2026-03-01-meho-for-claude-cli-devops-extension.md` for the full idea capture.
