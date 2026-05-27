<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `examples/r2-approval-gate/` — Operator-approval gate reference

This is **R2** of the [G11.6 reference patterns][initiative] —
runnable sample + guide showing how a change-class action by an agent
pauses for human approval, how the operator receives + responds, and
how the agent run resumes after the decision lands.

[initiative]: https://github.com/evoila/meho/issues/807

Composition only — every primitive used here ships in `evoila/meho`.
No new MEHO surface; this directory is the recipe.

## When to use this pattern

Use the operator-approval gate when you want an agent to **propose**
a change-class action (write, restart, revert, delete, rotate) and
have a human **execute** the decision. Examples:

- An incident-triage agent finds a misconfigured VM and proposes
  reverting to the last-known-good snapshot — the operator approves
  the revert.
- An on-call agent identifies a stale Vault role binding and proposes
  rotating it — the operator approves the rotation.
- A cron-scheduled cost-control agent proposes shutting down a dev
  cluster overnight — the operator approves the shutdown the first
  time, then grants standing approval for the subsequent runs.

The gate is **per-(principal, op, target)**: the same agent can be
auto-execute on read ops, needs-approval on snapshot reverts, and
denied on cluster deletes — wired by the
[AgentPermission grants](#1-permission-setup) the tenant admin
inserts. The verdict is resolved at dispatch time by the
[`policy_gate`][policy_gate] seam (`_validate.py`).

[policy_gate]: ../../backend/src/meho_backplane/operations/_validate.py

## What you need before reading this

- A working MEHO backplane (v0.2 or later — the approval primitives
  shipped under [Initiative #803][i803]) reachable at `$MEHO_INSTANCE`.
- A Keycloak agent principal registered for the agent
  ([G11.2-T1 #815][t815] — `kind=agent` Keycloak client with a
  stable `sub`).
- `tenant_admin` role for setting up grants; `operator` (or higher)
  role for approving/rejecting requests.

[i803]: https://github.com/evoila/meho/issues/803
[t815]: https://github.com/evoila/meho/issues/815

## What ships in this directory

| File | Purpose |
|---|---|
| `README.md` | This file. The end-to-end guide. |
| [`agent_definition.json`](./agent_definition.json) | Runnable `AgentDefinitionCreate` payload for the demo agent — a single-tool agent that attempts `vmware.composite.vm.snapshot.revert`. |
| [`permissions.json`](./permissions.json) | The agent's `AgentPermission` grants. Wires the snapshot-revert op to `needs-approval` for this principal. |
| `../../backend/tests/test_examples_r2_approval_gate.py` | The CI exercise. Drives the full pause → approve → resume cycle in-process; runs in the unit lane on every PR. |

## The end-to-end flow

```
       Agent loop                Backplane                Operator
           |                         |                       |
           | call_operation(         |                       |
           |   op=vmware...revert,   |                       |
           |   target=vm-42 )        |                       |
           |------------------------>|                       |
           |                         | resolve_verdict →     |
           |                         |   needs-approval      |
           |                         | create_pending +      |
           |                         |   approval.pending    |
           |   awaiting_approval     |   broadcast event     |
           |   {approval_request_id} |---------------------->|
           |<------------------------|     (SSE / wall /     |
           |                         |      MCP watch)       |
           | wait_for_approval_      |                       |
           |   decision(...)         |                       |
           |   [XREAD BLOCK]         |                       |
           |                         |                       |
           |                         |     POST /decide      |
           |                         |     (or MCP           |
           |                         |      meho.approvals   |
           |                         |      .approve)        |
           |                         |<----------------------|
           |                         | approve_request →     |
           |                         |   row.status=approved |
           |                         |   audit.decision      |
           |                         |   approval.approved   |
           |     approval.approved   |   broadcast event     |
           |<------------------------|                       |
           | re-dispatch(            |                       |
           |   _approved=True,       |                       |
           |   original_params)      |                       |
           |------------------------>|                       |
           |                         | gate bypassed →       |
           |                         |   execute op          |
           |                         |   audit.success       |
           |   ok + result           |                       |
           |<------------------------|                       |
```

The whole loop is composition over three substrates the backplane
already ships:

- **G11.2 approval queue** — durable `ApprovalRequest` row + two
  synchronous audit rows ([`approval_queue.py`][aq]).
- **G11.2 approval surface** — REST `/api/v1/approvals/*` and MCP
  `meho.approvals.*` for operator decisions ([`approvals.py REST`][rest],
  [`approvals.py MCP`][mcp]).
- **G11.1 agent-runtime resume** — `XREAD BLOCK` over the per-tenant
  Valkey stream → re-dispatch with `_approved=True` on the approved
  broadcast ([`approval_wait.py`][aw]).

[aq]: ../../backend/src/meho_backplane/operations/approval_queue.py
[rest]: ../../backend/src/meho_backplane/api/v1/approvals.py
[mcp]: ../../backend/src/meho_backplane/mcp/tools/approvals.py
[aw]: ../../backend/src/meho_backplane/agent/approval_wait.py

## 1. Permission setup

The verdict resolver ([`auth/permissions.py`][perms]) computes:

```
effective_verdict = user-role ∩ agent-permission ∩ op-requirement
```

For a change-class op like `vmware.composite.vm.snapshot.revert`:

- The op is shipped with `safety_level="dangerous"` and
  `requires_approval=True` ([composites registry][composites]).
- A `dangerous` op with no matching grant defaults to **deny**.
- A grant with verdict `auto-execute` is *tightened* to
  `needs-approval` by the safety-level ceiling — destructive ops are
  never auto-executed.
- A grant with verdict `needs-approval` passes through the ceiling
  unchanged.

So to make the snapshot-revert op pause for human approval (not deny,
not auto-execute), insert one `AgentPermission` row matching:

| Column | Value | Meaning |
|---|---|---|
| `tenant_id` | the agent's tenant UUID | scopes the grant |
| `principal_sub` | the agent's Keycloak `sub` | who this grants applies to |
| `op_pattern` | `vmware.composite.vm.snapshot.revert` | fnmatch glob; an exact op id here, but `vmware.composite.vm.*` would cover all VM composites |
| `target_scope` | `"*"` (any VM) or a specific VM UUID | restrict to one target if you want |
| `verdict` | `needs-approval` | the gate verdict |

[perms]: ../../backend/src/meho_backplane/auth/permissions.py
[composites]: ../../backend/src/meho_backplane/connectors/vmware_rest/composites/_register.py

`permissions.json` in this directory carries the exact payload — one
JSON row per grant. Apply each row via one of:

- REST: `POST /api/v1/agents/grants` (G11.2-T6 #819), one POST per row.
- CLI: `meho agent grant create --principal ... --op ... --target ... --verdict ...`,
  one invocation per row. See [§6](#6-productionising-the-agent-definition)
  for the literal commands.
- MCP: `meho.agents.grant.create` tool, one call per row.

All three wrap the same INSERT into `agent_permission`.

### Pattern specificity tip

If you also grant `vmware.composite.vm.*` to the same principal with
verdict `auto-execute`, the snapshot-revert row above (more specific
literal prefix) wins and the op still pauses. The resolver's
[specificity tie-break][specificity] is deterministic — see the
`_pattern_specificity` helper in `permissions.py`.

[specificity]: ../../backend/src/meho_backplane/auth/permissions.py

### Principal-kind branch

`needs-approval` only fires for **agent principals**
(`principal_kind=agent` claim). Human operators and service accounts
keep the v0.2 default — a `requires_approval=True` op is hard-denied
for non-agent principals so a human who could always run an op does
not suddenly start pending or denying on the same call. See
[`policy_gate`][policy_gate] for the discriminator.

## 2. Request flow — the agent pauses

When the agent (running via the runtime in `meho_backplane.agent.invoke`)
calls the wrapped `call_operation` tool on a `needs-approval` op:

1. The dispatcher resolves the verdict (`policy_gate` → `needs-approval`).
2. `create_pending_request` inserts an `ApprovalRequest` row +
   one `approval.request` audit row in the **same transaction**.
3. The dispatcher returns an `OperationResult` with:
   ```jsonc
   {
     "status": "awaiting_approval",
     "extras": {
       "approval_request_id": "fb3a…-aa11-…",
       "error_code": "awaiting_approval"
     },
     "error": "awaiting_approval: …"
   }
   ```
4. Right after commit, the dispatcher publishes
   `approval.pending` on the per-tenant Valkey stream
   (`meho:feed:{tenant_id}`).

The agent loop's wrapped `call_operation`
([`meta_tools.py`][meta_tools]) sees the `awaiting_approval` envelope
and hands control to `resume_or_surface_awaiting_approval`
([`approval_wait.py`][aw]). That helper subscribes to the per-tenant
Valkey stream via `XREAD BLOCK` and blocks until either the matching
`approval.{approved,rejected}` event arrives or the agent's
configured `agent_approval_wait_timeout_seconds` (default 30 min,
settings-driven) elapses.

[meta_tools]: ../../backend/src/meho_backplane/operations/meta_tools.py

The model sees only the `awaiting_approval` envelope inside the wait;
the wait blocks the tool call so the model loop does not progress
past the parked op until a decision lands.

## 3. Response flow — the operator decides

The operator has four canonical surfaces (all driving the same
`approve_request` / `reject_request` service-layer functions in
[`approval_queue.py`][aq]):

### REST

```bash
# List pending approvals in the tenant
curl -sH "Authorization: Bearer $TOKEN" \
  "$MEHO_INSTANCE/api/v1/approvals?status=pending"

# Approve by id alone — the /decide path (no params needed; the
# durable row + audit row are the authorization). The decision goes in
# the JSON body, not a query param — the DecideRequestBody is
# extra="forbid" so anything outside {"decision", "reason"} 422s.
curl -sX POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approved"}' \
  "$MEHO_INSTANCE/api/v1/approvals/$REQUEST_ID/decide"

# Reject via the same /decide path (decision + optional reason in the body)
curl -sX POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"decision": "rejected", "reason": "revert window violates change calendar"}' \
  "$MEHO_INSTANCE/api/v1/approvals/$REQUEST_ID/decide"

# Or use the dedicated /reject endpoint (legacy; same outcome)
curl -sX POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "revert window violates change calendar"}' \
  "$MEHO_INSTANCE/api/v1/approvals/$REQUEST_ID/reject"
```

The legacy REST `POST /approve` endpoint (which re-dispatches inline
with the operator-supplied `params`) coexists for backwards
compatibility, but the **decide** path is the canonical one for any
operator who came in via the surfacing channel — the agent picks up
execution on the broadcast event, not on the REST round-trip.

### MCP

For operators driving Claude Code (or any other MCP-aware client):

```
meho.approvals.list                            # pending requests
meho.approvals.get(request_id="…")             # inspect proposed_effect
meho.approvals.approve(request_id="…")         # approve
meho.approvals.reject(request_id="…", reason="…")  # reject
```

Same wire format as the REST surface; tools auto-load when the
operator's MCP client connects to `$MEHO_INSTANCE/mcp`. Tool
definitions live in [`mcp/tools/approvals.py`][mcp].

### CLI

```
meho approvals list                            # pending requests
meho approvals show <request-id>               # inspect proposed_effect
meho approvals approve <request-id>            # approve
meho approvals reject <request-id> --reason …  # reject
```

Source: [`cli/internal/cmd/approvals`][cli].

[cli]: ../../cli/internal/cmd/approvals

### Wall monitor

Any operator with `meho status --watch` open sees the `approval.pending`
event arrive in real time (per-tenant SSE feed). The wall pairs well
with the MCP surfaces — the operator sees the pending request the
instant the agent posts it, then approves via their MCP client without
leaving the chat.

### What `approve_request` does, atomically

In the same DB transaction:

- Flips `approval_request.status` to `"approved"`,
- Stamps `reviewed_by = operator.sub` + `decided_at = now()`,
- Writes one `approval.decision` audit row (`status_code=200`,
  payload includes `{"decision": "approved", "reviewed_by": …}`).

After commit, `publish_approval_event` posts an `approval.approved`
event on the per-tenant Valkey stream. The publish is **fail-open** —
a Valkey outage does not block the durable decision; the row +
audit row remain the source of truth.

A params-hash check guards against substitution attacks on the
**legacy** REST `/approve+params` path: the operator-decision path
(REST `/decide`, MCP, CLI) skips the hash check because it does not
hand the operator the params at all — the agent's in-memory params
are the authoritative source for the re-dispatch.

## 4. Resume flow — the agent continues

The agent's wait observes `approval.approved` on the Valkey stream,
matches it by `payload.approval_request_id`, and:

- **approved** — calls `call_operation_with_approval(operator,
  call_arguments)` which threads `_approved=True` into the dispatcher.
  The policy gate is bypassed (the durable approval row is the
  authorization). The op executes normally and returns the result
  envelope to the model. A new audit row records the executed dispatch
  under the **agent principal** (subject = agent), with the operator's
  identity preserved on the approval-decision audit row — the audit
  chain has both.
- **rejected** — returns an annotated envelope to the model:
  ```jsonc
  {
    "status": "awaiting_approval",
    "extras": {"error_code": "approval_rejected", "decision": "rejected"},
    "error": "awaiting_approval: operator rejected request …. Try a different approach or stop."
  }
  ```
  The model decides what to do next (try an alternative, abort, ask
  the user). Nothing is executed.
- **timeout** — same envelope shape with
  `extras["error_code"] = "awaiting_approval_timeout"`. Distinct from
  rejection so the model can tell "no decision yet" apart from
  "operator said no".

The model never sees the operator's identity inside its prompt — only
the structured decision outcome. The full attribution chain (which
operator approved which request for which agent on which target)
lives in the audit log.

## 5. Verifying it works end-to-end

The CI exercise lives at
[`backend/tests/test_examples_r2_approval_gate.py`][test].
It drives the full cycle in-process.

**A note on the test's op id.** The test registers a stand-in op
`examples.r2.snapshot.revert` rather than driving the production
`vmware.composite.vm.snapshot.revert` directly — the stand-in keeps
the unit-lane test from colliding with the real composite registry and
from needing a vCenter. The stand-in carries the **same descriptor
flags as the production composite** (`requires_approval=True`,
`safety_level="dangerous"`), so the gate's behaviour is identical:
the verdict resolver, the durable approval row, the audit chain, and
the broadcast / resume path all exercise the same code paths. The
integration coverage for the real `vmware.composite.vm.snapshot.revert`
path lives in [`backend/tests/test_approval_queue.py`][test-aq] and
[`backend/tests/test_agent_approval_resume.py`][test-aar], which drive
the production code against representative composites end-to-end.

[test-aq]: ../../backend/tests/test_approval_queue.py
[test-aar]: ../../backend/tests/test_agent_approval_resume.py

The cycle the demo test drives:

1. Registers a `requires_approval=True` typed op.
2. Inserts an `AgentPermission` row granting the agent
   `needs-approval` on the op (matches `permissions.json`).
3. Dispatches the op as an agent principal — asserts
   `awaiting_approval` with a fresh `approval_request_id`.
4. Operator (a higher-role principal in the same tenant) calls
   `approve_request` — asserts the row flipped + the
   `approval.decision` audit row landed.
5. Stubs the broadcast client with a synthetic
   `approval.approved` event for the request id.
6. Calls `resume_or_surface_awaiting_approval` — asserts the wait
   observed the decision, re-dispatched, and the op executed
   (`status="ok"`, handler payload echoed back).
7. Verifies the audit-row attribution: the executed op's audit row
   carries the agent's `sub` (not the operator's), the approval
   decision row carries the operator's `sub`.

[test]: ../../backend/tests/test_examples_r2_approval_gate.py

Run locally:

```bash
cd backend
uv sync --locked --all-groups
uv run pytest tests/test_examples_r2_approval_gate.py -x -q
```

CI runs the same test in the **Python (ruff + mypy + pytest)** lane
on every PR (it's collected by the default `tests/` testpath in
`backend/pyproject.toml`; no testcontainers needed — the test stubs
the broadcast client and uses the in-memory SQLite engine).

## 6. Productionising the agent definition

`agent_definition.json` is a starter. To run the agent against a real
backplane:

```bash
# 1. Pick up the operator's token
meho login $MEHO_INSTANCE

# 2. Register the agent's Keycloak principal (G11.2-T1 #815). The
#    backend creates the kind=agent Keycloak client and a DB row; the
#    assigned `sub` is the clientId (`agent:<name>`). --owner-sub sets
#    the kill-switch owner and defaults to the caller's sub.
meho agent-principal register vmware-snapshot-revert-agent \
  --owner-sub $MY_SUB
AGENT_SUB="agent:vmware-snapshot-revert-agent"

# 3. Apply the AgentPermission grants. The grant API takes one
#    row per call, so the two permissions.json rows become two
#    `meho agent grant create` invocations. --target '*' scopes the
#    grant to any VM; pin a specific UUID to narrow it.
meho agent grant create \
  --principal $AGENT_SUB \
  --op vmware.composite.vm.snapshot.revert \
  --target '*' \
  --verdict needs-approval
meho agent grant create \
  --principal $AGENT_SUB \
  --op 'vmware.composite.vm.*' \
  --target '*' \
  --verdict auto-execute

# 4. Create the AgentDefinition. The CLI takes the fields
#    agent_definition.json carries as flags (no `--file`). --toolset
#    accepts inline JSON, @<path> to read a file, or @- for stdin —
#    the value must be a JSON object, so extract the nested
#    `toolset` field out of agent_definition.json first. Required
#    flags: --identity-ref, --model-tier, --system-prompt,
#    --turn-budget.
jq .toolset agent_definition.json > /tmp/r2-toolset.json
meho agent create vmware-snapshot-revert-agent \
  --identity-ref "$AGENT_SUB" \
  --model-tier standard \
  --system-prompt "$(jq -r .system_prompt agent_definition.json)" \
  --turn-budget 5 \
  --toolset @/tmp/r2-toolset.json

# 5. Run the agent. The first call hits the needs-approval gate and
#    pauses with status='awaiting_approval'.
meho agent run vmware-snapshot-revert-agent \
  --input "Snapshot-revert vm-42 to last-known-good"
```

When the operator sees the pending approval on `meho status --watch`
or `meho approvals list`, they decide:

```bash
meho approvals approve <request-id>
```

The agent resumes within a sub-second on the Valkey broadcast event;
the revert executes; the run completes.

## Out of scope

- **R1 — Tiered triage loop** (`examples/r1-tiered-triage/`): a parallel
  sibling under #807. The triage agent that *invokes* this approval-
  gated agent lives there.
- **R3 — Closed-loop kb write-back** (`examples/r3-kb-writeback/`):
  another sibling.
- **R4 — Local-Claude-as-triage** (`examples/r4-local-claude/`): another
  sibling.
- **Detailed Keycloak realm setup.** This guide assumes the agent
  principal already exists. See [G11.2-T1 #815][t815] for the
  client-registration recipe.
- **Cost budgets** (G11.5 / C3). The approval queue is the gate;
  budgets are a separate Initiative.
- **Process-restart resume.** The wait helper is live-only — a
  backplane restart while the wait is blocked drops the in-memory
  wait state, but the durable approval row + audit row + broadcast
  event preserve the decision. The agent's parent runtime can re-issue
  the wait on restart; the broadcast cursor will pick up the
  already-published `approval.approved` event. A first-class
  durable-checkpoint follow-up is filed as part of G11.1.

## References

- [Initiative #807][initiative] — G11.6 Reference patterns (R1–R4)
- [Initiative #803][i803] — G11.2 Agent identity + RBAC + approval
- [Task #817](https://github.com/evoila/meho/issues/817) — durable approval queue
- [Task #818](https://github.com/evoila/meho/issues/818) — approval surfacing channel
- [Task #820](https://github.com/evoila/meho/issues/820) — per-(principal, op, target) permission model
- [Task #1117](https://github.com/evoila/meho/issues/1117) — agent-runtime resume on broadcast
- [RFC 8693 §1.1](https://datatracker.ietf.org/doc/html/rfc8693#section-1.1) — delegation vs impersonation (background on the audit-chain shape)
