# Reference architecture

MEHO deploys as **one Kubernetes application** (a Helm release). By
default the chart also brings its own PostgreSQL, Keycloak and OpenBao
(an open-source fork of Vault). This is the normal way. You can connect
your own PostgreSQL or secret store instead
([two ways to install](install/index.md#two-ways-to-install)). This page names every box in the picture, what it
does, and — the part that matters when you plan an install — which
boxes MEHO ships and which ones you can bring.

```mermaid
flowchart LR
    subgraph callers["Callers"]
        AG["AI agents<br/>(any MCP client)"]
        OP["Operators<br/>(meho CLI · browser console)"]
    end
    subgraph backplane["MEHO backplane"]
        MCP["/mcp — MCP server"]
        API["/api — REST API"]
        POL["Policy gate<br/>+ approvals"]
        DISP["Connector<br/>dispatch"]
        RED["Result<br/>reduction"]
    end
    subgraph backing["Backing services"]
        KC["Keycloak<br/>(identity)"]
        CB["Credential backend<br/>(Vault or<br/>Google Secret Manager)"]
        PG["PostgreSQL + pgvector<br/>(state · audit · search)"]
        VK["Valkey<br/>(activity feed)"]
    end
    T["Targets<br/>(vCenter/VCF · NSX ·<br/>Kubernetes · cloud · appliances)"]
    SAT["Satellite gateway<br/>(isolated networks)"]

    AG -->|"OAuth 2.1 + PKCE"| MCP
    OP -->|"device-code login"| API
    MCP --> POL
    API --> POL
    POL --> DISP --> RED
    POL -.->|"token validation"| KC
    DISP -.->|"just-in-time credential"| CB
    RED -.->|"audit rows"| PG
    RED -.->|"broadcast events"| VK
    DISP --> T
    SAT -->|"outbound poll"| API
    SAT --> T
```

## Who runs what

| Component | Who provides it | Notes |
|---|---|---|
| Backplane (container image) | **MEHO** — `ghcr.io/evoila/meho` | One Deployment, rendered by the Helm chart. |
| Valkey (activity feed) | **MEHO** — bundled subchart | Installed by the same Helm release; no separate setup. |
| PostgreSQL + pgvector | **MEHO** by default, or **you** | The chart's built-in PostgreSQL, with pgvector on. Turn it off (`corePostgres.enabled: false`) to use any reachable PostgreSQL with the `vector` extension available — see [the install trail](install/index.md#step-1-pre-create-the-pgvector-extension) for the one privilege nuance. |
| Keycloak | **MEHO** | MEHO's built-in Keycloak makes the realm `meho`. Connect your [company login](install/login/index.md) in front of it (AD/LDAP, Entra ID, authentik, your own Keycloak). Replacing it with your own Keycloak is an expert path: it needs realm admin rights to make MEHO's realm, clients and groups — see [Keycloak realm setup](install/keycloak-realm.md). |
| Credential backend | **MEHO** by default, or **you** | The chart's built-in OpenBao (an open-source fork of Vault). Turn it off (`coreVault.enabled: false`) to use your own HashiCorp Vault **or** Google Secret Manager — see [Credential backends](install/credential-backends.md). |
| AI agent / MCP client | **You** | MEHO governs agents; it does not run a model. Bring Claude, Cursor, or any MCP client. |

## The backplane

The backplane is a single Python service (FastAPI) that exposes two
front doors and one governed pipeline behind both:

- **`/mcp`** — an MCP server. AI agents authenticate with OAuth 2.1
  authorization-code + PKCE (the flow the MCP specification mandates)
  and see a catalog of tools: search operations, preview them, call
  them, query results. A session that asks for the `mcp:admin` scope
  also gets the operator tools, for example to read the audit trail.
- **`/api`** — a REST API. The `meho` CLI drives it after a device-code
  login; anything that can send a Bearer token can use it directly.

Both doors lead to the same pipeline. There is no privileged side
entrance: a CLI call and an agent tool call are authorised, dispatched,
reduced, audited, and broadcast identically.

The pipeline stages:

1. **Authentication** — every request carries a JWT issued by your
   Keycloak. The backplane validates issuer, audience, and the tenant
   claims that scope everything downstream.
2. **Policy gate** — the operation is checked against the caller's role
   and per-target grants. A call made with a token (MCP, the CLI or
   REST) runs only `safe` operations at once. Every other change waits
   until a person approves it. Service principals keep their standing
   grants.
3. **Connector dispatch** — the operation executes through a
   *connector*: the code that knows how to talk to a class of
   infrastructure (vCenter, NSX, Kubernetes, cloud APIs, network
   appliances). Connectors encode operational sequences — for
   example, evacuating a hypervisor host as one governed operation —
   not just raw API passthrough.
4. **Just-in-time credentials** — if the target needs a credential, the
   backplane fetches it from the credential backend at dispatch time,
   scoped to the caller's tenant. The caller never sees it.
5. **Result reduction** — large results are reduced server-side; the
   caller can drill into the full result set with follow-up queries
   instead of receiving megabytes of raw output.
6. **Audit + broadcast** — the operation lands as an immutable audit
   row in PostgreSQL and as an event on the Valkey-backed activity
   feed.

## Identity: Keycloak

Every caller — human or agent — is a Keycloak identity. MEHO does not
maintain its own user database; it validates the OIDC tokens your realm
issues and reads tenant membership and role from claims on the token.

Three token flows matter:

- **CLI** — `meho login` runs the OAuth device-code flow against a
  public client in your realm.
- **MCP clients** — browser-capable agents run authorization-code +
  PKCE against a second public client.
- **Browser console** — the optional `/ui` console authenticates
  server-side against a confidential client.

[Keycloak realm setup](install/keycloak-realm.md) walks through
creating all of these.

## State: PostgreSQL + pgvector

PostgreSQL is MEHO's only database. It holds the registered targets,
the operations catalog, the append-only audit log, approvals, sensor
state, and the knowledge/memory store. The
[pgvector](https://github.com/pgvector/pgvector) extension provides the
vector similarity search behind semantic operation discovery and
knowledge retrieval — it is the reason the install trail has a
[one-time extension step](install/index.md#step-1-pre-create-the-pgvector-extension).

The chart brings its own PostgreSQL by default. If your platform team
must own the audit data (backup, retention), turn the built-in
PostgreSQL off and use theirs.

## Activity feed: Valkey

Every governed action emits a broadcast event to
[Valkey](https://valkey.io/) (the open-source Redis fork), giving
agents and operators a shared, real-time view of who is doing what.
Valkey **is** bundled — the chart installs it as a subchart, and no
operator action is needed beyond installing the release.

## Credential backends: Vault or Google Secret Manager

Target credentials (the vCenter password, the Kubernetes token, the
appliance API key) live in a secret store MEHO reads *per operation*,
scoped to the calling tenant — never cached in the agent, never
embedded in MEHO's own configuration. Two backends are supported:
HashiCorp **Vault** and **Google Secret Manager**. By default the chart
brings OpenBao, an open-source fork of Vault, and MEHO uses it like
Vault. The
choice shapes the install, so it has its own
[decision page](install/credential-backends.md) with an honest support
matrix per backend.

## Targets and connectors

A **target** is a registered piece of your infrastructure: a vCenter,
an NSX manager, a Kubernetes cluster, a cloud project, a firewall. Each
target carries its address, how to authenticate to it (a reference into
the credential backend — never the secret itself), and TLS trust
settings.

A **connector** is the code that operates a class of targets. MEHO has
two kinds, and an agent uses both the same way. A **generic** connector
is made from a vendor's API file (for example an OpenAPI file). A
**typed** connector is written by hand against a vendor SDK or
protocol, where no usable API file exists. Each
connector publishes a catalog of operations with descriptions, safety
levels, and approval requirements — that catalog is what agents search
and call. The public image carries connectors for Kubernetes, the
Microsoft estate, Linux hosts, Vault, Keycloak, cloud and network
systems ([connector inventory](reference/connectors.md)). evoila's
enterprise pack adds the VMware and VCF estate (vCenter, NSX, SDDC
Manager, VCF Operations, Fleet, Logs and Automation).

## Operators: CLI and browser console

The **`meho` CLI** is a static Go binary (Linux/macOS, amd64/arm64),
released with signed checksums on every release. It logs in with the
device-code flow — which works from private networks and
jump hosts, because only the operator's browser needs to reach
Keycloak's login page. Registering targets and storing secrets is
operator work, done with the CLI, the console or REST. On MCP, the
default tools only read targets (`list_targets`). Target registration
is an operator tool: it lists only for a session with the `mcp:admin`
scope and needs the `tenant_admin` role. MEHO has no MCP tool to store
a secret.

The **browser console** (`/ui`) is an optional, read-heavy operator
surface: dashboards for sensors, approvals, audit, and the activity
feed. It is off by default and enabled all-or-nothing via chart values.

## Satellite gateway

Some targets live in networks the central backplane cannot dial:
behind NAT, in a private control plane, on an isolated management
network. The **satellite gateway** is a second deploy mode of the same
backplane image that runs *inside* such a network and dials **out** to
the central instance — never the reverse. It polls for authorised work,
executes it locally against the same connector surface, and reports
results back. By default a satellite runs only `safe` operations. A
tenant admin can turn on a write tier per runner: then `caution` changes on
an allowlist also run there, each after a person approves it.
`dangerous` and `destructive` operations never run on a satellite.
Authorisation, approval, and audit all stay central: a satellite cannot
self-authorise anything.

The satellite gateway is a **Beta** feature — see its entry in the
[feature maturity index](reference/maturity.md).

## What MEHO is not

- **Not an agent runtime first.** MEHO governs what any MCP client is
  allowed to do. It can also run agents on the backplane
  ([hosted agents](guides/hosted-agents.md), experimental), under the
  same rules.
- **Not a session broker.** Tools like Teleport or StrongDM broker
  *human* sessions (SSH, database, kubectl). MEHO authorises and audits
  *individual agent actions*, per call, with infrastructure-aware
  connectors.
- **Not a SaaS.** You run everything; your audit trail stays in your
  PostgreSQL.

## Next step

Follow [the install trail](install/index.md).
