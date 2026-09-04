<!--
  GENERATED FILE — do not edit by hand.
  Regenerate from backend/ with: uv run python scripts/generate_reference_docs.py
  The freshness gate in backend/tests/test_reference_docs_drift.py fails CI when this page and the registry disagree.
-->

# MCP tool surface

MEHO exposes one narrow, stable surface of meta-tools over MCP — the same tools across every connector and product version, so an agent never has to route through a vendor's thousands of operations. The surface has three tiers, generated here from the in-process tool registry.

- **Working surface** — what every MCP session lists by default.
- **Operator plane** — governance tools a session lists only after requesting the `mcp:admin` OAuth scope (request-only; the realm grants it on ask).
- **Human-only** — decision verbs with no MCP path under any claim set; a human makes these at the console or CLI.

Each tool also exists as a `meho` CLI command against the same dispatch path — see the [CLI reference](cli.md). Maturity labels (`beta` / `experimental`) come from the [feature maturity registry](maturity.md); a `—` maturity is deliberately unclassified infrastructure.

## Working surface

The default agent surface. No elevation required.

| Tool | Maturity | Extra gate | Summary |
| --- | --- | --- | --- |
| `add_to_knowledge` | ga | — | Add a new entry to the tenant's knowledge base. |
| `add_to_memory` | ga | — | Add a new memory entry. |
| `ask_docs` | experimental | capability `meho-docs` | Answer a vendor-reference question with a SYNTHESIZED, CITED answer composed over a vendor-document collection (product manuals, KB articles, design / reference guides) — e.g. 'What are the NSX 9.0 config maximums for logical switches?'. |
| `call_operation` | ga | — | Invoke an operation. |
| `list_doc_collections` | experimental | capability `meho-docs` | List the documentation collections you are entitled to search — the catalogue an agent reads to learn WHICH `collection` to pass to `search_docs` / `ask_docs` before searching. |
| `list_operation_groups` | ga | — | List enabled operation groups for a connector. |
| `list_targets` | ga | — | List the operator's accessible infrastructure targets, optionally filtered by connector. |
| `meho_broadcast_announce` | beta | — | Publish an agent-authored announcement to the operator's tenant broadcast stream. |
| `meho_broadcast_recent` | beta | — | Read the operator's tenant's recent broadcast events from meho:feed:{tenant_id}. |
| `meho_broadcast_watch` | beta | — | Long-poll the operator's tenant broadcast stream for new events past 'cursor'. |
| `meho_connector_list` | ga | — | List ingested connectors visible to the operator's tenant (plus built-in / global). |
| `meho_runbook_abort` | beta | — | Abort an in-progress run. |
| `meho_runbook_list_runs` | beta | — | List runs in the tenant. |
| `meho_runbook_list_templates` | beta | — | List runbook templates in the operator's tenant. |
| `meho_runbook_next` | beta | — | Advance one step in an in-progress runbook run. |
| `meho_runbook_show_template` | beta | — | Read the full body of a runbook template, including step contents. |
| `meho_runbook_start` | beta | — | Start a new runbook run. |
| `meho_status` | — | — | Returns the operator's identity (sub, name, email, tenant_id, tenant_role) plus the MEHO backplane's dependency status: Vault federation chain (reachable + KV read OK?) and DB migration state. |
| `preview_operation` | ga | — | Preview an operation WITHOUT running it. |
| `query_topology` | beta | — | Query the topology graph. |
| `result_query` | ga | — | Read rows back from a JSONFlux result handle. |
| `search_docs` | experimental | capability `meho-docs` | Search a vendor-document collection (product manuals, KB articles, design / reference guides) for an authoritative vendor fact — e.g. 'NSX config maximums for 9.0' or 'vCenter 8.0 supported snapshot depth'. |
| `search_knowledge` | ga | — | Search the tenant's knowledge base for distilled operator knowledge: vendor API patterns, lab conventions, known-good runbooks, post-incident learnings. |
| `search_memory` | ga | — | Search the operator's accessible memories (own user-scoped entries + tenant-shared + target-shared entries visible to this operator). |
| `search_operations` | ga | — | Hybrid BM25 + cosine retrieval over a connector's enabled operations. |

### Add-on working tools

Listed on the working surface only while a paired add-on advertising the named family is active for the tenant; a backplane with no such add-on paired never lists them.

| Tool | Maturity | Extra gate | Summary |
| --- | --- | --- | --- |
| `meho_automation_list` | experimental | add-on `automation` | List the surface a paired automation add-on advertises — the automation analogue of `meho_connector_list`. |

## Operator plane (`mcp:admin`)

Connector lifecycle, principals and grants, scheduler, sensors, topology mutations, audit admin, and the other governance planes. A session lists and can call these only when it holds the `mcp:admin` scope.

| Tool | Maturity | Extra gate | Summary |
| --- | --- | --- | --- |
| `create_doc_collections` | experimental | capability `meho-docs` | Register a new documentation collection in your tenant so `search_docs` / `ask_docs` can route to it — the write half of the doc-collection registry (tenant_admin only). |
| `delete_doc_collections` | experimental | capability `meho-docs` | Deregister a disabled, tenant-owned documentation collection and free its `collection_key` for re-registration — the delete half of the doc-collection registry (tenant_admin only). |
| `meho_agent_principals_list` | experimental | — | List agent principals registered for the operator's tenant. |
| `meho_agent_principals_register` | experimental | — | Register a new agent principal for the operator's tenant. |
| `meho_agent_principals_revoke` | experimental | — | Revoke an agent principal — kill switch. |
| `meho_agents_create` | experimental | — | Create an agent definition for the operator's tenant. |
| `meho_agents_delete` | experimental | — | Delete an agent definition by name for the operator's tenant. |
| `meho_agents_edit` | experimental | — | Apply a partial update to an agent definition by name. |
| `meho_agents_grant_create` | experimental | — | Grant a permission to an agent principal. |
| `meho_agents_grant_list` | experimental | — | List agent permission grants for the operator's tenant. |
| `meho_agents_grant_revoke` | experimental | — | Revoke (delete) a permission grant by id. |
| `meho_agents_grant_show` | experimental | — | Fetch one agent permission grant by id. |
| `meho_agents_list` | experimental | — | List agent definitions for the operator's tenant. |
| `meho_agents_list_runs` | experimental | — | List the operator's tenant's agent runs, newest first. |
| `meho_agents_run` | experimental | — | Run a named agent for the operator's tenant. |
| `meho_agents_run_status` | experimental | — | Poll an agent run's durable status by run_id. |
| `meho_agents_show` | experimental | — | Fetch one agent definition by name for the operator's tenant. |
| `meho_approvals_get` | ga | — | Inspect a single approval request by id. |
| `meho_approvals_list` | ga | — | List approval requests for your tenant. |
| `meho_audit_replay` | ga | — | Reconstruct the full trace of one agent session — every operation, its result, and the parent/child graph between them — as a chronological ReplayNode forest. |
| `meho_broadcast_overrides_list` | beta | — | List broadcast-detail override rules for the operator's tenant. |
| `meho_broadcast_overrides_remove` | beta | — | Delete a broadcast-detail override rule by id for the operator's tenant. |
| `meho_broadcast_overrides_set` | beta | — | Create a broadcast-detail override rule for the operator's tenant. |
| `meho_connector_delete` | ga | — | Delete one connector (tenant_admin only): remove its operation_group + endpoint_descriptor rows under the target scope and, when no rows remain for the triple anywhere, deregister the auto-registered ingest shim from the v2 registry. |
| `meho_connector_disable` | ga | — | Flip every group of a connector to review_status='disabled' (tenant_admin only). |
| `meho_connector_edit_group` | experimental | — | Edit one operation group's when_to_use prose or display name (tenant_admin only). |
| `meho_connector_edit_op` | experimental | — | Edit one operation's per-op overrides (tenant_admin only). |
| `meho_connector_enable` | ga | — | Flip every group of a connector to review_status='enabled' (tenant_admin only). |
| `meho_connector_enable_reads` | ga | — | Bulk-enable every read-class operation of a connector in one pass (tenant_admin only). |
| `meho_connector_ingest` | experimental | — | Ingest one or more OpenAPI specs into a MEHO connector (tenant_admin only). |
| `meho_connector_ingest_status` | experimental | — | Poll the durable status of an async connector-ingest job by handle (operator-level). |
| `meho_connector_review` | experimental | — | Get the full review payload for one connector (groups + per-group operations + per-op flags). |
| `meho_memory_promote` | ga | — | Promote one memory to a strictly broader scope along the ladder: user -> user-tenant -> tenant, OR user -> user-target -> target. |
| `meho_runbook_deprecate_template` | beta | — | Mark a published version as deprecated. |
| `meho_runbook_discard_template` | beta | — | Delete an UNPUBLISHED DRAFT version of a template. |
| `meho_runbook_draft_template` | beta | — | Create the first draft of a new runbook template. |
| `meho_runbook_edit_template` | beta | — | Edit a runbook template. |
| `meho_runbook_publish_template` | beta | — | Flip a draft template to published. |
| `meho_runbook_reassign` | beta | — | Reassign an in-progress run to a different operator. |
| `meho_scheduler_cancel` | beta | — | Cancel one scheduled trigger by id. |
| `meho_scheduler_create` | beta | — | Create one scheduled trigger under the operator's tenant. |
| `meho_scheduler_list` | beta | — | List scheduled triggers for the operator's tenant. |
| `meho_sensor_create` | beta | — | Create one sensor under the operator's tenant. |
| `meho_sensor_delete` | beta | — | Hard-delete one sensor by id. |
| `meho_sensor_list` | beta | — | List sensors for the operator's tenant. |
| `meho_sensor_results` | beta | — | Per-tick evidence trend query for one sensor. |
| `meho_targets_register` | ga | — | Register a new target in the operator's tenant (tenant_admin only). |
| `meho_topology_annotate` | beta | — | Assert a curated `graph_edge` that auto-discovery cannot infer (tenant_admin only). |
| `meho_topology_bulk_import` | beta | — | Batch-assert curated `graph_edge` rows in one atomic pass (tenant_admin only). |
| `meho_topology_create_node` | beta | — | Manually seed a `graph_node` row in the operator's tenant (tenant_admin only). |
| `meho_topology_delete_node` | beta | — | Hard-delete a manually-seeded `graph_node` row by `node_id` (tenant_admin only), writing a `removed` history tombstone so the delete stays visible in `query_topology {kind: timeline}`. |
| `meho_topology_unannotate` | beta | — | Hard-delete a curated `graph_edge` and clear its reciprocal markers (tenant_admin only). |
| `query_audit` | ga | — | Query the audit log for forensic reconstruction. |

## Human-only (no MCP path)

Approving or rejecting a parked operation, and granting an agent a privilege elevation, are human decisions with no agent-facing path under any claim set. An agent that reaches for one is told where the human makes the decision instead.

| Tool | Where the decision is made |
| --- | --- |
| `meho_agents_grant_elevate` | operator console approvals queue, or the `meho` CLI |
| `meho_approvals_approve` | operator console approvals queue, or the `meho` CLI |
| `meho_approvals_reject` | operator console approvals queue, or the `meho` CLI |
