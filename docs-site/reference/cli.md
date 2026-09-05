<!--
  GENERATED FILE — do not edit by hand.
  Regenerate from cli/ with: make cli-docs
  Source of truth: the meho cobra command tree (cli/internal/cmd).
  Freshness is enforced by the cli-api-snapshot-freshness CI job.
-->

# CLI reference

`meho` is the operator CLI for the MEHO governance backplane. It dispatches through the same policy, audit, and approval path as the MCP tool surface — the CLI and the agent are dual front-ends on one backplane, not wrappers of each other.

This page is generated from the built-in command tree. Further operations are discovered from a connected backplane at runtime, so a logged-in `meho` may list more commands than appear here.

Every command accepts the global flags below.

## Global flags

- `--config` — path to the meho config file (default: `$XDG_CONFIG_HOME/meho/config.json`).
- `--verbose`, `-v` — enable verbose output.

## `meho admin`

Deployer-side install-time provisioning verbs

```
meho admin
```

### `meho admin keycloak`

Provision Keycloak realm resources for the MEHO auth onramp

```
meho admin keycloak
```

#### `meho admin keycloak bootstrap-clients`

Idempotently provision the public CLI + MCP clients in a Keycloak realm

```
meho admin keycloak bootstrap-clients [flags]
```

- `--admin-group-name` — top-level group the admin user joins (drives group-gated tools)
- `--admin-user-email` — optional email for the new admin user
- `--admin-user-username` — username of the admin user to provision (required unless --skip-user-provisioning)
- `--admin-username` — master-realm admin username (or set KEYCLOAK_ADMIN_USER)
- `--backplane-audience` — audience claim the `audience-meho-backplane` mapper emits (matches chart's `config.keycloakAudience`)
- `--cli-client-id` — public client_id for the device-code flow (matches chart's `config.keycloakCliClientId`)
- `--cli-offline-access` — opt the device-code CLI client into the long-lived offline-token path: assign `offline_access` as an optional client scope and bound its per-client offline-session idle timeout to the given number of seconds. Off by default. A bare `--cli-offline-access` uses 172800 seconds (48h); to pass a custom value use the equals form, e.g. `--cli-offline-access=86400`. Pairs with `meho login --offline`. Security: this enables a long-lived refresh token on the operator's disk — keep the bound tight and prefer the OS keyring for storage
- `--dry-run` — print what would be provisioned without making any API calls
- `--insecure-skip-tls-verify` — skip TLS verification when calling Keycloak (one-time bootstrap convenience; do not use in CI against untrusted Keycloaks)
- `--keycloak-base-url` — Keycloak base URL, e.g. https://keycloak.example.com
- `--mcp-client-id` — public client_id for the MCP browser-flow client
- `--mcp-redirect-uri` — redirect URI(s) for the MCP browser-flow client (default: loopback localhost + <ip>, any port/path)
- `--mcp-resource-uri` — audience the `meho-mcp-audience` mapper emits, e.g. https://meho.example.com/mcp (no trailing slash)
- `--mcp-web-origin` — CORS web origin(s) for the MCP browser-flow client (default: `+` — allow the redirect-URI origins)
- `--realm` — target realm name (NOT the master realm — that's where the admin token is minted)
- `--skip-user-provisioning` — skip the group + user creation steps (use when users are externally-managed via federation / SCIM)
- `--tenant-id` — hardcoded value for the `tenant_id` claim mapper (UUID; the lab convention is one tenant per realm)
- `--tenant-role` — hardcoded value for the `tenant_role` claim mapper (one of tenant_admin / operator / read_only)

## `meho agent`

Manage agent definitions (list / show / create / edit / delete)

```
meho agent
```

### `meho agent create`

Create one agent definition (tenant_admin)

```
meho agent create <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--disabled` — create the definition parked (enabled=false; default is enabled)
- `--identity-ref` — reference to the agent principal whose permissions bound the toolset
- `--json` — emit raw AgentDefinitionRead JSON instead of the human summary
- `--model-tier` — logical model tier: standard | fast | deep
- `--output-schema` — optional structured-output JSON Schema as a JSON object: inline JSON, @<path>, or @-
- `--system-prompt` — the agent's system prompt
- `--toolset` — allowed-tools spec as a JSON object: inline JSON, @<path>, or @- (default {})
- `--turn-budget` — max model turns the runtime allows (1..1000)

### `meho agent delete`

Delete one agent definition by name (tenant_admin)

```
meho agent delete <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--confirm` — skip the stdin confirmation prompt
- `--json` — emit a machine-readable success envelope instead of the human line

### `meho agent edit`

Apply a partial update to one agent definition (tenant_admin)

```
meho agent edit <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--disabled` — disable (park) the definition
- `--enabled` — enable the definition
- `--identity-ref` — new identity reference
- `--json` — emit raw AgentDefinitionRead JSON instead of the human summary
- `--model-tier` — new model tier: standard | fast | deep
- `--output-schema` — new output schema as a JSON object: inline JSON, @<path>, or @-
- `--system-prompt` — new system prompt
- `--toolset` — new toolset spec as a JSON object: inline JSON, @<path>, or @-
- `--turn-budget` — new turn budget (1..1000)

### `meho agent grant`

Manage agent permission grants (tenant_admin)

```
meho agent grant
```

#### `meho agent grant create`

Create a permission grant for an agent principal (tenant_admin)

```
meho agent grant create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--expires` — ISO 8601 UTC expiry for a time-bounded elevation, e.g. 2026-05-25T18:00:00Z
- `--json` — emit raw AgentGrantRead JSON
- `--op` — fnmatch op-pattern, e.g. '*' or 'vault.kv.*' (required)
- `--principal` — JWT sub of the agent principal (required)
- `--target` — target UUID or '*' for any target (default: any)
- `--verdict` — auto-execute | needs-approval | deny (required)

#### `meho agent grant elevate`

Create a time-bounded elevation grant (tenant_admin)

```
meho agent grant elevate [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--expires` — required ISO 8601 UTC expiry, e.g. 2026-06-01T00:00:00Z
- `--json` — emit raw AgentGrantRead JSON
- `--op` — fnmatch op-pattern (required)
- `--principal` — JWT sub of the agent principal (required)
- `--target` — target UUID or '*' for any target (optional)
- `--verdict` — auto-execute | needs-approval | deny (required)

#### `meho agent grant list`

List agent permission grants in your tenant (tenant_admin)

```
meho agent grant list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--include-expired` — include expired elevations (default: active only)
- `--json` — emit raw AgentGrantListResponse JSON
- `--limit` — max grants per page (1..500, server default 100)
- `--offset` — page offset (default 0)
- `--principal` — filter by agent principal JWT sub

#### `meho agent grant revoke`

Revoke a permission grant by id (tenant_admin)

```
meho agent grant revoke <grant-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--confirm` — skip the stdin confirmation prompt
- `--json` — emit a machine-readable result JSON

#### `meho agent grant show`

Fetch one permission grant by id (tenant_admin)

```
meho agent grant show <grant-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--json` — emit raw AgentGrantRead JSON

### `meho agent list`

List agent definitions in your tenant

```
meho agent list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AgentDefinitionListResponse JSON instead of the human table
- `--limit` — max definitions per page (1..500, server default 100 when omitted)
- `--offset` — offset into the name-sorted result set (default 0)

### `meho agent run`

Run an agent (sync block-and-return, or --async for a handle)

```
meho agent run <name> [flags]
```

- `--async` — return a run handle immediately instead of blocking for the result
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--input` — the user prompt to run the agent on (required)
- `--json` — emit the raw run response JSON instead of the human summary
- `--work-ref` — external change-ticket reference to bind the run to; filterable via `meho agent run-list --work-ref`

### `meho agent run-cancel`

Cancel a non-terminal agent run by handle

```
meho agent run-cancel <handle> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw AgentRunSummaryResponse JSON instead of the human summary

### `meho agent run-events`

Stream a fresh agent run's events over SSE

```
meho agent run-events <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--input` — the user prompt to run the agent on (required)
- `--json` — emit one raw JSON object per event instead of a compact human line

### `meho agent run-list`

List agent runs (filter by --work-ref / --status)

```
meho agent run-list [flags]
```

- `--agent-name` — filter by agent definition name (exact match); an unknown name returns an empty list
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw []AgentRunSummaryResponse JSON instead of the table
- `--limit` — max runs per page (1..500, server default 100 when omitted)
- `--offset` — rows to skip for paging into the result set (default 0)
- `--status` — filter by lifecycle status: pending, running, awaiting_approval, succeeded, failed, or cancelled
- `--work-ref` — filter by external change-ticket reference

### `meho agent run-status`

Poll an agent run's status by handle

```
meho agent run-status <handle> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw AgentRunStatusResponse JSON instead of the human summary

### `meho agent show`

Fetch one agent definition by name

```
meho agent show <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AgentDefinitionRead JSON instead of the human summary

## `meho agent-principal`

Manage agent principals (register / list / revoke)

```
meho agent-principal
```

### `meho agent-principal list`

List agent principals in your tenant

```
meho agent-principal list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--include-revoked` — include revoked principals in the listing (default false)
- `--json` — emit raw ListResponse JSON instead of the human table

### `meho agent-principal register`

Register a new agent principal (tenant_admin)

```
meho agent-principal register <name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AgentPrincipalRead JSON instead of the human summary
- `--owner-sub` — OIDC sub of the kill-switch owner (defaults to the caller's sub)

### `meho agent-principal revoke`

Revoke an agent principal — kill switch (tenant_admin)

```
meho agent-principal revoke <name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AgentPrincipalRead JSON instead of the human summary

## `meho approvals`

Manage approval requests (list / show / approve / reject)

```
meho approvals
```

### `meho approvals approve`

Approve a pending approval request

```
meho approvals approve <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--json` — emit raw JSON response instead of summary
- `--reason` — optional rationale for the approval

### `meho approvals list`

List approval requests in your tenant

```
meho approvals list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--json` — emit raw JSON array instead of the human table
- `--limit` — max requests per page (1..500, server default 50 when omitted)
- `--offset` — offset into the result set (default 0)
- `--status` — filter by status: pending, approved, rejected, expired (default: all)
- `--work-ref` — filter by external change-ticket reference, exact match

### `meho approvals reject`

Reject a pending approval request

```
meho approvals reject <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--json` — emit raw JSON response instead of summary
- `--reason` — optional rationale for the rejection

### `meho approvals show`

Inspect a pending approval request

```
meho approvals show <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from `meho login`)
- `--json` — emit raw JSON instead of the human-readable view

## `meho argocd`

Pre-scoped CLI verbs for the argocd-api-3.x connector

```
meho argocd
```

### `meho argocd app`

ArgoCD Application sub-verbs (list, get, diff, resource-tree; sync, rollback, set, refresh, delete)

```
meho argocd app
```

#### `meho argocd app delete`

Delete an ArgoCD Application with cascade (approval-gated)

```
meho argocd app delete [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--no-cascade` — leave managed cluster resources orphaned
- `--propagation-policy` — deletion propagation policy (foreground|background|orphan)
- `--target` — target slug to dispatch against (required)

#### `meho argocd app diff`

Show the desired-vs-live drift for an ArgoCD Application

```
meho argocd app diff [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--project` — optional AppProject to scope the lookup
- `--target` — target slug to dispatch against (required)

#### `meho argocd app get`

Read one ArgoCD Application's full spec and status by name

```
meho argocd app get [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--project` — optional AppProject to scope the lookup
- `--target` — target slug to dispatch against (required)

#### `meho argocd app list`

List ArgoCD Applications with their sync and health status

```
meho argocd app list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--project` — filter to one or more AppProjects (repeatable)
- `--selector` — Kubernetes label selector (e.g. team=payments,env=prod)
- `--target` — target slug to dispatch against (required)

#### `meho argocd app refresh`

Force an immediate reconcile of an ArgoCD Application (approval-gated)

```
meho argocd app refresh [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--no-hard` — do a normal refresh instead of a hard refresh
- `--target` — target slug to dispatch against (required)

#### `meho argocd app resource-tree`

Show an ArgoCD Application's reconciled resource tree

```
meho argocd app resource-tree [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--project` — optional AppProject to scope the lookup
- `--target` — target slug to dispatch against (required)

#### `meho argocd app rollback`

Roll an ArgoCD Application back to a prior deployed revision (approval-gated)

```
meho argocd app rollback [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--dry-run` — render + validate without applying
- `--id` — deployment history id to roll back to (required)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--poll-timeout` — seconds to poll operationState (default 300 backend-side)
- `--prune` — delete resources no longer defined at that revision
- `--target` — target slug to dispatch against (required)

#### `meho argocd app set`

Update an ArgoCD Application's spec / target revision (approval-gated)

```
meho argocd app set [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--no-validate` — skip server-side spec validation
- `--spec-file` — JSON file with the full ApplicationSpec (required)
- `--target` — target slug to dispatch against (required)

#### `meho argocd app sync`

Sync an ArgoCD Application and wait for a terminal phase (approval-gated)

```
meho argocd app sync [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--dry-run` — render + validate without applying
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — the Application's metadata.name (required)
- `--poll-timeout` — seconds to poll operationState (default 300 backend-side)
- `--prune` — delete resources no longer defined in Git
- `--revision` — Git revision to sync to (default: app target revision)
- `--target` — target slug to dispatch against (required)

### `meho argocd appproject`

ArgoCD AppProject sub-verbs (list; create, update)

```
meho argocd appproject
```

#### `meho argocd appproject create`

Create an ArgoCD AppProject (approval-gated)

```
meho argocd appproject create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--project-file` — JSON file with the AppProject object (required)
- `--target` — target slug to dispatch against (required)
- `--upsert` — update the project if it already exists

#### `meho argocd appproject list`

List ArgoCD AppProjects and their source/destination allow-lists

```
meho argocd appproject list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

#### `meho argocd appproject update`

Update an ArgoCD AppProject (approval-gated)

```
meho argocd appproject update [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--project-file` — JSON file with the AppProject object (required)
- `--target` — target slug to dispatch against (required)

### `meho argocd repo`

ArgoCD repository sub-verbs (list)

```
meho argocd repo
```

#### `meho argocd repo list`

List configured ArgoCD repositories and their connection state

```
meho argocd repo list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

## `meho audit`

Query the MEHO audit log (query / recent / show / who-touched / my-recent / replay / reflex)

```
meho audit
```

### `meho audit my-recent`

Show your own recent audit activity

```
meho audit my-recent [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw {items, next_cursor} envelope JSON instead of the human table
- `--limit` — max rows (1..1000, server default 100 when omitted)
- `--since` — earliest occurred_at; defaults server-side to 24h when omitted

### `meho audit query`

Query the audit log with arbitrary filter combinations

```
meho audit query [flags]
```

- `--audit-id` — exact audit-id lookup (UUID)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cursor` — opaque forward-pagination cursor from a prior page's NEXT line
- `--json` — emit raw AuditQueryResult JSON instead of the human table
- `--limit` — max rows per page (1..1000, server default 100 when omitted)
- `--op-class` — narrow to one op-class (read|write|credential_read|audit_query|other)
- `--op-id` — narrow to one op-id (glob with * wildcards)
- `--parent-audit-id` — narrow to the composite-op subtree under this audit-id
- `--principal` — narrow to one operator (JWT subject; partial-match supported)
- `--result-status` — narrow to one result-status (ok|error|denied)
- `--session-id` — narrow to one agent session (UUID); the flat companion to `meho audit replay`
- `--since` — earliest occurred_at; accepts 24h / 7d / 30m / 2w shorthand or ISO-8601
- `--target` — narrow to one target (name or alias; server-side resolution)
- `--until` — latest occurred_at; accepts the same shorthand as --since
- `--work-ref` — narrow to one external change-ticket reference — "show every write authorised by ticket X"

### `meho audit recent`

Show the most recent audit rows in the operator's tenant

```
meho audit recent [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw QueryResult JSON instead of the human table
- `--limit` — max rows (1..1000, server default 100 when omitted)

### `meho audit reflex`

Read reflex-adoption KPIs (read-before-act / announce coverage / write-back)

```
meho audit reflex [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw ReflexReport on stdout instead of the human table
- `--since` — window start; accepts relative (`7d`, `24h`) or ISO-8601 date (`2026-08-01`)
- `--tenant` — tenant UUID filter (platform_admin only; other tokens get a 403)
- `--until` — window end; same grammar as --since; defaults to now when omitted

### `meho audit replay`

Replay one agent session as a parent/child audit tree

```
meho audit replay <session-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AuditReplayResult JSON instead of the human ASCII tree
- `--max-depth` — fold tree nodes deeper than this level (rendering only; default 20)

### `meho audit show`

Fetch a single audit row by id

```
meho audit show <audit-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human summary

### `meho audit who-touched`

Show every audit row that touched a specific target

```
meho audit who-touched <target> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AuditQueryResult JSON instead of the human table
- `--limit` — max rows (1..1000, server default 100 when omitted)
- `--since` — earliest occurred_at; defaults server-side to 24h when omitted

## `meho automation`

Inspect the paired automation add-on surface

```
meho automation
```

### `meho automation list`

List the paired automation add-on surface

```
meho automation list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human table

## `meho bind9`

Pre-scoped CLI verbs for the bind9-ssh-9.x connector

```
meho bind9
```

### `meho bind9 about`

Show bind9 vendor / product / version / OS for a target

```
meho bind9 about [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — target slug to dispatch against (required for ops that read a target)

### `meho bind9 config`

bind9 config verbs (show / apply-views / apply-file / backup / reload)

```
meho bind9 config
```

#### `meho bind9 config apply-file`

Replace a bind9 config fragment from a local file (atomic)

```
meho bind9 config apply-file <name> <local-src> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho bind9 config apply-views`

Apply a views fragment + zonefile tree (atomic; rollback on failure)

```
meho bind9 config apply-views <local-views.conf> <zones-dir> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--primary-path` — which staged file's content the audit row captures (defaults to first key sorted)
- `--target` — target slug to dispatch against
- `--verify-fqdn` — sample FQDN to dig-verify post-reload (optional)

#### `meho bind9 config backup`

Snapshot /etc/bind/ into /var/backups/meho-bind9/<timestamp>-<tag>.tar.gz

```
meho bind9 config backup [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--tag` — friendly tag embedded in the backup filename ([A-Za-z0-9._-]{1,64})
- `--target` — target slug to dispatch against

#### `meho bind9 config reload`

rndc reload — re-read the active bind9 configuration

```
meho bind9 config reload [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho bind9 config show`

Read a bind9 config file from the target

```
meho bind9 config show <file> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho bind9 record`

bind9 record verbs (get / add / remove)

```
meho bind9 record
```

#### `meho bind9 record add`

Add an A/AAAA record (atomic-apply; rollback on failure)

```
meho bind9 record add <fqdn> <ip> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against
- `--type` — record type (A / AAAA). Omitted → handler default (A).
- `--view` — split-horizon view owning the zone. Required only when the zone is in multiple views.
- `--zone` — owning zone (e.g. example.com). Omitted → handler resolves via longest-suffix match.

#### `meho bind9 record get`

Resolve an FQDN through the local bind9 (dig @localhost)

```
meho bind9 record get <fqdn> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against
- `--type` — record type (A / AAAA / CNAME / MX / TXT). Omitted → handler default (A).

#### `meho bind9 record remove`

Remove the A/AAAA records at an FQDN (atomic-apply)

```
meho bind9 record remove <fqdn> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against
- `--view` — split-horizon view owning the zone. Required only when the zone is in multiple views.
- `--zone` — owning zone (e.g. example.com). Omitted → handler resolves via longest-suffix match.

### `meho bind9 zone`

bind9 zone verbs (list / read)

```
meho bind9 zone
```

#### `meho bind9 zone list`

List zones declared in the active bind9 configuration

```
meho bind9 zone list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho bind9 zone read`

Read the records of a zone (name / ttl / class / type / rdata rows)

```
meho bind9 zone read <zone> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

## `meho broadcast`

Manage broadcast-detail overrides (overrides list / set / remove)

```
meho broadcast
```

### `meho broadcast overrides`

List, create, and delete broadcast-detail override rules

```
meho broadcast overrides
```

#### `meho broadcast overrides list`

List broadcast-detail override rules for the operator's tenant

```
meho broadcast overrides list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw JSON array instead of the human table
- `--op-id-pattern` — exact-match filter on op_id_pattern (the rule's stored pattern, not a glob match)

#### `meho broadcast overrides remove`

Delete a broadcast-detail override rule by id

```
meho broadcast overrides remove <override-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit JSON error envelope on failure (success is still silent)

#### `meho broadcast overrides set`

Create a broadcast-detail override rule

```
meho broadcast overrides set [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--detail` — override detail (full | aggregate)
- `--json` — emit the created row as JSON instead of the human summary
- `--op-id-pattern` — op_id glob (e.g. "vault.kv.*" or "k8s.configmap.info"); regex chars are rejected
- `--scope-field` — scope field (one of: namespace, target_name); leave empty for an op-wide rule
- `--scope-value` — scope value (e.g. "kube-system"); required when --scope-field is set

## `meho connector`

spec-ingestion + review workflow (ingest / list / catalog / review / edit / enable / enable-reads / disable)

```
meho connector
```

### `meho connector catalog`

Curated connector-spec catalog (the raw-REST ingest on-ramp)

```
meho connector catalog
```

#### `meho connector catalog list`

List curated connector-spec catalog entries

```
meho connector catalog list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human table

### `meho connector disable`

Flip an enabled connector back to disabled (rollback; per-op overrides preserved)

```
meho connector disable <connector_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--confirm` — skip the interactive confirmation prompt
- `--json` — emit machine-readable JSON to stdout instead of the human summary

### `meho connector edit-group`

Patch a group's when_to_use hint or display name

```
meho connector edit-group <connector_id> <group_key> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human summary
- `--name` — replacement display name; supports `@<path>` to read from a file
- `--when-to-use` — replacement when_to_use text; supports `@<path>` to read from a file

### `meho connector edit-op`

Patch a per-op override (custom_description, safety, approval, enabled)

```
meho connector edit-op <connector_id> <op_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--custom-description` — replacement custom_description; supports `@<path>` to read from a file
- `--disable` — set is_enabled=false on this op
- `--enable` — set is_enabled=true on this op
- `--json` — emit machine-readable JSON to stdout instead of the human summary
- `--no-requires-approval` — clear the requires_approval flag
- `--requires-approval` — mark the op as requiring an approval workflow
- `--safety` — replacement safety_level: safe | caution | dangerous

### `meho connector enable`

Flip a staged or disabled connector to enabled (operations dispatchable)

```
meho connector enable <connector_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--confirm` — skip the interactive confirmation prompt
- `--json` — emit machine-readable JSON to stdout instead of the human summary

### `meho connector enable-reads`

Bulk-enable every read-class (GET/HEAD) op; writes stay default-deny

```
meho connector enable-reads <connector_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--confirm` — skip the interactive confirmation prompt
- `--json` — emit machine-readable JSON to stdout instead of the human summary

### `meho connector ingest`

Ingest one or more vendor specs into a new connector (staged state)

```
meho connector ingest [flags]
```

- `--auth-scheme` — manual mode: select a named auth scheme (closed catalog) so the connector is stamped DISPATCHABLE (a profiled connector, still staged behind review) instead of a non-dispatchable shim. One of: basic, static_header, session_login, session_login_basic, session_login_token, oauth2_mint. Selection only — no free-form auth config. Mutually exclusive with --catalog
- `--auth-secret-field` — manual mode: override a secret-field NAME the --auth-scheme reads at dispatch (never the value — that stays in the target's secret_ref); repeat for multiple. Omit for the per-scheme defaults. Requires --auth-scheme
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--catalog` — catalog mode: ingest the curated entry for <product>/<version> (e.g. vmware/9.0); mutually exclusive with --product/--version/--impl/--spec
- `--dry-run` — parse and plan without writing to the DB; the response carries an IngestionResult with counts but no GroupingResult
- `--impl` — impl identifier (e.g. vmware-rest, k8s-go); manual mode
- `--json` — emit machine-readable JSON to stdout instead of the human summary
- `--no-wait` — on an async 202 answer, exit 0 with the job handle (job_id + poll URL) instead of polling the job to completion; no effect when the backplane answers synchronously (HTTP 200)
- `--product` — product name (e.g. vmware, kubernetes); manual mode (required with --version/--impl/--spec)
- `--spec` — spec URI; repeat for multi-spec merge under one connector_id; manual mode
- `--spec-info-versions-compatible` — manual mode: declare that the spec's info.version is compatible with --version even when they differ (e.g. a vendor /api/v2 surface self-versioning as info.version=v2 ingested under --version 9.0). Each entry is a glob (2.x, 9.0.x) or a PEP 440 specifier set (>=2,<3); repeatable or comma-separated. Without it, a spec/label major mismatch is rejected; mutually exclusive with --catalog (the catalog row carries its own band)
- `--tenant-id` — write scope for the ingested rows (works with both modes): omit for the built-in / global scope (tenant_id left unset — visible to every tenant); pass your own tenant UUID for a tenant-curated ingest (another tenant's UUID is rejected with HTTP 403)
- `--version` — product version (e.g. 9.0, 1.x); manual mode

### `meho connector ingest-status`

Poll or inspect an async ingest job by id (after `ingest --no-wait`)

```
meho connector ingest-status <job-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human render (the raw IngestJobStatusResponse for a snapshot, the assembled IngestResponse on success)
- `--wait` — poll the job (2s cadence) until it reaches a terminal status, then render the result; without it, read one snapshot and exit (a running job exits 0 with its current state)

### `meho connector list`

List ingested connectors filtered by review status

```
meho connector list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--status` — filter by review status: staged | enabled | disabled | all (default all)

### `meho connector review`

Show the per-group + per-op review payload for one connector

```
meho connector review <connector_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human render

## `meho conventions`

Manage tenant conventions (list / show / create / edit / delete / history)

```
meho conventions
```

### `meho conventions create`

Create one convention (tenant_admin)

```
meho conventions create [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--body` — convention body: inline text, @<path> to read a file, or @- to read from stdin
- `--json` — emit raw Convention JSON instead of the human summary
- `--kind` — convention kind: operational | workflow | reference
- `--priority` — ranking key (default 0; range -32768..32767; higher wins on over-budget drops)
- `--slug` — operator-visible identifier (lowercase ASCII, digits, hyphen; max 128 chars)
- `--title` — short display label

### `meho conventions delete`

Delete one convention by slug (tenant_admin)

```
meho conventions delete <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--confirm` — skip the stdin confirmation prompt
- `--json` — emit a machine-readable success envelope instead of the human line

### `meho conventions edit`

Edit one convention via flags or $EDITOR (tenant_admin)

```
meho conventions edit <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--body` — new body: inline text, @<path>, or @- (omit for $EDITOR mode)
- `--json` — emit raw Convention JSON instead of the human summary
- `--priority` — new ranking key (range -32768..32767)
- `--title` — new short display label

### `meho conventions history`

Show the edit history of one convention

```
meho conventions history <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw history rows as JSON instead of the unified-diff view
- `--limit` — cap the number of history rows rendered (default: all)

### `meho conventions list`

List tenant conventions, optionally filtered by kind

```
meho conventions list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw ConventionListResponse JSON instead of the human table
- `--kind` — narrow entries by kind: operational | workflow | reference

### `meho conventions show`

Fetch one convention by slug

```
meho conventions show <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw Convention JSON instead of the Markdown body

## `meho dashboard`

Manage deterministic-check dashboards (list / show / create / delete)

```
meho dashboard
```

### `meho dashboard create`

Create one dashboard (tenant_admin)

```
meho dashboard create [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--description` — optional free-form description
- `--investigator-prompt` — operator context appended to the investigator's briefing (max 4096 chars)
- `--json` — emit raw DashboardDetail JSON instead of the human summary
- `--name` — operator-facing dashboard name (unique per tenant)
- `--notify-email` — comma-separated recipient(s) for transition mail (unset = notifications off)
- `--notify-min-state` — notification floor: degraded or critical (server default: critical)
- `--sensor-id` — member sensor UUID (repeatable; empty set rolls up 'unknown')
- `--tenant` — target tenant UUID (platform_admin cross-tenant create)

### `meho dashboard delete`

Delete one dashboard by id (tenant_admin)

```
meho dashboard delete <dashboard_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a structured JSON result instead of plain text
- `--tenant` — target tenant UUID (platform_admin cross-tenant delete)

### `meho dashboard list`

List dashboards in your tenant

```
meho dashboard list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw ListResponse JSON instead of the human table
- `--limit` — max dashboards per page (1..500, server default 100 when omitted)
- `--offset` — offset into the result set (default 0)
- `--tenant` — target tenant UUID (platform_admin only; operator role is locked to its own tenant)

### `meho dashboard show`

Show one dashboard with its member breakdown

```
meho dashboard show <dashboard_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw DashboardDetail JSON instead of the human summary
- `--tenant` — target tenant UUID (platform_admin cross-tenant read)

## `meho docs`

Search the meho-docs vendor-document add-on

```
meho docs
```

### `meho docs collections`

List, create, delete, and probe / toggle doc collections

```
meho docs collections
```

#### `meho docs collections create`

Register a new doc collection (tenant_admin)

```
meho docs collections create <collection-key> [flags]
```

- `--backend-ref` — backend config as a JSON object (e.g. '{"endpoint":"https://corpus/v1/search"}')
- `--backend-type` — search-backend type to route to (e.g. corpus-http)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--description` — optional free-text description
- `--from-file` — read the full create body from a JSON file instead of the flags
- `--json` — emit the created collection as JSON instead of a confirmation line
- `--product` — product the corpus covers (repeatable, e.g. --product vsphere --product nsx)
- `--vendor` — vendor the corpus covers (e.g. 'VMware by Broadcom')
- `--when-to-use` — optional 'pick this collection when…' blurb surfaced to agents

#### `meho docs collections delete`

Deregister a disabled, tenant-owned doc collection (tenant_admin)

```
meho docs collections delete <collection-key> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a JSON status envelope

#### `meho docs collections disable`

Hide a collection from search service

```
meho docs collections disable <collection-key> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a JSON status envelope

#### `meho docs collections enable`

Return a disabled collection to search service

```
meho docs collections enable <collection-key> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a JSON status envelope

#### `meho docs collections list`

List the doc collections you are entitled to search

```
meho docs collections list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cursor` — keyset pagination cursor (the last collection key from the previous page)
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--limit` — max collections per page (1..500, server default 100 when omitted)
- `--vendor` — filter by vendor (exact match)

#### `meho docs collections probe`

Probe a collection's backend and refresh its cached liveness

```
meho docs collections probe <collection-key> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw BackendReadiness JSON

### `meho docs search`

Search vendor-document collection(s) (mandatory --collection)

```
meho docs search <query> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--collection` — collection key to search (required; e.g. vmware). Repeat for a cross-collection fan-out, or pass 'all' to fan out across every entitled collection.
- `--json` — emit raw SearchDocsResponse JSON (full DocsChunk shape)
- `--limit` — max chunks to return (1..50, server default 10 when omitted)
- `--product` — optional vendor-product refinement within a single collection
- `--version` — optional product-version refinement within a single collection

## `meho event-source`

Operate the MEHO event_source registry (add / list / describe / update / delete)

```
meho event-source
```

### `meho event-source add`

Register a single event source (alias `create`)

```
meho event-source add <slug> [flags]
```

- `--auth-strategy` — auth strategy: hmac-sha256 | static-header | basic (required)
- `--backplane` — backplane URL (defaults to the URL recorded by `meho login`)
- `--extras` — per-source tuning as a JSON object (body cap, rate limit, replay window, dedupe)
- `--json` — emit the created event source as JSON
- `--kind` — producer kind: alertmanager | grafana | vcf-operations | harbor | generic-json (required)
- `--name` — human-readable name, unique within the tenant (required)
- `--secret-stdin` — read the auth secret from stdin (else MEHO_EVENT_SOURCE_SECRET); never a flag value
- `--status` — initial status: active | paused

### `meho event-source delete`

Soft-delete one event source by slug (tenant_admin)

```
meho event-source delete <slug> [flags]
```

- `--backplane` — backplane URL (defaults to `meho login`'s)
- `--confirm` — skip the stdin confirmation prompt
- `--json` — emit a machine-readable envelope instead of the human line

### `meho event-source describe`

Describe a single event source by slug

```
meho event-source describe <slug> [flags]
```

- `--backplane` — backplane URL (defaults to `meho login`'s)
- `--json` — emit machine-readable JSON instead of the summary

### `meho event-source list`

List event sources in your tenant

```
meho event-source list [flags]
```

- `--backplane` — backplane URL (defaults to `meho login`'s)
- `--cursor` — keyset cursor (the last name from the previous page)
- `--json` — emit machine-readable JSON instead of the table
- `--limit` — max sources per page (1..500, server default 100)
- `--status` — filter by status: active | paused

### `meho event-source update`

Apply a partial update to one event source (tenant_admin)

```
meho event-source update <slug> [flags]
```

- `--auth-strategy` — new auth strategy
- `--backplane` — backplane URL (defaults to `meho login`'s)
- `--extras` — replace per-source tuning with this JSON object
- `--json` — emit the updated event source as JSON
- `--kind` — new producer kind
- `--secret-stdin` — rotate the auth secret, reading it from stdin (else MEHO_EVENT_SOURCE_SECRET)
- `--status` — new status: active | paused

## `meho forget`

Delete one memory by natural key (DELETE /api/v1/memory)

```
meho forget <scope>/<slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by `meho login`)
- `--confirm` — skip the stdin confirmation prompt
- `--json` — emit a machine-readable success envelope instead of the human line
- `--target` — target name (required when --scope=target or user-target)

## `meho gcloud`

Pre-scoped CLI verbs for the gcloud-rest-1.0 connector

```
meho gcloud
```

### `meho gcloud about`

Show GCP project identity (project_id, lifecycle_state, organization)

```
meho gcloud about [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — target slug to dispatch against (required for ops that read a target)

### `meho gcloud compute`

GCP Compute Engine verbs (instances, networks, subnets)

```
meho gcloud compute
```

#### `meho gcloud compute instances`

Compute Engine instance verbs (list)

```
meho gcloud compute instances
```

##### `meho gcloud compute instances list`

List Compute Engine instances (all zones or a specific zone)

```
meho gcloud compute instances list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against
- `--zone` — optional zone filter (e.g. europe-west3-a); omit to list all zones via aggregatedList

#### `meho gcloud compute networks`

VPC network verbs (list)

```
meho gcloud compute networks
```

##### `meho gcloud compute networks list`

List VPC networks in the project

```
meho gcloud compute networks list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho gcloud compute subnets`

VPC subnet verbs (list)

```
meho gcloud compute subnets
```

##### `meho gcloud compute subnets list`

List VPC subnets (all regions or a specific region)

```
meho gcloud compute subnets list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--region` — optional region filter (e.g. europe-west3); omit to list all regions via aggregatedList
- `--target` — target slug to dispatch against

### `meho gcloud iam`

GCP IAM verbs (service-account list, policy read)

```
meho gcloud iam
```

#### `meho gcloud iam policy`

GCP IAM policy verbs (read)

```
meho gcloud iam policy
```

##### `meho gcloud iam policy read`

Read the project-level IAM policy (all role bindings)

```
meho gcloud iam policy read [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho gcloud iam sa`

GCP service-account verbs (list)

```
meho gcloud iam sa
```

##### `meho gcloud iam sa list`

List IAM service accounts in the project

```
meho gcloud iam sa list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho gcloud project`

GCP project verbs (describe)

```
meho gcloud project
```

#### `meho gcloud project describe`

Return the full Cloud Resource Manager project resource

```
meho gcloud project describe [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho gcloud services`

GCP services (APIs) verbs (list)

```
meho gcloud services
```

#### `meho gcloud services list`

List GCP services (APIs) enabled on the project

```
meho gcloud services list [flags]
```

- `--all` — include disabled services in addition to enabled ones
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

## `meho harbor`

Pre-scoped CLI verbs for the harbor-rest-2.x connector

```
meho harbor
```

### `meho harbor about`

Show Harbor version, auth mode, and registry URL

```
meho harbor about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

### `meho harbor artifact`

List or inspect Harbor artifacts within a repository

```
meho harbor artifact
```

#### `meho harbor artifact info`

Show full metadata for a Harbor artifact by tag or digest

```
meho harbor artifact info <project_name> <repository_name> <reference> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

#### `meho harbor artifact list`

List artifacts (tags + digests) in a Harbor repository

```
meho harbor artifact list <project_name> <repository_name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

### `meho harbor health`

Show Harbor composite health across all subsystems

```
meho harbor health [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

### `meho harbor operation`

Pre-scoped meta-tool wrappers (search / call) for harbor-rest-2.x

```
meho harbor operation
```

#### `meho harbor operation call`

Dispatch any harbor-rest-2.x op_id (escape hatch for ops without aliases)

```
meho harbor operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — Harbor target slug

#### `meho harbor operation search`

Hybrid BM25 + cosine RRF search across harbor-rest-2.x operations

```
meho harbor operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho harbor project`

List or inspect Harbor projects

```
meho harbor project
```

#### `meho harbor project info`

Show full details for a Harbor project

```
meho harbor project info <project_name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

#### `meho harbor project list`

List all Harbor projects

```
meho harbor project list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

### `meho harbor repository`

List or inspect Harbor repositories within a project

```
meho harbor repository
```

#### `meho harbor repository info`

Show full details for a Harbor repository

```
meho harbor repository info <project_name> <repository_name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

#### `meho harbor repository list`

List repositories within a Harbor project

```
meho harbor repository list <project_name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

### `meho harbor robot`

List, create, or delete Harbor robot accounts

```
meho harbor robot
```

#### `meho harbor robot create`

Create a project-scoped robot account in Harbor

```
meho harbor robot create [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--duration` — validity in days (-1 = never expires)
- `--json` — emit the full OperationResult envelope as JSON
- `--name` — robot account name (alphanumeric, hyphens, underscores)
- `--project` — Harbor project to scope the robot to
- `--target` — Harbor target slug

#### `meho harbor robot delete`

Delete a project-scoped robot account from Harbor

```
meho harbor robot delete [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--id` — numeric robot account ID
- `--json` — emit the full OperationResult envelope as JSON
- `--project` — Harbor project that scopes the robot account
- `--target` — Harbor target slug

#### `meho harbor robot list`

List Harbor system-level robot accounts

```
meho harbor robot list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Harbor target slug

## `meho hetzner-robot`

Pre-scoped CLI verbs for the hetzner-rest-2026.04 connector

```
meho hetzner-robot
```

### `meho hetzner-robot failover`

List failover IPs in the Hetzner Robot account

```
meho hetzner-robot failover
```

#### `meho hetzner-robot failover list`

List all failover IPs and their active routing targets

```
meho hetzner-robot failover list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot firewall`

Read the packet-filter firewall of a dedicated server

```
meho hetzner-robot firewall
```

#### `meho hetzner-robot firewall get`

Show the packet-filter firewall for one dedicated server by its primary IP

```
meho hetzner-robot firewall get <server-ip> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot ip`

List IP addresses assigned to the Hetzner Robot account

```
meho hetzner-robot ip
```

#### `meho hetzner-robot ip list`

List all IPs assigned to the Hetzner Robot account

```
meho hetzner-robot ip list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot operation`

Pre-scoped meta-tool wrappers (search / call) for hetzner-rest-2026.04

```
meho hetzner-robot operation
```

#### `meho hetzner-robot operation call`

Dispatch any hetzner-rest-2026.04 op_id (escape hatch for ops without aliases)

```
meho hetzner-robot operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — Hetzner Robot target slug

#### `meho hetzner-robot operation search`

Hybrid BM25 + cosine RRF search across hetzner-rest-2026.04 operations

```
meho hetzner-robot operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho hetzner-robot rdns`

List reverse DNS (PTR record) entries for the Hetzner Robot account

```
meho hetzner-robot rdns
```

#### `meho hetzner-robot rdns list`

List all reverse DNS (PTR) entries set on the account's IPs

```
meho hetzner-robot rdns list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot server`

List or inspect dedicated servers in the Hetzner Robot account

```
meho hetzner-robot server
```

#### `meho hetzner-robot server info`

Show full detail for one dedicated server by its primary IP

```
meho hetzner-robot server info <server-ip> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

#### `meho hetzner-robot server list`

List all dedicated servers in the Hetzner Robot account

```
meho hetzner-robot server list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot ssh-key`

List SSH public keys registered in the Hetzner Robot portal

```
meho hetzner-robot ssh-key
```

#### `meho hetzner-robot ssh-key list`

List all SSH public keys registered in the Hetzner Robot portal

```
meho hetzner-robot ssh-key list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot subnet`

List subnets assigned to the Hetzner Robot account

```
meho hetzner-robot subnet
```

#### `meho hetzner-robot subnet list`

List all subnets assigned to the Hetzner Robot account

```
meho hetzner-robot subnet list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

### `meho hetzner-robot vswitch`

List or inspect vSwitches in the Hetzner Robot account

```
meho hetzner-robot vswitch
```

#### `meho hetzner-robot vswitch info`

Show full detail for one vSwitch by its numeric ID

```
meho hetzner-robot vswitch info <vswitch-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

#### `meho hetzner-robot vswitch list`

List all vSwitches in the Hetzner Robot account

```
meho hetzner-robot vswitch list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — Hetzner Robot target slug

## `meho holodeck`

Pre-scoped CLI verbs for the holodeck-ssh-9.0 connector

```
meho holodeck
```

### `meho holodeck about`

Show Holodeck product / version / Photon OS for a target

```
meho holodeck about [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — target slug to dispatch against (required)

### `meho holodeck config`

Holodeck appliance config sub-verbs (show)

```
meho holodeck config
```

#### `meho holodeck config show`

Return the full Holodeck appliance configuration dict

```
meho holodeck config show [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho holodeck k8s`

In-appliance K8s sub-verbs (exec — read-only)

```
meho holodeck k8s
```

#### `meho holodeck k8s exec`

Run a read-only kubectl command on the in-appliance K8s cluster

```
meho holodeck k8s exec <kubectl-command> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho holodeck logs`

Holodeck runtime log sub-verbs (tail)

```
meho holodeck logs
```

#### `meho holodeck logs tail`

Tail Holodeck runtime log files for a given component

```
meho holodeck logs tail <component> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--lines` — number of trailing lines to return per file (backend clamps to [1, 5000])
- `--target` — target slug to dispatch against (required)

### `meho holodeck networking`

Holodeck networking sub-verbs (show)

```
meho holodeck networking
```

#### `meho holodeck networking show`

Composite FRR/BGP + DNS + DHCP snapshot for the appliance

```
meho holodeck networking show [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho holodeck pod`

Holodeck nested-pod sub-verbs (list, info)

```
meho holodeck pod
```

#### `meho holodeck pod info`

Return per-pod detail (state, networking, VMs) for a Holodeck pod

```
meho holodeck pod info <pod-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

#### `meho holodeck pod list`

List active Holodeck nested pods (Get-HoloDeckPod)

```
meho holodeck pod list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho holodeck service`

Holodeck Photon service sub-verbs (list)

```
meho holodeck service
```

#### `meho holodeck service list`

List Holodeck Photon services and their status

```
meho holodeck service list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

## `meho k8s`

Pre-scoped CLI verbs for the k8s-1.x connector

```
meho k8s
```

### `meho k8s about`

Identify the cluster (product / version / platform)

```
meho k8s about [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s configmap`

ConfigMap verbs (list keys-only / info full data)

```
meho k8s configmap
```

#### `meho k8s configmap info`

Fetch one ConfigMap including all key=value data

```
meho k8s configmap info <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace the configmap lives in (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

#### `meho k8s configmap list`

List ConfigMaps in a namespace - KEY NAMES ONLY, no values

```
meho k8s configmap list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace to list within (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s deployment`

Deployment verbs (list / info)

```
meho k8s deployment
```

#### `meho k8s deployment info`

Full detail for one deployment (kubectl describe deployment)

```
meho k8s deployment info <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace the deployment lives in (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

#### `meho k8s deployment list`

List deployments (kubectl get deployments)

```
meho k8s deployment list [flags]
```

- `--all-namespaces` — list across every namespace (mutually exclusive with --namespace)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--continue-token` — pagination cursor from a prior response's next_continue field
- `--field-selector` — k8s field selector forwarded server-side (e.g. status.phase=Running)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--label-selector` — k8s label selector forwarded server-side (e.g. app=argocd-server)
- `--limit` — server-side ?limit= for paginated reads (1..1000)
- `--namespace` — namespace to list within (mutually exclusive with --all-namespaces)
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s event`

Event verbs (list)

```
meho k8s event
```

#### `meho k8s event list`

List recent events in a namespace (kubectl get events)

```
meho k8s event list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--field-selector` — k8s field selector forwarded server-side (e.g. type=Warning)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--limit` — maximum rows to return (server default 100, capped at 500)
- `--namespace` — namespace to list within (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s ingress`

Ingress verbs (list)

```
meho k8s ingress
```

#### `meho k8s ingress list`

List Ingresses in a namespace (kubectl get ingress)

```
meho k8s ingress list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace to list within (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s logs`

Fetch a chunk of pod logs (kubectl logs - non-streaming)

```
meho k8s logs <pod> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--container` — container name within the pod (required for multi-container pods)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace the pod lives in (required)
- `--previous` — fetch logs from the previous container instance (after a restart)
- `--since` — duration string for time-bounded fetch (e.g. 5m, 1h, 24h, 7d)
- `--tail` — lines from the end of the log (default 100, capped at 5000)
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s ls`

Inventory walker (cluster root / namespace summary / kind list)

```
meho k8s ls [path] [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s namespace`

Namespace verbs (list)

```
meho k8s namespace
```

#### `meho k8s namespace list`

List Kubernetes namespaces (name / status / age / labels)

```
meho k8s namespace list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s node`

Node verbs (list)

```
meho k8s node
```

#### `meho k8s node list`

List cluster nodes (status / roles / version / taints)

```
meho k8s node list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s pod`

Pod verbs (list / info)

```
meho k8s pod
```

#### `meho k8s pod info`

Full detail for one pod (kubectl describe pod)

```
meho k8s pod info <name> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace the pod lives in (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

#### `meho k8s pod list`

List pods (kubectl get pods)

```
meho k8s pod list [flags]
```

- `--all-namespaces` — list across every namespace (mutually exclusive with --namespace)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--continue-token` — pagination cursor from a prior response's next_continue field
- `--field-selector` — k8s field selector forwarded server-side (e.g. status.phase=Running)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--label-selector` — k8s label selector forwarded server-side (e.g. app=argocd-server)
- `--limit` — server-side ?limit= for paginated reads (1..1000)
- `--namespace` — namespace to list within (mutually exclusive with --all-namespaces)
- `--target` — K8s target slug to dispatch against (resolved server-side)

### `meho k8s service`

Service verbs (list)

```
meho k8s service
```

#### `meho k8s service list`

List Services in a namespace (kubectl get svc)

```
meho k8s service list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--namespace` — namespace to list within (required)
- `--target` — K8s target slug to dispatch against (resolved server-side)

## `meho kb`

Operate the MEHO knowledge base (ingest / search / list / show / add / delete)

```
meho kb
```

### `meho kb add`

Create or re-index one kb entry (tenant_admin)

```
meho kb add <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--body` — entry body: inline text, @<path> to read a file, or @- to read from stdin
- `--json` — emit raw KbEntry JSON instead of the human summary
- `--metadata` — comma-separated key=value pairs to attach as entry metadata (e.g. owner=ops,source=runbook)

### `meho kb delete`

Delete one kb entry by slug (tenant_admin)

```
meho kb delete <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--confirm` — skip the stdin confirmation prompt
- `--json` — emit a machine-readable success envelope instead of the human line

### `meho kb ingest`

Bulk-ingest a kb/ directory on the backplane host (tenant_admin)

```
meho kb ingest <directory> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--dry-run` — resolve the plan without writing to the substrate (counters reflect intent only)
- `--json` — emit raw KbIngestionResult JSON instead of the human summary

### `meho kb list`

List kb entries in your tenant

```
meho kb list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--filter` — narrow entries by a SQL LIKE pattern (e.g. `vcenter%`)
- `--json` — emit raw KbListResponse JSON instead of the human table
- `--limit` — max entries per page (1..500, server default 100 when omitted)
- `--offset` — offset into the slug-sorted result set (default 0)

### `meho kb search`

Search kb entries via hybrid BM25 + cosine retrieval

```
meho kb search <query> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw RetrieveResponse JSON (full RetrievalHit shape)
- `--limit` — max hits to return (1..50, server default 10 when omitted)

### `meho kb show`

Fetch a kb entry by slug

```
meho kb show <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw KbEntry JSON instead of the Markdown body

## `meho keycloak`

Pre-scoped CLI verbs for the keycloak-admin-26.x connector

```
meho keycloak
```

### `meho keycloak client`

Keycloak client sub-verbs (list, get, create, update)

```
meho keycloak client
```

#### `meho keycloak client create`

Create a Keycloak client (approval-gated)

```
meho keycloak client create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--representation-file`, `-f` — path to a JSON file with the ClientRepresentation body (required)
- `--target` — target slug to dispatch against (required)

#### `meho keycloak client get`

Read one Keycloak client's full config by internal UUID (secret redacted)

```
meho keycloak client get [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--id` — the client's internal UUID (from `meho keycloak client list`) (required)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

#### `meho keycloak client list`

List Keycloak clients in the managed realm (secrets redacted)

```
meho keycloak client list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--client-id` — filter to a single client by its human clientId (Keycloak ?clientId=)
- `--json` — emit the full OperationResult envelope as JSON
- `--max` — cap on the number of clients returned (0 = no cap)
- `--target` — target slug to dispatch against (required)

#### `meho keycloak client update`

Update a Keycloak client by UUID or clientId (approval-gated)

```
meho keycloak client update [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--client-id` — the human clientId (resolved to UUID when --id is absent)
- `--id` — the client's internal UUID (skips name→UUID resolution)
- `--json` — emit the full OperationResult envelope as JSON
- `--representation-file`, `-f` — path to a JSON file with the partial ClientRepresentation body (required)
- `--target` — target slug to dispatch against (required)

### `meho keycloak client-scope`

Keycloak client-scope sub-verbs (list, create)

```
meho keycloak client-scope
```

#### `meho keycloak client-scope create`

Create a Keycloak client scope (approval-gated)

```
meho keycloak client-scope create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--representation-file`, `-f` — path to a JSON file with the ClientScopeRepresentation body (required)
- `--target` — target slug to dispatch against (required)

#### `meho keycloak client-scope list`

List Keycloak client scopes in the managed realm

```
meho keycloak client-scope list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho keycloak protocol-mapper`

Keycloak protocol-mapper sub-verbs (create)

```
meho keycloak protocol-mapper
```

#### `meho keycloak protocol-mapper create`

Add a protocol mapper to a Keycloak client (approval-gated)

```
meho keycloak protocol-mapper create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--client-id` — the human clientId (resolved to UUID when --id is absent)
- `--id` — the client's internal UUID (skips name→UUID resolution)
- `--json` — emit the full OperationResult envelope as JSON
- `--representation-file`, `-f` — path to a JSON file with the ProtocolMapperRepresentation body (required)
- `--target` — target slug to dispatch against (required)

### `meho keycloak realm`

Keycloak realm sub-verbs (get, create, update)

```
meho keycloak realm
```

#### `meho keycloak realm create`

Create a Keycloak realm (approval-gated)

```
meho keycloak realm create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--representation-file`, `-f` — path to a JSON file with the RealmRepresentation body (required)
- `--target` — target slug to dispatch against (required)

#### `meho keycloak realm get`

Read the managed realm's top-level configuration

```
meho keycloak realm get [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

#### `meho keycloak realm update`

Update a Keycloak realm's top-level config (approval-gated)

```
meho keycloak realm update [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--realm` — realm to update (defaults to the target's managed realm)
- `--representation-file`, `-f` — path to a JSON file with the partial RealmRepresentation body (required)
- `--target` — target slug to dispatch against (required)

### `meho keycloak role-mapping`

Keycloak role-mapping sub-verbs (get, assign)

```
meho keycloak role-mapping
```

#### `meho keycloak role-mapping assign`

Grant realm roles to a Keycloak user (approval-gated, privilege grant)

```
meho keycloak role-mapping assign [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--id` — the user's internal UUID (skips name→UUID resolution)
- `--json` — emit the full OperationResult envelope as JSON
- `--role` — realm role name to grant (repeatable)
- `--target` — target slug to dispatch against (required)
- `--username` — the username (resolved to UUID when --id is absent)

#### `meho keycloak role-mapping get`

Read a Keycloak user's realm + client role mappings by UUID

```
meho keycloak role-mapping get [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--id` — the user's internal UUID (from `meho keycloak user list`) (required)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho keycloak user`

Keycloak user sub-verbs (list, create, reset-password)

```
meho keycloak user
```

#### `meho keycloak user create`

Create a Keycloak user with a Vault-sourced password (approval-gated)

```
meho keycloak user create [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--password-secret-key` — field within the Vault secret payload (default 'password')
- `--password-secret-mount` — Vault KV-v2 mount point (default 'secret')
- `--password-secret-ref` — Vault KV-v2 path the password is read from (the password is never passed inline)
- `--representation-file`, `-f` — path to a JSON file with the UserRepresentation body (required)
- `--target` — target slug to dispatch against (required)
- `--temporary` — force a password change on first login

#### `meho keycloak user list`

List Keycloak users in the managed realm (credentials redacted)

```
meho keycloak user list [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--max` — cap on the number of users returned (0 = no cap)
- `--target` — target slug to dispatch against (required)
- `--username` — filter to matching users by username (Keycloak ?username=)

#### `meho keycloak user reset-password`

Reset a Keycloak user's password from Vault (approval-gated)

```
meho keycloak user reset-password [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--id` — the user's internal UUID (skips name→UUID resolution)
- `--json` — emit the full OperationResult envelope as JSON
- `--password-secret-key` — field within the Vault secret payload (default 'password')
- `--password-secret-mount` — Vault KV-v2 mount point (default 'secret')
- `--password-secret-ref` — Vault KV-v2 path the password is read from (the password is never passed inline)
- `--target` — target slug to dispatch against (required)
- `--temporary` — force a password change on first login
- `--username` — the username (resolved to UUID when --id is absent)

## `meho list`

List memories visible to the operator (GET /api/v1/memory)

```
meho list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by `meho login`)
- `--include-expired` — include memories past their expires_at (omitted: expired entries filtered out)
- `--json` — emit raw MemoryListResponse JSON instead of the human table
- `--limit` — max memories per page (1..500, server default 100 when omitted)
- `--scope` — filter by memory scope: user|user-tenant|user-target|tenant|target
- `--slug-pattern` — filter by substring match on slug (forwarded to MemoryService.list_memories)
- `--tag` — filter by tag (memories whose metadata.tags contains this string)

## `meho login`

Authenticate against the MEHO backplane via Keycloak device-code flow

```
meho login <backplane-url> [flags]
```

- `--client-id` — OAuth client_id to use for the device-code flow (auto-discovered when blank)
- `--insecure-allow-http` — permit a plaintext http:// backplane URL for a localhost backplane only (local-dev convenience; the bearer token is sent in the clear — never use against a remote host)
- `--issuer` — Keycloak realm issuer URL (auto-discovered from the backplane when blank)
- `--offline` — request a long-lived offline refresh token (adds the OIDC `offline_access` scope) so you stay logged in across SSO idle timeouts instead of re-running the device dance. Requires the backplane's meho-cli client to allow offline_access (provision via `meho admin keycloak bootstrap-clients --cli-offline-access`). The refresh token is stored like any credential (OS keyring first, 0600 file fallback) — treat it as a long-lived secret
- `--print-token` — after a successful login, print ONLY the access token to stdout (every other line — the device-code prompt, the success message, warnings — goes to stderr) so it can be captured with 'TOKEN=$(meho login --print-token <backplane-url>)'. WARNING: the value is a live bearer credential — never log it or paste it into shared channels
- `--resolve` — pin a host to an IP for the flow, mirroring `curl --resolve <host>:<port>:<ip>` (split-DNS escape hatch when the Keycloak host doesn't resolve). Repeat for multiple hosts. TLS SNI/Host use the real hostname, so certificate validation is unaffected
- `--scope` — OAuth scopes to request (default: openid). Repeat or comma-separate for multiple.

## `meho migrate`

Migrate laptop-local data to the MEHO backplane

```
meho migrate
```

### `meho migrate memory`

Migrate laptop-local memory entries to the backplane

```
meho migrate memory [flags]
```

- `--backplane` — backplane URL override (default: from meho login config)
- `--dry-run` — preview entries that would be migrated without submitting
- `--include-machine-local` — include machine-local entries (default: skip them)
- `--mark-migrated` — touch the migration-complete marker after successful submission
- `--non-interactive` — skip the interactive picker; migrates only user/feedback entries
- `--source` — path to the local memory directory to scan (default: XDG-resolved)

## `meho nsx`

Pre-scoped CLI verbs for the nsx-rest-4.2 connector

```
meho nsx
```

### `meho nsx about`

Show NSX Manager version, hostname, and node UUID

```
meho nsx about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

### `meho nsx cluster`

NSX management cluster verbs (status)

```
meho nsx cluster
```

#### `meho nsx cluster status`

Show NSX management cluster health

```
meho nsx cluster status [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

### `meho nsx firewall`

NSX distributed-firewall verbs (policy list / rule list)

```
meho nsx firewall
```

#### `meho nsx firewall policy`

NSX distributed-firewall policy verbs (list)

```
meho nsx firewall policy
```

##### `meho nsx firewall policy list`

List distributed-firewall security policies in a domain

```
meho nsx firewall policy list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--scope` — NSX policy domain-id (default "default")
- `--target` — NSX target slug

#### `meho nsx firewall rule`

NSX distributed-firewall rule verbs (list)

```
meho nsx firewall rule
```

##### `meho nsx firewall rule list`

List rules in a distributed-firewall security policy

```
meho nsx firewall rule list <policy-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--scope` — NSX policy domain-id (default "default")
- `--target` — NSX target slug

### `meho nsx node`

NSX transport-node verbs (list)

```
meho nsx node
```

#### `meho nsx node list`

List NSX transport nodes (ESXi + edge)

```
meho nsx node list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

### `meho nsx operation`

Pre-scoped meta-tool wrappers (search / call) for nsx-rest-4.2

```
meho nsx operation
```

#### `meho nsx operation call`

Dispatch any nsx-rest-4.2 op_id (escape hatch for ops without aliases)

```
meho nsx operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — NSX target slug

#### `meho nsx operation search`

Hybrid BM25 + cosine RRF search across nsx-rest-4.2 operations

```
meho nsx operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho nsx segment`

NSX segment verbs (list)

```
meho nsx segment
```

#### `meho nsx segment list`

List NSX policy-API segments (logical + DVS-backed portgroups)

```
meho nsx segment list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

### `meho nsx tier0`

NSX tier-0 gateway verbs (list)

```
meho nsx tier0
```

#### `meho nsx tier0 list`

List NSX tier-0 (provider edge) gateways

```
meho nsx tier0 list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

### `meho nsx tier1`

NSX tier-1 gateway verbs (list)

```
meho nsx tier1
```

#### `meho nsx tier1 list`

List NSX tier-1 (per-tenant) gateways

```
meho nsx tier1 list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

### `meho nsx transport-zone`

NSX transport-zone verbs (list)

```
meho nsx transport-zone
```

#### `meho nsx transport-zone list`

List NSX transport zones under the default enforcement point

```
meho nsx transport-zone list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — NSX target slug

## `meho operation`

operation meta-tool surface (groups / search / call)

```
meho operation
```

### `meho operation call`

Invoke an operation through the dispatcher

```
meho operation call <connector_id> <op_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--params` — operation params as inline JSON or @<file>; omitted means no params
- `--preview-hash` — preview_hash from a prior `meho operation preview` — required for a destructive-tier op
- `--target` — target slug to dispatch against (required for ops that read a target)

### `meho operation groups`

List enabled operation groups for a connector

```
meho operation groups <connector_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human table

### `meho operation preview`

Resolve an op to its would-be request + preview_hash, without sending

```
meho operation preview <connector_id> <op_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full preview envelope as JSON instead of the human render
- `--params` — operation params as inline JSON or @<file>; omitted means no params
- `--target` — target slug to resolve against (required for ops that read a target)

### `meho operation result-query`

Page or query rows back from a JSONFlux result handle

```
meho operation result-query <handle_id> [flags]
```

- `--aggregate` — query: aggregate "<FUNC> [field]" (repeatable; FUNC = COUNT SUM MIN MAX AVG)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--group-by` — query: GROUP BY key column (repeatable, max 4)
- `--json` — emit the full result-query envelope as JSON instead of the human render
- `--limit` — paging: page size; default 50, max 500 (matches the result_query MCP tool)
- `--offset` — paging: zero-based index of the first row to return (advance by --limit)
- `--order-by` — query: sort term "<field> [asc|desc]" (repeatable, max 4)
- `--query-limit` — query: max output rows (clamps to 500); the result flags truncation when more matched
- `--select` — query: projection column to return (repeatable; omit for all columns)
- `--where` — query: WHERE predicate "<field> <op> [value]" (repeatable; op = != < <= > >= IN 'IS NULL')

### `meho operation search`

Hybrid BM25 + cosine RRF search across enabled operations

```
meho operation search <connector_id> <query> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key within the connector
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--limit` — max hits to return (1..50, clamped by the API at 50)

## `meho pfsense`

Pre-scoped CLI verbs for the pfsense-ssh-2.7 connector

```
meho pfsense
```

### `meho pfsense about`

Show pfSense product / version / build for a target

```
meho pfsense about [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — target slug to dispatch against (required)

### `meho pfsense config`

pfSense config sub-verbs (show)

```
meho pfsense config
```

#### `meho pfsense config show`

Return the full pfSense configuration as XML (/cf/conf/config.xml)

```
meho pfsense config show [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho pfsense dhcp`

pfSense DHCP sub-verbs (leases)

```
meho pfsense dhcp
```

#### `meho pfsense dhcp leases`

List live pfSense DHCPv4 leases (ISC dhcpd lease DB)

```
meho pfsense dhcp leases [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho pfsense firewall`

pfSense firewall sub-verbs (rules, state)

```
meho pfsense firewall
```

#### `meho pfsense firewall rules`

List active pfSense firewall filter rules (pfctl -sr)

```
meho pfsense firewall rules [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

#### `meho pfsense firewall state`

List active pfSense connection-state table entries (pfctl -ss)

```
meho pfsense firewall state [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho pfsense nat`

pfSense NAT sub-verbs (rules)

```
meho pfsense nat
```

#### `meho pfsense nat rules`

List active pfSense NAT ruleset (pfctl -sn)

```
meho pfsense nat rules [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho pfsense network`

pfSense network sub-verbs (interface, gateway)

```
meho pfsense network
```

#### `meho pfsense network gateway`

List pfSense routing gateways (from config.xml)

```
meho pfsense network gateway [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

#### `meho pfsense network interface`

List pfSense network interfaces (ifconfig -a)

```
meho pfsense network interface [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against (required)

### `meho pfsense version`

Show pfSense version / build / kernel for a target

```
meho pfsense version [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — target slug to dispatch against (required)

## `meho promote`

Promote one memory to a strictly broader scope (POST /api/v1/memory/{scope}/{slug}/promote)

```
meho promote <scope>/<slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by `meho login`)
- `--json` — emit raw MemoryEntry JSON instead of the human summary
- `--move` — delete the source row in the same transaction (broadens-and-leaves vs. broadens-and-rewires)
- `--to` — target scope: user-tenant|user-target|tenant|target (required)

## `meho recall`

Fetch a memory by natural key or via retrieval (GET /api/v1/memory or /api/v1/retrieve)

```
meho recall <scope>/<slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by `meho login`)
- `--json` — emit raw MemoryEntry / RetrieveResponse JSON instead of human output
- `--limit` — max hits to return in --query mode (1..50, server default 10 when omitted)
- `--query` — run hybrid retrieval against memories with this query; mutually exclusive with the <scope>/<slug> positional
- `--scope` — narrow --query mode to one scope: user|user-tenant|user-target|tenant|target
- `--target` — target name for user-target / target scopes (positional mode only)

## `meho remember`

Persist one memory in the backplane (POST /api/v1/memory)

```
meho remember <body> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by `meho login`)
- `--json` — emit raw MemoryEntry JSON instead of the human summary
- `--persist` — persist forever — opt out of the backend's default-7-day TTL on memory-user writes (sends expires_at=null)
- `--scope` — memory scope: user|user-tenant|user-target|tenant|target
- `--slug` — override the auto-generated slug with an operator-supplied identifier
- `--tag` — tag to attach to the memory; repeat for multiple tags
- `--target` — target name (required when --scope=target or user-target)
- `--ttl` — time-to-live shorthand (e.g. `7d`, `36h`, `30m`) — set expires_at

## `meho retrieval`

Retrieval-quality + migration-decision tooling

```
meho retrieval
```

### `meho retrieval eval`

Run the checked-in eval corpus + report precision@5 / MRR / coverage

```
meho retrieval eval [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--baseline` — baseline kind to also run; only `grep` is supported in v0.2 (kb surface only)
- `--compare-baseline` — compare today's eval against this saved baseline; exit 1 on any per-metric regression
- `--json` — emit a machine-readable JSON envelope on stdout instead of the human table
- `--save-baseline` — write the eval result to this file for future regression comparison
- `--surface` — retrieval surface to evaluate (kb|memory|operations|all)

### `meho retrieval retire-checklist`

Run the 5-criterion retire-decision checklist per retrieval surface

```
meho retrieval retire-checklist [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--baseline-file` — JSON file containing per-surface baseline metrics (output of `meho retrieval eval --baseline grep --save-baseline ...`); without it, criterion 4 stays yellow
- `--gh-repo` — GitHub repo to query for `retrieval-migration-blocker` issues
- `--json` — emit the structured RetireChecklistReport on stdout instead of the human table
- `--no-blockers` — skip the gh lookup; the backplane reports criterion 5 as REVIEW MANUALLY
- `--surface` — retrieval surface to evaluate (kb|memory|operations|all)

### `meho retrieval usage`

Read audit-log-backed retrieval usage telemetry (daily buckets per surface)

```
meho retrieval usage [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the raw UsageReport on stdout instead of the human table
- `--since` — window start; accepts relative (`30d`, `7d`, `24h`) or ISO-8601 date (`2026-04-01`)
- `--surface` — retrieval surface to report (kb|memory|operations|all)
- `--tenant` — tenant UUID filter (tenant_admin only; operator-role tokens get a 403)

## `meho runbook`

Author and operate runbook templates and runs

```
meho runbook
```

### `meho runbook abort`

Abort an in-progress runbook run (assignee or tenant_admin)

```
meho runbook abort <run_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw AbortRunResponse JSON instead of the human confirmation
- `--reason` — non-empty reason persisted to audit_log (required; prompts if omitted on a TTY)

### `meho runbook deprecate-template`

Mark a published version as deprecated (tenant_admin)

```
meho runbook deprecate-template <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw DeprecateTemplateResponse JSON instead of the human confirmation
- `--version` — template version to deprecate (required; positive integer)

### `meho runbook draft-template`

Create the first draft of a new runbook template (tenant_admin)

```
meho runbook draft-template <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--from` — path to the YAML file describing the template body (required)
- `--json` — emit raw DraftTemplateResponse JSON instead of the human summary

### `meho runbook edit-template`

Edit a draft template — in-place or fork-on-publish (tenant_admin)

```
meho runbook edit-template <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--from` — path to the YAML file describing the template body (required)
- `--json` — emit raw EditTemplateResponse JSON instead of the human summary

### `meho runbook list-templates`

List runbook templates in your tenant

```
meho runbook list-templates [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw RunbookTemplateListResponse JSON instead of the human table
- `--limit` — max templates per page (1..500, server default 100 when omitted)
- `--status` — filter by lifecycle status: draft, published, or deprecated
- `--target-kind` — filter by target_kind (free-form connector kind like `vmware-rest`)

### `meho runbook next`

Advance an in-progress runbook run by one step

```
meho runbook next <run_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw NextStepResponse JSON instead of the human block
- `--verify-response` — answer for a confirm-typed verify: yes|no|escalate (omit to prompt interactively)

### `meho runbook publish-template`

Flip a draft template to published (tenant_admin)

```
meho runbook publish-template <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw PublishTemplateResponse JSON instead of the human confirmation
- `--version` — template version to publish (required; the value returned by draft-template / edit-template)

### `meho runbook reassign`

Transfer ownership of an in-progress run (tenant_admin)

```
meho runbook reassign <run_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw ReassignRunResponse JSON instead of the human confirmation
- `--to` — required: operator subject identifier (`sub`) to transfer ownership to

### `meho runbook runs`

List runbook runs in your tenant

```
meho runbook runs [flags]
```

- `--assignee` — filter by assignee subject (tenant_admin only; operators see own regardless)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw RunbookListRunsResponse JSON instead of the human table
- `--limit` — max runs per page (1..500, server default 100 when omitted)
- `--status` — filter by run state: in_progress, completed, or abandoned
- `--template-slug` — filter by template slug
- `--work-ref` — filter by external change-ticket reference

### `meho runbook show-template`

Read the full body of a runbook template, including step contents

```
meho runbook show-template <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw ShowTemplateResponse JSON instead of the human-readable block
- `--version` — pin to a specific template version (default: latest non-deprecated)

### `meho runbook start`

Start a new runbook run (operator)

```
meho runbook start <slug> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw CurrentStepResponse JSON instead of the human block
- `--param` — k=v substitution context entry for ${run.params.k}; repeat for multiple params
- `--target` — required: run subject (host, cluster, cert thumbprint) -- substituted as ${run.target}
- `--work-ref` — optional external change-ticket reference the run executes under; inherited by every operation_call step's audit row

## `meho runner-principal`

Manage runner principals (register / list / show / revoke)

```
meho runner-principal
```

### `meho runner-principal list`

List runner principals in your tenant

```
meho runner-principal list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--include-revoked` — include revoked principals in the listing (default false)
- `--json` — emit raw ListResponse JSON instead of the human table

### `meho runner-principal register`

Register a new runner principal (tenant_admin)

```
meho runner-principal register <name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw RunnerPrincipalRead JSON instead of the human summary
- `--owner-sub` — OIDC sub of the kill-switch owner (defaults to the caller's sub)

### `meho runner-principal revoke`

Revoke a runner principal — kill switch (tenant_admin)

```
meho runner-principal revoke <name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw RunnerPrincipalRead JSON instead of the human summary

### `meho runner-principal show`

Show one runner principal by name (operator)

```
meho runner-principal show <name> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw RunnerPrincipalRead JSON instead of the human summary

## `meho scheduler`

Manage scheduled triggers (list / create / cancel)

```
meho scheduler
```

### `meho scheduler cancel`

Cancel one scheduled trigger by id (tenant_admin)

```
meho scheduler cancel <trigger_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a structured JSON result instead of plain text
- `--tenant` — target tenant UUID (tenant_admin cross-tenant cancel)

### `meho scheduler create`

Create one scheduled trigger (tenant_admin)

```
meho scheduler create [flags]
```

- `--agent-definition` — UUID of the agent definition to fire
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cron-expr` — 5-field cron expression (required when --kind=cron)
- `--event-filter` — event-match filter JSON object (required when --kind=event; inline JSON, @<path>, or @-)
- `--fire-at` — ISO 8601 fire time (required when --kind=one_off)
- `--identity-sub` — identity sub the scheduler impersonates at fire time (default '__scheduler__')
- `--in-flight-policy` — killed-mid-flight policy: fail_into_audit | resume (default fail_into_audit)
- `--inputs` — optional inputs JSON object forwarded as the agent run's input (inline JSON, @<path>, or @-)
- `--json` — emit raw Trigger JSON instead of the human summary
- `--kind` — trigger kind: cron | one_off | event
- `--tenant` — target tenant UUID (tenant_admin cross-tenant create)
- `--timezone` — IANA timezone name for cron evaluation (default 'UTC')
- `--work-ref` — external change-ticket reference inherited by every dispatched run

### `meho scheduler list`

List scheduled triggers in your tenant

```
meho scheduler list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit raw ListResponse JSON instead of the human table
- `--kind` — filter by trigger kind: cron | one_off | event
- `--limit` — max triggers per page (1..500, server default 100 when omitted)
- `--offset` — offset into the result set (default 0)
- `--status` — filter by trigger status: active | paused | cancelled | fired
- `--tenant` — target tenant UUID (tenant_admin only; operator role is locked to its own tenant)
- `--work-ref` — filter by external change-ticket reference

## `meho sddc-manager`

Pre-scoped CLI verbs for the sddc-rest-9.0 connector

```
meho sddc-manager
```

### `meho sddc-manager about`

Show SDDC Manager VCF release version, build date, and component BOM

```
meho sddc-manager about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager bundle`

VCF LCM bundle operations

```
meho sddc-manager bundle
```

#### `meho sddc-manager bundle list`

List LCM bundles (VCF update packages, async patches)

```
meho sddc-manager bundle list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager cluster`

VCF cluster operations

```
meho sddc-manager cluster
```

#### `meho sddc-manager cluster list`

List vSphere clusters across all or one VCF domain

```
meho sddc-manager cluster list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--domain` — filter to a specific domain id
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager domain`

VCF domain operations (list / info)

```
meho sddc-manager domain
```

#### `meho sddc-manager domain info`

Show full detail for one VCF domain

```
meho sddc-manager domain info <domain-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

#### `meho sddc-manager domain list`

List VCF domains (management + workload)

```
meho sddc-manager domain list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager host`

VCF ESXi host operations

```
meho sddc-manager host
```

#### `meho sddc-manager host list`

List ESXi hosts across all or one VCF domain or cluster

```
meho sddc-manager host list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--cluster` — filter to a specific cluster id
- `--domain` — filter to a specific domain id
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager manager`

SDDC Manager appliance operations

```
meho sddc-manager manager
```

#### `meho sddc-manager manager list`

List SDDC Manager appliances (FQDN, IP, version, management domain)

```
meho sddc-manager manager list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager network-pool`

VCF network pool operations (list / get)

```
meho sddc-manager network-pool
```

#### `meho sddc-manager network-pool get`

Show one network pool's networks with free/used IP capacity

```
meho sddc-manager network-pool get <network-pool-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

#### `meho sddc-manager network-pool list`

List VCF network pools (IP ranges and VLANs for host commission)

```
meho sddc-manager network-pool list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — SDDC Manager target slug

### `meho sddc-manager operation`

Pre-scoped meta-tool wrappers (search / call) for sddc-rest-9.0

```
meho sddc-manager operation
```

#### `meho sddc-manager operation call`

Dispatch any sddc-rest-9.0 op_id (escape hatch for ops without aliases)

```
meho sddc-manager operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — SDDC Manager target slug

#### `meho sddc-manager operation search`

Hybrid BM25 + cosine RRF search across sddc-rest-9.0 operations

```
meho sddc-manager operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho sddc-manager workflow`

VCF workflow task operations

```
meho sddc-manager workflow
```

#### `meho sddc-manager workflow list`

List in-flight or recent VCF workflow tasks

```
meho sddc-manager workflow list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--status` — filter by task status: Successful, Failed, In_Progress, Pending, Cancelled
- `--target` — SDDC Manager target slug

## `meho secret`

Secret-broker verbs for the secret-broker-1.x connector

```
meho secret
```

### `meho secret move`

Move a credential between stores server-side (references only, approval-gated)

```
meho secret move [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--from` — source '<kind>:<ref>' reference the credential is read from (required)
- `--json` — emit the full OperationResult envelope as JSON
- `--reason` — justification recorded for the approver and the audit trail (required)
- `--to` — sink '<kind>:<ref>' reference the credential is written to (required)

### `meho secret read`

Pipe a single raw secret field to stdout (pipe-only, audited)

```
meho secret read <mount> <path> [flags]
```

- `--backplane` — backplane URL (defaults to the URL from the most recent `meho login`)
- `--field` — key within the secret's data map whose raw value is written to stdout (required)
- `--target` — Vault target slug to dispatch against (resolved server-side)

## `meho sensor`

Manage deterministic-check sensors (list / create / delete)

```
meho sensor
```

### `meho sensor create`

Create one sensor (tenant_admin)

```
meho sensor create [flags]
```

- `--assertion` — bounded select->compare assertion spec JSON object (inline JSON, @<path>, or @-)
- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cadence-kind` — cadence kind: interval | cron
- `--connector-id` — connector id of the operation to evaluate
- `--cron-expr` — 5-field cron expression (required when --cadence-kind=cron)
- `--for-seconds` — hold-time hysteresis in seconds a failing state must persist (default 0)
- `--identity-sub` — identity sub the runner dispatches under (default '__sensor__')
- `--interval-seconds` — interval in seconds, 5..86400 (required when --cadence-kind=interval)
- `--json` — emit raw Sensor JSON instead of the human summary
- `--name` — operator-facing sensor name (unique per tenant)
- `--op-id` — operation id (must be safety_level='safe')
- `--params` — optional op-params JSON object (inline JSON, @<path>, or @-)
- `--retry-backoff-seconds` — accelerated re-check spacing in seconds while a state change is pending, 5..300 (omitted when unset; the server then applies its default of 15)
- `--retry-times` — consecutive confirming re-checks required before a state change commits, 0..5 (default 0 = off)
- `--severity` — worst rollup state a failing assertion drives: degraded | critical (default critical)
- `--target` — optional dispatch-target JSON object (inline JSON, @<path>, or @-)
- `--tenant` — target tenant UUID (platform_admin cross-tenant create)
- `--timezone` — IANA timezone name for cron evaluation (default 'UTC')

### `meho sensor delete`

Delete one sensor by id (tenant_admin)

```
meho sensor delete <sensor_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a structured JSON result instead of plain text
- `--tenant` — target tenant UUID (platform_admin cross-tenant delete)

### `meho sensor list`

List sensors in your tenant

```
meho sensor list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cadence-kind` — filter by cadence kind: interval | cron
- `--json` — emit raw ListResponse JSON instead of the human table
- `--limit` — max sensors per page (1..500, server default 100 when omitted)
- `--offset` — offset into the result set (default 0)
- `--status` — filter by sensor status: active | paused
- `--tenant` — target tenant UUID (platform_admin only; operator role is locked to its own tenant)

### `meho sensor results`

Show a sensor's per-tick evidence history (trend query)

```
meho sensor results <sensor_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cursor` — opaque keyset pagination token (echo the printed next cursor to continue)
- `--from` — inclusive lower bound on evaluated_at (RFC 3339, e.g. 2026-08-01T00:00:00Z)
- `--json` — emit the raw {items, next_cursor} JSON envelope instead of the human table
- `--limit` — max rows per page (1..500, server default 100 when omitted)
- `--state` — filter by state: ok | degraded | critical | unknown | skip
- `--to` — inclusive upper bound on evaluated_at (RFC 3339, e.g. 2026-08-02T00:00:00Z)

## `meho status`

Show operator identity + backplane health; --watch streams live activity

```
meho status [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit a single JSON document on stdout instead of the human summary
- `--op-class` — filter --watch events by op_class (read, write, credential_read, audit_query)
- `--principal` — filter --watch events by principal_sub (JWT subject claim)
- `--target` — filter --watch events by target_name (the connector instance name)
- `--watch`, `-w` — stream a live SSE feed of broadcast events (one line per event; Ctrl-C to exit)

## `meho targets`

Operate the MEHO targets registry (add / list / describe / probe / import / discover)

```
meho targets
```

### `meho targets add`

Register a single target (alias `create`)

```
meho targets add <name> [flags]
```

- `--alias` — additional name the target resolves by (repeatable)
- `--auth-model` — per-target identity model (default: shared_service_account)
- `--backplane` — backplane URL to create the target in (defaults to the URL recorded by `meho login`)
- `--fqdn` — fully-qualified domain name, when distinct from --host
- `--host` — host or IP the connector dials (required)
- `--json` — emit the created target as JSON instead of the human summary
- `--note` — free-form operator note stored on the target
- `--port` — connection port (default: the connector's product default)
- `--preferred-impl` — connector impl_id override for the resolver's tie-break
- `--product` — connector product slug (required; must match a registered connector — see `meho connector list`)
- `--secret-ref` — Vault path of the target's credential (omit to derive the per-tenant default)
- `--tls-ca-pin` — PEM CA bundle to pin for this target (mutually exclusive with --verify-tls=false)
- `--tls-server-name` — TLS SNI / certificate-verification hostname, decoupled from --host
- `--verify-tls` — verify the target's TLS certificate; pass --verify-tls=false to opt out
- `--version` — operator-asserted product version (e.g. 9.0) the resolver consults before the first probe

### `meho targets describe`

Describe a single target (alias-aware)

```
meho targets describe <name-or-alias> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human summary

### `meho targets discover`

Discover candidate targets a connector can reach for a product

```
meho targets discover <product> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human tables
- `--seed-target` — scope discovery to one already-registered target's reach (resolved tenant-scoped)

### `meho targets import`

Bulk-import targets from a targets.yaml file

```
meho targets import <file> [flags]
```

- `--backplane` — backplane URL to import into (defaults to the URL recorded by `meho login`)
- `--dry-run` — print the plan (read-only: one GET, no writes); does not apply
- `--json` — output the plan as JSON (use with --dry-run)
- `--update` — PATCH existing targets instead of erroring on duplicate names

### `meho targets list`

List targets in your tenant

```
meho targets list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cursor` — keyset pagination cursor (the last name from the previous page)
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--limit` — max targets per page (1..500, server default 100 when omitted)
- `--product`, `-p` — filter by product slug (exact match)

### `meho targets probe`

Probe a target's connector and refresh its fingerprint

```
meho targets probe <name-or-alias> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human summary

## `meho tenants`

Operate per-tenant policy (flight-recorder capture policy)

```
meho tenants
```

### `meho tenants flight-recorder-policy`

Manage the tenant's flight-recorder capture policy (tenant_admin)

```
meho tenants flight-recorder-policy
```

#### `meho tenants flight-recorder-policy set`

Update the tenant flight-recorder capture policy (tenant_admin)

```
meho tenants flight-recorder-policy set [flags]
```

- `--agent-readable` — agent-read override (F5): true | false | inherit (inherit clears to the capture default)
- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--clear-retention` — clear the retention override back to the global default
- `--enabled` — per-tenant capture default (F1); send true or false
- `--json` — emit the resolved policy as JSON instead of the human summary
- `--retention-days` — per-tenant trace retention window in days (F4; 1..365)

## `meho topology`

Query and refresh the MEHO topology graph (refresh / dependents / dependencies / path / timeline / diff / history / annotate / unannotate / list-edges)

```
meho topology
```

### `meho topology annotate`

Assert a curated topology edge (operator-curated cross-system relationship)

```
meho topology annotate <from> <kind> <to> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--evidence-url` — URL pointing at evidence for the asserted relationship (max 2000 chars)
- `--from-kind` — pin the `from` endpoint to one node kind when its name is ambiguous
- `--json` — emit the raw TopologyEdge response to stdout instead of the human summary
- `--note` — free-form operator note (max 2000 chars) attached to the edge
- `--to-kind` — pin the `to` endpoint to one node kind when its name is ambiguous

### `meho topology bulk-import`

Annotate a list of curated topology edges from one file in a single transaction

```
meho topology bulk-import <file> [flags]
```

- `--backplane` — backplane URL to import into (defaults to the URL recorded by the most recent `meho login`)
- `--dry-run` — compute the plan without applying any annotation (no writes, no audit, no broadcast events)
- `--json` — emit the raw POST /edges/bulk response JSON instead of the human table

### `meho topology dependencies`

Walk what a node depends on (forward closure)

```
meho topology dependencies <name|alias> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--depth` — max traversal depth (1..64, server default 16 when omitted)
- `--include-stale` — include soft-deleted (stale) nodes and edges in the walk (last-refresh-wins); pass --include-stale=false for live rows only
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--kind` — restrict the walk to edges of this kind (e.g. runs-on, mounts, routes-through, belongs-to)
- `--node-kind` — pin the anchor to one node kind when the name is ambiguous across kinds

### `meho topology dependents`

Walk what depends on a node (reverse closure)

```
meho topology dependents <name|alias> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--depth` — max traversal depth (1..64, server default 16 when omitted)
- `--include-stale` — include soft-deleted (stale) nodes and edges in the walk (last-refresh-wins); pass --include-stale=false for live rows only
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--kind` — restrict the walk to edges of this kind (e.g. runs-on, mounts, routes-through, belongs-to)
- `--node-kind` — pin the anchor to one node kind when the name is ambiguous across kinds

### `meho topology diff`

Diff the topology graph between two timestamps

```
meho topology diff <ts1> <ts2> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--changed-only` — suppress `updated` entries whose only mutation was a `last_seen` bump
- `--json` — emit raw TopologyDiffResult JSON instead of the human summary
- `--kind` — narrow to one resource kind (node kind like `vm` or edge kind like `runs-on`)

### `meho topology history`

Walk the per-resource history of one node

```
meho topology history <node-name|alias> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--include-edges` — also walk history rows for edges incident to the anchor node
- `--json` — emit raw TopologyHistoryResult JSON instead of the human table
- `--limit` — max rows returned (1..5000, server-side cap when omitted)
- `--node-kind` — pin the anchor to one node kind when the name is ambiguous across kinds
- `--since` — earliest valid_from; accepts 24h / 7d / 30m / 2w shorthand, RFC3339, or YYYY-MM-DD
- `--until` — latest valid_from; accepts the same shorthand as --since

### `meho topology list-edges`

List curated + auto topology edges (filterable, tenant-scoped)

```
meho topology list-edges [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--conflicts` — surface only edges flagged by the conflict detector (recoverability listing)
- `--from` — filter to edges whose `from` endpoint matches this node name
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--kind` — restrict to one edge kind (run `meho topology annotate --help` for the closed 10-kind vocabulary)
- `--limit` — max edges to return (1..1000, server default 200 when omitted)
- `--offset` — pagination offset (default 0)
- `--source` — restrict by source: `curated` (operator-asserted) or `auto` (probe-derived)
- `--to` — filter to edges whose `to` endpoint matches this node name

### `meho topology path`

Find the shortest path between two nodes

```
meho topology path <from> <to> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--from-kind` — pin the `from` endpoint to one node kind when its name is ambiguous
- `--include-stale` — include soft-deleted (stale) nodes and edges in the search (last-refresh-wins); pass --include-stale=false for live rows only
- `--json` — emit machine-readable JSON to stdout instead of the human chain
- `--max-hops` — max path length in hops (1..32, server default 8 when omitted)
- `--to-kind` — pin the `to` endpoint to one node kind when its name is ambiguous

### `meho topology refresh`

Rediscover one target's topology and reconcile it into the graph

```
meho topology refresh <target> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit machine-readable JSON to stdout instead of the human summary

### `meho topology timeline`

Walk the tenant timeline of graph changes

```
meho topology timeline [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cursor` — opaque forward-pagination cursor from a prior page's NEXT line
- `--json` — emit raw TopologyTimelineResult JSON instead of the human table
- `--limit` — max rows per page (1..1000, server default 50 when omitted)
- `--since` — earliest valid_from; accepts 24h / 7d / 30m / 2w shorthand, RFC3339, or YYYY-MM-DD
- `--target` — narrow to one target (name or alias; server-side resolution)
- `--until` — latest valid_from; accepts the same shorthand as --since

### `meho topology unannotate`

Delete a curated topology edge (by id or by from/kind/to tuple)

```
meho topology unannotate <edge-id> | <from> <kind> <to> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--from-kind` — pin the `from` endpoint to one node kind when its name is ambiguous (tuple form only)
- `--json` — emit machine-readable JSON ({"deleted": "<edge_id>"}) instead of the human line
- `--to-kind` — pin the `to` endpoint to one node kind when its name is ambiguous (tuple form only)

## `meho vault`

Pre-scoped CLI verbs for the vault-1.x connector

```
meho vault
```

### `meho vault auth`

Vault identity verbs (userpass / approle, read-only)

```
meho vault auth
```

#### `meho vault auth approle-list`

List configured approle role names

```
meho vault auth approle-list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault auth approle-read`

Read one approle role (policies, ttls)

```
meho vault auth approle-read <role> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault auth userpass-list`

List configured userpass users

```
meho vault auth userpass-list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault auth userpass-read`

Read one userpass user (policies, ttl)

```
meho vault auth userpass-read <user> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

### `meho vault kv`

KV-v2 secret verbs (read / list / put / versions / delete)

```
meho vault kv
```

#### `meho vault kv delete`

Soft-delete specific versions of a KV-v2 secret

```
meho vault kv delete <mount> <path> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)
- `--versions` — comma-separated version numbers to soft-delete (e.g. 3,4,5); required

#### `meho vault kv list`

List keys at a KV-v2 path

```
meho vault kv list <mount> <path> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault kv put`

Write a new version of a KV-v2 secret

```
meho vault kv put <mount> <path> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--cas` — check-and-set: only write if the current version equals this value
- `--data` — secret body as inline JSON object or @<file>; required
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault kv read`

Read the latest version of a KV-v2 secret

```
meho vault kv read <mount> <path> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault kv versions`

List the version history of a KV-v2 secret

```
meho vault kv versions <mount> <path> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

### `meho vault sys`

Vault system diagnostics (health / seal-status / mounts-list / auth-list)

```
meho vault sys
```

#### `meho vault sys auth-list`

List enabled auth backends

```
meho vault sys auth-list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault sys health`

Report Vault health (initialized / sealed / standby)

```
meho vault sys health [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault sys mounts-list`

List enabled secret backends

```
meho vault sys mounts-list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

#### `meho vault sys seal-status`

Read the Vault seal state

```
meho vault sys seal-status [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — Vault target slug to dispatch against (resolved server-side)

## `meho vcf-automation`

Pre-scoped CLI verbs for the vcfa-rest-9.0 dual-plane connector

```
meho vcf-automation
```

- `--fqdn` — per-call vhost override (target.fqdn); honoured by the connector for vhost routing
- `--plane` — VCFA plane to target: 'provider' (cloudapi/*) or 'tenant' (iaas/api/*)

### `meho vcf-automation about`

Show VCFA appliance identity (plane-specific)

```
meho vcf-automation about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

### `meho vcf-automation blueprint`

Tenant-plane VCFA catalog blueprints (list)

```
meho vcf-automation blueprint
```

#### `meho vcf-automation blueprint list`

List tenant-plane catalog blueprints

```
meho vcf-automation blueprint list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

### `meho vcf-automation deployment`

Tenant-plane VCFA catalog deployments (list / get)

```
meho vcf-automation deployment
```

#### `meho vcf-automation deployment get`

Read one tenant-plane deployment by id

```
meho vcf-automation deployment get <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

#### `meho vcf-automation deployment list`

List tenant-plane catalog deployments

```
meho vcf-automation deployment list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

### `meho vcf-automation operation`

Pre-scoped meta-tool wrappers (search / call) for vcfa-rest-9.0

```
meho vcf-automation operation
```

#### `meho vcf-automation operation call`

Dispatch any vcfa-rest-9.0 op_id (escape hatch for ops without aliases)

```
meho vcf-automation operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — VCFA target slug

#### `meho vcf-automation operation search`

Hybrid BM25 + cosine RRF search across vcfa-rest-9.0 operations

```
meho vcf-automation operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho vcf-automation org`

Provider-plane VCFA organizations (list / get)

```
meho vcf-automation org
```

#### `meho vcf-automation org get`

Read one provider-plane organization by id

```
meho vcf-automation org get <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

#### `meho vcf-automation org list`

List provider-plane organizations on a VCFA appliance

```
meho vcf-automation org list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

### `meho vcf-automation project`

Tenant-plane VCFA projects (list)

```
meho vcf-automation project
```

#### `meho vcf-automation project list`

List tenant-plane projects on a VCFA appliance

```
meho vcf-automation project list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

### `meho vcf-automation region`

Provider-plane VCFA regions (list / get)

```
meho vcf-automation region
```

#### `meho vcf-automation region get`

Read one provider-plane region by id

```
meho vcf-automation region get <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

#### `meho vcf-automation region list`

List provider-plane regions on a VCFA appliance

```
meho vcf-automation region list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

### `meho vcf-automation user`

Provider-plane VCFA system users (list)

```
meho vcf-automation user
```

#### `meho vcf-automation user list`

List provider-plane system users on a VCFA appliance

```
meho vcf-automation user list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCFA target slug

## `meho vcf-fleet`

Pre-scoped CLI verbs for the fleet-rest-9.0 connector

```
meho vcf-fleet
```

### `meho vcf-fleet about`

Show vRSLCM appliance identity (apiVersion + productVersion + build)

```
meho vcf-fleet about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

### `meho vcf-fleet datacenter`

VCF Fleet datacenter operations (list)

```
meho vcf-fleet datacenter
```

#### `meho vcf-fleet datacenter list`

List Fleet-managed datacenters (wrapper-verified reachability probe)

```
meho vcf-fleet datacenter list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

### `meho vcf-fleet environment`

VCF Fleet environment operations (list / info)

```
meho vcf-fleet environment
```

#### `meho vcf-fleet environment info`

Show the full detail of one Fleet environment

```
meho vcf-fleet environment info <environment-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

#### `meho vcf-fleet environment list`

List Fleet-managed environments (the primary inventory unit)

```
meho vcf-fleet environment list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

### `meho vcf-fleet operation`

Pre-scoped meta-tool wrappers (search / call) for fleet-rest-9.0

```
meho vcf-fleet operation
```

#### `meho vcf-fleet operation call`

Dispatch any fleet-rest-9.0 op_id (escape hatch for ops without aliases)

```
meho vcf-fleet operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — VCF Fleet target slug

#### `meho vcf-fleet operation search`

Hybrid BM25 + cosine RRF search across fleet-rest-9.0 operations

```
meho vcf-fleet operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho vcf-fleet product`

VCF Fleet product operations (list)

```
meho vcf-fleet product
```

#### `meho vcf-fleet product list`

List products deployed under a Fleet environment

```
meho vcf-fleet product list <environment-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

### `meho vcf-fleet request`

VCF Fleet lifecycle request operations (list / info)

```
meho vcf-fleet request
```

#### `meho vcf-fleet request info`

Show the full detail of one Fleet lifecycle request

```
meho vcf-fleet request info <request-id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

#### `meho vcf-fleet request list`

List Fleet lifecycle requests (deploy / patch / upgrade workflows)

```
meho vcf-fleet request list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

### `meho vcf-fleet vcenter`

VCF Fleet vCenter operations (list)

```
meho vcf-fleet vcenter
```

#### `meho vcf-fleet vcenter list`

List vCenters registered under a Fleet datacenter

```
meho vcf-fleet vcenter list <datacenter-vmid> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — VCF Fleet target slug

## `meho vcf-logs`

Pre-scoped CLI verbs for the vrli-rest-9.0 connector (VCF Operations for Logs)

```
meho vcf-logs
```

### `meho vcf-logs about`

Show vRLI appliance version, release name, and build

```
meho vcf-logs about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vRLI target slug

### `meho vcf-logs aggregated`

Run a vRLI aggregated event query (group-by / count / time-bin)

```
meho vcf-logs aggregated [constraints] [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vRLI target slug
- `--time-range` — aggregation time window (e.g. 5m, 1h, 24h, 7d); empty = appliance default

### `meho vcf-logs alert`

vRLI alert-definition verbs (list)

```
meho vcf-logs alert
```

#### `meho vcf-logs alert list`

List vRLI alert definitions

```
meho vcf-logs alert list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vRLI target slug

### `meho vcf-logs content-pack`

vRLI content-pack inventory verbs (list)

```
meho vcf-logs content-pack
```

#### `meho vcf-logs content-pack list`

List installed vRLI content packs

```
meho vcf-logs content-pack list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vRLI target slug

### `meho vcf-logs field`

vRLI indexer-field catalog verbs (list)

```
meho vcf-logs field
```

#### `meho vcf-logs field list`

List vRLI indexer fields (static + extracted)

```
meho vcf-logs field list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vRLI target slug

### `meho vcf-logs host`

vRLI host-inventory verbs (list)

```
meho vcf-logs host
```

#### `meho vcf-logs host list`

List hosts currently reporting log events to this vRLI cluster

```
meho vcf-logs host list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vRLI target slug

### `meho vcf-logs operation`

Pre-scoped meta-tool wrappers (search / call) for vrli-rest-9.0

```
meho vcf-logs operation
```

#### `meho vcf-logs operation call`

Dispatch any vrli-rest-9.0 op_id (escape hatch for ops without aliases)

```
meho vcf-logs operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — vRLI target slug

#### `meho vcf-logs operation search`

Hybrid BM25 + cosine RRF search across vrli-rest-9.0 operations

```
meho vcf-logs operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho vcf-logs query`

Run a vRLI event query (constraints, optional limit)

```
meho vcf-logs query [constraints] [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--limit` — max events to return (0 = appliance default)
- `--target` — vRLI target slug

## `meho vcf-operations`

Pre-scoped CLI verbs for the vrops-rest-9.0 connector

```
meho vcf-operations
```

### `meho vcf-operations about`

Show vROps appliance release name and build number

```
meho vcf-operations about [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vROps target slug

### `meho vcf-operations alert`

vROps alert verbs (list)

```
meho vcf-operations alert
```

#### `meho vcf-operations alert list`

List vROps alerts (currently firing or recently resolved)

```
meho vcf-operations alert list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — filter params as inline JSON or @<file>
- `--target` — vROps target slug

### `meho vcf-operations alertdefinition`

vROps alert-definition verbs (list)

```
meho vcf-operations alertdefinition
```

#### `meho vcf-operations alertdefinition list`

List vROps alert definitions (the policy surface)

```
meho vcf-operations alertdefinition list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — filter params as inline JSON or @<file>
- `--target` — vROps target slug

### `meho vcf-operations operation`

Pre-scoped meta-tool wrappers (search / call) for vrops-rest-9.0

```
meho vcf-operations operation
```

#### `meho vcf-operations operation call`

Dispatch any vrops-rest-9.0 op_id (escape hatch for ops without aliases)

```
meho vcf-operations operation call <op_id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — params as inline JSON or @<file>
- `--target` — vROps target slug

#### `meho vcf-operations operation search`

Hybrid BM25 + cosine RRF search across vrops-rest-9.0 operations

```
meho vcf-operations operation search <query> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key
- `--json` — emit machine-readable JSON
- `--limit` — max hits (1..50, clamped by the API)

### `meho vcf-operations recommendation`

vROps recommendation verbs (list)

```
meho vcf-operations recommendation
```

#### `meho vcf-operations recommendation list`

List vROps recommendations (remediation hints attached to alerts/symptoms)

```
meho vcf-operations recommendation list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — filter params as inline JSON or @<file>
- `--target` — vROps target slug

### `meho vcf-operations resource`

vROps resource verbs (list, get)

```
meho vcf-operations resource
```

#### `meho vcf-operations resource get`

Get one vROps resource by identifier (UUID)

```
meho vcf-operations resource get <id> [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — vROps target slug

#### `meho vcf-operations resource list`

List vROps resources (VMs, hosts, datastores, adapter instances)

```
meho vcf-operations resource list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — filter params as inline JSON or @<file>
- `--target` — vROps target slug

### `meho vcf-operations supermetric`

vROps super-metric verbs (list)

```
meho vcf-operations supermetric
```

#### `meho vcf-operations supermetric list`

List vROps super metrics (user-defined metric formulae)

```
meho vcf-operations supermetric list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — filter params as inline JSON or @<file>
- `--target` — vROps target slug

### `meho vcf-operations symptom`

vROps symptom verbs (list)

```
meho vcf-operations symptom
```

#### `meho vcf-operations symptom list`

List vROps symptoms (per-condition signals beneath alerts)

```
meho vcf-operations symptom list [flags]
```

- `--backplane` — backplane URL (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--params` — filter params as inline JSON or @<file>
- `--target` — vROps target slug

## `meho version`

Print CLI version and build metadata

```
meho version
```

## `meho vmware`

Pre-scoped CLI verbs for the vmware-rest-9.0 connector

```
meho vmware
```

### `meho vmware about`

Show vSphere product, version, and build for a target

```
meho vmware about [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--target` — target slug to dispatch against (required for ops that read a target)

### `meho vmware cluster`

vSphere cluster verbs (list / patch)

```
meho vmware cluster
```

#### `meho vmware cluster list`

List vSphere clusters on a vCenter target

```
meho vmware cluster list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho vmware cluster patch`

Patch a vSphere cluster (composite: lifecycle-managed)

```
meho vmware cluster patch <name-or-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--spec` — patch spec as inline JSON or @<file>; optional
- `--target` — target slug to dispatch against

### `meho vmware datacenter`

vSphere datacenter verbs (list)

```
meho vmware datacenter
```

#### `meho vmware datacenter list`

List vSphere datacenters on a vCenter target

```
meho vmware datacenter list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho vmware datastore`

vSphere datastore verbs (list)

```
meho vmware datastore
```

#### `meho vmware datastore list`

List vSphere datastores on a vCenter target

```
meho vmware datastore list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho vmware host`

vSphere host verbs (list / evacuate)

```
meho vmware host
```

#### `meho vmware host evacuate`

Evacuate a host (composite: vMotion all VMs off then maintenance-mode)

```
meho vmware host evacuate <name-or-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho vmware host list`

List ESXi hosts on a vCenter target

```
meho vmware host list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho vmware network`

vSphere network verbs (list)

```
meho vmware network
```

#### `meho vmware network list`

List vSphere networks on a vCenter target

```
meho vmware network list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

### `meho vmware operation`

Pre-scoped meta-tool wrappers (search / call) for vmware-rest-9.0

```
meho vmware operation
```

#### `meho vmware operation call`

Dispatch any vmware-rest-9.0 op_id (escape hatch for ops without aliases)

```
meho vmware operation call <op_id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON instead of the human render
- `--params` — operation params as inline JSON or @<file>; omitted means no params
- `--target` — target slug to dispatch against (required for ops that read a target)

#### `meho vmware operation search`

Hybrid BM25 + cosine RRF search across vmware-rest-9.0 operations

```
meho vmware operation search <query> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--group` — narrow the search to one group_key within the connector
- `--json` — emit machine-readable JSON to stdout instead of the human table
- `--limit` — max hits to return (1..50, clamped by the API at 50)

### `meho vmware vm`

vSphere VM verbs (list / info / create)

```
meho vmware vm
```

#### `meho vmware vm create`

Create a VM via the composite create flow

```
meho vmware vm create [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--spec` — vSphere CreateSpec as inline JSON or @<file>; required
- `--target` — target slug to dispatch against

#### `meho vmware vm info`

Show details for one VM by name or moid

```
meho vmware vm info <name-or-id> [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--json` — emit the full OperationResult envelope as JSON
- `--target` — target slug to dispatch against

#### `meho vmware vm list`

List VMs on a vCenter target

```
meho vmware vm list [flags]
```

- `--backplane` — backplane URL to query (defaults to the URL recorded by the most recent `meho login`)
- `--filter` — raw vSphere filter as k=v; repeat for multiple filters (e.g. --filter clusters=domain-c1)
- `--json` — emit the full OperationResult envelope as JSON
- `--names` — filter by VM name; repeat for multiple matches
- `--power-state` — filter by powered_states (POWERED_ON / POWERED_OFF / SUSPENDED); repeat for OR
- `--target` — target slug to dispatch against
