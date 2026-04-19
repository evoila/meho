# MEHO -- AI-Powered Diagnostic Platform for Complex IT Environments

> Last verified: v2.0

MEHO (Machine Enhanced Human Operator) is an AI-powered diagnostic and operations platform that connects to every system in your infrastructure and reasons across all of them as a connected graph. Operators express intent in natural language; MEHO investigates using a ReAct reasoning loop that queries multiple systems, preserves raw data, and traces root causes across layers -- from application down to hypervisor -- in a single conversation.

Think of it as **Claude Code for infrastructure**: instead of reasoning across files in a codebase, MEHO reasons across systems in your stack.

## The Problem

Modern infrastructure is layered and interconnected. A slow API response might trace back through a service mesh, into a Kubernetes pod, down to a node running out of memory, caused by a VM on an overcommitted hypervisor. Diagnosing this today means switching between 5-10 different tools, each with its own query language, auth model, and mental model. By the time you find the root cause, the incident is an hour old.

## How MEHO Solves It

MEHO connects to your entire stack through **15 typed connectors** and reasons across all of them in a single conversation. You describe the problem; MEHO investigates.

- **Cross-system tracing is automatic.** MEHO resolves entities across systems -- a Kubernetes pod maps to a VM maps to a hypervisor host -- using deterministic resolution (providerID, IP, hostname) with LLM fallback.
- **Data stays on the server.** MEHO's JSONFlux data pipeline processes raw API responses into Apache Arrow tables, caches them as Parquet, and queries them with DuckDB SQL. The LLM only sees reduced, relevant data -- never raw megabytes of JSON.
- **Trust is built in.** Every connector operation is classified as READ, WRITE, or DESTRUCTIVE. Write operations require explicit approval with a full audit trail. MEHO never executes a destructive action without confirmation.
- **Two modes for different needs.** Ask mode answers knowledge questions instantly. Agent mode launches full investigations with real-time connector queries.

## Connectors

MEHO connects to 15 system types, grouped by function:

### Infrastructure

| Connector | What It Connects To |
|-----------|-------------------|
| **Kubernetes** | Clusters, namespaces, pods, services, deployments, nodes, events |
| **VMware vSphere** | Datacenters, clusters, hosts, VMs, datastores, networks |
| **Proxmox VE** | Nodes, VMs, containers, storage pools |
| **Google Cloud** | Projects, compute instances, networks, disks |

### Observability

| Connector | What It Connects To |
|-----------|-------------------|
| **Prometheus** | Metrics, targets, recording rules, alert rules |
| **Loki** | Log streams, log queries, label exploration |
| **Tempo** | Distributed traces, trace search, service graphs |
| **Alertmanager** | Active alerts, silences, alert groups, receivers |

### CI/CD

| Connector | What It Connects To |
|-----------|-------------------|
| **ArgoCD** | Applications, sync status, deployment history, projects |
| **GitHub** | Repositories, pull requests, workflows, deployments, commits |

### Collaboration

| Connector | What It Connects To |
|-----------|-------------------|
| **Jira** | Issues, projects, boards, sprints, comments |
| **Confluence** | Pages, spaces, search, content hierarchy |
| **Email** | Send notifications via SMTP, SendGrid, SES, or Mailgun |

### Generic

| Connector | What It Connects To |
|-----------|-------------------|
| **REST (OpenAPI)** | Any system with an OpenAPI/Swagger spec -- auto-discovers endpoints and generates skills |
| **SOAP (WSDL)** | Legacy enterprise systems with WSDL service definitions |

## Key Differentiators

**Cross-system reasoning.** MEHO doesn't just query systems in isolation. It builds a topology graph linking entities across connectors -- Kubernetes pods to VMs, VMs to hypervisor hosts, services to Prometheus metrics -- and traces problems through these relationships automatically.

**Intelligent data pipeline.** MEHO never sends raw API responses to the LLM. The [JSONFlux pipeline](how-it-works.md) detects response shapes, converts JSON to Arrow tables, caches as Parquet, and uses DuckDB SQL for precise data reduction. This enables querying gigabytes of infrastructure data without context overflow.

**Trust model.** Every operation is classified (READ / WRITE / DESTRUCTIVE) with an approval modal and audit trail. Operators stay in control. See [Trust & Safety](trust-and-safety.md).

**Topology auto-discovery.** Every connector query enriches a live topology graph. Entity resolution across systems is deterministic-first (providerID, IP, hostname matching), with LLM fallback for ambiguous cases.

**Dual-mode chat.** Ask mode for quick knowledge lookups. Agent mode for full investigations with connector queries, hypothesis tracking, and investigation visualization.

## Next Steps

- **[Features](features.md)** -- Full list of capabilities across all milestones
- **[How It Works](how-it-works.md)** -- The JSONFlux data pipeline that makes cross-system reasoning possible
- **[Example Investigation](example-investigation.md)** -- Watch MEHO trace a problem across five systems in one conversation
