# Twitter/X Thread (Damir Topic)

**Account:** Damir Topic (personal)
**Type:** Thread (6 tweets)

---

## Tweet 1

We just open-sourced MEHO -- an AI agent that traces problems across your entire infrastructure stack.

One question, every system, actual root cause.

[IMAGE: MEHO investigation screenshot showing 5-system synthesis]

GITHUB_URL

#DevOps #OpenSource #Kubernetes

---

## Tweet 2

The problem:

When a pod keeps crashing, you check K8s. Then Prometheus. Then logs. Then VMware.

30 minutes across 6 dashboards. If you know all the tools.

Most root causes span multiple systems. No single dashboard shows the full picture.

---

## Tweet 3

MEHO connects to all of them and reasons across the whole graph.

Prometheus alert -> K8s pod status -> VMware host resources -> Loki logs -> ArgoCD deploys -> root cause.

One conversation. Parallel queries. Actual data from every system.

---

## Tweet 4

Here's what that looks like:

"payment-svc v2.4.0 added an in-memory cache. ESXi host at 92% memory is ballooning the K8s node. Pod hits 512Mi limit and OOMKills."

One question. 5 systems. ~60 seconds.

[IMAGE: Demo GIF showing investigation flow from question to synthesis]

---

## Tweet 5

What's included:

- 15 connectors, all open source
- Docker Compose -- 5 min to running
- Multi-LLM: Claude, OpenAI, or Ollama (fully air-gapped)
- Local embeddings, no cloud dependency
- No artificial limits in the free edition

AGPLv3.

---

## Tweet 6

Try it:
MEHO_AI_URL

Star on GitHub:
GITHUB_URL

How cross-system reasoning works under the hood:
DEEP_DIVE_URL

#DevOps #OpenSource #Kubernetes
