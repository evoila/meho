# Jira

> Last verified: v2.0

Jira is the project tracking and issue management platform. MEHO connects to Jira Cloud to let operators search issues, create tickets from investigation findings, add comments with diagnostic results, and transition issue statuses -- all through natural language, without ever needing to write JQL.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Email + API Token | `email`, `api_token` | Atlassian Cloud API token (not password) |

Jira and Confluence share the same `AtlassianHTTPConnector` base class, using identical authentication: your Atlassian account email paired with an API token.

**Setup:**

1. Go to [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and create an API token.
2. Provide your Jira Cloud site URL (e.g., `https://yourcompany.atlassian.net`), your Atlassian account email, and the API token when creating the connector in MEHO.
3. MEHO verifies connectivity by calling `/rest/api/3/myself` and reports back the authenticated user and number of accessible projects.

!!! warning "API Token, Not Password"
    Atlassian Cloud requires an API token for REST API access. Your Atlassian account password will not work. API tokens are managed at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens).

## Operations

MEHO registers 8 operations for Jira (4 READ, 4 WRITE):

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `search_issues` | READ | Search issues using structured filters (project, status, type, assignee, priority, labels, text). Builds JQL internally -- no JQL knowledge needed |
| `get_recent_changes` | READ | Get issues recently created or updated in a project within a configurable time window |
| `get_issue` | READ | Get full issue details including comments. Description and comments are returned as clean markdown (ADF converted automatically). Custom field names are human-readable |
| `list_projects` | READ | List accessible projects with key, name, and type |
| `search_by_jql` | WRITE | Execute raw JQL for complex queries the structured search cannot express. Requires approval because arbitrary JQL can be expensive |
| `create_issue` | WRITE | Create a new issue with markdown description (auto-converted to Atlassian Document Format) |
| `add_comment` | WRITE | Add a comment to an issue with markdown body (auto-converted to ADF) |
| `transition_issue` | WRITE | Change issue status through its workflow. Fetches available transitions and matches by target status name |

!!! info "Markdown In, ADF Out"
    MEHO's Jira connector handles Atlassian Document Format (ADF) transparently. When reading, ADF is converted to clean markdown. When writing, markdown is auto-converted to ADF. The agent never sees or produces raw ADF.

## Example Queries

Ask MEHO questions like:

- "Show me all open bugs assigned to me"
- "Create a Jira ticket for the memory leak in the checkout service"
- "What's the status of PROJ-123?"
- "List all high-priority tickets in the current sprint"
- "Find all critical bugs created in the last week"
- "Add a comment to PROJ-456 with the investigation results"
- "Move PROJ-789 to In Progress"
- "What projects do I have access to?"
- "Search for issues with the label 'incident' in the OPS project"
- "Show me recent changes in the PLATFORM project from the last 12 hours"

## Topology

Jira does not contribute topology entities. It functions as a workflow connector -- operators use it to create, update, and track issues related to findings from infrastructure and observability connectors.

## Troubleshooting

### API Token vs Password

**Symptom:** Connector creation fails with 401 Unauthorized.
**Cause:** Using an Atlassian account password instead of an API token.
**Fix:** Generate an API token at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens). API tokens are separate from your account password.

### Jira Cloud vs Jira Server/Data Center

**Symptom:** Connector creation fails with connection errors or unexpected API responses.
**Cause:** MEHO's Jira connector uses the Jira Cloud REST API v3. Jira Server and Data Center use different API versions.
**Fix:** MEHO currently supports Jira Cloud only. The site URL should be in the format `https://yourcompany.atlassian.net`.

### Project Key Format

**Symptom:** `search_issues` returns no results when filtering by project.
**Cause:** The project key is case-sensitive and must match exactly (e.g., `PROJ`, not `proj`).
**Fix:** Use `list_projects` first to discover available projects and their exact keys.

### Custom Fields Show as customfield_XXXXX

**Symptom:** Issue details show raw custom field IDs instead of readable names.
**Cause:** This should not happen -- the connector resolves custom field names automatically.
**Fix:** If you see raw field IDs, report it as a bug. The connector calls `/rest/api/3/field` to build a field name map at connection time.

### JQL Search Requires Approval

**Symptom:** MEHO asks for approval when you request a complex search.
**Cause:** The `search_by_jql` operation is classified as WRITE because arbitrary JQL can be expensive (full table scans). MEHO tries structured search first and only falls back to raw JQL when needed.
**Fix:** Approve the operation if the JQL looks reasonable. For common queries (by project, status, assignee, priority), the structured `search_issues` operation handles them without approval.
