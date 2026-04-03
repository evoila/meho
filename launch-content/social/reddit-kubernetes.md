# r/kubernetes Post

**Subreddit:** r/kubernetes

**Title:** When a pod keeps OOMKilling, how long does it take you to trace it back to the hypervisor? We built MEHO to do it in one question (open source)

---

You know the scenario. A pod keeps getting OOMKilled -- memory limit is 512Mi, usage peaked at 510Mi. The node is under memory pressure. You check the node metrics and everything looks tight, but the resource requests add up fine. So where is the pressure coming from?

If your K8s cluster runs on VMs (ESXi, Proxmox, GCP), the answer might not be in Kubernetes at all. The underlying hypervisor could be at 92% memory utilization with ballooning active across six VMs -- including the one your K8s node runs on. The VM is being squeezed from below, and the pod has no headroom. But most K8s operators never check the hypervisor layer because it's a different team, different tools, different expertise.

We built MEHO to bridge that gap. It connects to Kubernetes AND VMware (and Prometheus, Loki, ArgoCD, and 10 more systems) and reasons across all of them in a single conversation. When you ask "why does the payment service keep crashing," MEHO queries K8s for pod events, VMware for host resource stats, Prometheus for metrics, and Loki for logs -- in parallel. Then it links the entities: it knows that K8s node `node-7` IS VMware VM `node-7` via a SAME_AS edge in the topology graph. The resolution is deterministic -- provider IDs, IP addresses, and hostnames, in priority order. No LLM guessing involved.

The result for our scenario: "payment-svc v2.4.0 (deployed 14:28) added an unbounded in-memory cache. Combined with ESXi host esxi-prod-03 at 92% memory causing ballooning on the node-7 VM, the pod hits its 512Mi limit and OOMKills." One question, five systems, actual root cause with specific data from each layer.

**Details:**

- 15 connectors, all open source -- including K8s, VMware, Proxmox, GCP, the full PLG stack (Prometheus, Loki, Tempo, Alertmanager), ArgoCD, GitHub, Jira, Confluence, plus generic REST and SOAP
- Docker Compose: `git clone GITHUB_URL && cd meho && docker compose up -d`
- Multi-LLM: Claude, OpenAI, or Ollama (fully air-gapped)
- AGPLv3 -- no license key needed for the community edition

Full disclosure: I'm a co-founder at evoila, the company behind MEHO. It's fully open-source and you can self-host it without any dependency on us.

Repo: GITHUB_URL | Website: MEHO_AI_URL | Deep-dive on cross-system reasoning: DEEP_DIVE_URL
