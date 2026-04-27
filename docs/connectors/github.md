# GitHub

> Last verified: v2.0

GitHub is the source code and CI/CD platform. MEHO connects to GitHub to give operators visibility into repositories, commits, pull requests, workflow runs, deployments, and CI/CD status -- completing the deployment pipeline tracing story from code change to production.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Personal Access Token (PAT) | `personal_access_token` | Fine-grained or classic PAT with appropriate scopes |

**Setup:**

1. In GitHub, navigate to **Settings > Developer settings > Personal access tokens**.
2. Create a fine-grained token (recommended) or classic token with the following scopes:
   - **Repositories:** `repo` (read access to code, commits, pull requests)
   - **Actions:** `actions:read` (read workflow runs and logs)
   - **Deployments:** `deployments:read` (read deployment status)
3. Specify the **organization name** when creating the connector in MEHO. All repository operations are scoped to this organization.
4. The default base URL is `https://api.github.com`. For GitHub Enterprise Server, provide your instance URL.

!!! tip "Fine-Grained Tokens"
    Fine-grained tokens let you scope access to specific repositories and permissions. For MEHO, grant read access to all org repos, plus write access only if you want to re-run failed workflow jobs.

## Operations

MEHO registers 12 operations for GitHub (11 READ, 1 WRITE):

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_repositories` | READ | List repositories in the configured organization with name, description, default branch, language, and visibility |
| `list_commits` | READ | List recent commits on a branch with SHA, author, message, and timestamp |
| `compare_refs` | READ | Compare two git refs (branches, tags, SHAs) to see commits and files changed between them |
| `list_pull_requests` | READ | List pull requests with title, state, author, and branch info. Filter by state (open/closed/all) |
| `get_pull_request` | READ | Get detailed PR information including title, state, author, merge status, and timestamps |
| `list_workflow_runs` | READ | List GitHub Actions workflow runs with status, conclusion, triggering branch/commit, and timing |
| `get_workflow_run` | READ | Get detailed workflow run information including status, conclusion, triggering event, and head SHA |
| `list_workflow_jobs` | READ | List jobs within a workflow run with step-level status, conclusion, timing, and runner info |
| `get_workflow_logs` | READ | Download logs for a specific workflow job. Returns the last 200 lines by default |
| `rerun_failed_jobs` | WRITE | Re-run only the failed jobs in a workflow run, preserving successful job results |
| `list_deployments` | READ | List deployments with environment, SHA, ref, and status history |
| `get_commit_status` | READ | Get combined CI/CD status for a commit by merging legacy statuses and Actions check runs |

## Example Queries

Ask MEHO questions like:

- "Show me open PRs for the meho-app repo"
- "What was the last commit on main?"
- "List failed workflow runs in the last 24 hours"
- "What deployments happened this week to production?"
- "Compare main and the feature-auth branch -- what changed?"
- "Show me the logs for the failing CI job"
- "What's the CI status of the latest commit on main?"
- "Which repos in the org were pushed to today?"
- "Re-run the failed jobs in the latest workflow run for checkout-service"
- "Who authored the last 5 commits on the payments service?"

## Deployment Pipeline Tracing

GitHub is the starting point for MEHO's cross-system deployment tracing. When investigating issues, MEHO can:

1. **GitHub** -- Find the commit, PR, and workflow run that introduced the change
2. **ArgoCD** -- Track how the commit was synced to the cluster
3. **Kubernetes** -- Verify the deployment rolled out successfully

The `get_commit_status` operation is particularly powerful -- it merges both legacy commit statuses and GitHub Actions check runs into a single unified view, so MEHO can quickly assess whether all CI/CD checks passed for a given commit.

## Topology

GitHub entities are managed through MEHO's topology schema:

| Entity Type | Properties | Cross-System Links |
|-------------|------------|-------------------|
| Repository | Full name, description, default branch, language, visibility | Links to **ArgoCD** applications via source repository URL |
| Pull Request | Number, title, state, author, head/base refs | Links to commits and CI/CD status |
| Workflow Run | ID, name, status, conclusion, triggering branch | Links to commits via head SHA |

## Troubleshooting

### PAT Scope Insufficient

**Symptom:** Operations return 404 (not 403) for resources that exist.
**Cause:** GitHub returns 404 instead of 403 for private resources when the token lacks access. This is a security measure to avoid leaking repository existence.
**Fix:** Verify the PAT has `repo` scope for classic tokens, or appropriate repository permissions for fine-grained tokens. Ensure the token has access to the specified organization.

### Rate Limiting

**Symptom:** Operations fail with 403 and a message about rate limits.
**Cause:** GitHub's API rate limit is 5,000 requests per hour for authenticated requests.
**Fix:** MEHO tracks rate limit headers automatically. If you hit limits frequently, consider creating a dedicated service account PAT. The connector logs remaining rate limit quota in debug mode.

### Organization vs User Repositories

**Symptom:** `list_repositories` returns empty results or unexpected repos.
**Cause:** The organization name may be incorrect, or the PAT may be scoped to a different set of repos.
**Fix:** Verify the organization name matches exactly (case-sensitive). For fine-grained tokens, check that the token has access to the organization's repositories.

### Workflow Logs Too Large

**Symptom:** `get_workflow_logs` returns truncated output.
**Cause:** Workflow logs can be very large. The operation returns the last 200 lines by default.
**Fix:** Use the `tail_lines` parameter to adjust the number of lines returned. For very large logs, first use `list_workflow_jobs` to identify the specific failing job, then fetch logs for just that job.
