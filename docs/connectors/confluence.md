# Confluence

> Last verified: v2.0

Confluence is the knowledge management and documentation platform. MEHO connects to Confluence Cloud to let operators search for runbooks, retrieve page content, create documentation from investigation findings, and check for recent documentation changes -- turning your team's knowledge base into a live resource during incident response.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Email + API Token | `email`, `api_token` | Atlassian Cloud API token (not password) |

Confluence and Jira share the same `AtlassianHTTPConnector` base class, using identical authentication: your Atlassian account email paired with an API token.

**Setup:**

1. Go to [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and create an API token (or reuse the one created for Jira).
2. Provide your Confluence Cloud site URL (e.g., `https://yourcompany.atlassian.net`), your Atlassian account email, and the API token when creating the connector in MEHO.
3. MEHO verifies connectivity by calling `/wiki/api/v2/spaces` and reports back the authenticated user and number of accessible spaces.

!!! tip "Shared Credentials"
    If you already created a Jira connector, the same Atlassian email and API token work for Confluence. You still need to create a separate connector instance in MEHO since they are different services.

## Operations

MEHO registers 8 operations for Confluence (4 READ, 4 WRITE):

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `search_pages` | READ | Search pages using structured filters (space, title, labels, content type, modified date, full-text). Builds CQL internally |
| `get_recent_changes` | READ | Get recently modified pages within a configurable time window. Useful for checking if runbooks changed before an incident |
| `get_page` | READ | Get full page content with metadata. Body is converted from ADF to markdown automatically. Includes title, labels, version info, and child pages |
| `list_spaces` | READ | List accessible spaces with keys, names, types, and URLs |
| `search_by_cql` | WRITE | Execute raw CQL for complex queries the structured search cannot express. Requires approval because arbitrary CQL can be expensive |
| `create_page` | WRITE | Create a new page with markdown content (auto-converted to ADF). Can specify parent page for hierarchy |
| `update_page` | WRITE | Update page content. Version handling is automatic -- fetches current version, increments, and retries on conflict |
| `add_comment` | WRITE | Add a footer comment to a page with markdown body (auto-converted to ADF) |

!!! info "Markdown In, ADF Out"
    Like the Jira connector, Confluence handles Atlassian Document Format transparently. Content is read as markdown and written as markdown, with ADF conversion handled behind the scenes.

!!! info "Dual API Strategy"
    The Confluence connector uses the v1 API for CQL search (which has richer query capabilities) and the v2 API for page CRUD operations (which has a cleaner interface). This is handled transparently.

## Example Queries

Ask MEHO questions like:

- "Find the runbook for database failover"
- "Show me pages updated in the last week in the DevOps space"
- "What's the content of the incident response page?"
- "Search for pages labeled 'runbook' in the OPS space"
- "Create a post-mortem page for today's outage"
- "Update the deployment checklist with the new steps"
- "What Confluence spaces do I have access to?"
- "Were any runbooks modified in the last 24 hours?"
- "Find documentation about the authentication service"
- "Add an investigation comment to the troubleshooting page"

## Topology

Confluence does not contribute topology entities. It functions as a knowledge connector -- operators use it to find relevant documentation, runbooks, and procedures during investigations, and to create or update documentation based on findings.

## Troubleshooting

### API Token Permissions

**Symptom:** Operations return 403 Forbidden for certain spaces or pages.
**Cause:** The Atlassian account associated with the API token does not have access to the requested space or page.
**Fix:** Confluence permissions are managed at the space level. Ensure the account has at least "Can view" permission on spaces you want MEHO to read, and "Can edit" permission for write operations.

### Space Key Format

**Symptom:** `search_pages` with a space filter returns no results.
**Cause:** Space keys are case-sensitive and typically uppercase (e.g., `OPS`, `DEV`, `PLATFORM`).
**Fix:** Use `list_spaces` first to discover available spaces and their exact keys.

### Page Hierarchy

**Symptom:** Created pages appear at the space root instead of under the expected parent.
**Cause:** The `parent_page_id` parameter was not specified when creating the page.
**Fix:** Use `search_pages` or `get_page` to find the parent page ID, then pass it as `parent_page_id` when creating the page.

### Version Conflict on Update

**Symptom:** `update_page` fails with a version conflict error.
**Cause:** Another user or process updated the page between when MEHO read it and when MEHO tried to write. This is rare because the connector auto-retries once on version conflict.
**Fix:** Retry the update. The connector fetches the latest version number automatically and retries once. If it still fails, someone is actively editing the page -- wait and try again.

### CQL Search Requires Approval

**Symptom:** MEHO asks for approval when you request a complex search.
**Cause:** The `search_by_cql` operation is classified as WRITE because arbitrary CQL can be expensive. MEHO tries structured search first and only falls back to raw CQL when needed.
**Fix:** Approve the operation if the CQL looks reasonable. For common searches (by space, title, labels, date), the structured `search_pages` operation handles them without approval.
