# Troubleshooting

> Last verified: v2.0

Common issues, their causes, and how to fix them. Organized by problem category.

---

## Connection Issues

### Connector can't connect -- authentication failure

!!! warning "Symptom"
    Connector test returns `401 Unauthorized` or `403 Forbidden`. MEHO reports "Authentication failed" when trying to use a connector.

**Cause:** Credentials are incorrect, expired, or have insufficient permissions.

**Fix:**

1. Verify the credentials in **Connectors > [connector name] > Edit**
2. For token-based connectors (Kubernetes, ArgoCD, GitHub), check that the token hasn't expired
3. For Atlassian connectors (Jira, Confluence), verify you're using an API token, not your account password
4. For observability connectors (Prometheus, Loki, Tempo, Alertmanager), check if authentication is required -- many installations run without auth behind a reverse proxy

!!! tip "Quick test"
    Use the **Test Connection** button on the connector edit page. It performs a lightweight health check that validates auth without running full operations.

### Connector can't connect -- network/SSL errors

!!! warning "Symptom"
    Connector test returns `ConnectionError`, `SSLError`, or `TimeoutError`. MEHO reports "Could not reach [system]".

**Cause:** The MEHO backend container can't reach the target system. Common causes: DNS resolution failure, firewall rules, SSL certificate issues, or the target system is on a private network.

**Fix:**

1. Verify the target URL is reachable from the MEHO backend container:
    ```bash
    docker exec meho-meho-1 curl -v https://your-system-url/health
    ```
2. For self-signed certificates, ensure the CA certificate is mounted in the backend container
3. For private networks, ensure the Docker network has access (may need `network_mode: host` or additional Docker network configuration)
4. Check that the port is correct -- some systems use non-standard ports (e.g., Kubernetes API on 6443, not 443)

### Connector connects but returns empty results

!!! warning "Symptom"
    Connector test succeeds, but queries return no data. MEHO says "No results found" for queries that should return data.

**Cause:** The connector credentials have limited scope (e.g., read access to only certain namespaces, projects, or repositories).

**Fix:**

1. Check the permissions of the service account or API token used by the connector
2. For Kubernetes: verify the service account has `ClusterRole` bindings (not just namespace-scoped `Role`)
3. For GitHub: verify the token has `repo` scope for private repositories
4. For Prometheus/Loki: check if the connector is pointed at the correct datasource or tenant

---

## Agent Issues

### Agent not using the right connector

!!! warning "Symptom"
    MEHO queries the wrong system or uses a connector you didn't intend. For example, asking about "production pods" hits a staging Kubernetes cluster.

**Cause:** Multiple connectors of the same type exist, and the agent selected the wrong one. The agent uses connector names and descriptions to determine which to query.

**Fix:**

1. Give connectors descriptive names: "Production Kubernetes" vs "Staging Kubernetes" rather than "K8s 1" and "K8s 2"
2. Use `@connector_name` mentions in your message to explicitly target a connector
3. Update connector descriptions to clearly state their scope (environment, region, team)

### Agent makes unexpected tool calls

!!! warning "Symptom"
    MEHO queries systems you didn't ask about, or performs operations that seem unrelated to your question.

**Cause:** The agent's ReAct reasoning loop determined that additional context from other systems would help answer your question. This is by design -- cross-system reasoning is MEHO's core value.

**Fix:**

1. If the additional queries are helpful but slow, this is expected behavior. MEHO traces problems across systems automatically.
2. If the queries are genuinely irrelevant, be more specific in your question. Instead of "what's wrong?", try "what's the CPU usage on the checkout-service pods?"
3. Use **Ask mode** (toggle in chat input) for simple questions that don't need investigation. Ask mode queries the knowledge base without invoking connectors.

### Agent hits context limits

!!! warning "Symptom"
    MEHO's response is cut short or it says "I've reached my context limit". Investigations with many data-heavy queries may hit this.

**Cause:** The combined data from multiple connector queries exceeded the LLM's context window, even after reduction.

**Fix:**

1. Ask more specific questions to reduce the amount of data returned
2. Break complex investigations into smaller steps: first identify the problem area, then drill down
3. Start a new session if the current one has accumulated too much context -- MEHO persists session state, so the next session can reference previous findings via knowledge base

!!! tip "Context monitoring"
    The chat input area shows a context usage indicator. When it approaches the limit, MEHO automatically compacts earlier messages to free space.

---

## Data Issues

### Stale or cached data

!!! warning "Symptom"
    MEHO returns data that doesn't match what you see in the source system. Results seem outdated.

**Cause:** MEHO caches connector responses in DuckDB/Parquet for SQL reduction within a session. If the source data changed after the initial query, the cache still holds the old data.

**Fix:**

1. Ask MEHO to "refresh" or "re-query" the data -- it will make a new API call instead of using the cache
2. Start a new session for a fresh investigation
3. Note that some connectors have built-in time ranges (e.g., Prometheus queries default to the last hour). Specify explicit time ranges in your question if needed.

### Large response handling

!!! warning "Symptom"
    Queries that return very large datasets (thousands of pods, millions of log lines) are slow or cause errors.

**Cause:** The target system returned more data than expected. MEHO's data pipeline handles this, but very large responses take longer to normalize and reduce.

**Fix:**

1. Add filters to your question: "show me pods in the `checkout` namespace" instead of "show me all pods"
2. Use time ranges for log queries: "logs from the last 30 minutes" instead of "show me the logs"
3. For Prometheus, prefer instant queries over range queries when you only need current values

---

## Authentication Issues

### Keycloak token expiry -- 401 responses

!!! danger "Symptom"
    The MEHO UI suddenly starts showing 401 errors. All API calls fail. The page may redirect to the Keycloak login screen.

**Cause:** The Keycloak access token has expired and automatic token refresh failed. This can happen if Keycloak is temporarily unreachable or if the refresh token has also expired (default: 30 minutes idle).

**Fix:**

1. Refresh the browser page -- keycloak-js will attempt to re-authenticate
2. If the login page appears, log in again. Your session data (chat history, investigation state) is persisted and will be restored
3. If Keycloak itself is down, check the container: `docker logs meho-keycloak`

### CORS errors in browser console

!!! danger "Symptom"
    Browser console shows `Access-Control-Allow-Origin` errors. API calls from the frontend fail with CORS rejection.

**Cause:** The frontend URL is not in the backend's allowed origins list.

**Fix:**

1. Check `.env` for `CORS_ORIGINS` -- it must include the frontend URL (default: `["http://localhost:5173"]`)
2. If running the frontend on a different port or domain, update `CORS_ORIGINS` accordingly
3. Restart the backend after changing CORS settings: `./scripts/dev-env.sh restart meho`

### 403 Forbidden on specific operations

!!! warning "Symptom"
    MEHO can read data from a connector but fails when attempting write operations. Error: "Forbidden" or "Insufficient permissions".

**Cause:** The connector's credentials have read-only access. Write and destructive operations require elevated permissions.

**Fix:**

1. This is often intentional -- many organizations configure read-only credentials for safety
2. If write access is needed, update the connector credentials with a service account that has appropriate permissions
3. Check the connector's documentation page for the exact permissions required for each operation

---

## Deployment Issues

### Docker Compose startup failures

!!! danger "Symptom"
    `./scripts/dev-env.sh up` fails. Containers crash on startup or fail health checks.

**Cause:** Missing environment variables, port conflicts, or insufficient resources.

**Fix:**

1. **Missing `.env` file:** Copy `env.example` to `.env` and set the required secrets:
    ```bash
    cp env.example .env
    # Edit .env: set ANTHROPIC_API_KEY, VOYAGE_API_KEY, CREDENTIAL_ENCRYPTION_KEY
    ```

2. **Port conflicts:** Check that ports 5432, 6379, 8000, 8080, 5173, 9000, 5341 are not in use:
    ```bash
    lsof -i :8000  # Check if port is occupied
    ```

3. **Insufficient memory:** The full stack requires approximately 4GB of RAM. Docker Desktop default is often 2GB.
    - macOS/Windows: Docker Desktop > Settings > Resources > Memory > Set to 6GB+

4. **Keycloak slow startup:** Keycloak can take 60-90 seconds to initialize on first run. The health check has a 90-second start period, but on slow machines it may need longer. Check logs: `docker logs meho-keycloak`

### Database migration errors

!!! danger "Symptom"
    The backend starts but API calls fail with database errors. Logs show "relation does not exist" or "column not found".

**Cause:** Database migrations haven't run or failed silently.

**Fix:**

1. Always use `./scripts/dev-env.sh up` instead of raw `docker compose up` -- the helper script runs migrations automatically
2. If migrations need to be run manually:
    ```bash
    ./scripts/run-migrations-monolith.sh
    ```
3. If migrations fail with version conflicts, check for stale Alembic version entries:
    ```bash
    docker exec meho-postgres-1 psql -U meho -c "SELECT * FROM alembic_version_meho_knowledge;"
    ```
4. For a clean slate (destroys all data):
    ```bash
    ./scripts/dev-env.sh down --volumes
    ./scripts/dev-env.sh up
    ```

### Backend crashes on startup

!!! danger "Symptom"
    The `meho` container exits immediately or enters a restart loop. Logs show import errors or configuration errors.

**Cause:** Missing or invalid environment variables, or a Python dependency issue.

**Fix:**

1. Check backend logs for the specific error:
    ```bash
    docker logs meho-meho-1
    ```
2. Common causes:
    - `ANTHROPIC_API_KEY` not set or invalid
    - `CREDENTIAL_ENCRYPTION_KEY` not set (must be a valid Fernet key, minimum 32 characters)
    - `DATABASE_URL` pointing to wrong host (should be `postgres` inside Docker network, not `localhost`)
3. Rebuild the image if dependencies changed:
    ```bash
    ./scripts/dev-env.sh up --build
    ```

---

## Getting Help

If you encounter an issue not covered here:

1. **Check the logs:** `./scripts/dev-env.sh logs` shows all service logs. Add a service name for filtered output: `./scripts/dev-env.sh logs meho`
2. **Check connector-specific pages:** Each [connector documentation page](index.md) includes a troubleshooting section for connector-specific issues
3. **Check the API docs:** The [API Reference](api-reference.md) documents all endpoints and their expected responses
