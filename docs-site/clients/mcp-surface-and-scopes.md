# MCP surface and scopes

When an MCP client connects to MEHO, it does **not** see one tool per
vendor operation. It sees a small, stable set of meta-tools that works
the same way for every connector and every product version. That set is
deliberately tiered by trust: a default agent session gets a narrow
working surface, the operator planes are hidden until a session
explicitly asks for them, and a few human-decision verbs have no MCP
path at all.

This page explains the three tiers, how a client opts into the operator
planes, and why an agent can never approve its own work. It is the same
model whichever client you wired in [Connect clients](index.md).

## The default working surface (25 tools)

Every session lists the **25-tool working surface** with no elevation —
enough to discover connectors, run governed operations, page large
results, and coordinate with other operators. The tools group into ten
families:

| Family | Tools |
|---|---|
| **Health** | `meho_status` |
| **Connectors** | `meho_connector_list` |
| **Operation discovery** | `list_operation_groups`, `search_operations` |
| **Execution** | `call_operation`, `preview_operation` |
| **Result handles** | `result_query` |
| **Knowledge** | `search_knowledge`, `add_to_knowledge` (plus the capability-gated docs add-on: `ask_docs`, `search_docs`, `list_doc_collections`) |
| **Memory** | `search_memory`, `add_to_memory` |
| **Broadcast** | `meho_broadcast_recent`, `meho_broadcast_announce`, `meho_broadcast_watch` |
| **Targets and topology** | `list_targets`, `query_topology` |
| **Runbooks (run family)** | `meho_runbook_start`, `meho_runbook_next`, `meho_runbook_abort`, `meho_runbook_list_runs`, `meho_runbook_list_templates`, `meho_runbook_show_template` |

Three of the 25 — the grounded-documentation add-on tools `ask_docs`,
`search_docs`, and `list_doc_collections` — appear only when the tenant
has provisioned the `meho-docs` capability. A session without that
capability lists the other 22.

The working surface is the same across every connector. An agent picks a
connector, lists its operation groups, searches for the operation it
needs, previews it, and calls it — the vendor's own identifiers stay in
the operation id, never in a tool name.

### The discovery-and-execution ladder

A useful session almost always walks the same rungs:

1. `meho_connector_list` — which connectors can this session reach.
2. `list_operation_groups` — the enabled operation groups on one
   connector, each with a "when to use this group" description.
3. `search_operations` — find the operation, optionally scoped to a
   group.
4. `preview_operation` / `call_operation` — preview, then run against a
   target.
5. `result_query` — page a large, set-shaped result back from its
   handle.

## The operator planes (behind `mcp:admin`)

The governance and lifecycle tools — connector lifecycle and ingest,
agents / principals / grants, scheduler, sensors, broadcast overrides,
runbook **template authoring**, topology **mutations**, target
registration, doc-collection create/delete, memory promotion, approvals
**read**, and audit query — form a second tier of **53 operator-plane
tools**. They are absent from a default session's tool list and refused
at call time, and they appear **only** for a session that explicitly
requested the `mcp:admin` OAuth scope.

Elevation is a per-session, explicit opt-in — not a standing role. A
session must *choose* to hold the operator planes; it never carries them
by default on the strength of its identity. That is what keeps a routine
agent session from reaching the governance controls it does not need.

### How a client requests elevation

The client asks for the extra scope, and your realm mints it into the
token only on request (the realm grants `mcp:admin` request-only):

- **Claude Code plugin** — set the `MEHO_MCP_SCOPES` environment seam to
  the space-separated scopes you want, adding `mcp:admin`:

    ```bash
    MEHO_MCP_SCOPES="mcp:read mcp:execute mcp:admin"
    ```

- **Claude Desktop `.mcpb` bundle** — set the bundle's **OAuth scopes**
  (`scopes`) user-config field to the same value in the install dialog.

Requesting a scope your realm does not grant simply degrades to the
default working surface — it never fails the connection. Leave the scope
off entirely and the session stays on the 25-tool working surface, which
is the right default for almost every agent.

## The human-only decision verbs (no MCP path)

Three verbs have **no MCP registration under any scope**, elevated or
not:

- `meho_approvals_approve`
- `meho_approvals_reject`
- `meho_agents_grant_elevate`

Approving an operation, rejecting it, or granting an agent more access
are human decisions. They are absent from every session's tool list, and
a direct `tools/call` on any of them is refused **before** the tool
registry is even consulted, with a remediation that names the human
path: the operator console approvals queue, or the concrete CLI verb
(`meho approvals approve <request-id>`, `meho approvals reject
<request-id> --reason <text>`, `meho agent grant elevate …`).

This is why an agent can never approve its own work. A model session that
parks a risky operation cannot hold the button that clears its own gate —
that decision moves to a person at the console or the CLI. Elevating to
`mcp:admin` does not change this: the three verbs are not "operator-plane
tools you can unlock", they simply do not exist on MCP.

## Naming that trips people up

A few names on the working surface are easy to get wrong. Getting them
right saves a round of `-32602` errors:

- **`call_operation`'s operation argument is `op_id`** — the operation
  identifier from `search_operations`, not a field called `op` or
  `operation`.
- **`call_operation`'s `target` resolves by name** — the human-readable
  target name, not the UUID that `list_targets` returns.
- **The broadcast tools carry the `meho_` prefix** —
  `meho_broadcast_recent`, `meho_broadcast_announce`,
  `meho_broadcast_watch`. A bare `broadcast_announce` is not a
  registered tool.

## The full tool inventory

This page describes the tiering, not every row. The complete per-tool
inventory — each tool, its surface tier, and the claim that gates it —
is published as a generated MCP tool reference in the
[Reference](../reference/index.md) section, generated from the live
`tools/list` snapshot so it cannot drift from the product.
