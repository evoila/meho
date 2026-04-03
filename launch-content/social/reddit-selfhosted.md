# r/selfhosted Post

**Subreddit:** r/selfhosted

**Title:** MEHO -- self-hosted AI that investigates your entire infrastructure stack in one conversation (15 connectors, Docker Compose, air-gapped capable)

---

Sharing something we've been building and just open-sourced: MEHO is a self-hosted AI operations tool that connects to your infrastructure systems and runs cross-system investigations via natural language.

**The self-hosting story:** MEHO runs entirely on your hardware with zero external dependencies if you want it that way. The LLM can be Ollama (we test with llama3, mistral, and others) running locally. The embedding engine is a TEI (Text Embeddings Inference) sidecar with bge-m3 and bge-reranker-v2-m3 -- included in the Docker Compose stack. No Voyage AI account needed. No OpenAI key needed. No data leaves your network. Everything runs in Docker containers on your machine.

If you prefer cloud LLMs for better investigation quality, MEHO also supports Anthropic Claude and OpenAI -- but that's your choice. The architecture is provider-agnostic; you switch by setting one environment variable.

**What it does:** You connect your Prometheus, Kubernetes, VMware, Loki, ArgoCD, or any of the 15 supported systems. Then you ask a question: "Why is the checkout service slow?" MEHO dispatches parallel queries to each connected system, collects real data (actual metrics, actual pod events, actual host stats, actual log lines), and synthesizes a root cause that crosses system boundaries. It builds a topology graph that links entities across systems -- it knows your K8s node is the same thing as your VMware VM -- so it can trace problems across layers automatically.

**Setup:**

```bash
git clone GITHUB_URL && cd meho
cp env.example .env
# Set MEHO_LLM_PROVIDER=ollama for fully local, or add your API key
docker compose up -d
```

Five minutes to a running instance. Connect your first system through the web UI and start asking questions.

**What's included (all free, all open source):**

- 15 connectors: Kubernetes, VMware, Proxmox, GCP, Prometheus, Loki, Tempo, Alertmanager, ArgoCD, GitHub, Jira, Confluence, generic REST, SOAP
- Cross-system investigation with entity resolution and topology mapping
- Knowledge base with hybrid search (BM25 + semantic)
- Multi-LLM support (Ollama, Claude, OpenAI)
- Local embeddings (TEI sidecar, no cloud embedding service needed)
- Dual-mode chat: Ask for quick lookups, Agent for full investigations
- AGPLv3 license

Community edition has no artificial limits. Enterprise (when it ships) will add multi-tenancy, SSO, and audit compliance -- the organizational stuff, not the technical stuff. You never need a license key for personal use.

We're sharing this on r/selfhosted because this community appreciates tools that respect your infrastructure and your data. Happy to answer questions.

Full disclosure: I'm a co-founder of evoila, the company behind MEHO. It is fully open-source and self-hostable.

Repo: GITHUB_URL | Website: MEHO_AI_URL | Blog post: BLOG_URL
