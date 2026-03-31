# r/devops Post

**Subreddit:** r/devops

**Title:** We open-sourced an AI agent that connects Prometheus + K8s + VMware + Loki into a single investigation -- here's how it works

---

When a pod crashes at 2 AM, how long does it take you to trace it to root cause? For our team, it was consistently 30+ minutes across six dashboards -- Prometheus for metrics, Kubernetes for pod events, VMware for host resources, Loki for logs, ArgoCD for recent deployments. Each tool has its own query language, its own mental model, its own browser tab. And the root cause often spans multiple systems -- a deployment that increased memory usage, combined with an ESXi host that was already overcommitted, causing a cascade from pod OOMKills to connection pool exhaustion to a 20x latency spike.

We built MEHO (Machine Enhanced Human Operator) to do this investigation in one conversation. You type a question in plain English -- "the payment service has been slow for 30 minutes" -- and MEHO dispatches parallel queries to every relevant system. Prometheus returns the latency metrics. Kubernetes reports the OOMKilled pods. VMware surfaces the host memory pressure. Loki delivers the error logs. ArgoCD identifies the recent deployment. The AI reasons across all the results and synthesizes a root cause with specific evidence from each system.

The key technical piece is entity resolution. MEHO builds a topology graph where entities from different systems are linked via SAME_AS edges -- it knows that a Kubernetes node IS a VMware VM, matched by provider ID, IP address, or hostname. This is what lets it trace a Prometheus alert down through the K8s layer to the VMware hypervisor in one pass. The resolution is deterministic (no LLM guessing), and the graph grows organically through investigations.

**What you get:**

- 15 connectors: K8s, VMware, Proxmox, GCP, Prometheus, Loki, Tempo, Alertmanager, ArgoCD, GitHub, Jira, Confluence, plus generic REST and SOAP
- All connectors are open source -- no connector paywalling
- Docker Compose quickstart: `git clone GITHUB_URL && cd meho && docker compose up -d`
- Multi-LLM: Claude (recommended), OpenAI, or Ollama for air-gapped environments
- Local embeddings so zero data needs to leave your network
- AGPLv3

Community edition includes everything a single engineer needs. Enterprise adds organizational features -- multi-tenancy, SSO/SAML, audit compliance. The technical capability is not gated.

Full disclosure: I'm a co-founder of the company behind MEHO (evoila), but MEHO is fully open-source under AGPLv3 and you can self-host it with zero dependencies on us.

Technical deep-dive on how the cross-system reasoning works: DEEP_DIVE_URL

Repo: GITHUB_URL | Website: MEHO_AI_URL
