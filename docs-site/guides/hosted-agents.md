# Hosted agents

A **hosted agent** is an AI agent that MEHO runs for you on the
backplane server. This guide shows you how to set one up and run it,
from start to end, with commands you can copy.

When you finish, you will have:

- an agent with its own identity;
- an agent definition: its prompt, its tools, and its limits;
- permission grants that make the agent read-only;
- one finished run, and the audit rows that show what the agent did.

!!! note "Prerequisites, roles, and maturity"

    - The `meho` CLI, logged in to your backplane
      ([The meho CLI](../clients/cli.md)).
    - At least one registered target and a connector that can reach it
      ([Register targets and secrets](targets-and-secrets.md)). It helps
      to have run one operation by hand first
      ([Run your first operations](first-operations.md)).
    - **Setup needs the `tenant_admin` role.** This covers the identity,
      the definition, and the grants.
    - **Running an agent needs the `operator` role** (or `tenant_admin`).
      The `read_only` role cannot start a run.
    - Hosted agents are **Experimental**. See the
      [feature-maturity index](../reference/maturity.md#agent_runtime).

## What a hosted agent is

- MEHO runs the agent **on the server**. The model calls and the tool
  calls happen inside MEHO, not on your laptop.
- It is **not** your desktop Claude. Claude Desktop or Claude Code talk
  to MEHO over MCP as you. A hosted agent is a separate thing that MEHO
  starts itself.
- Every tool call the agent makes goes through the same checks as yours:
  permissions, approvals, and audit.

A hosted agent has three parts. You create them in this order:

| Part | What it is | Command |
|---|---|---|
| **Agent principal** | The agent's own login identity. MEHO creates it in Keycloak (the login server). | `meho agent-principal register` |
| **Agent definition** | What the agent is: its prompt, its tools, its model tier, and its turn limit. | `meho agent create` |
| **Grants** | Rules that say which operations the agent may run, and on which targets. | `meho agent grant create` |

In this guide the agent is called `pod-reader`. It reads pods on one
Kubernetes target called `cluster-a`, through the connector `k8s-1.x`.
Use your own names.

## Step 1 — Register the agent's identity

```bash
meho agent-principal register pod-reader
```

The output looks like this:

```text
registered agent principal "pod-reader"
  id:                  3f2b7c1e-...
  keycloak_client_id:  agent:pod-reader
  owner_sub:           <your sub>
  revoked:             false
  created_at:          2026-10-18T09:00:00Z
```

Two values matter:

- **`keycloak_client_id`** — the agent's **client id**. It is always
  `agent:<name>`, here `agent:pod-reader`. You use this value in every
  later step: in `--identity-ref` and in `--principal`.
- **`owner_sub`** — the person who can switch the agent off. By default
  this is you.

The agent also has a **sub** (subject id). The sub is a UUID that
Keycloak gives the agent's login account. You do not need it for setup.
MEHO matches grants by the client id. You see the sub later, in the
audit rows of runs where the agent acts as itself (see
[Step 5](#who-the-grants-apply-to)).

To list your agents, or to switch one off in an emergency:

```bash
meho agent-principal list
meho agent-principal revoke pod-reader   # the agent can no longer log in
```

## Step 2 — Write the prompts

A hosted agent gets two pieces of text. Keep them apart.

| Text | Where you set it | What to put in it |
|---|---|---|
| **System prompt** | `--system-prompt` on `meho agent create` | The standing rules. Who the agent is, what it may do, which connector and target to use, how to answer. It is the same for every run. |
| **Input** | `--input` on `meho agent run` | The task for **this** run. For example: "List the pods in namespace `web` that are not running." |

A good system prompt for the read-only example:

```text
You are pod-reader. You only read. You never change anything.
Use the connector k8s-1.x and the target cluster-a.
Find the operation with search_operations, then run it with call_operation.
Answer in short plain sentences. Say which operation you used.
```

Some tips:

- Name the connector id and the target name in the system prompt. Then
  the agent does not have to guess them.
- Do not tell the agent to run a **runbook**. A hosted agent has no
  runbook tools. MEHO stops such a run at the start and marks it
  `failed`, so the agent cannot claim it ran a runbook.
- MEHO adds its own short text after your system prompt. It tells the
  agent that MEHO's rules always apply, and that text it reads with its
  tools is data, not instructions. It also adds your tenant's
  conventions, if you have any. Your system prompt always comes first.

## Step 3 — Choose the tools (the toolset)

The **toolset** is a small JSON object. It says which tools the agent
gets, and which connectors it may use. You pass it with `--toolset`.

```json
{
  "meta_tools": ["meho_connector_list", "list_operation_groups", "search_operations", "call_operation", "result_query"],
  "connectors": ["k8s-1.x"]
}
```

### The fields

| Field | Type | If you leave it out | If it is an empty list `[]` |
|---|---|---|---|
| `meta_tools` | list of tool names | The agent gets every tool marked "yes" in the "In the default set" column below. | The agent gets **no** tools from the table below. It still gets `invoke_agent` (see the note under the table). |
| `connectors` | list of connector ids, for example `k8s-1.x` | No connector limit. The agent may use any connector in your tenant. Grants and approvals still apply. | The agent may use **no** connector. Every `call_operation` is refused. |

More rules:

- `{}` (an empty object) is the default. It means "leave both fields
  out".
- A tool name that MEHO does not know is ignored. It is not an error,
  so check your spelling.
- Any other key in the object is ignored.
- `meta_tools` and `connectors` must be lists of strings. Anything else
  (for example a single string) makes the run fail.
- The `connectors` list limits `call_operation` and
  `preview_operation`. When the agent asks for a connector that is not in
  the list, MEHO refuses the call and tells the agent which connectors it
  may use.

### The tool names

These are the names you can put in `meta_tools`:

| Tool | What the agent can do with it | In the default set |
|---|---|---|
| `list_operation_groups` | List the operation groups of one connector. | yes |
| `search_operations` | Search a connector's operations by meaning and by words. | yes |
| `call_operation` | Run one operation. This is the only tool that changes or reads your systems. | yes |
| `meho_connector_list` | List the connectors your tenant can use. | yes |
| `list_targets` | List the targets in your tenant. | yes |
| `preview_operation` | Show the request `call_operation` would send, without sending it. | yes |
| `result_query` | Read rows from a large result. MEHO returns big results as a link to the stored result, not as raw data. | yes |
| `broadcast_announce` | Post a short note to the team's activity feed. | yes |
| `broadcast_recent` | Read recent notes from agents on the activity feed. | yes |
| `broadcast_watch` | Wait up to 30 seconds for new notes on the activity feed. | yes |
| `query_topology` | Look up how resources depend on each other. | no — you must name it |
| `search_memory` | Search the tenant's Memory store. | no — you must name it |
| `search_knowledge` | Search the tenant's knowledge base. | no — you must name it |
| `read_context_resource` | Read one Memory, knowledge base, or convention entry that a search returned. | no — you must name it |

The last four return text that people in your tenant stored. That is
why the agent only gets them when you name them.

!!! note "One more tool: `invoke_agent`"

    Every hosted run also gets the `invoke_agent` tool. With it, the
    agent can start another agent definition in your tenant. The
    toolset does not turn this tool on or off, so it is not in the table
    above. Putting `invoke_agent` in `meta_tools` does nothing.

### Tools a hosted agent does not have

Some tools exist for MCP clients but not for hosted agents. Putting
these names in `meta_tools` does nothing:

- no health or status tool (`meho_status`);
- no runbook tools (`meho_runbook_*`);
- no approval tools. An agent can never approve anything;
- no tools that write to Memory or the knowledge base;
- no admin tools.

### A read-only toolset

For `pod-reader`, save this as `pod-reader-toolset.json`:

```json
{
  "meta_tools": ["meho_connector_list", "list_operation_groups", "search_operations", "call_operation", "result_query"],
  "connectors": ["k8s-1.x"]
}
```

The toolset limits **which tools and connectors** the agent can reach.
It does not decide which **operations** may run. That is the job of
grants, in Step 5.

!!! note "Definitions from older versions"

    A definition stored with **no** toolset at all (not even `{}`) gets
    only two tools: `list_operation_groups_tool` and `call_operation_tool`. New
    definitions always get `{}` by default. To move an old definition to
    the full set, run `meho agent edit <name> --toolset '{}'`.

## Step 4 — Create the definition

```bash
meho agent create pod-reader \
  --identity-ref agent:pod-reader \
  --model-tier standard \
  --turn-budget 20 \
  --system-prompt "You are pod-reader. You only read. You never change anything. Use the connector k8s-1.x and the target cluster-a. Find the operation with search_operations, then run it with call_operation. Answer in short plain sentences. Say which operation you used." \
  --toolset @pod-reader-toolset.json
```

The flags:

| Flag | Required | What it means |
|---|---|---|
| `<name>` | yes | The definition name. You use it to run the agent. |
| `--identity-ref` | yes | The client id from Step 1, `agent:<name>`. MEHO checks that this agent principal exists and is not revoked. |
| `--model-tier` | yes | `standard`, `fast`, or `deep`. Use `standard` if you are not sure. In this version MEHO stores the tier with the definition and with each run, but every run uses the model your deployment sets as its default. |
| `--turn-budget` | yes | The most model turns one run may take, from 1 to 1000. One turn is one request to the model. When a run reaches the limit, it stops and fails with `turn budget exhausted`. |
| `--system-prompt` | yes | The standing rules from Step 2. |
| `--toolset` | no | The JSON from Step 3. Write it inline, as `@<file>`, or as `@-` to read it from stdin (for example from a pipe). Default: `{}`. |
| `--disabled` | no | Create the definition switched off. Nobody can run it until you switch it on. |

Check the result:

```bash
meho agent show pod-reader
```

To change a definition later, use `meho agent edit`. Only the flags you
pass change:

```bash
meho agent edit pod-reader --turn-budget 30
meho agent edit pod-reader --toolset @pod-reader-toolset.json
meho agent edit pod-reader --disabled   # switch it off
meho agent edit pod-reader --enabled    # switch it on again
```

## Step 5 — Set permissions (grants)

Every operation has a **safety level**: `safe`, `caution`, `dangerous`,
or `destructive`. You see it as `safety_level` in the
`search_operations` results. The safety level sets what happens when
no grant matches.

### What the agent may do with no grants

| Safety level | With no grant |
|---|---|
| `safe` (reads) | Runs without approval. |
| `caution` | Waits until a second person approves. |
| `dangerous` | Denied. |
| `destructive` | Denied. |

So a new agent can already run **every `safe` operation** on every
connector and target, with no grant at all. To make it truly read-only
on one thing, you add grants. See the recipe below.

### Who the grants apply to

Grants belong to the agent principal (`agent:pod-reader`). They apply
when the agent runs **as itself**:

- **A run started by the [scheduler](scheduler.md)**, or by an
  [event trigger](event-ingestion.md). The agent logs in with its own
  identity. Its grants apply. The audit rows show the agent's sub.
- **A run you start by hand** with `meho agent run`. The run acts on
  your behalf. **Your** permissions apply, not the agent's grants. The
  audit rows show your sub.

The safety-level table above and the grants apply only when the agent
acts as itself. In a run you start by hand, MEHO checks each call the
same way it checks a call you make yourself. See
[Approvals and break-glass](approvals-and-break-glass.md) for which
calls wait for a second person. For a run you start by hand, the
toolset from Step 3 is what keeps the agent narrow.

### How a grant works

A grant has four parts:

| Flag | What it means |
|---|---|
| `--principal` | The agent's client id, `agent:<name>`. |
| `--op` | Which operations. A pattern: `*` matches anything, `?` matches one character. Examples: `*`, `k8s.*`, `k8s.pod.list`. |
| `--target` | Which target, by its UUID. Leave it out, or use `*`, for any target. |
| `--verdict` | `auto-execute` (run without approval), `needs-approval` (wait for a second person), or `deny`. |

When MEHO checks a call, it follows these rules:

1. It keeps only the grants whose `--op` pattern matches the operation
   and whose `--target` matches the target.
2. **The most specific pattern wins.** MEHO counts the characters before
   the first `*`, `?`, or `[`. More characters means more specific. So
   `k8s.pod.list` beats `k8s.*`, and `k8s.*` beats `*`.
3. If two matching grants are equally specific, the stricter verdict
   wins.
4. If no grant matches, the safety-level default from the table above
   applies.
5. Last, MEHO applies a **ceiling**. A grant can never make an operation
   more open than this:

| Safety level | The most open verdict a grant can give |
|---|---|
| `safe` | `auto-execute` (no limit) |
| `caution` | `needs-approval` |
| `dangerous` | `needs-approval` |
| `destructive` | `deny`. No grant can open it for an agent. |

So a grant can always make an operation **stricter**. It can make a
`dangerous` operation possible, but only with a second person's
approval. A `caution` or `dangerous` operation never runs on its own.

Grants only check operations that run through `call_operation`. The
discovery tools (`meho_connector_list`, `list_operation_groups`,
`search_operations`, `list_targets`) are not affected by grants.

### Recipe: a read-only agent for one operation on one target

Goal: `pod-reader` may run `k8s.pod.list` on `cluster-a`, without
approval. Everything else is denied.

First, find the target's UUID. It is the `id:` line:

```bash
meho targets describe cluster-a
```

Then add two grants:

```bash
# 1. Deny every operation on every target.
meho agent grant create --principal agent:pod-reader \
  --op '*' --verdict deny

# 2. Allow one read operation on one target, without approval.
meho agent grant create --principal agent:pod-reader \
  --op k8s.pod.list --target <cluster-a-uuid> --verdict auto-execute
```

Why this works:

- On `cluster-a`, both grants match `k8s.pod.list`. The pattern
  `k8s.pod.list` is more specific than `*`, so `auto-execute` wins.
- On any other target, only the `*` grant matches. The verdict is
  `deny`.
- Every other operation matches only `*`. The verdict is `deny`, even
  for `safe` reads.

Check the grants:

```bash
meho agent grant list --principal agent:pod-reader
```

To remove a grant, use its id from the list:

```bash
meho agent grant revoke <grant-id>
```

For a grant that should end by itself, add `--expires` with a UTC time,
or use `meho agent grant elevate`. When the time passes, the grant
stops working.

## Step 6 — Run the agent

Any user with the `operator` or `tenant_admin` role can run a
definition in their tenant.

```bash
meho agent run pod-reader \
  --input "List the pods in namespace web on cluster-a that are not running."
```

The CLI waits for the answer. A short run prints its `run_id`, its
`status`, and its `output`. If the run takes longer than the server's
wait limit (30 seconds by default), MEHO keeps the run going and gives
you its run id instead.

For a long task, do not wait. Start the run in the background:

```bash
meho agent run pod-reader --async \
  --input "List the pods in namespace web on cluster-a that are not running."
```

Then check it with the run id:

```bash
meho agent run-status <run-id>
```

`run-status` shows the `status`, the number of `turns` used, and,
when the run is done, the `output` or the `error`. The status is one
of: `pending`, `running`, `succeeded`, `failed`, `cancelled`.

Sometimes the agent calls an operation that needs a second person's
approval. The run then waits, and `run-status` still shows `running`.
If a run seems to stop moving, check `meho approvals list`. Approve or
reject the request with `meho approvals approve <id>` (see
[Approvals and break-glass](approvals-and-break-glass.md)). After about
30 minutes with no decision, the agent is told that no decision came.

More run commands:

```bash
meho agent run-list --agent-name pod-reader   # the runs of this agent
meho agent run-list --status running          # runs that are still going
meho agent run-cancel <run-id>                # stop a run
meho agent run-events pod-reader --input "..." # start a run and watch each step live
```

To run the agent on a schedule, with its own identity and its own
grants, see the [scheduler guide](scheduler.md). The scheduler needs
the agent's login secret. That guide explains where to store it.

## Step 7 — Check what the agent did

Every tool call the agent makes writes an audit row. All rows of one run
carry the run id in the `agent_session_id` field. So the run id is all
you need.

List every row of the run:

```bash
meho audit query --session-id <run-id>
```

Or show the run as a tree, with each step under the step that caused
it:

```bash
meho audit replay <run-id>
```

In each row, check:

- `op_id` — which operation the agent ran;
- `target_name` — on which target;
- `policy_decision` — `auto-execute`, `needs-approval`, or `deny`;
- `result_status` — `ok`, `error`, or `denied`;
- `principal_sub` — who acted: the agent's sub for a scheduled run, your
  sub for a run you started by hand.

`meho audit query` needs the `operator` role. `meho audit replay` needs
`tenant_admin`. The
[audit forensics guide](audit-forensics.md) explains all filters.

## What can go wrong here

| What you see | Why | What to do |
|---|---|---|
| `403` with `insufficient_role` on `agent create`, `agent edit`, or `agent grant create` | Setup needs `tenant_admin`. | Ask a tenant admin, or log in with a `tenant_admin` account. |
| `agent create` rejects `--identity-ref` | The agent principal does not exist, is revoked, or the value is not `agent:<name>`. | Run Step 1 first. Copy `keycloak_client_id` exactly. |
| `agent grant create` says the principal "does not name a registered, non-revoked agent principal" | `--principal` is not the client id. | Use `agent:<name>`, not a UUID. |
| The agent says it has no tool for something | The tool is not in `meta_tools`, the name is misspelled, or hosted agents do not have that tool. | Check the tool table in Step 3. |
| The agent says a connector is not allowed | The connector is not in the `connectors` list. | Add the connector id to `connectors`, or remove the list. |
| A call is `denied` | A grant or the safety level denies it. | Check `meho agent grant list --principal agent:<name>` and the operation's `safety_level`. |
| The run fails with `turn budget exhausted` | The agent used all its turns. | Raise `--turn-budget` with `meho agent edit`, or make the task smaller. |
| The run fails right away and the error names a runbook | The prompt or input tells the agent to run a runbook. Hosted agents cannot run runbooks. | Remove that instruction. |
| `agent run` gives back a run id but no output | The run took longer than the wait limit, so it went on in the background. | Run `meho agent run-status <run-id>`. |
| `409` on `agent run` | The definition is switched off. | Run `meho agent edit <name> --enabled`. |
