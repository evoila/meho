---
title: "How MEHO Traces a Prometheus Alert to Its VMware Root Cause in One Conversation"
author: "Damir Topic"
date: "YYYY-MM-DD"
description: "A technical deep-dive into MEHO's cross-system reasoning engine — entity extraction, deterministic SAME_AS resolution, topology traversal, and how an AI agent traces a latency spike across Prometheus, Kubernetes, Loki, VMware, and ArgoCD to identify a root cause that spans five different systems."
tags: ["cross-system-reasoning", "topology", "entity-resolution", "devops", "ai-ops", "open-source"]
---

# How MEHO Traces a Prometheus Alert to Its VMware Root Cause in One Conversation

Most AI operations tools query one system at a time. MEHO queries all of them simultaneously, then uses a topology graph with deterministic entity resolution to trace problems across system boundaries. This article walks through exactly how that works — with real code, real data structures, and a concrete investigation scenario.

## The Scenario

An ESXi host, `esxi-prod-03`, is running at 92% memory utilization (187GB out of 204GB). Memory ballooning is active across six virtual machines, including `node-7` — a Kubernetes worker node. The memory pressure squeezes `node-7`, causing pod `payment-svc-7b9f4d-xk2p4` to OOMKill repeatedly as it hits its 512Mi memory limit. The connection pool exhausts, and p99 latency spikes from 120ms to 2.4 seconds.

Making it worse: someone deployed `payment-svc` v2.4.0 thirty minutes ago, which added an unbounded in-memory cache that pushed the pod's baseline memory usage dangerously close to its limit. The deployment alone might not have caused the OOMKill — but combined with the ESXi-level memory ballooning, the pod had no headroom.

An operator sees a Prometheus alert: `PaymentServiceHighLatency`. Finding the root cause manually requires correlating data across five systems — Prometheus, Kubernetes, Loki, VMware, and ArgoCD — each with its own dashboard, query language, and mental model. MEHO does this in one conversation.

## Traditional Troubleshooting

Open Grafana. The latency spike is obvious — p99 went from 120ms to 2.4 seconds at 14:32 UTC. Error rate jumped to 12.3%. Request volume is normal, so it is not a traffic surge. This rules out the easy explanation.

Switch to the Kubernetes dashboard. Pod `payment-svc-7b9f4d-xk2p4` on `node-7` has been OOMKilled three times in the last hour. Memory usage peaked at 510Mi against a 512Mi limit. The node itself is under memory pressure. But why? Is it a noisy neighbor? A node-level issue? Something underneath the node?

Check the underlying VM. Open vSphere, find `node-7`, discover it is running on `esxi-prod-03`. The host is at 92% memory with ballooning active on six VMs. Now you know the infrastructure layer is involved — but what triggered the application-layer failure?

Search Loki for `payment-svc` logs. "Connection pool exhausted, waiting for available connection" appears 47 times. GC pauses of 340ms suggest the JVM is struggling with memory. Check ArgoCD: `payment-svc` image was updated from v2.3.1 to v2.4.0 at 14:28 UTC. The commit message mentions an in-memory cache.

Total time: 30-45 minutes. Requires expertise in Prometheus, Kubernetes, VMware, Loki, and ArgoCD. The root cause — a deployment that increased memory baseline, combined with ESXi-level memory ballooning — spans five systems and two infrastructure layers. No single dashboard shows it.

## How Cross-System Reasoning Works

MEHO's approach has four components: an orchestrator that decides which systems to query, connectors that execute those queries in parallel, an entity resolution system that links discovered entities across systems, and a synthesis step that reasons over the combined evidence.

### The Orchestrator and ReAct Loop

When the user asks "The payment service has been slow for the last 30 minutes," the orchestrator enters a ReAct (Reason-Act-Observe) loop. It evaluates the available connectors, reasons about which systems are relevant, and dispatches parallel queries.

The routing decision considers connector descriptions, relationships between connectors (a Kubernetes cluster might be "related to" a VMware vCenter and a Prometheus instance), and what has already been queried in this investigation. Each dispatch is a deliberate decision, not a broadcast to every connector.

```
Available connectors:
- K8s Prod (Type: kubernetes) (related: VMware Prod, Prometheus)
- VMware Prod (Type: vmware) (related: K8s Prod)
- Prometheus (Type: prometheus) (related: K8s Prod, Loki)
- Loki (Type: loki) (related: Prometheus)
- ArgoCD (Type: argocd)
```

For this investigation, the orchestrator dispatches to all five in parallel. The connectors execute typed operations against each system's API and return raw results — actual metrics, actual pod specs, actual host statistics. Nothing is summarized before the LLM sees it.

The investigation runs under a configurable budget (default: 20 automated dispatches for automated sessions, 30 for interactive). The orchestrator tracks visited connectors and remaining budget, forcing synthesis when the budget is exhausted. This prevents runaway investigations that query the same system in circles.

### Entity Extraction

As results flow back from each connector, MEHO extracts entity references. The `EntityExtractor` parses user messages and connector results for infrastructure names using pattern matching:

```python
class EntityExtractor:
    """
    Extracts potential entity references from user messages.
    The goal is to find anything that might be an entity name so we can
    look it up in the topology database. False positives are OK (we'll
    just get "not found" from lookup), but we want high recall.
    """

    KNOWN_PREFIXES = [
        "pod/", "deployment/", "service/", "node/",
        "vm/", "host/", "cluster/", "namespace/",
    ]

    K8S_NAME_PATTERN = re.compile(
        r"\b([a-z][a-z0-9-]{2,}(?:-[a-z0-9]{4,10}){0,3})\b",
        re.IGNORECASE,
    )

    HOSTNAME_PATTERN = re.compile(
        r"\b([a-z][a-z0-9-]*-\d+)\b", re.IGNORECASE
    )

    IP_PATTERN = re.compile(
        r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
    )
```

From the investigation results, the extractor identifies `payment-svc-7b9f4d-xk2p4` (K8s name pattern), `node-7` (hostname pattern), and `esxi-prod-03` (hostname pattern). These references are looked up in the topology database to find known entities and their relationships.

### SAME_AS Edges: The Cross-System Link

This is the core data structure that makes cross-system reasoning possible. A SAME_AS relationship declares that two entities from different connectors represent the same physical or logical resource:

```python
class TopologySameAsModel(Base):
    """
    SAME_AS relationships — cross-connector correlation.

    When entities from different connectors represent the same
    real-world thing:
    - K8s Node "node-01" <-> VMware VM "k8s-worker-01"
    - DNS record "shop.example.com" <-> K8s Ingress "shop-ingress"
    """
    __tablename__ = "topology_same_as"

    entity_a_id = Column(UUID, ForeignKey("topology_entities.id"))
    entity_b_id = Column(UUID, ForeignKey("topology_entities.id"))
    similarity_score = Column(Float)
    verified_via = Column(ARRAY(Text))
    # e.g., ["IP: 192.168.1.10", "Both exist in APIs"]
```

Each SAME_AS relationship records how it was discovered — which matching evidence led to the correlation, and what confidence level it carries. This provenance is critical: when the agent traverses a SAME_AS edge during an investigation, it can evaluate whether the cross-system link is trustworthy.

The topology also stores standard relationships within a single system — `runs_on`, `routes_to`, `uses_storage` — creating a full graph of how infrastructure components relate to each other both within and across systems.

```
                   SAME_AS
K8s Node node-7 -----------> VMware VM node-7
     ^                            |
     | runs_on                    | runs_on
     |                            v
K8s Pod payment-svc       ESXi Host esxi-prod-03
```

See the full entity relationship diagram: [same-as-edges.mmd](diagrams/same-as-edges.mmd)

### Deterministic Resolution: How SAME_AS Edges Are Created

MEHO uses a `DeterministicResolver` that applies matchers in strict priority order. No LLM is involved in entity resolution — it is entirely deterministic:

```python
class DeterministicResolver:
    """
    Orchestrates matchers in priority order for entity resolution.

    Usage:
        resolver = DeterministicResolver(matchers=[
            ProviderIDMatcher(),
            IPAddressMatcher(),
            HostnameMatcher(),
        ])

        evidence = resolver.resolve_pair(k8s_node, vmware_vm)
        if evidence and evidence.auto_confirm:
            # Create SAME_AS relationship
    """

    def resolve_pair(self, entity_a, entity_b):
        # Same connector check — entities from the same
        # connector cannot be SAME_AS
        if entity_a.connector_id == entity_b.connector_id:
            return None

        # SameAsEligibility — prevent nonsensical comparisons
        # (e.g., Pod vs VM is not eligible)
        if not self._are_eligible(entity_a, entity_b):
            return None

        # Try matchers in priority order: ProviderID > IP > Hostname
        for matcher in self.matchers:
            evidence = matcher.match(entity_a, entity_b)
            if evidence:
                return evidence

        return None
```

The priority order matters. `ProviderID` matching is the highest confidence: a Kubernetes node's `spec.providerID` field directly encodes which cloud VM backs it. The `ProviderIDMatcher` parses four formats:

```python
# GCE format: gce://project/zone/vm-name
_GCE_PATTERN = re.compile(r"^gce://([^/]+)/([^/]+)/(.+)$")

# vSphere format: vsphere://datacenter/vm/vm-moref
_VSPHERE_PATTERN = re.compile(r"^vsphere://([^/]+)/vm/(.+)$")

# AWS EKS format: aws:///availability-zone/instance-id
_AWS_PATTERN = re.compile(r"^aws:///([a-z0-9-]+)/(.+)$")

# Azure AKS format: azure:///subscriptions/{sub}/resourceGroups/{rg}/...
_AZURE_PATTERN = re.compile(
    r"^azure:///subscriptions/([^/]+)/resourceGroups/([^/]+)/"
    r"providers/Microsoft\.Compute/(?:virtualMachineScaleSets/"
    r"([^/]+)/virtualMachines/(\d+)|virtualMachines/(.+))$",
    re.IGNORECASE,
)
```

When providerID is not available (common in on-prem VMware environments where the Kubernetes cloud-provider is not configured), the resolver falls back to IP address matching and then hostname matching. The `HostnameMatcher` normalizes domain suffixes — stripping `.internal`, `.local`, `.compute.googleapis.com`, and GCP internal DNS patterns — before comparing:

```python
def normalize_hostname(hostname: str) -> str:
    """
    "node-01.internal" -> "node-01"
    "gke-cluster-abc.us-central1-a.c.myproject.internal"
        -> "gke-cluster-abc"
    "worker-01.local" -> "worker-01"
    """
    normalized = hostname.strip().lower()
    # Strip GCP internal DNS pattern first (most specific)
    normalized = _GCP_INTERNAL_PATTERN.sub("", normalized)
    # Iteratively strip fixed suffixes
    for suffix in _STRIP_SUFFIXES_FIXED:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
    return normalized
```

In our scenario, `node-7` from the Kubernetes connector matches `node-7` from the VMware connector via hostname matching at confidence 0.95. This creates the SAME_AS edge that links the Kubernetes layer to the VMware layer.

Each match produces a `MatchEvidence` object recording the match type, matched values, confidence score, and whether the match can be auto-confirmed:

```python
class MatchPriority(IntEnum):
    PROVIDER_ID = 1   # Highest priority
    IP_ADDRESS = 2
    HOSTNAME = 3      # Lowest priority

@dataclass
class MatchEvidence:
    match_type: str        # "provider_id", "ip_exact", "hostname_exact"
    matched_values: dict   # {"matched_hostname": "node-7", ...}
    confidence: float      # 0.0 to 1.0
    auto_confirm: bool     # Can be confirmed without human review
```

### Data Preservation

A critical design decision in MEHO is that raw connector results are preserved, not summarized. When Prometheus returns latency metrics, the agent sees the actual numbers. When Kubernetes returns pod events, the agent sees the actual OOMKill timestamps and memory figures. The JSONFlux data pipeline achieves 81% token reduction through structural compression — flattening nested JSON, removing redundant keys, compacting arrays — while preserving every data point.

This matters because accurate diagnosis requires specific evidence. "Memory is high" is not useful. "Pod memory usage peaked at 510Mi against a 512Mi limit, OOMKilled 3 times since 14:28 UTC" is actionable. The LLM can only produce specific synthesis if it sees specific data.

## The Architecture

The full investigation flow, from user question to root cause report:

See: [topology-resolution-flow.mmd](diagrams/topology-resolution-flow.mmd) for the complete pipeline diagram.

See: [investigation-sequence.mmd](diagrams/investigation-sequence.mmd) for the temporal sequence of a real investigation.

1. **User asks a question** — natural language, no query syntax
2. **Orchestrator reasons** about which connectors are relevant (ReAct loop)
3. **Parallel connector dispatch** — all five connectors queried simultaneously
4. **Raw results preserved** — actual metrics, events, logs, host stats, deployment records
5. **Entity extraction** — infrastructure names identified from results
6. **Topology graph updated** — new entities and relationships stored in PostgreSQL
7. **SAME_AS resolution** — deterministic matching links entities across connectors
8. **Cross-system synthesis** — LLM reasons over all evidence with topology context
9. **Root cause report** with specific data from every system

## The Investigation Result

Here is what the synthesis looks like for our scenario:

> **Root Cause:** payment-svc v2.4.0 (deployed 14:28) added an unbounded in-memory cache, increasing baseline memory usage. Combined with ESXi host esxi-prod-03 at 92% memory causing ballooning on the node-7 VM, the pod hits its 512Mi limit and OOMKills. Remaining pods overloaded, saturating the connection pool (20/20 active, 47 pending), driving p99 latency from 120ms to 2.4s.

This is not a guess. Every claim is backed by specific data from a specific system:

- **"p99 latency from 120ms to 2.4s"** — Prometheus connector, payment-svc RED metrics
- **"OOMKills, 512Mi limit"** — Kubernetes connector, pod events on node-7
- **"ESXi host esxi-prod-03 at 92% memory, ballooning"** — VMware connector, host resource stats
- **"Connection pool exhausted (20/20 active, 47 pending)"** — Loki connector, payment-svc error logs
- **"v2.4.0 deployed 14:28, added in-memory cache"** — ArgoCD connector, sync history

The synthesis identifies that neither the deployment nor the ESXi memory pressure alone would have caused the outage. It was the combination — a deployment that increased memory baseline on a host that was already overcommitted. This kind of cross-layer causal analysis is exactly what takes 45 minutes to do manually across five different tools.

## The Topology Graph

The SAME_AS edge between `K8s Node node-7` and `VMware VM node-7` is what makes this investigation possible. Without it, the Kubernetes findings and the VMware findings are isolated — two separate data points from two separate systems. With it, the agent can traverse from an OOMKilled pod to its node to the underlying VM to the ESXi host, building a causal chain that crosses system boundaries.

Each entity in the topology carries its connector context, its raw attributes from the source system, and timestamps for when it was discovered and last verified:

```python
class TopologyEntityModel(Base):
    __tablename__ = "topology_entities"

    name = Column(String(255), nullable=False)
    entity_type = Column(String(100))  # "Pod", "VM", "Host"
    connector_type = Column(String(50))  # "kubernetes", "vmware"
    canonical_id = Column(String(500))  # "prod/nginx" or moref
    raw_attributes = Column(JSONB)
    # {"ip": "192.168.1.10", "namespace": "prod"}
    discovered_at = Column(TIMESTAMP(timezone=True))
    last_verified_at = Column(TIMESTAMP(timezone=True))
```

The topology graph grows organically as investigations run. Every time MEHO queries a connector and discovers entities, they are stored, relationships are created, and SAME_AS resolution runs against existing entities from other connectors. Over time, the graph becomes a comprehensive map of how infrastructure components relate to each other — not because someone manually configured it, but because the system learned it through investigations.

## Try It Yourself

MEHO is open-source under AGPLv3. All 15 connectors are included in the community edition — no connector paywalls.

```bash
git clone GITHUB_URL && cd meho && docker compose up -d
```

Connect a Kubernetes cluster and a Prometheus instance. Ask a question. Watch the topology graph build itself.

Full documentation, the interactive investigation demo, and the community vs. enterprise comparison are at [MEHO_AI_URL](MEHO_AI_URL). The code referenced in this article lives in [`meho_app/modules/topology/`](GITHUB_URL) — start with `resolution/resolver.py` and follow the matchers.

Star us on [GitHub](GITHUB_URL) if cross-system reasoning is a problem you care about.
