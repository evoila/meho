# REST (OpenAPI)

> Last verified: v2.0

The REST connector is MEHO's extensibility story. Connect **any system with an OpenAPI specification** to MEHO, and it automatically discovers all available API endpoints, generates callable operations, and enables the agent to interact with the system through natural language. No custom connector code required.

This is how MEHO goes from supporting 15 built-in connector types to supporting thousands of systems -- any REST API with an OpenAPI spec can be integrated in minutes.

## How It Works

1. **Upload an OpenAPI spec** -- Provide an OpenAPI 3.x specification (JSON or YAML) when creating the connector. MEHO parses the spec and stores it.
2. **Automatic endpoint discovery** -- MEHO's `OpenAPIParser` extracts every endpoint from the spec: HTTP method, path, parameters (path, query, body), response schemas, and operation descriptions.
3. **Operation registration** -- Each discovered endpoint is registered as an `EndpointDescriptor` with:
   - Method and path (e.g., `GET /customers/{id}`)
   - Operation ID and description from the spec
   - Parameter schemas for validation
   - Tags for categorization
   - Safety level classification (safe/caution/dangerous)
4. **Agent interaction** -- The agent discovers available endpoints through hybrid search (BM25 + semantic). When an operator asks a question, MEHO finds the relevant endpoint, collects parameters conversationally, and executes the API call.

```
OpenAPI Spec --> Parser --> EndpointDescriptors --> Knowledge Index --> Agent Discovery
                                                                           |
                                                                     Natural Language
                                                                      Query/Action
```

## Authentication

The REST connector supports configurable authentication to handle any API's auth scheme:

| Method | Description | Use Case |
|--------|-------------|----------|
| None | No authentication | Public APIs, internal services without auth |
| API Key | API key in header or query parameter | Most SaaS APIs (e.g., Stripe, Twilio) |
| Basic Auth | HTTP Basic (username:password) | Internal services, legacy APIs |
| OAuth2 | OAuth2 flows (client credentials, etc.) | Modern SaaS platforms |
| Session | Login-based sessions with automatic refresh | Enterprise systems with session-based auth |

**Setup:**

1. Create a REST connector in MEHO and provide the target API's base URL.
2. Upload or point to the OpenAPI specification (JSON or YAML).
3. Configure the authentication method and provide credentials.
4. MEHO parses the spec, discovers endpoints, and registers them as operations.
5. Optionally, review discovered endpoints in the MEHO UI to enable/disable specific operations and adjust safety levels.

## Operations

Operations are **dynamically generated from the OpenAPI spec** -- there is no hardcoded operation list. The number and nature of operations depends entirely on the API being connected.

For each endpoint in the spec, MEHO creates an operation with:

| Property | Source |
|----------|--------|
| Operation ID | `operationId` from spec, or generated from method + path |
| Name | `summary` from spec |
| Description | `description` from spec |
| Parameters | Extracted from `parameters` and `requestBody` schemas |
| Tags | `tags` from spec (used for categorization and search) |
| Safety Level | Inferred from HTTP method: GET = `safe`, POST/PUT/PATCH = `caution`, DELETE = `dangerous` |

**Safety levels:**

| HTTP Method | Default Safety | Requires Approval |
|-------------|---------------|-------------------|
| GET, HEAD, OPTIONS | Safe (READ) | No |
| POST, PUT, PATCH | Caution (WRITE) | Configurable |
| DELETE | Dangerous (DESTRUCTIVE) | Yes |

Administrators can override safety levels and approval requirements per endpoint through the MEHO UI.

## Endpoint Management

Each discovered endpoint has rich metadata stored in the database:

- **Parameter schemas** -- Path params, query params, and request body schemas from the OpenAPI spec
- **LLM instructions** -- Auto-generated guidance for the agent on how to collect parameters conversationally
- **Custom descriptions** -- Admin-written descriptions that supplement or override the spec's documentation
- **Usage examples** -- Example payloads for common use cases
- **Agent notes** -- Learning from previous interactions (common errors, success patterns)
- **Activation status** -- Enable or disable individual endpoints without removing them

## Example Queries

Since REST connectors can represent any API, example queries depend on the connected system. Here are generic patterns:

- "List all customers in the CRM system"
- "Get the details of order #12345"
- "What inventory items are low in stock?"
- "Create a new support ticket for the billing issue"
- "Update the shipping address for customer ABC"
- "Show me the last 10 transactions"
- "What payment methods are available?"
- "Search for products matching 'wireless headphones'"
- "Get the account balance for organization XYZ"
- "Delete the expired session tokens"

## Topology

REST connectors do not contribute topology entities. They are generic connectors designed for extending MEHO's reach to any API-accessible system.

## Troubleshooting

### OpenAPI Spec Validation Errors

**Symptom:** Connector creation fails with spec parsing errors.
**Cause:** The OpenAPI specification has validation issues (missing required fields, invalid schema references, unsupported spec version).
**Fix:** Validate your OpenAPI spec using an online validator like [editor.swagger.io](https://editor.swagger.io). MEHO supports OpenAPI 3.0.x and 3.1.x. Ensure all `$ref` references resolve correctly.

### Auth Configuration Issues

**Symptom:** API calls return 401 Unauthorized after successful connector creation.
**Cause:** The authentication method or credentials are incorrect for the target API.
**Fix:** Verify the auth type matches what the API expects. For API key auth, confirm the header name (e.g., `Authorization`, `X-API-Key`). For OAuth2, verify client credentials and token endpoint.

### Endpoint URL Rewriting

**Symptom:** API calls go to the wrong URL or return 404.
**Cause:** The OpenAPI spec's `servers` array may define a different base URL than the one configured in MEHO.
**Fix:** The base URL configured during connector creation overrides the spec's server URLs. Ensure the base URL is correct and includes the API prefix if needed (e.g., `https://api.example.com/v2`).

### Too Many Endpoints Discovered

**Symptom:** The spec generates hundreds of endpoints, making it hard for the agent to find the right one.
**Cause:** Large APIs can have hundreds of endpoints. The agent's search may return too many matches.
**Fix:** Use the MEHO UI to disable endpoints that are not relevant for your use case. Add custom descriptions to key endpoints to improve search relevance. Use tags in your OpenAPI spec to group related endpoints.

### Safety Level Overrides

**Symptom:** A POST endpoint that only reads data still requires approval.
**Cause:** MEHO defaults POST operations to "caution" safety level based on HTTP method.
**Fix:** Use the MEHO UI to adjust the safety level for specific endpoints. For example, a `POST /search` endpoint that only reads data can be set to "safe".
