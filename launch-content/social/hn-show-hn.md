# Hacker News: Show HN Submission

## Submission

**Title:** Show HN: MEHO -- open-source cross-system infrastructure diagnosis

**URL:** DEEP_DIVE_URL

---

## First Comment

Hi HN, I'm Damir from evoila. We're a managed services company in Europe, and we built MEHO to solve a problem our team hits multiple times a week.

When a pod OOMKills at 2 AM, the investigation goes something like this: check Prometheus for the latency spike. Switch to Kubernetes -- pod is OOMKilled, node under memory pressure. SSH into the node -- it's a VM, not bare metal. Open vSphere -- the ESXi host is at 92% memory, ballooning active across six VMs. Search Loki for application logs -- connection pool exhaustion errors. Check ArgoCD -- someone deployed a new version 30 minutes ago with an unbounded in-memory cache. Forty-five minutes across five tools, three browser tabs, two terminals. Root cause spans five systems and two infrastructure layers. No single dashboard shows it.

MEHO connects to all of these systems and reasons across them in one conversation. You type "the payment service has been slow for 30 minutes" and it dispatches parallel queries to Prometheus, Kubernetes, VMware, Loki, and ArgoCD. The results come back with actual data -- real metrics, real pod events, real host stats, real log entries. No summaries.

The piece that makes cross-system tracing work is the topology graph. When MEHO discovers entities from different connectors, it runs deterministic resolution to find SAME_AS relationships -- a Kubernetes node IS a VMware VM. The resolver uses provider IDs, IP addresses, and hostnames in strict priority order (no LLM involved in entity resolution). These SAME_AS edges are what let the agent trace an OOMKilled pod to its node to the underlying VM to the ESXi host, building a causal chain across system boundaries.

**What's included in the open-source release:**

- 15 connectors: Kubernetes, VMware, Proxmox, GCP, Prometheus, Loki, Tempo, Alertmanager, ArgoCD, GitHub, Jira, Confluence, plus generic REST and SOAP. Every connector is open source -- no paywalling.
- Multi-LLM: Anthropic Claude (best quality), OpenAI, or Ollama for fully air-gapped deployments. Switch with one env var.
- Local embeddings: TEI sidecar with bge-m3 and bge-reranker-v2-m3. Zero data leaves your network if you pair with Ollama.
- Docker Compose quickstart: `git clone GITHUB_URL && cd meho && docker compose up -d`
- AGPLv3 license. Internal deployment has zero licensing obligations.

**What works well:** Cross-system reasoning across K8s/VMware/Prometheus is where MEHO genuinely shines. The entity resolution is deterministic and reliable. The topology graph grows organically through investigations -- you don't configure it, the system learns it. Investigation synthesis produces specific evidence-backed root causes, not vague suggestions.

**What's rough:** The UI is functional but won't win design awards. Ollama quality is noticeably lower than Claude for complex multi-system investigations -- we publish an honest model compatibility matrix documenting the differences. The contributor community is just getting started. Some connectors (Proxmox, SOAP) have had less production mileage than the core K8s/VMware/Prometheus stack.

- Repo: GITHUB_URL
- Website + interactive demo: MEHO_AI_URL
- Technical deep-dive on how cross-system reasoning works: DEEP_DIVE_URL

I'll be here answering questions for the next few hours.
