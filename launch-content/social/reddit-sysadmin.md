# r/sysadmin Post

**Subreddit:** r/sysadmin

**Title:** Tired of context-switching between 6 dashboards during incidents? We open-sourced an AI ops tool with 15 connectors

---

During an incident, how many tabs do you have open? For our team it was usually six or seven: Grafana for metrics, kubectl for pod status, vCenter for host resources, Kibana or Grafana for logs, ArgoCD for deployment history, and Jira for the incident ticket. Each tool is a different query language, a different mental model, a different set of credentials. The root cause is almost never in one system. It's the intersection of things across multiple layers.

We built MEHO to make that investigation a single conversation. 15 connectors -- Kubernetes, VMware, Proxmox, GCP, Prometheus, Loki, Tempo, Alertmanager, ArgoCD, GitHub, Jira, Confluence, plus generic REST and SOAP for anything with an API. You ask a question in plain English: "the payment service has been slow for 30 minutes." MEHO dispatches queries to every relevant system in parallel, gets back actual data (real metrics, real pod events, real host stats, real log entries), and synthesizes a root cause with evidence from each system.

The synthesis is specific, not hand-wavy. Instead of "memory is high," you get "payment-svc v2.4.0 deployed at 14:28 added an unbounded in-memory cache. ESXi host esxi-prod-03 at 92% memory is ballooning the node-7 VM. Pod hits 512Mi limit and OOMKills. Connection pool saturates. p99 goes from 120ms to 2.4s." Every claim traced to a specific system and specific data.

**Practical details:**

- Docker Compose setup -- `git clone GITHUB_URL && cd meho && docker compose up -d`
- Community edition includes all 15 connectors, full cross-system reasoning, topology mapping, knowledge base. No artificial limits.
- Enterprise adds multi-tenancy and SSO -- the organizational stuff, not the technical stuff
- Multi-LLM: Anthropic Claude (best results), OpenAI, or Ollama for fully self-hosted
- Local embeddings included -- with Ollama, zero data leaves your network
- AGPLv3 -- free for internal use, no license key required

This is not a replacement for your monitoring stack. You still need Prometheus, Loki, and the rest. MEHO sits on top and connects them into a unified investigation surface. Think of it as the layer between your dashboards and your brain during an incident.

Full disclosure: I'm a co-founder of evoila, the company that built MEHO. It is fully open-source (AGPLv3) and self-hostable with zero dependency on us.

Repo: GITHUB_URL | Website: MEHO_AI_URL | Blog: BLOG_URL
