# Phase 0: Pre-Migration Analysis

**Date:** 2025-12-09  
**Status:** COMPLETE  
**Branch:** `refactor/modular-monolith`

---

## Test Baseline

### Critical Tests (Mandatory)
- **Smoke tests:** 33 passed
- **Contract tests:** 117 passed (2 skipped)
- **Total critical:** **150 passing**

### Full Test Suite (Reference)
- Unit: ~457 tests
- Integration: ~315 tests
- E2E: ~103 tests
- **Total: ~1,025 tests**

---

## Current Architecture Analysis

### Service HTTP Clients (To Be Replaced)

| File | Lines | Purpose | Replacement |
|------|-------|---------|-------------|
| `meho_api/http_clients/agent_client.py` | 671 | Agent service HTTP calls | Direct `AgentService` imports |
| `meho_api/http_clients/knowledge_client.py` | 483 | Knowledge service HTTP calls | Direct `KnowledgeService` imports |
| `meho_api/http_clients/openapi_client.py` | 406 | OpenAPI service HTTP calls | Direct `OpenAPIService` imports |
| **Total** | **1,560 lines** | HTTP overhead to be eliminated | **Direct Python calls** |

---

## Cross-Service Dependencies

### meho_agent → meho_knowledge
```python
# meho_agent/dependencies.py
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.bm25_hybrid_service import BM25HybridService
from meho_knowledge.bm25_service import BM25Service
```
**Impact:** Agent directly imports KnowledgeStore - already prepared for monolith!

### meho_agent → meho_openapi
```python
# meho_agent/dependencies.py
from meho_openapi.repository import ConnectorRepository, EndpointDescriptorRepository
from meho_openapi.user_credentials import UserCredentialRepository
from meho_openapi.http_client import GenericHTTPClient

# meho_agent/react/tool_handlers.py (1224 lines)
from meho_openapi.repository import ConnectorRepository, ConnectorOperationRepository
from meho_openapi.bm25_operation_search import OperationBM25Service
from meho_openapi.soap import SOAPSchemaIngester, SOAPConnectorConfig
from meho_openapi.connectors import get_pooled_connector
```
**Impact:** Agent uses OpenAPI repositories directly - good for monolith!

### meho_api → meho_agent
```python
# meho_api/routes_chat.py
from meho_agent.session_state import AgentSessionState
from meho_agent.react import MEHOReActGraph
from meho_agent.unified_executor import get_unified_executor
from meho_agent.approval import ApprovalStore

# meho_api/routes_recipes.py
from meho_agent.recipes import RecipeRepository
from meho_agent.data_reduction import DataQuery
```
**Impact:** BFF already imports agent directly - prepared for monolith!

### meho_api → meho_knowledge
```python
# meho_api/routes_connectors.py
from meho_knowledge.object_storage import ObjectStorage
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import get_embedding_provider

# meho_api/routes_knowledge.py
from meho_knowledge.job_repository import IngestionJobRepository
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.knowledge_store import KnowledgeStore
```
**Impact:** BFF mixes HTTP clients and direct imports - needs cleanup!

---

## Database Architecture

### Migration Files by Service
- **meho_knowledge:** 8 migration files
- **meho_openapi:** 7 migration files
- **meho_agent:** 13 migration files
- **meho_ingestion:** 1 migration file
- **Total:** 29 migration files

**Strategy:** Keep all migrations in original service directories during transition, consolidate later.

### Table Ownership (Confirmed)

| Service | Tables |
|---------|--------|
| meho_knowledge | `knowledge_chunk`, `ingestion_job`, vector indexes |
| meho_openapi | `connector`, `endpoint_descriptor`, `user_credential`, `openapi_spec`, `soap_type`, `connector_operation` |
| meho_agent | `workflow`, `chat_session`, `chat_message`, `recipe`, `pending_approval`, `tenant_config` |
| meho_ingestion | `ingestion_job` (may overlap with knowledge) |

---

## HTTP Routes Analysis

### Total Routes: 102 endpoints across services

**Breakdown:**
- **meho_api/** (BFF): ~60 routes (chat, connectors, knowledge, recipes, auth, admin)
- **meho_knowledge/routes.py**: ~15 routes
- **meho_openapi/routes.py**: ~12 routes
- **meho_agent/routes.py**: ~10 routes
- **meho_ingestion/routes.py**: ~5 routes

**Migration Strategy:**
1. Keep internal service routes for backwards compatibility (optional)
2. Main API traffic goes through BFF routes (already the case)
3. Eventually deprecate internal service routes

---

## Key Files Requiring Careful Migration

### Large/Complex Files (>1000 lines)

| File | Lines | Complexity | Risk |
|------|-------|------------|------|
| `meho_agent/dependencies.py` | 2,113 | High - Core tool implementations | **HIGH** |
| `meho_agent/unified_executor.py` | 1,702 | High - Agent execution engine | **HIGH** |
| `meho_api/routes_connectors.py` | 2,790 | Medium - Large route file | **MEDIUM** |
| `meho_agent/react/tool_handlers.py` | 1,224 | High - Tool execution logic | **HIGH** |
| `meho_openapi/repository.py` | 1,138 | Medium - Complex DB operations | **MEDIUM** |

### Critical Dependencies Pattern

**CURRENT (HTTP):**
```python
# meho_api/routes_chat.py
from meho_api.http_clients.knowledge_client import get_knowledge_client
result = await get_knowledge_client().search(query, user_context)
```

**FUTURE (Direct):**
```python
# meho_app/api/routes_chat.py
from meho_app.modules.knowledge import get_knowledge_service
result = await get_knowledge_service(session).search(query, ...)
```

---

## Migration Advantages (Confirmed)

### 1. Already Direct Imports!
- **meho_agent** already imports from meho_knowledge and meho_openapi
- **meho_api** already imports from meho_agent
- Only HTTP clients need to be replaced!

### 2. Shared Database Session
All services already use PostgreSQL - no coordination issues!

### 3. Less HTTP Overhead
- Remove 1,560 lines of HTTP client code
- Eliminate serialization/deserialization
- Reduce latency significantly

### 4. Simplified Development
- Single codebase to navigate
- Direct debugging (no HTTP boundaries)
- Easier refactoring

---

## Rollback Safety

### Backups Created
- ✅ `docker-compose.dev.yml.backup`
- ✅ Git branch: `refactor/modular-monolith`
- ✅ Test baseline: 150 critical tests passing

### Rollback Steps (If Needed)
```bash
git checkout pivot/react-graph-architecture
cp docker-compose.dev.yml.backup docker-compose.dev.yml
./scripts/dev-env.sh up
./scripts/run-critical-tests.sh --fast
```

---

## Phase 0 Deliverables

- ✅ Critical test baseline: 150 passing
- ✅ HTTP client analysis: 1,560 lines to eliminate
- ✅ Cross-service imports documented: Mostly direct already!
- ✅ Database tables documented: 29 migrations
- ✅ Routes counted: 102 endpoints
- ✅ Large files identified: 5 high-risk files
- ✅ Migration strategy confirmed: Incremental, with test checkpoints
- ✅ Rollback plan established

---

## Next Phase: Phase 1

**Create unified application structure:**
- Create `meho_app/` directory
- Create `meho_app/main.py` (unified FastAPI app)
- Create `meho_app/database.py` (unified DB config)
- Create `meho_app/dependencies.py` (shared deps)
- Update `pyproject.toml`

**Duration:** 0.5 day  
**Risk:** LOW (no code changes, just scaffolding)

