# Start here

MEHO is a self-hosted governance layer that sits between AI agents and
the infrastructure they operate. Agents connect over
[MCP](https://modelcontextprotocol.io/) (the open protocol AI
assistants use to call tools); operators connect through a CLI and an
optional browser console. Every action — whether an agent asked for it
or a human did — passes through one governed path: the caller is
authenticated, the operation is checked against policy, a short-lived
backend credential is fetched just in time, the result is reduced to
what the caller needs, and an immutable audit row records who did what,
where, and when.

MEHO is open source (Apache 2.0) and runs entirely on infrastructure
you control. There is no hosted service and no phone-home: you deploy
the backplane into your own Kubernetes cluster, next to your own
PostgreSQL, Keycloak, and secret store.

## The problem it solves

AI agents are getting good enough to *do* infrastructure work — roll a
credential, drain a node, restart a service — not just describe it. But
handing an agent a long-lived admin token and a shell creates an
un-auditable, over-privileged actor. The moment an agent can act, you
need the same controls you would demand of any operator: who may do
what, with credentials that expire, against which systems, with every
action recorded and reviewable.

MEHO is that control plane. It governs the *actions*, not the agent:
you bring your own AI assistant (Claude, Cursor, a custom MCP client —
anything that speaks MCP), and MEHO decides what it is allowed to do
and keeps the receipts.

## What every operation gets

- **Policy-gated** — operations are authorised against the caller's
  role and per-target grants before they execute; destructive
  operations can require explicit human approval.
- **Credential-federated** — the agent never holds a backend
  credential. A short-lived identity token is exchanged with your
  secret store ([Vault or Google Secret
  Manager](install/credential-backends.md)) for a just-in-time
  credential per operation.
- **Server-reduced** — large results are reduced server-side, so the
  caller sees a compact, relevant view instead of raw firehose output.
- **Broadcast** — every action is published to a real-time activity
  feed that other agents and humans can watch.
- **Audited** — every interaction lands as an immutable audit row in
  PostgreSQL, attributed to the calling identity.
- **Tenant-scoped** — every lookup and every credential read is scoped
  to the caller's tenant.

The [reference architecture](architecture.md) page shows how the pieces
fit together and which of them you run.

## What 1.0 promises

MEHO is honest about maturity. Every feature carries an explicit tier —
**GA**, **Beta**, or **Experimental** — declared once in the codebase
and propagated to every surface you touch: MCP tool descriptions, the
REST API document, CLI help, and the browser console. GA features carry
the 1.0 stability promise; Beta features work end-to-end somewhere real
but may still change with notice; Experimental features are outside the
promise entirely.

The [feature maturity index](reference/maturity.md) is generated from
that registry on every release and lists, for each non-GA feature, the
milestone it targets and where its road to GA is tracked. If a page on
this site describes a Beta feature, the caveats you read here are the
same ones the product itself displays.

## How this site is organised

| Section | What lives there |
|---|---|
| **Start here** | This page, plus the [reference architecture](architecture.md). |
| **[Install & operate](install/index.md)** | The [install trail](install/index.md) — one continuous path from a bare Kubernetes cluster to a running backplane and a successful login — plus [credential backends](install/credential-backends.md), [Keycloak realm setup](install/keycloak-realm.md), [TLS and ingress](install/tls-ingress.md), [upgrades](install/upgrades.md), and a [local quickstart](install/kind-quickstart.md). |
| **[Connect clients](clients/index.md)** | Connecting the CLI and MCP clients (Claude Desktop, Claude Code, and others) to a running backplane. Content is being migrated — tracked by [evoila/meho#2672](https://github.com/evoila/meho/issues/2672). |
| **[Do real work](guides/index.md)** | Task guides: register targets and secrets, run first operations, watch your estate. Content is being migrated — tracked by [evoila/meho#2673](https://github.com/evoila/meho/issues/2673). |
| **[Reference](reference/index.md)** | Generated reference material, starting with the [feature maturity index](reference/maturity.md). |
| **[Project](project/index.md)** | How the project is run: versioning, security policy, roadmap. |

## Where to start

- **Deploying MEHO for your team?** Follow the
  [install trail](install/index.md). It assumes a Kubernetes cluster, a
  PostgreSQL database, and a Keycloak — and takes you from there to a
  running backplane and a working `meho login`.
- **Just want to see it run?** The
  [local kind quickstart](install/kind-quickstart.md) brings the
  backplane up on your workstation in minutes — with placeholder
  authentication, clearly labelled.
- **Backplane already running?** Go to
  [Connect clients](clients/index.md).
- **Evaluating?** Read the [reference architecture](architecture.md)
  and the [feature maturity index](reference/maturity.md) — together
  they are the honest picture of what MEHO is and how far along each
  part is.
