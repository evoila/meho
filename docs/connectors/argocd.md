# ArgoCD

> Last verified: v2.0

ArgoCD is the GitOps continuous delivery tool for Kubernetes. MEHO connects to ArgoCD to give operators visibility into application deployment state, sync status, resource health, and deployment history -- enabling cross-system tracing from a Git commit all the way through to running pods.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Bearer Token (PAT) | `api_token` | ArgoCD Personal Access Token or API key |

**Setup:**

1. In ArgoCD, navigate to **Settings > Accounts** and create an API token for a service account (or use an existing account).
2. Assign the account the `apiKey` capability and appropriate RBAC roles (at minimum, `readonly` for read operations).
3. Provide the ArgoCD server URL (e.g., `https://argocd.example.com`) and the API token when creating the connector in MEHO.
4. If your ArgoCD uses a self-signed certificate, enable **Skip TLS Verification** during connector setup.

!!! tip "Least Privilege"
    Start with a read-only role to explore applications and resource status. Only grant write access when operators need to trigger syncs or rollbacks through MEHO.

## Operations

MEHO registers 10 operations for ArgoCD (8 READ, 1 WRITE, 1 DESTRUCTIVE):

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_applications` | READ | List applications with sync status, health status, source repo, and destination cluster/namespace |
| `get_application` | READ | Get detailed application status including composite sync+health state, source revision, conditions, and managed resource summary |
| `get_resource_tree` | READ | View the resource tree showing managed K8s resources (Deployments, ReplicaSets, Pods, Services) with health status |
| `get_managed_resources` | READ | View managed resources with live vs desired state diff status for drift detection |
| `get_application_events` | READ | View K8s events for resources managed by an application -- warnings, errors, and lifecycle events |
| `get_sync_history` | READ | View sync/operation history showing deployment revisions, timestamps, and deployment IDs |
| `get_revision_metadata` | READ | View revision metadata including commit message, author, and date for a deployed revision |
| `get_server_diff` | READ | View server-side diff showing differences between live state and desired state |
| `sync_application` | WRITE | Trigger a sync to apply desired state from Git to the cluster. Supports dry run and prune options |
| `rollback_application` | DESTRUCTIVE | Roll back an application to a previous deployment by deployment ID. Reverts to an earlier state |

!!! danger "Destructive Operations"
    `rollback_application` requires explicit operator approval before execution. MEHO will present the rollback details and wait for confirmation.

## Example Queries

Ask MEHO questions like:

- "What ArgoCD applications are out of sync?"
- "Show me the sync status of the production app"
- "When was the last deployment of the checkout service?"
- "What resources are managed by the payments app?"
- "Are there any unhealthy resources in the staging application?"
- "Show me the diff between live state and desired state for my-app"
- "What changed in the last sync of the API gateway?"
- "Sync the staging app to the latest commit"
- "Show me the deployment history for the auth service"
- "Roll back the frontend app to the previous deployment"

## Deployment Pipeline Tracing

ArgoCD is a critical link in MEHO's cross-system deployment tracing capability. When investigating a deployment issue, MEHO can trace the full pipeline:

1. **GitHub** -- Identify the commit and PR that triggered the change
2. **ArgoCD** -- Track the sync from Git revision to cluster, check if the app is healthy or degraded
3. **Kubernetes** -- Inspect the actual pods, events, and resource state in the target cluster

For example, if an operator asks "Why is the checkout service down?", MEHO can:

- Check ArgoCD sync status and discover the app is `OutOfSync` or `Degraded`
- Pull the resource tree to find failing pods
- Look at the sync history to find when the last deployment happened
- Check the revision metadata to link back to the Git commit
- Cross-reference with GitHub to find the PR and author

## Topology

ArgoCD entities are managed through MEHO's topology schema:

| Entity Type | Properties | Cross-System Links |
|-------------|------------|-------------------|
| Application | Name, project, sync status, health status, source repo, destination cluster/namespace | Links to **Kubernetes** namespaces and clusters via destination; links to **GitHub** repos via source repository |

## Troubleshooting

### Connection Fails with SSL Error

**Symptom:** Connector creation fails with certificate verification error.
**Cause:** ArgoCD is using a self-signed or internal CA certificate.
**Fix:** Enable **Skip TLS Verification** when creating the connector. For production, add your CA certificate to the MEHO trust store.

### Token Permissions Insufficient

**Symptom:** Operations return 403 Forbidden errors.
**Cause:** The API token's RBAC role does not have permission for the requested operation.
**Fix:** In ArgoCD, assign the account a role with the required permissions. Read operations need at minimum `applications: get` and `projects: get`. Sync operations need `applications: sync`. Rollback needs `applications: action/rollback`.

### Sync Status vs Health Status

**Symptom:** An application shows `Synced` but `Degraded`, or `OutOfSync` but `Healthy`.
**Cause:** Sync status and health status are independent. Sync tracks whether the cluster matches Git. Health tracks whether the running resources are functioning.
**Fix:** This is expected behavior. Use `get_application` to see both statuses. An `OutOfSync` + `Healthy` app means the cluster is running fine but has drifted from Git. A `Synced` + `Degraded` app means Git was applied but something is failing (e.g., image pull errors, resource limits).

### Application Not Found

**Symptom:** `get_application` returns 404 for an app that exists in the ArgoCD UI.
**Cause:** The application may be in a non-default namespace, or the RBAC role lacks access to the application's project.
**Fix:** Specify the `app_namespace` parameter if the application is not in the `argocd` namespace. Verify that the token's RBAC role includes the application's project.
