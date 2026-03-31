# Trust & Safety

> Last verified: v2.0

MEHO connects to production systems and executes operations on behalf of operators. The trust model ensures that **MEHO never modifies or destroys resources without explicit human approval**. Every operation is classified, every write requires confirmation, and every action is audited.

This page answers the question enterprise evaluators ask first: *"What if the AI breaks something?"*

The answer: it cannot. Not without the operator explicitly approving each modifying action.

## Three-Tier Trust Classification

Every operation MEHO can execute is classified into one of three trust tiers:

| Tier | Behavior | UI Treatment | Example Operations |
|------|----------|--------------|-------------------|
| **READ** | Executed automatically | No modal, no pause | `list_pods`, `query_prometheus`, `get_logs` |
| **WRITE** | Pauses for operator approval | Yellow approval modal | `scale_deployment`, `create_issue`, `send_email` |
| **DESTRUCTIVE** | Pauses for approval + confirmation | Red approval modal | `delete_pod`, `destroy_vm`, `delete_namespace` |

**READ** operations are view-only. They query metrics, list resources, fetch logs, and retrieve status. These execute automatically because they have no side effects.

**WRITE** operations modify state. Scaling a deployment, creating a Jira issue, syncing an ArgoCD application, or sending an email -- all require the operator to review the operation details in a modal dialog and explicitly approve before MEHO proceeds.

**DESTRUCTIVE** operations are irreversible. Deleting a pod, destroying a VM, or removing a namespace requires approval with an additional confirmation step. These are flagged with a red indicator and a warning about permanent consequences.

## How Trust Levels Are Assigned

Trust classification follows a five-level priority chain. The first match wins:

1. **Per-endpoint override** -- Administrators can override any operation's trust level in the database. A POST endpoint that is actually safe (like `/auth/login`) can be reclassified as READ.

2. **Static connector registry** -- Each typed connector has a hardcoded trust map. Operations are explicitly registered as READ, WRITE, or DESTRUCTIVE based on their actual behavior.

3. **Operation name heuristic** -- Operations following naming conventions are classified by prefix:
    - `list_`, `get_`, `describe_`, `search_`, `query_`, `export_`, `find_` → **READ**
    - `delete_`, `destroy_`, `remove_`, `unregister_` → **DESTRUCTIVE**

4. **HTTP method heuristic** -- For REST/OpenAPI connectors: `GET`/`HEAD`/`OPTIONS` → READ, `POST`/`PUT`/`PATCH` → WRITE, `DELETE` → DESTRUCTIVE.

5. **Default: WRITE** -- Any operation that cannot be classified falls through to WRITE. The system fails safe -- unknown operations always require approval.

### Typed Connector Trust Maps

Each typed connector has an explicit trust registry. Here are examples across connector families:

**Kubernetes:**

| Operation | Trust Level |
|-----------|-------------|
| `list_pods`, `list_namespaces`, `get_pod_logs`, `get_events` | READ |
| `scale_deployment`, `create_namespace`, `restart_deployment` | WRITE |
| `delete_pod`, `delete_deployment`, `delete_namespace` | DESTRUCTIVE |

**VMware vSphere:**

| Operation | Trust Level |
|-----------|-------------|
| `list_virtual_machines`, `list_hosts`, `get_virtual_machine` | READ |
| `snapshot_vm`, `power_on_vm`, `migrate_vm` | WRITE |
| `destroy_vm`, `delete_snapshot` | DESTRUCTIVE |

**ArgoCD:**

| Operation | Trust Level |
|-----------|-------------|
| `list_applications`, `get_application`, `get_resource_tree`, `get_sync_history` | READ |
| `sync_application` | WRITE |
| `rollback_application` | DESTRUCTIVE |

**GitHub:**

| Operation | Trust Level |
|-----------|-------------|
| `list_repositories`, `list_commits`, `list_pull_requests`, `get_workflow_logs` | READ |
| `rerun_failed_jobs` | WRITE |

**GCP:**

| Operation | Trust Level |
|-----------|-------------|
| `list_instances`, `get_cluster`, `list_builds`, `get_build_logs` | READ |
| `cancel_build`, `retry_build` | WRITE |
| `delete_instance`, `delete_cluster` | DESTRUCTIVE |

**Proxmox:**

| Operation | Trust Level |
|-----------|-------------|
| `list_nodes`, `list_vms`, `get_vm_status`, `get_storage` | READ |
| `start_vm`, `stop_vm`, `snapshot_vm` | WRITE |
| `delete_vm`, `delete_container` | DESTRUCTIVE |

### REST and SOAP Connectors

For generic REST connectors (auto-generated from OpenAPI specs) and SOAP connectors (from WSDL definitions), trust classification relies on the HTTP method heuristic and per-endpoint overrides. Administrators can fine-tune classification for any endpoint after connector registration.

### Skill-Based Operations

MEHO auto-generates markdown skills from OpenAPI specifications. These skills inherit trust classification from the underlying operation's metadata -- the same classification pipeline applies whether an operation is invoked directly or through a generated skill.

## The Approval Flow

When MEHO's agent identifies a WRITE or DESTRUCTIVE operation during an investigation, the following flow executes:

1. **Agent pauses** -- The ReAct execution loop halts. The agent's full context (scratchpad, reasoning steps, pending operation) is preserved in memory.

2. **Approval request created** -- A pending approval record is written to the database with the tool name, arguments, trust level, and a 60-minute expiry.

3. **Modal presented** -- The frontend displays an approval modal showing:
    - The operation name and connector
    - Full parameters being passed
    - Trust level indicator (yellow for WRITE, red for DESTRUCTIVE)
    - Impact warning message
    - Approve and Reject buttons

4. **Operator decides** -- The operator reviews and either approves or rejects.

5. **Agent resumes or aborts** -- On approval, the agent picks up exactly where it paused and executes the operation. On rejection, the agent acknowledges the denial and adjusts its investigation approach.

6. **Audit entry logged** -- Every approval decision (approve, reject, or expiry) is recorded with the actor, timestamp, IP address, and user agent.

!!! warning "Expiry Protection"
    Pending approvals expire after 60 minutes. If the operator does not respond, the approval is automatically marked as expired and the agent cannot execute the operation. This prevents stale approvals from being acted upon.

## Audit Trail

Every operation execution in MEHO is fully audited. The audit log captures:

| Field | Description |
|-------|-------------|
| **Approval ID** | Unique identifier for the approval request |
| **Session ID** | Chat session where the operation was requested |
| **Tenant ID** | Multi-tenant isolation identifier |
| **Actor** | User who approved or rejected the operation |
| **Tool Name** | The operation that was executed (e.g., `call_endpoint`, `scale_deployment`) |
| **Tool Arguments** | Full parameters passed to the operation |
| **Trust Level** | READ, WRITE, or DESTRUCTIVE classification |
| **HTTP Method** | The HTTP method used (for REST operations) |
| **Endpoint Path** | The target API endpoint |
| **Action** | `created`, `approved`, `rejected`, `expired`, or `executed` |
| **Outcome** | Success or failure status of the executed operation |
| **Timestamp** | When each action occurred |
| **IP Address** | Client IP of the approver |

Audit entries are immutable -- they are append-only records that cannot be modified or deleted.

### Execution Outcome Tracking

After an approved operation executes, a follow-up audit entry records the outcome:

- **Success**: The operation completed as expected. The outcome summary describes what happened.
- **Failure**: The operation failed. The error is captured in the audit trail for post-incident review.

This creates a complete chain: request → approval → execution → outcome.

## Key Guarantees

- **No silent writes.** MEHO will never execute a WRITE or DESTRUCTIVE operation without displaying an approval modal and receiving explicit operator confirmation.

- **Fail-safe classification.** Any operation that cannot be classified defaults to WRITE, requiring approval. The system errs on the side of caution.

- **Context preservation.** When the agent pauses for approval, its full investigation context is preserved. Approval does not restart the investigation -- it resumes exactly where it stopped.

- **Multi-tenant isolation.** Approval requests and audit logs are scoped to tenants. One tenant's operators cannot see or approve another tenant's operations.

- **Deduplication.** Identical tool calls (same arguments, same hash) are deduplicated to prevent double-execution from race conditions.

For security hardening details (authentication, encryption, transport security), see [Security](security.md).
