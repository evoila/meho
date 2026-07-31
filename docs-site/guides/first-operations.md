# Run your first operations

MEHO does not expose one tool per vendor operation — a vCenter alone
would be thousands. Instead, every connector publishes its operations
into one searchable catalog, and you (or your agent) reach them
through a small, stable set of meta-tools: **discover the connector,
list its operation groups, search for the operation, preview it,
call it**. This guide walks that ladder once, end to end, against a
typed Kubernetes connector — the same flow applies to every connector,
including ones ingested from an OpenAPI spec.

Every rung exists twice: as an MCP tool (for agents) and as a CLI verb
(for operators). Both run the same dispatch path — policy, credential
resolution, result reduction, audit.

!!! note "Prerequisites"

    - A registered, probe-green target —
      [Register targets and secrets](targets-and-secrets.md). The
      examples below use the `lab-rke2` Kubernetes target from that
      guide.
    - An **operator**-role session. Nothing in this guide needs
      tenant_admin.

## The ladder at a glance

| Step | MCP tool | CLI |
|---|---|---|
| Find the connector | `meho.connector.list` | `meho connector list` |
| List operation groups | `list_operation_groups` | `meho operation groups <connector_id>` |
| Search operations | `search_operations` | `meho operation search <connector_id> "<query>"` |
| Preview the request | `preview_operation` | — (REST/MCP only) |
| Call | `call_operation` | `meho operation call <connector_id> <op_id>` |
| Page a large result | `result_query` | — (MCP; agents drill in) |

## Step 0 — find your connector id

Operations are addressed by **`connector_id`**, which has the form
`<impl_id>-<version>` — for example `k8s-1.x`, `vault-1.x`,
`vmware-rest-9.0`. It is *not* the bare product name; a product can
have several connector implementations, and the resolver picks one
per target.

```bash
meho connector list
```

Pick the connector whose product matches your target. For the
`lab-rke2` target (`product: k8s`), that is `k8s-1.x`.

## Step 1 — list the operation groups

Groups are the map of a connector's surface. Each carries a
`when_to_use` hint, so you narrow hundreds of operations to a handful
before searching.

```bash
meho operation groups k8s-1.x
```

Typical output shape: `inventory` (nodes, namespaces, versions),
`workload` (pods, deployments, services), `logs`, `events`, `write`
(mutating ops), each with an operation count.

A group flagged `partial: true` is only partly enabled — an operator
enabled specific operations rather than the whole group. Only the
`enabled_op_count` operations in it are live; search will find exactly
those.

## Step 2 — search for the operation

Search is hybrid lexical + semantic over the connector's *enabled*
operations. Scope it to a group when you know one:

```bash
meho operation search k8s-1.x "pods that are not running" --group workload
```

Each hit carries the fields that matter before you call anything:

- **`op_id`** — the handle you pass to call, e.g. `k8s.pod.list`.
- **`safety_level`** — `safe`, `caution`, or `dangerous`.
- **`requires_approval`** — whether a call parks for a second pair of
  eyes instead of executing.

Read those two flags on every hit **before** calling — see
[Safety flags at first contact](#safety-flags-at-first-contact).

## Step 3 — preview (when you want the wire truth)

`preview_operation` resolves the exact operation + target + params a
call would use and returns the literal would-be HTTP request —
method, resolved path, query, and a redacted body — **without sending
it**. It is the fastest way to diagnose a rejected write: re-issue the
same arguments to preview and read back exactly what would go on the
wire.

Two honest limits:

- It covers **spec-ingested HTTP operations**. A typed or composite
  operation (like the Kubernetes ops here) has no single literal HTTP
  request, so preview returns `status: "unavailable"` — that is the
  expected answer, not a failure.
- It is available over MCP and REST (`POST
  /api/v1/operations/preview`); there is no CLI verb for it today.

## Step 4 — call

The worked example: find every pod in the cluster that is not
`Running`.

```bash
meho operation call k8s-1.x k8s.pod.list \
  --target lab-rke2 \
  --params '{"all_namespaces": true, "field_selector": "status.phase!=Running"}'
```

The result envelope always has the same shape — `status`, `op_id`,
`result`, `error`, `duration_ms`, `extras`. On success
(`status: "ok"`), the payload here is rows of
`{name, namespace, status, ready, restarts, age_seconds, node, ip}`
plus a `total`:

```json
{
  "status": "ok",
  "op_id": "k8s.pod.list",
  "result": {
    "rows": [
      {"name": "web-6f7d4b", "namespace": "demo", "status": "CrashLoopBackOff",
       "ready": "0/1", "restarts": 17, "age_seconds": 5520,
       "node": "rke2-w1", "ip": "10.42.0.31"}
    ],
    "total": 1
  }
}
```

Drill into the offender with the sibling read ops — same ladder, no
new concepts:

```bash
meho operation call k8s-1.x k8s.pod.info \
  --target lab-rke2 --params '{"pod_name": "web-6f7d4b", "namespace": "demo"}'

meho operation call k8s-1.x k8s.logs \
  --target lab-rke2 --params '{"pod_name": "web-6f7d4b", "namespace": "demo"}'

meho operation call k8s-1.x k8s.event.list \
  --target lab-rke2 --params '{"namespace": "demo"}'
```

Two conveniences worth knowing:

- Frequently-used connectors also ship **shortcut verbs** that pre-bake
  the connector id — `meho k8s pod list --target lab-rke2 --namespace
  demo` dispatches the identical operation through the identical
  governed path. Sugar only.
- `--params` accepts inline JSON or `@<file>`; pass `work_ref` (MCP)
  to stamp a change-ticket reference onto the audit row.

## Large results: handles

Set-shaped results are **automatically reduced server-side** above a
threshold (more than 50 rows, or more than 4 KB serialized). You get
a representative sample inline plus a **result handle** — never a
multi-megabyte payload in your context. This is not opt-in and cannot
be opted out of per call.

When a result was reduced, its `fetch_more.drill_in` block tells you
so — `available: true`, an `example_call`, and the handle's
`expires_at`. Page through the full set with `result_query`:

```json
{"handle_id": "<uuid from the result>", "offset": 50, "limit": 100}
```

The envelope returns `rows`, `offset`, `limit`, `returned_rows`,
`total_rows`, `stored_rows`, `truncated`. Notes that save you a
confused hour:

- Handles are **scoped to you** (operator + tenant) and expire. A
  cross-operator read, an expired handle, and a nonexistent one are
  indistinguishable: *"handle … is not readable: it does not exist,
  has expired, or belongs to a different operator. Re-run the
  operation to get a fresh handle."* That is isolation working, not a
  bug.
- `result_query` is the drill-in tool that ships today; aggregation
  and export tools are on the roadmap but do not exist yet.
- When `drill_in.available` is `false`, the full set was *not*
  spilled (the `reason` field says why) — re-run the operation with
  narrower params instead of hunting for a handle.

## Safety flags at first contact

Every operation carries two independent markers, set per-op when the
connector is registered or reviewed:

- **`safety_level`** — `safe` (read-class, executes under
  default-allow), `caution`, or `dangerous` (write/destructive class,
  subject to policy).
- **`requires_approval`** — when `true`, calling the operation does
  **not** execute it. The dispatcher durably parks it and returns:

```json
{
  "status": "awaiting_approval",
  "op_id": "k8s.delete",
  "error": "awaiting_approval: 'k8s.delete' requires approval before execution",
  "extras": {"error_code": "awaiting_approval", "approval_request_id": "<uuid>"}
}
```

`awaiting_approval` is a first-class outcome, not an error. The gate
is **server-side** — neither you nor an agent can opt out of it, and
the classification keys on the operation, not on who is calling. The
parked request is resolved on the operator surfaces:

```bash
meho approvals list
meho approvals show <approval_request_id>
meho approvals approve <approval_request_id> --reason "verified blast radius"
meho approvals reject  <approval_request_id> --reason "wrong target"
```

By default you cannot approve your own request (the four-eyes rule),
and parked requests expire on a TTL. The full story — including the
audited single-operator break-glass — is the
[approvals & break-glass guide](index.md#coming-to-this-section).

Before approving *any* fan-out write: open the approval payload and
verify the resolved object list first. An unconstrained filter is how
a one-VM drill becomes a cluster-wide incident.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `-32602` with `data.reason: unknown_connector` | The `connector_id` names nothing registered — usually a bare product name (`k8s`) where `<impl_id>-<version>` (`k8s-1.x`) belongs. | `meho connector list`, copy the id verbatim. |
| `-32602` with `data.reason: connector_not_ingested` | The connector is registered but its spec has not been ingested, so it has no operations yet. Recoverable: the error's `data.next_step.verb` carries the exact `meho connector ingest …` command to run. | Run the named command, then retry. |
| `meho operation groups` returns an empty list | The connector exists but nothing is enabled yet — operations ship default-deny until reviewed/enabled. | `meho connector review <id>`, then enable groups or single ops (`meho connector enable-reads <id>` bulk-enables the read class). |
| Search cannot find an operation you know exists | Search only indexes **enabled** operations; a disabled op is invisible by design. An unknown `--group` also silently narrows to zero hits (that is not an error). | Check enablement via `meho connector review`; drop the group filter. |
| `status: "error"` with `invalid_params` | The params failed the operation's schema. **Do not retry the same arguments verbatim.** | Read `extras.validation_errors`, fix the shape (e.g. `k8s.pod.list` requires exactly one of `namespace` / `all_namespaces: true`), re-call. |
| `status: "error"` with a `connector_error` / credential message | Dispatch reached the connector but the target's credential failed to resolve or was rejected. | Work the [targets & secrets failure table](targets-and-secrets.md#what-can-go-wrong-here) — the error strings there map one-to-one. |
| `status: "denied"` | Policy refused the call for this principal — distinct from a missing approval. | Inspect the reason in `extras`; this is a grants/policy conversation, not a retry. |
| `status: "awaiting_approval"` on what you expected to just run | The op carries `requires_approval` — working as designed. | See [Safety flags](#safety-flags-at-first-contact). |
| `handle_not_found` from `result_query` | Handle expired, or it belongs to another operator/tenant. | Re-run the producing operation; page promptly. |

**Next:** [Watch your estate with sensors](sensors-quickstart.md).
