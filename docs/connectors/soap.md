# SOAP (WSDL)

> Last verified: v2.0

The SOAP connector extends MEHO's reach into enterprise systems that expose WSDL-based APIs. Many critical enterprise platforms -- VMware vSphere, ServiceNow, SAP, and legacy banking systems -- use SOAP web services. MEHO automatically discovers operations from WSDL definitions, maps XML Schema types to JSON for unified handling, and manages complex authentication patterns like WS-Security and session-based login flows.

## How It Works

1. **Provide a WSDL URL** -- Point MEHO to a WSDL endpoint (e.g., `https://vcenter.example.com/sdk/vimService.wsdl`). MEHO fetches and parses the WSDL using the `zeep` library.
2. **Automatic operation discovery** -- MEHO's `SOAPSchemaIngester` extracts every operation from the WSDL: service names, port bindings, operation names, SOAP actions, and input/output schemas.
3. **Type mapping** -- XML Schema complex types are mapped to JSON Schema format, so the agent can understand and construct parameters in a natural JSON-like format instead of raw XML.
4. **Type definition indexing** -- Complex type definitions are indexed as knowledge chunks for BM25/hybrid search, so the agent can discover what types and properties exist in the service.
5. **Agent interaction** -- The agent discovers SOAP operations through hybrid search, collects parameters conversationally, and the `SOAPClient` handles XML serialization, SOAP envelope construction, and response deserialization transparently.

```
WSDL --> Ingester --> SoapOperationDescriptors --> Knowledge Index --> Agent Discovery
                  \-> SoapTypeDescriptors     -->                        |
                                                                   Natural Language
                                                                    Query/Action
```

## Authentication

The SOAP connector supports multiple authentication patterns used by enterprise SOAP services:

| Method | Credential Fields | Use Case |
|--------|------------------|----------|
| None | -- | Internal services, development environments |
| Basic Auth | `username`, `password` | HTTP Basic Authentication over HTTPS |
| WS-Security | `ws_security_username`, `ws_security_password` | WS-Security UsernameToken with optional digest, timestamp, and nonce |
| Session | `login_operation`, `logout_operation`, `session_cookie_name` | Login-based session management (e.g., VMware VIM `SessionManager.Login`) |
| Certificate | `client_cert_path`, `client_key_path`, `ca_cert_path` | Client certificate mutual TLS authentication |

**WS-Security options:**

| Option | Default | Description |
|--------|---------|-------------|
| Use password digest | `false` | Hash the password instead of sending plain text |
| Use timestamp | `true` | Add a Timestamp element to prevent replay attacks |
| Timestamp TTL | 300s | Timestamp validity window (5 minutes) |
| Use nonce | `true` | Add a Nonce to prevent replay attacks |

**Setup:**

1. Create a SOAP connector in MEHO and provide the WSDL URL.
2. Select the authentication type and provide credentials.
3. Optionally specify an **endpoint override** if the WSDL defines a different endpoint URL than the actual service location (common with VMware, which often has `localhost` in the WSDL).
4. MEHO fetches the WSDL, discovers all operations and types, and indexes them for agent search.

!!! info "VMware vSphere"
    For VMware, use Session authentication with `login_operation: "SessionManager.Login"` and `logout_operation: "SessionManager.Logout"`. The SOAP connector handles the cookie-based session lifecycle automatically.

## Operations

Operations are **dynamically generated from the WSDL** -- there is no hardcoded operation list. The number and nature of operations depends entirely on the WSDL being connected.

For each operation in the WSDL, MEHO creates a `SoapOperationDescriptor` with:

| Property | Source |
|----------|--------|
| Name | `{ServiceName}.{OperationName}` (e.g., `VimService.RetrieveProperties`) |
| Service name | From WSDL `<service>` element |
| Port name | From WSDL `<port>` element |
| SOAP action | From WSDL binding `<operation>` `soapAction` attribute |
| Input schema | JSON Schema mapped from XML Schema input message |
| Output schema | JSON Schema mapped from XML Schema output message |
| Style | Document or RPC from WSDL binding |
| Safety level | Defaults to `caution` for SOAP (operations are harder to classify than REST) |

**Safety classification:**

SOAP operations default to `caution` because WSDL does not inherently distinguish read from write operations the way HTTP methods do. Administrators can adjust safety levels per operation through the MEHO UI.

## Type Discovery

Beyond operations, MEHO indexes complex type definitions from the WSDL schema:

| Property | Description |
|----------|-------------|
| Type name | XML Schema complexType name (e.g., `ClusterComputeResource`) |
| Namespace | XML namespace (e.g., `urn:vim25`) |
| Base type | Parent type if the type uses inheritance |
| Properties | List of properties with name, type, array/required flags |

These types are indexed as knowledge chunks, so the agent can ask questions like "What properties does a VirtualMachine have?" and get accurate answers from the actual WSDL schema.

## Example Queries

Since SOAP connectors represent any WSDL-based system, example queries depend on the connected service. Here are patterns for common enterprise SOAP systems:

- "List all virtual machines managed by vCenter"
- "What's the status of the production ESXi host?"
- "Show me the cluster resource utilization"
- "Get the properties of the datastore cluster"
- "List all active incidents in ServiceNow"
- "What change requests are pending approval?"
- "Check the SAP order status for order 4500012345"
- "Get the account balance from the core banking system"
- "List available services in the WSDL"
- "What properties does the VirtualMachine type have?"

## Topology

SOAP connectors do not contribute topology entities directly. They are generic connectors designed for integrating enterprise systems. However, the data returned from SOAP operations can inform MEHO's cross-system reasoning (e.g., VMware VMs linked to Kubernetes nodes through provider IDs).

## Troubleshooting

### WSDL Parsing Failures

**Symptom:** Connector creation fails with WSDL parsing errors.
**Cause:** The WSDL may be malformed, use unsupported schema features, or have unresolvable imports.
**Fix:** Verify the WSDL URL is accessible from the MEHO server. If the WSDL imports external schemas (XSD files), ensure those URLs are also reachable. Try loading the WSDL in a SOAP client like SoapUI to validate it independently.

### Namespace Handling

**Symptom:** SOAP calls fail with "element not found" or namespace errors.
**Cause:** SOAP services are sensitive to XML namespace prefixes. The WSDL may define multiple namespaces for different parts of the schema.
**Fix:** MEHO's zeep-based client handles namespace mapping automatically. If you encounter namespace issues, check that the target namespace in the WSDL matches the service's expectations. The `namespace` field in each operation descriptor shows which namespace is used.

### Complex Type Mapping

**Symptom:** The agent constructs parameters incorrectly for operations with complex nested types.
**Cause:** Deeply nested XML Schema types can be challenging to map to JSON accurately.
**Fix:** Use the "What properties does [TypeName] have?" query pattern to discover the expected structure. The agent uses indexed type definitions to understand nested types. If mapping issues persist, check the `input_schema` field of the operation descriptor for the expected JSON structure.

### WS-Security Configuration

**Symptom:** Operations fail with security-related SOAP faults.
**Cause:** WS-Security configuration (digest mode, timestamp, nonce) may not match the server's expectations.
**Fix:** Check the server's WS-Security requirements:
- Some servers require password digest (hash), others require plain text.
- Timestamp validity window must be sufficient for network latency.
- Some servers do not accept nonce elements.
Adjust the WS-Security options during connector configuration to match.

### Endpoint Override for VMware

**Symptom:** SOAP calls fail with connection errors even though the WSDL loaded successfully.
**Cause:** Many WSDLs (especially VMware's) define `localhost` or an internal hostname as the service endpoint, which differs from the actual server address.
**Fix:** The SOAP connector automatically derives the endpoint URL from the WSDL URL (stripping the filename). If this derivation is incorrect, use the **endpoint override** setting to specify the correct service endpoint explicitly.

### Session Management

**Symptom:** Operations work for a few minutes then start failing with authentication errors.
**Cause:** Session-based authentication (e.g., VMware VIM) has session timeouts. The session may expire between operations.
**Fix:** The SOAP connector handles session lifecycle automatically (login, cookie tracking, re-login on expiry). If sessions expire too quickly, check the server's session timeout configuration. Ensure the `session_cookie_name` matches the actual cookie used by the service (e.g., `vmware_soap_session` for vSphere).
