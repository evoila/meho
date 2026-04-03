# LinkedIn Personal Post (Damir Topic)

**Account:** Damir Topic (personal)
**Type:** Text post with images

---

Today we're open-sourcing MEHO -- an AI agent that traces infrastructure problems across Kubernetes, VMware, Prometheus, and 12 more systems in a single conversation.

Here's why we built it.

At evoila, we're a managed services provider. Our engineers operate complex customer infrastructure every day -- Kubernetes clusters, VMware environments, GCP projects, the full PLG monitoring stack, CI/CD pipelines, and everything in between. When something breaks, the investigation always follows the same pattern: open Grafana for the latency spike, switch to kubectl for pod status, open vCenter for host resources, search Loki for error logs, check ArgoCD for recent deployments. Six dashboards, six query languages, six mental models. The root cause is almost never in one system. It's in the intersection of things happening across multiple layers.

We got tired of spending 30-45 minutes per incident on what is fundamentally the same investigation pattern: check metrics, check orchestration, check the hypervisor, check logs, check what changed. So we started building MEHO.

MEHO (Machine Enhanced Human Operator) connects to 15 infrastructure systems and reasons across all of them simultaneously. You ask one question in plain English. It dispatches parallel queries to Prometheus, Kubernetes, VMware, Loki, ArgoCD -- whatever is connected and relevant. It collects actual data from each system (real metrics, real pod events, real host statistics), builds a topology graph that links entities across system boundaries, and synthesizes a root cause backed by specific evidence from every layer.

Here's a concrete example: a payment service latency spike. MEHO queries Prometheus -- p99 jumped from 120ms to 2.4 seconds. Kubernetes -- pod OOMKilled three times, memory at 510Mi against a 512Mi limit. VMware -- the ESXi host is at 92% memory with ballooning active. Loki -- 47 connection pool exhaustion errors. ArgoCD -- a deployment 4 minutes ago added an in-memory cache. Root cause: the new cache pushed memory to the limit, and the hypervisor was already overcommitted. Neither alone would have caused the outage. MEHO traced the full chain in about 60 seconds.

[IMAGE: MEHO investigation screenshot showing cross-system root cause synthesis]

We chose to open-source MEHO under AGPLv3 because we believe the investigation engine should be accessible to every engineer. The community edition includes every connector, full cross-system reasoning, topology mapping, knowledge base with hybrid search, multi-LLM support (Claude, OpenAI, or Ollama for fully air-gapped deployments), and local embeddings. No artificial limits. No connector paywalling.

Community solves the technical problem. Enterprise solves the organizational problem -- multi-tenancy, SSO, audit compliance, team collaboration. A single engineer at 2 AM needs the full investigation engine, not a crippled version.

[IMAGE: Topology graph showing SAME_AS edges linking K8s nodes to VMware VMs]

Getting started takes 5 minutes:
```
git clone GITHUB_URL && cd meho && docker compose up -d
```

Star us on GitHub: GITHUB_URL
Try the interactive demo: MEHO_AI_URL
Read the full story: BLOG_URL

#DevOps #OpenSource #Kubernetes #InfrastructureAsCode #AIOps
