# vulture_whitelist.py
# False-positive whitelist for dynamic dispatch patterns in MEHO.X
#
# Vulture cannot detect usage through framework-level indirection.
# This file declares references that Vulture will recognize as "used".
#
# Categories:
# 1. React Agent tools — loaded dynamically via TOOL_REGISTRY
# 2. PydanticAI agent tools — registered via @agent.tool decorator
# 3. Pydantic model validators — called by pydantic at (de)serialization time
# 4. FastAPI dependency injection — called by FastAPI's DI container
# 5. Alembic migrations — upgrade/downgrade called by alembic CLI
# 6. Connector handler registries — handlers loaded dynamically by connector type
# 7. __all__ re-exports — used by importers, not always directly referenced

# --- 1. React Agent Tool Classes (TOOL_REGISTRY in meho_app/modules/agents/react_agent/tools/) ---
from meho_app.modules.agents.react_agent.tools.call_operation import CallOperationTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.forget_memory import ForgetMemoryTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.invalidate_topology import InvalidateTopologyTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.list_connectors import ListConnectorsTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.lookup_topology import LookupTopologyTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.reduce_data import ReduceDataTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.search_knowledge import SearchKnowledgeTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.search_operations import SearchOperationsTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.search_types import SearchTypesTool  # noqa: F401
from meho_app.modules.agents.react_agent.tools.store_memory import StoreMemoryTool  # noqa: F401

# --- 2. PydanticAI agent tool functions (registered via @agent.tool) ---
# These are defined inline within agent creation functions and registered
# via decorator — Vulture cannot trace the decorator registration.
# File: meho_app/modules/agents/data_reduction/query_generator.py
#   get_available_fields, get_sample_values
# File: meho_app/modules/agents/approval/exceptions.py
#   call_endpoint

# --- 3. Pydantic model validators (called by Pydantic, not directly) ---
# field_validator, model_validator, computed_field decorated methods
# are called by Pydantic during (de)serialization. Vulture sees them as unused.

# --- 4. FastAPI dependency injection (Depends() parameters) ---
# Functions passed to Depends() are called by FastAPI's DI container.
from meho_app.api.dependencies import (  # noqa: F401
    create_agent_dependencies,
    create_agent_state_store,
    create_state_store,
    get_agent_service_dep,
    get_ingestion_service_dep,
    get_knowledge_service_dep,
    get_openapi_service_dep,
)

# --- 5. Alembic migration functions (called by alembic CLI) ---
# Every alembic migration has upgrade() and downgrade() functions.
# They are called by `alembic upgrade head` / `alembic downgrade`.
# These are excluded via the [tool.vulture] exclude config for alembic dirs.

# --- 6. Connector handler registries ---
# Connector handlers are registered per type and loaded dynamically.
# The handler classes themselves appear unused to static analysis.

# --- 7. Annotated type aliases for FastAPI DI ---
# These are used in route function signatures, resolved by FastAPI.
from meho_app.api.dependencies import (  # noqa: F401
    AgentServiceDep,
    CurrentUser,
    DbSession,
    IngestionServiceDep,
    KnowledgeServiceDep,
    OpenAPIServiceDep,
)

# --- 8. __aexit__ parameters (required by async context-manager protocol) ---
# Vulture reports exc_type, exc_val, exc_tb as unused in every __aexit__.
# These are mandatory parameters per Python's context manager protocol.
_.exc_type  # type: ignore[name-defined]  # noqa: F821
_.exc_val  # type: ignore[name-defined]  # noqa: F821
_.exc_tb  # type: ignore[name-defined]  # noqa: F821

# --- 9. Abstract async generator pattern in agent.py ---
# The `if False: yield` at agent.py:147 is a deliberate Python pattern that
# makes an abstract method an async generator. Vulture flags the unreachable yield.
False  # noqa -- deliberate async generator pattern (agent.py if False: yield)
