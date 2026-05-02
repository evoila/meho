# MEHO for the evoila Cloud Platform

**Proposal: Integrating an AI-Powered Operations Assistant into VCF Platform Operations**

**Date:** March 2026
**From:** MEHO Development Team (evoila Bosnia)
**To:** evoila Cloud Platform Team
**Point of Contact:** Haris Hodzic (already embedded in cloud platform maintenance)

---

## Executive Summary

The evoila cloud platform runs on VMware Cloud Foundation (VCF) hosted at Hetzner. The team operating it faces a persistent challenge: **VCF is a deep, complex stack and in-house expertise is limited.** When something breaks at 2 AM or a customer escalates a capacity question, the answer often lives in a 150-page VMware KB article nobody has time to read.

We are building **MEHO** — an AI-powered operations assistant that connects to infrastructure APIs, ingests documentation, and uses LLM reasoning to help operators investigate problems and answer questions. Think of it as having a VCF expert available 24/7 who can also query your vCenter, check your clusters, and read your runbooks — all in a single chat conversation.

We propose a **zero-overhead pilot** where MEHO connects to the evoila cloud platform as a read-only assistant. The cloud platform team gets an AI-powered VCF knowledge base and diagnostic tool at no cost. We get real-world feedback that makes MEHO better for everyone.

---

## The Problem We Solve

| Challenge | How it manifests | Impact |
|-----------|-----------------|--------|
| **Limited VCF expertise** | Not enough resident experts to cover all VCF layers (vSphere, vSAN, NSX, SDDC Manager, Aria) | Longer incident resolution, reliance on external support |
| **Knowledge scattered** | Official VMware docs, internal runbooks, Confluence pages, tribal knowledge in people's heads | New team members ramp up slowly, knowledge walks out the door when people leave |
| **Manual cross-system troubleshooting** | "Is it the VM? The host? vSAN? NSX? The physical layer?" requires checking multiple consoles | A 5-minute check becomes a 45-minute investigation |
| **Repetitive diagnostic tasks** | Same health checks, capacity reviews, and alert investigations done manually every week | Experienced engineers spend time on routine work instead of improvements |

---

## What MEHO Does

### 1. VCF Knowledge Base — Your Team's Second Brain

Upload any documentation and MEHO makes it searchable and queryable via natural language:

- **Official VMware documentation** — VCF Administration Guide, vSphere docs, vSAN guides, NSX references, SDDC Manager docs
- **Internal runbooks** — your team's operational procedures, escalation guides, architecture decisions
- **KB articles and release notes** — known issues, workarounds, upgrade paths
- **Architecture diagrams and design docs** — platform design, network topology, storage layout

**What this looks like in practice:**

> **Operator:** "What's the procedure for replacing a failed vSAN disk in our VCF environment?"
>
> **MEHO:** *Searches your uploaded runbooks and VMware docs, combines them with LLM knowledge*
> "Based on your runbook and the VCF 9.0 Administration Guide, here's the procedure:
> 1. Verify disk status in vSAN Health... 2. Check SDDC Manager for maintenance workflows... 3. ..."

The LLM doesn't just retrieve documents — it **reasons** over them. It can synthesize information from multiple sources, answer follow-up questions, and explain VCF concepts in plain language.

### 2. Live Infrastructure Queries — Talk to Your vCenter

MEHO connects directly to your vCenter via pyvmomi and can query your environment in real time:

| Category | What MEHO can do | Example queries |
|----------|-----------------|-----------------|
| **Inventory** | List VMs, hosts, clusters, datastores, networks | "How many VMs are running in cluster prod-01?" |
| **Health** | Check host status, cluster HA/DRS config, alarm states | "Are there any active alarms in the production datacenter?" |
| **Performance** | CPU, memory, disk, network metrics with historical data | "Show me the CPU usage trend for host esxi-07 over the last 24 hours" |
| **Storage** | Datastore capacity, vSAN health, disk status | "Which datastores are above 80% utilization?" |
| **Networking** | DVS configuration, port groups, VLAN assignments | "What VLAN is VM web-frontend-03 connected to?" |
| **Compute** | VM details, resource allocation, snapshot status | "List all VMs with snapshots older than 7 days" |

**179 pre-built operations** are available today, covering the most common vSphere tasks. No scripting required — MEHO translates natural language into the right API calls.

### 3. Cross-System Investigation — Connect the Dots

VCF doesn't exist in isolation. The evoila cloud platform likely includes Kubernetes workloads, monitoring (Prometheus/Grafana), CI/CD, and ticketing. MEHO can connect to **all of these** simultaneously and reason across system boundaries:

> **Operator:** "Customer X reports their application is slow"
>
> **MEHO:** *Queries Kubernetes for pod status → checks Prometheus for latency metrics → follows topology to the underlying VM → queries vCenter for host performance → finds the ESXi host is memory-constrained*
>
> "The application pods for Customer X are running on node k8s-worker-04, which maps to VM vcf-worker-04 on ESXi host esxi-12. That host is at 94% memory utilization with 3 other tenant VMs competing for resources. Recommend: DRS rebalance or migrate the customer's VM to a less loaded host."

This cross-system reasoning is MEHO's core differentiator. No single monitoring tool gives you this — it requires an AI that understands the relationships between your systems.

### 4. Topology Auto-Discovery — A Living Map of Your Infrastructure

MEHO automatically builds and maintains a topology graph of your environment:

- **VMs ↔ Hosts ↔ Clusters** — which VMs run where, which hosts belong to which clusters
- **K8s Pods ↔ Nodes ↔ VMs** — SAME_AS entity resolution (a K8s node IS a VM in vSphere)
- **Services ↔ Networks ↔ Storage** — full dependency mapping

This topology powers MEHO's cross-system investigation and gives your team a visual infrastructure map in the UI.

---

## Supported Connectors (Available Today)

| Stack | Connectors | Relevance to evoila Cloud Platform |
|-------|-----------|-------------------------------------|
| **VMware vSphere** | Native pyvmomi (179 operations) | **Core** — direct vCenter integration |
| **Kubernetes** | kubernetes-asyncio (49 operations) | **High** — if VCF runs K8s workloads (Tanzu, etc.) |
| **REST/OpenAPI** | Generic connector for any REST API | **High** — connect SDDC Manager, NSX Manager, Aria APIs via OpenAPI specs |
| **Prometheus** | Typed connector (metrics, alerts) | **High** — if using Prometheus for monitoring |
| **Loki** | Typed connector (log search) | **Medium** — if using Loki for centralized logging |
| **ArgoCD** | Typed connector (GitOps) | **Medium** — if using ArgoCD for deployments |
| **GitHub** | Typed connector (repos, PRs, Actions) | **Medium** — if using GitHub for IaC/config management |
| **Jira** | Typed connector (issues, JQL) | **Medium** — if using Jira for incident management |
| **Confluence** | Typed connector (page search, create) | **Medium** — if docs live in Confluence |
| **Proxmox VE** | Native connector | Low — unless Proxmox is in the stack |
| **Google Cloud Platform** | Native connector | Low — unless GCP is used |
| **SOAP/WSDL** | Generic connector for SOAP services | As needed |

### Special Note: SDDC Manager & NSX via REST Connector

MEHO's **generic REST connector** can ingest any OpenAPI specification. This means we can connect to:

- **SDDC Manager API** — lifecycle operations, workload domain management, upgrade orchestration
- **NSX Manager API** — network segments, firewall rules, load balancers, VPN
- **Aria Operations API** — alerts, metrics, recommendations (if deployed)
- **Aria Automation API** — catalog, deployments, blueprints (if deployed)

We upload the OpenAPI spec, MEHO parses every endpoint, and the AI agent can use them immediately. No custom connector development needed — just point it at the API.

---

## How a Pilot Would Work

### Phase 0: Setup (Week 1) — Zero Effort from Cloud Platform Team

| Task | Who | Effort |
|------|-----|--------|
| Deploy MEHO (Docker Compose) on a management VM | Haris + MEHO team | 2 hours |
| Create a read-only vCenter service account | Haris | 15 minutes |
| Connect MEHO to vCenter | MEHO team | 30 minutes |
| Upload VCF documentation (admin guides, release notes) | MEHO team | 1 hour |
| Upload internal runbooks (whatever is available) | Haris | As available |

**Total effort from cloud platform team: ~15 minutes** (create a service account). Everything else is handled by Haris and the MEHO development team.

### Phase 1: Knowledge Base (Weeks 1-2) — Ask MEHO About VCF

The team starts using MEHO as a VCF knowledge assistant:

- Ask VCF questions in natural language ("How do I expand a workload domain in VCF 9?")
- Search across all uploaded documentation
- Get answers that combine official docs, your internal runbooks, and LLM knowledge

**Success metric:** Team members find answers faster than searching VMware docs manually.

### Phase 2: Live Queries (Weeks 2-4) — Ask MEHO About Your Environment

Connect the vCenter connector and start querying live infrastructure:

- "Show me all hosts in the management domain with their CPU/memory utilization"
- "Are there any VMs with CPU ready time above 5%?"
- "Which datastore in cluster prod-01 has the most free space?"

**Success metric:** Routine health checks that took 15 minutes now take 30 seconds in chat.

### Phase 3: Expand Connections (Month 2+) — As the Team Wants

Based on what the team finds useful, we progressively connect more systems:

- NSX Manager via REST connector (network visibility)
- SDDC Manager via REST connector (lifecycle operations)
- Kubernetes if applicable
- Prometheus/Grafana if applicable
- Jira/Confluence if applicable

Each new connection is done by Haris and the MEHO team — **no additional workload on the cloud platform team**.

---

## What the Cloud Platform Team Gets

| Benefit | Description |
|---------|-------------|
| **24/7 VCF knowledge base** | Ask any VCF question, get answers from your docs + VMware documentation + LLM knowledge |
| **Faster troubleshooting** | Natural language queries instead of navigating vCenter UI for common checks |
| **Onboarding accelerator** | New team members ask MEHO instead of interrupting senior engineers |
| **Operational consistency** | Same procedures followed every time (MEHO references your runbooks) |
| **Cross-system visibility** | One place to query vSphere, Kubernetes, monitoring, and networking |
| **Living documentation** | Knowledge base grows as you upload more docs and MEHO learns your environment |

### What It Costs the Cloud Platform Team

| Item | Cost |
|------|------|
| Time to set up | ~15 minutes (create service account) |
| Ongoing maintenance | Zero (Haris + MEHO team handle everything) |
| Infrastructure | One small VM for MEHO (4 vCPU, 8GB RAM, 50GB disk) |
| Training | None (it's a chat interface — type questions, get answers) |
| Risk | Zero (read-only access, no write operations without explicit approval) |

---

## What We Get (Transparency)

We want to be upfront about why this is also valuable for us:

1. **Real-world VCF testing** — MEHO's VMware connector has 179 operations but hasn't been tested against a production VCF environment. Your platform is the perfect testbed.
2. **Feature feedback** — What questions does the team actually ask? What's missing? What's confusing? This shapes our roadmap.
3. **Knowledge base validation** — How well does MEHO handle large VMware documentation sets? We've already identified challenges with 150MB PDFs — your usage helps us improve.
4. **Cross-system scenarios** — VCF + K8s + monitoring + networking is exactly the multi-system environment MEHO is designed for. Real scenarios > synthetic tests.
5. **Reference story** — "evoila uses MEHO to operate their cloud platform" is a powerful statement for future customers.

This is a genuine partnership. We build features the team actually needs, and the team gets an AI assistant that improves every week.

---

## Security & Access Model

| Concern | How MEHO handles it |
|---------|-------------------|
| **Data residency** | MEHO runs entirely inside your infrastructure (Docker on a management VM). No data leaves the environment except LLM API calls. |
| **LLM data** | Queries sent to the LLM provider (Anthropic/OpenAI) contain only the conversation context, not raw API credentials. Option: use Ollama for fully local LLM (no external calls at all). |
| **vCenter access** | Read-only service account. MEHO cannot modify VMs, hosts, or configuration unless explicitly configured and approved. |
| **Authentication** | Keycloak OIDC — each operator has their own login. Audit trail of all queries. |
| **Credential storage** | AES-256-GCM encrypted at rest. Credentials never appear in logs or chat. |
| **Multi-tenancy** | Tenant isolation built-in. Cloud platform data is completely separated from other tenants. |

---

## Architecture (How It Fits)

```
┌─────────────────────────────────────────────────────────────┐
│                    Hetzner Co-Location                       │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              VMware Cloud Foundation                   │   │
│  │                                                       │   │
│  │   vCenter ◄──── MEHO (read-only) ────► NSX Manager   │   │
│  │   vSAN         (Docker VM)              SDDC Manager  │   │
│  │   ESXi hosts        │                                 │   │
│  │                     │                                 │   │
│  │                     ▼                                 │   │
│  │              ┌──────────────┐                         │   │
│  │              │  Knowledge   │                         │   │
│  │              │  Base (KB)   │                         │   │
│  │              │              │                         │   │
│  │              │ • VCF docs   │                         │   │
│  │              │ • Runbooks   │                         │   │
│  │              │ • KB articles│                         │   │
│  │              └──────────────┘                         │   │
│  │                                                       │   │
│  │   Kubernetes ◄──── MEHO ────► Prometheus/Grafana      │   │
│  │   (if applicable)           (if applicable)           │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  Operators access MEHO via browser ───► https://meho.local  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Example VCF Scenarios

Here are realistic scenarios where MEHO adds immediate value for a VCF operations team:

### Scenario 1: "Is my cluster healthy?"
> **Operator:** "Give me a health overview of all clusters in the management domain"
>
> **MEHO:** Queries vCenter → lists clusters with HA status, DRS configuration, host count, CPU/memory utilization, active alarms → presents a clear summary table

### Scenario 2: "Customer VM is slow"
> **Operator:** "VM tenant-x-app-01 is reported as slow, investigate"
>
> **MEHO:** Gets VM details → checks CPU ready time, memory ballooning, disk latency → checks the host it runs on → checks co-located VMs → checks datastore performance → synthesizes: "The VM is experiencing high CPU ready time (8.2%) because host esxi-05 is overcommitted. DRS is disabled on this cluster. Recommend enabling DRS or manually migrating 2 VMs to esxi-06 which has 40% CPU headroom."

### Scenario 3: "How do I upgrade VCF?"
> **Operator:** "What's the upgrade path from VCF 5.1 to VCF 9.0?"
>
> **MEHO:** Searches uploaded VCF documentation + VMware interoperability matrix knowledge → provides step-by-step upgrade path with prerequisites, compatibility notes, and links to relevant docs

### Scenario 4: "What changed last night?"
> **Operator:** "Show me all vCenter events from the last 12 hours that involved configuration changes"
>
> **MEHO:** Queries vCenter events API → filters for configuration changes → presents timeline: "3 events found: 1) VM migration at 02:15, 2) DRS recommendation applied at 03:30, 3) Host entered maintenance mode at 04:00"

### Scenario 5: "vSAN capacity planning"
> **Operator:** "How much vSAN capacity do we have left, and at the current growth rate, when will we run out?"
>
> **MEHO:** Queries vSAN capacity → queries historical usage from Prometheus (if connected) → calculates trend → "Current vSAN utilization: 68% (12.4 TB used of 18.2 TB). Based on 2.1% monthly growth over the last 6 months, you'll reach 80% in approximately 3 months (June 2026). Consider planning a capacity expansion."

---

## Frequently Asked Questions

**Q: Does MEHO replace our monitoring tools (Prometheus, Grafana, Aria)?**
No. MEHO complements them. It queries your existing monitoring tools and adds natural language access + cross-system reasoning on top. Your dashboards and alerts stay exactly as they are.

**Q: Can MEHO break things?**
Not in the pilot configuration. We connect with a read-only service account. MEHO can look at everything but change nothing. Write operations (like VM migration) require explicit enablement AND per-operation approval in the chat UI.

**Q: Do we need to learn a new tool?**
It's a chat window. If you can use Slack, you can use MEHO. Type a question, get an answer.

**Q: What if MEHO gives wrong information?**
MEHO always shows its reasoning chain — which operations it called, what data it received, how it reached its conclusion. You can verify every step. It's a diagnostic assistant, not an autonomous operator.

**Q: What about data privacy?**
MEHO runs inside your infrastructure. The only external calls are to the LLM API (Anthropic/OpenAI) for language processing. These calls contain conversation context, not raw credentials or sensitive infrastructure data. For maximum isolation, we can use Ollama (fully local LLM, zero external calls).

**Q: How much maintenance does it need?**
Haris and the MEHO team handle all updates, connector configuration, and knowledge base management. The cloud platform team just uses the chat interface.

---

## Next Steps

1. **Haris schedules a 30-minute demo** with the cloud platform team — show MEHO querying a vCenter environment live
2. **Team decides if they want to try it** — no commitment, no contract, just a pilot
3. **Haris creates a read-only vCenter service account** — 15 minutes
4. **MEHO team deploys and configures** — same week
5. **Team starts using it** — ask VCF questions, query the environment, provide feedback

That's it. If the team loves it, we expand. If they don't, we shut it down. Zero risk.

---

*MEHO — Machine Enhanced Human Operator*
*Built by evoila Bosnia. Open source (AGPL-3.0).*
