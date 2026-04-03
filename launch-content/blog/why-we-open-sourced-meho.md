---
title: "Why We Open-Sourced MEHO"
author: "Damir Topic"
date: "YYYY-MM-DD"
description: "The story behind MEHO — an open-source AI agent that connects to your entire infrastructure stack and reasons across all systems simultaneously. Why we built it, why we open-sourced it, and why every connector is free."
tags: ["open-source", "devops", "observability", "infrastructure", "ai-ops"]
---

# Why We Open-Sourced MEHO

## The Problem Nobody Talks About

It is 2 AM. PagerDuty fires. The checkout service is slow. You open Grafana and see the latency spike — p99 jumped from 120ms to 2.4 seconds. Okay, but why?

You switch to the Kubernetes dashboard. A pod has been OOMKilled three times in the last hour. Memory limit is 512Mi, usage peaked at 510Mi. The node is under memory pressure. But what is causing the node pressure?

You SSH into the node. Top shows memory is exhausted. But this is a VM, not bare metal. You switch to vSphere and find the ESXi host running at 92% memory utilization. Memory ballooning is active across six VMs, including the one your K8s node runs on. Now you need to figure out if this is a host issue or a workload issue.

You open Loki and search for error logs. "Connection pool exhausted, waiting for available connection" — 47 occurrences in the last 30 minutes. GC pauses of 340ms. You check ArgoCD and discover someone deployed a new version 30 minutes ago that added an unbounded in-memory cache.

Forty-five minutes. Six tools. Three browser tabs. Two terminal windows. One root cause that spans five different systems. This is the daily reality of operating production infrastructure — and it is the daily reality for our team at evoila.

## What We Built

At evoila, we are a managed services provider. Our engineers operate customer infrastructure across Kubernetes, VMware, GCP, the entire PLG monitoring stack, CI/CD pipelines, and everything in between. The cross-system debugging problem is not theoretical for us. It is the core of our daily work. Every engineer on the team knows the pain of context-switching between six dashboards to diagnose a single incident.

MEHO (Machine Enhanced Human Operator) started as an internal tool to solve exactly this problem. The idea was simple: what if there was a single system that could connect to everything in our stack and reason across all of it simultaneously?

Today, MEHO connects to 15 different infrastructure systems: Kubernetes, VMware, Proxmox, GCP, Prometheus, Loki, Tempo, Alertmanager, ArgoCD, GitHub, Jira, Confluence, plus generic REST and SOAP connectors for anything with an API. When you ask MEHO a question — "Why is the checkout service slow?" — it dispatches parallel queries to every relevant system. Prometheus returns the latency metrics. Kubernetes reports the OOMKilled pods. VMware surfaces the host memory pressure. Loki delivers the error logs. ArgoCD identifies the recent deployment. All in the same conversation, all with actual data from the live systems.

The key insight that makes MEHO different from other AI ops tools is the topology graph. MEHO automatically discovers relationships between entities across different systems. It knows that a Kubernetes pod runs on a node, that node is a VM on an ESXi host, and that ESXi host is in a specific cluster. These cross-system identity links — we call them SAME_AS edges — are what enable tracing a Prometheus latency alert all the way down to a VMware hypervisor quota issue. The resolution is deterministic: provider IDs, IP addresses, and hostnames are matched with priority ordering, not guesswork.

The investigation results preserve actual data, not summaries. When MEHO queries Prometheus, you see the real metrics. When it queries Kubernetes, you see the actual pod events. The LLM reasons over raw data from every system, then synthesizes a root cause with specific evidence: "payment-svc v2.4.0 (deployed 14:28) added an unbounded in-memory cache, increasing baseline memory usage. Combined with ESXi host esxi-prod-03 at 92% memory causing ballooning on the node-7 VM, the pod hits its 512Mi limit and OOMKills." One question, every layer, actual root cause.

## Why Open-Core

We chose an open-core model for MEHO, and we want to be honest about why.

Community solves the technical problem. Enterprise solves the organizational problem.

The community edition gets everything a single engineer needs to debug production infrastructure at 2 AM. That means all 15 connectors — no connector paywalls. Cross-system reasoning. Topology auto-discovery and entity resolution. The knowledge base with hybrid search. The memory system. Multi-LLM support (Anthropic Claude, OpenAI, or Ollama for fully air-gapped deployments). Local embeddings via a TEI sidecar so zero data needs to leave your network. Dual-mode chat — Ask for quick knowledge lookups, Agent for full investigations. The JSONFlux data pipeline that achieves 81% token reduction while preserving raw data structure. No artificial limits.

Enterprise adds multi-tenancy with isolated Keycloak realms per tenant, SSO/SAML integration with corporate identity providers, audit log export and compliance reporting, group sessions and team collaboration, and priority support with SLA. These are organizational features — things that matter when the whole team is using MEHO, when compliance requires audit trails, and when you need to isolate customer environments.

The split is deliberate. A single engineer debugging at 2 AM needs the full investigation engine, not a crippled version with half the connectors disabled. Enterprise features only matter when the whole team needs corporate SSO, audit compliance, and multi-tenant isolation. We would rather have a thousand engineers using the free version effectively than fifty frustrated users hitting artificial walls.

The licensing is AGPLv3 for the open-source core. In practical terms: you can freely use, modify, and deploy MEHO on your own infrastructure. AGPL only requires sharing source code if you modify MEHO and offer it as a network service to others. Internal use — which is the primary use case for an infrastructure debugging tool — has zero licensing obligations. This is the same licensing model used by Grafana and MongoDB, and for the same reason: it prevents cloud vendors from strip-mining the project while keeping it genuinely free for every engineer who deploys it internally.

Our strategy is adoption-first. We want stars and users before enterprise sales. Every GitHub star is a potential enterprise conversation 6 to 12 months later. We believe the product is strong enough that the right path is getting it into as many hands as possible, not gating features behind a paywall and hoping someone clicks "Contact Sales."

## What Is In The Box

Getting started takes about five minutes:

```bash
git clone GITHUB_URL && cd meho
cp env.example .env
# Set your LLM provider API key (or use Ollama for fully local)
docker compose up -d
```

That gives you the full MEHO stack: the FastAPI backend, the React frontend, PostgreSQL with pgvector for hybrid search, Redis, Keycloak for authentication (pre-configured community realm — no setup needed), and optionally the TEI sidecar containers for local embeddings.

MEHO supports three LLM providers out of the box. Anthropic Claude delivers the best investigation quality — it is what we use internally and what we recommend. OpenAI is a solid alternative with broad availability. Ollama enables fully air-gapped deployments where zero data leaves your network. You switch providers by setting a single environment variable. We publish a model compatibility matrix that documents quality differences honestly across providers and model tiers.

The local embedding provider runs two TEI (Text Embeddings Inference) containers as Docker sidecars: one for bge-m3 embeddings and one for bge-reranker-v2-m3 reranking. This means the entire knowledge base — hybrid search with BM25 plus semantic similarity, followed by cross-encoder reranking — runs locally on your hardware. No Voyage AI account needed. No embedding data sent to external APIs. CPU-optimized by default, with GPU instructions available for higher throughput.

Connect your first system (a Kubernetes cluster, a Prometheus instance, a VMware vCenter), start a chat, and ask a question. MEHO handles the rest — querying the right systems, extracting entities, building the topology graph, and synthesizing the answer with actual evidence from your infrastructure.

## Why Now

Most AI operations tools on the market today are single-system, cloud-only SaaS products. They might help you query Kubernetes or parse your logs, but they do not trace a problem across your entire stack. The cross-system reasoning — connecting a Prometheus alert to an underlying VMware resource issue through Kubernetes pod events — is what takes 45 minutes of manual work and deep expertise in every system involved.

We have been building MEHO for months, shipping 12 milestones across 81 phases. The platform has 15 connectors, cross-system entity resolution with deterministic matching, a topology graph that auto-discovers infrastructure relationships, a proven investigation workflow with budget-aware convergence detection, and a fully redesigned frontend with investigation visualization. The differentiator is real and it works.

An open-source, self-hosted alternative with superior cross-system reasoning is a position nobody else occupies. We think the world needs it. So we are shipping it.

## Try It

```bash
git clone GITHUB_URL && cd meho && docker compose up -d
```

Connect your systems, start asking questions. Visit [MEHO_AI_URL](MEHO_AI_URL) for the full story, documentation, and the investigation demo.

If this resonates, star us on [GitHub](GITHUB_URL). If you run into issues, open a GitHub issue or join us on Discord. If you want to contribute, check out the CONTRIBUTING guide — we have a set of good-first-issues waiting.

Closing the expertise gap is not something we can do alone. But we think we have built a strong foundation. Come build on it with us.
