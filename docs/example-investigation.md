# Example Investigation

> Last verified: v2.0

This page demonstrates MEHO's core capability: tracing a problem across multiple systems in a single conversation. No context switching between tools, no copy-pasting between dashboards -- just natural language and cross-system reasoning.

## The Scenario

A team lead messages the on-call engineer at 9:47 AM:

> "Checkout is slow in production. Customers are complaining about timeouts. Can you take a look?"

The on-call engineer opens MEHO and starts an investigation.

---

## The Investigation

### Step 1: Initial Assessment

**Operator:**
> "Checkout is slow in production. Customers are reporting timeouts. What's going on?"

**MEHO** begins by checking metrics across the stack. It queries Prometheus for service-level indicators:

!!! note "MEHO's reasoning"
    The operator mentioned "checkout" and "slow" -- I'll start with latency metrics for the checkout service, then expand if needed. Querying Prometheus for P95 latency and error rates.

**MEHO:**
> I'm seeing elevated latency on `checkout-service`. Here's the current state:
>
> | Metric | Value | Normal Range |
> |--------|-------|-------------|
> | P95 latency | **4.2s** | 200-400ms |
> | Error rate (5xx) | **12.3%** | < 0.1% |
> | Request rate | 340 req/s | 300-400 req/s |
>
> Latency spiked 10x starting around 08:30 UTC. Request volume is normal, so this isn't a traffic surge. Let me check what's happening at the infrastructure level.

### Step 2: Kubernetes Investigation

MEHO automatically follows the trail to Kubernetes, querying pod status and resource utilization.

!!! note "MEHO's reasoning"
    High latency with normal request volume suggests a resource constraint, not a scaling issue. I'll check Kubernetes pod status for the checkout deployment.

**MEHO:**
> Found the issue at the pod level. The `checkout-service` deployment is struggling:
>
> | Pod | Status | Restarts (1h) | Memory Usage |
> |-----|--------|---------------|-------------|
> | checkout-7f8b9c-**x2k4p** | Running | 3 | 498Mi / 512Mi |
> | checkout-7f8b9c-**m9n1q** | OOMKilled | 5 | -- |
> | checkout-7f8b9c-**a3j7r** | Running | 2 | 487Mi / 512Mi |
>
> Two of three pods are at or near their 512Mi memory limit, and one is stuck in an OOMKill restart loop. This means the remaining pods are handling all traffic with degraded memory -- explaining the latency spike.

### Step 3: Log Analysis

MEHO queries Loki for error logs to understand *why* memory consumption is high.

!!! note "MEHO's reasoning"
    OOMKills suggest memory leak or increased memory demand. Checking Loki for error logs and memory-related warnings on the checkout pods.

**MEHO:**
> The logs tell a clear story. Here are the most frequent errors from `checkout-service` in the last 2 hours:
>
> ```
> [ERROR] HikariPool-1 - Connection is not available, request timed out after 30000ms
> [WARN]  Connection pool exhausted (active: 20/20, pending: 47)
> [ERROR] Failed to acquire connection for transaction: checkout.processOrder
> ```
>
> The database connection pool is completely exhausted. Every request is waiting for a connection, which keeps the request objects in memory longer than normal -- that's what's driving the OOM kills.

### Step 4: Topology Cross-Reference

MEHO checks the topology graph to understand the infrastructure underneath.

!!! note "MEHO's reasoning"
    Connection pool exhaustion could be caused by a slow database, an undersized pool, or too many concurrent requests per pod. Let me check the underlying infrastructure. The topology graph maps these K8s pods to their host nodes and the VMs underneath.

**MEHO:**
> Cross-referencing the topology graph, these checkout pods run on Kubernetes node `k8s-worker-03`, which maps to VMware VM `prod-worker-03` on ESXi host `esxi-rack2-01`. The VM's resource allocation:
>
> | Resource | Allocated | Host Available |
> |----------|-----------|---------------|
> | vCPU | 4 cores | 2.1 cores idle |
> | Memory | 8 GB | 1.2 GB free |
>
> The host is not the bottleneck -- there's capacity available. The constraint is at the Kubernetes resource limit level.

### Step 5: Root Cause -- Recent Deployment

MEHO checks GitHub for recent changes to the checkout service.

!!! note "MEHO's reasoning"
    The latency spike started at 08:30 UTC. Let me check GitHub for recent deployments to checkout-service around that time.

**MEHO:**
> Found it. There was a deployment at **08:22 UTC** this morning:
>
> | Field | Value |
> |-------|-------|
> | PR | [#847](https://github.com) "Add order history caching" |
> | Author | @sarah-dev |
> | Merged | 08:18 UTC |
> | Deployed | 08:22 UTC (ArgoCD auto-sync) |
>
> The PR added an in-memory LRU cache for order history lookups. The cache has no size bound and is loading order objects into memory on every request. This increased per-pod memory consumption from ~300Mi to 500Mi+, pushing pods past their 512Mi limit.
>
> **Root cause:** PR #847 added unbounded in-memory caching, increasing memory requirements beyond the pod's 512Mi resource limit. This caused OOMKills, which reduced available pods, which overloaded the connection pool on remaining pods, which caused the checkout timeouts customers are seeing.

---

## Resolution

**Operator:**
> "Can you scale up the memory limit to 1Gi as a quick fix while we add a cache size bound?"

**MEHO:**
> I can update the checkout-service deployment to increase the memory limit from 512Mi to 1Gi. This is a **WRITE** operation that will trigger a rolling restart of all checkout pods.

!!! warning "Trust Model -- Approval Required"
    MEHO identified this as a **WRITE** operation (modifying a Kubernetes deployment). It will not execute without explicit operator approval. The operator sees an approval dialog with the exact change being made.

**Operator approves the change.**

**MEHO:**
> Done. The checkout-service deployment memory limit has been updated to 1Gi. Rolling restart in progress:
>
> | Pod | Status |
> |-----|--------|
> | checkout-7f8b9c-**x2k4p** | Terminating |
> | checkout-7f8b9c-**n4w2d** | Running (1Gi limit) |
> | checkout-7f8b9c-**a3j7r** | Terminating |
> | checkout-7f8b9c-**p8k5f** | Running (1Gi limit) |
>
> I'd recommend the team also:
>
> 1. Add a `maxSize` parameter to the LRU cache in PR #847
> 2. Set up a Prometheus alert for pods approaching 80% memory utilization
> 3. Consider adding memory resource requests alongside limits for better scheduling

---

## What Just Happened

In a single conversation, MEHO:

1. **Queried Prometheus** -- identified latency spike and error rates
2. **Queried Kubernetes** -- found OOMKilled pods at memory limits
3. **Queried Loki** -- discovered connection pool exhaustion in error logs
4. **Checked Topology** -- traced pods to underlying VM and host resources
5. **Queried GitHub** -- found the recent deployment that caused the issue
6. **Executed a fix** -- scaled the memory limit (with operator approval)

**Five systems. One conversation. One root cause.** No tab-switching between Grafana, kubectl, Kibana, vCenter, and GitHub. No copy-pasting pod names between tools. MEHO maintained context across every query and traced the problem from symptom to root cause automatically.

### The Data Pipeline at Work

Throughout this investigation, MEHO's [data pipeline](how-it-works.md) was working behind the scenes:

- Prometheus returned 2,400 time series data points -- MEHO's JSONFlux engine normalized them into Arrow tables, and DuckDB SQL reduced them to the 3 relevant metrics shown above
- Kubernetes returned full pod specs (12KB each) -- reduced to the status, restart count, and memory fields that mattered
- Loki returned 847 log lines -- deduplicated and frequency-ranked to surface the connection pool errors
- GitHub returned the full PR diff (340 lines) -- summarized to the relevant cache change

The operator saw clean, focused data at every step. The LLM maintained full context without exceeding token limits. That's the [JSONFlux pipeline](how-it-works.md) at work.

### Trust at Every Step

Every operation MEHO performed was classified:

| Operation | Trust Level | Action |
|-----------|-------------|--------|
| Query Prometheus metrics | READ | Executed automatically |
| List Kubernetes pods | READ | Executed automatically |
| Query Loki logs | READ | Executed automatically |
| Query topology graph | READ | Executed automatically |
| List GitHub PRs | READ | Executed automatically |
| Update deployment limits | **WRITE** | Required operator approval |

READ operations execute without interruption. WRITE operations always require explicit approval. DESTRUCTIVE operations (like deleting a pod or force-pushing) would require additional confirmation. See [Trust & Safety](trust-and-safety.md) for the full trust model.
