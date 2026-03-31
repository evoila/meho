# Dev.to Article

The Dev.to frontmatter block below should be placed at the top of the article when publishing on Dev.to.

```yaml
---
title: "How MEHO traces a Prometheus alert to its VMware root cause in one question"
published: false
description: "A walkthrough of setting up MEHO and running your first cross-system investigation"
tags: devops, opensource, kubernetes, monitoring
canonical_url: "MEHO_AI_URL/blog/cross-system-reasoning"
---
```

---

## The Problem: Root Causes Don't Stay in One System

When something breaks in production, the root cause is rarely in the system that fired the alert. A Prometheus latency spike might be caused by a Kubernetes pod OOMKilling because the underlying VMware host is overcommitted on memory. A deployment from ArgoCD four minutes earlier pushed the application's memory baseline past its limit. The error logs in Loki show connection pool exhaustion as a symptom, not the cause.

Tracing this chain manually takes 30-45 minutes across five or six different tools, each with its own query language and mental model. It also requires expertise in every system involved -- and most engineers specialize in a subset.

MEHO (Machine Enhanced Human Operator) is an open-source AI agent that connects to your infrastructure systems and runs cross-system investigations via natural language. You ask one question, and it queries every relevant system in parallel, links entities across system boundaries, and synthesizes a root cause backed by evidence from each layer.

This walkthrough shows you how to set it up and run your first investigation.

## Getting Started

Prerequisites: Docker and Docker Compose installed. An LLM provider -- either an Anthropic API key, an OpenAI API key, or Ollama running locally.

```bash
git clone GITHUB_URL && cd meho
cp env.example .env
```

Open `.env` and set your LLM provider:

```bash
# Option A: Anthropic Claude (recommended for best investigation quality)
MEHO_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Option B: OpenAI
MEHO_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Option C: Ollama (fully local, no API key needed)
MEHO_LLM_PROVIDER=ollama
```

Start the stack:

```bash
docker compose up -d
```

This brings up the FastAPI backend, React frontend, PostgreSQL with pgvector, Redis, and a pre-configured Keycloak instance. If you chose Ollama, add the Ollama profile: `docker compose --profile ollama up -d`.

Open `http://localhost:5173` -- you should see the MEHO chat interface. The default community realm is pre-configured with an admin user, so there's nothing to set up.

## Connect Your Systems

Navigate to **Settings > Connectors** in the UI. Click "Add Connector" and select the type -- Kubernetes, Prometheus, VMware, Loki, or any of the 15 supported types.

For a Kubernetes cluster, you'll provide the kubeconfig or service account token. For Prometheus, the endpoint URL and optional basic auth. For VMware, the vCenter URL and credentials. MEHO validates the connection and shows a green status indicator when it's ready.

Start with at least two connectors from different layers. The cross-system reasoning becomes interesting when MEHO can trace problems across system boundaries -- for example, Kubernetes + VMware, or Prometheus + Loki + Kubernetes.

## Your First Investigation

Switch to the chat interface and type:

> The payment service has been slow for the last 30 minutes

MEHO enters Agent mode and starts investigating. Here's what happens behind the scenes:

1. **Routing:** The orchestrator evaluates your connected systems and determines which are relevant. It considers connector descriptions, relationships between connectors, and the nature of the question.

2. **Parallel dispatch:** MEHO queries multiple systems simultaneously. Prometheus returns latency metrics (p99 spiked from 120ms to 2.4s, error rate at 12.3%). Kubernetes reports that pod `payment-svc-7b9f4d-xk2p4` on `node-7` was OOMKilled three times. Loki finds 47 "connection pool exhausted" errors. VMware reveals that ESXi host `esxi-prod-03` is at 92% memory with ballooning active on the VM backing `node-7`. ArgoCD shows a deployment from v2.3.1 to v2.4.0 four minutes before the latency spike.

3. **Entity resolution:** MEHO extracts entity references from the results and looks them up in the topology graph. It discovers that K8s node `node-7` and VMware VM `node-7` are the same physical resource -- linked by a SAME_AS edge. This resolution is deterministic (provider IDs > IP addresses > hostnames), not LLM-based.

4. **Synthesis:** The agent reasons across all the evidence and produces a root cause:

> payment-svc v2.4.0 (deployed 14:28) added an unbounded in-memory cache, increasing baseline memory usage. Combined with ESXi host esxi-prod-03 at 92% memory causing ballooning on the node-7 VM, the pod hits its 512Mi limit and OOMKills. Remaining pods overloaded, saturating the connection pool (20/20 active, 47 pending), driving p99 latency from 120ms to 2.4s.

Every claim in the synthesis is backed by specific data from a specific system. The root cause spans five systems and two infrastructure layers -- something that would take 30+ minutes to piece together manually.

## How It Works

MEHO's cross-system reasoning relies on three components:

**Typed connectors** execute operations against each system's API and return raw data. The 15 built-in connectors cover Kubernetes, VMware, Proxmox, GCP, Prometheus, Loki, Tempo, Alertmanager, ArgoCD, GitHub, Jira, Confluence, plus generic REST and SOAP for anything with an API. Raw results are preserved -- the LLM sees actual metrics and events, not pre-summarized data.

**The topology graph** stores entities discovered during investigations and their relationships. Within a single system, entities have standard relationships (`runs_on`, `routes_to`, `uses_storage`). Across systems, SAME_AS edges link entities that represent the same physical or logical resource. The `DeterministicResolver` creates these edges using provider ID matching, IP address matching, and hostname matching -- in strict priority order with no LLM involvement.

**The ReAct loop** drives the investigation. The orchestrator reasons about which systems to query, observes the results, and decides whether to query additional systems or synthesize. An investigation budget (default: 20 automated dispatches) prevents runaway investigations, and convergence detection identifies when additional queries stop producing new information.

For a deeper technical dive with code snippets from the actual codebase, see the full article: DEEP_DIVE_URL

## What's Next

MEHO's community edition includes all 15 connectors, full cross-system reasoning, topology mapping, knowledge base with hybrid search, multi-LLM support, and local embeddings. No artificial limits. No connector paywalling. AGPLv3 license.

Enterprise adds organizational features: multi-tenancy, SSO/SAML, audit compliance, and team collaboration. The community edition is the complete technical product.

Get started: GITHUB_URL
Website and interactive demo: MEHO_AI_URL
Read why we open-sourced MEHO: BLOG_URL
Join the community on Discord and help shape what comes next.
