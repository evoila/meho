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
you control. There is no hosted service and no phone-home. You install
it with one Helm chart into your own Kubernetes cluster. The chart
brings its own PostgreSQL, Keycloak and OpenBao (an open-source fork of
Vault), or connects to the ones you already run. Two more ways to
install are coming: the install on a plain Linux server is available
and in testing, and a VMware appliance is in progress. See
[Ways to install MEHO](install/ways-to-install.md).

For a short answer to "what can MEHO do for us?", see
[What MEHO can do](can-do/index.md).

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
  role and per-target grants before they execute. A call made with a
  token (MCP, the CLI or REST) runs only `safe` operations at once;
  every other change waits until a person approves it.
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
| **Start here** | This page, plus the [reference architecture](architecture.md) and the [deployment shapes](deployment-shapes.md). |
| **[Install & operate](install/index.md)** | The [install trail](install/index.md) — one continuous path from a bare Kubernetes cluster to a running backplane and a successful login — plus [credential backends](install/credential-backends.md), [Keycloak realm setup](install/keycloak-realm.md), [TLS and ingress](install/tls-ingress.md), [upgrades](install/upgrades.md). [Ways to install MEHO](install/ways-to-install.md) helps you pick how to install. |
| **[Connect clients](clients/index.md)** | Connecting the CLI and MCP clients (Claude Desktop, Claude Code, and others) to a running backplane. |
| **[Do real work](guides/index.md)** | Task guides: register targets and secrets, run first operations, watch your estate. |
| **[Extend MEHO](extend/connector-kit.md)** | Write your own connector package against the connector kit. |
| **[Reference](reference/index.md)** | [What's new](reference/whats-new.md) in each release, plus generated reference material, such as the [feature maturity index](reference/maturity.md). |
| **[Project](project/index.md)** | How the project is run: versioning, security policy, roadmap. |

## Where to start

- **Deploying MEHO for your team?** Read
  [Ways to install MEHO](install/ways-to-install.md), then follow the
  [install trail](install/index.md). You need a Kubernetes cluster, a
  DNS name and a certificate. The chart brings the database, the login
  server and the password safe.
- **Just want to try it?** Read
  [Ways to install MEHO](install/ways-to-install.md). It lists every
  way to install MEHO and helps you pick one. Each way gives you a
  working login.
- **Backplane already running?** Go to
  [Connect clients](clients/index.md).
- **Evaluating?** Read the [reference architecture](architecture.md)
  and the [feature maturity index](reference/maturity.md) — together
  they are the honest picture of what MEHO is and how far along each
  part is.
