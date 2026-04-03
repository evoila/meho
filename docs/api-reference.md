# API Reference

> Last verified: v2.0

!!! info "Auto-generated documentation"
    This reference is auto-generated from MEHO's OpenAPI specification. To regenerate with the latest API changes, run the app and export:
    ```bash
    curl -s http://localhost:8000/openapi.json > docs/openapi.json
    ```

MEHO exposes a RESTful API built with [FastAPI](https://fastapi.tiangolo.com/). All endpoints require Keycloak JWT authentication unless otherwise noted. The API follows standard HTTP conventions: JSON request/response bodies, proper status codes, and consistent error formats.

## Authentication

All API requests require a valid Keycloak JWT bearer token in the `Authorization` header:

```
Authorization: Bearer <access_token>
```

Tokens are obtained through the Keycloak OIDC flow. The frontend handles this automatically via `keycloak-js`. For direct API access, use the Keycloak token endpoint:

```bash
curl -X POST "http://localhost:8080/realms/example-tenant/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=meho-frontend" \
  -d "username=YOUR_USER" \
  -d "password=YOUR_PASSWORD"
```

## Base URL

| Environment | URL |
|-------------|-----|
| Local development | `http://localhost:8000` |
| Interactive docs (Swagger) | `http://localhost:8000/docs` |
| OpenAPI spec (JSON) | `http://localhost:8000/openapi.json` |

## API Groups

The API is organized into the following groups:

| Group | Prefix | Description |
|-------|--------|-------------|
| Chat | `/api/chat` | Session management, message sending, SSE streaming |
| Connectors | `/api/connectors` | Connector CRUD, operations, test connection |
| Knowledge | `/api/knowledge` | Document upload, search, knowledge base management |
| Topology | `/api/topology` | Entity graph, resolution, auto-discovery |
| Memory | `/api/memory` | Agent memory entries, extraction history |
| Ingestion | `/api/ingestion` | Document processing jobs, status tracking |
| Health | `/health` | Service health check (no auth required) |

## Specification

[OAD(./openapi.json)]
