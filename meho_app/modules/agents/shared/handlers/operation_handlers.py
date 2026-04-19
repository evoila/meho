# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Operation handlers for MEHO ReAct Graph (TASK-97)

Generic tools that work for ALL connector types (REST, SOAP, VMware):
- search_operations_handler: Search for operations in any connector
- call_operation_handler: Execute operations with batch support
- search_types_handler: Search entity type definitions

These handlers route to the appropriate backend based on connector_type.

TRACING: Enhanced with comprehensive OTEL tracing for:
- Tool inputs and outputs
- HTTP request/response bodies
- Connector routing decisions
- Error details
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

from meho_app.core.errors import UpstreamApiError
from meho_app.core.otel import get_logger
from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.graph.graph_state import MEHOGraphState
from meho_app.modules.agents.shared.handlers.tracing import (
    format_for_logging,
    traced_tool_call,
)

logger = get_logger(__name__)

MSG_CONNECTOR_ID_REQUIRED = "connector_id is required"
MSG_CONNECTOR_NOT_FOUND = "Connector not found"
MSG_MEHO_DEPS_NOT_AVAILABLE = "MEHODependencies not available"

# =============================================================================
# SOAP CLIENT CACHE - Keep sessions alive across multiple calls
# =============================================================================
# Key: (connector_id, user_id) -> connected SOAP client
# This allows multiple SOAP operations to share a session
SOAP_CLIENT_CACHE: dict[tuple, Any] = {}


# =============================================================================
# CREDENTIAL RESOLUTION HELPER (Phase 74)
# =============================================================================


async def _resolve_credentials(
    deps: "MEHOGraphDeps", connector_id: str
) -> tuple[dict, str | None]:  # NOSONAR (cognitive complexity)
    """Resolve credentials via CredentialResolver.

    Uses the unified fallback chain instead of checking credential_strategy directly.
    Logs audit events for automated sessions on both success and failure.

    Args:
        deps: Graph dependencies containing MEHODependencies.
        connector_id: UUID of the connector needing credentials.

    Returns:
        Tuple of (credentials_dict, error_json_or_none).
        On success: (credentials, None).
        On failure: ({}, json_error_string).
    """
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.credential_resolver import (
        CredentialNotFoundError,
        CredentialResolver,
        CredentialScopeError,
        SessionType,
    )
    from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )

    meho = deps.meho_deps
    session_type_str = getattr(meho, "session_type", "interactive")

    try:
        session_type_enum = (
            SessionType(session_type_str)
            if session_type_str != "interactive"
            else SessionType.INTERACTIVE
        )
        session_maker = get_session_maker()
        async with session_maker() as session:
            # Phase 75: Check automation_enabled for automated sessions
            if session_type_str != "interactive":
                from sqlalchemy import select

                from meho_app.modules.connectors.models import ConnectorModel

                stmt = select(ConnectorModel).where(ConnectorModel.id == connector_id)
                result = await session.execute(stmt)
                connector_obj = result.scalar_one_or_none()
                if connector_obj and not connector_obj.automation_enabled:
                    return {}, json.dumps(
                        {
                            "error": f"Connector '{connector_obj.name}' is not available for automated sessions. "
                            "An admin has disabled automation for this connector.",
                            "error_code": "AUTOMATION_DISABLED",
                        }
                    )

            cred_repo = UserCredentialRepository(session)
            from meho_app.api.config import get_api_config

            config = get_api_config()
            keycloak_checker = KeycloakUserChecker(
                keycloak_url=config.keycloak_url,
                admin_username=config.keycloak_admin_username,
                admin_password=config.keycloak_admin_password,
            )
            resolver = CredentialResolver(
                cred_repo=cred_repo,
                keycloak_checker=keycloak_checker,
                delegation_flag_callback=getattr(meho, "delegation_flag_callback", None),
            )
            resolved = await resolver.resolve(
                session_type=session_type_enum,
                user_id=meho.user_context.user_id,
                connector_id=connector_id,
                created_by_user_id=getattr(meho, "created_by_user_id", None),
                allowed_connector_ids=getattr(meho, "allowed_connector_ids", None),
                trigger_type=getattr(meho, "trigger_type", None),
                trigger_id=getattr(meho, "trigger_id", None),
                tenant_id=meho.user_context.tenant_id,
                delegation_active=getattr(meho, "delegation_active", True),
            )

            # Phase 75: Mark credential as healthy on successful use
            try:
                from sqlalchemy import select

                from meho_app.modules.connectors.models import UserCredentialModel

                health_stmt = select(UserCredentialModel).where(
                    UserCredentialModel.user_id
                    == (resolved.delegated_by_user_id or meho.user_context.user_id),
                    UserCredentialModel.connector_id == connector_id,
                )
                cred_result = await session.execute(health_stmt)
                cred_obj = cred_result.scalar_one_or_none()
                if cred_obj and cred_obj.credential_health != "healthy":
                    cred_obj.credential_health = "healthy"  # type: ignore[assignment]  # SQLAlchemy ORM attribute
                    cred_obj.credential_health_message = None  # type: ignore[assignment]  # SQLAlchemy ORM attribute
                    cred_obj.credential_health_checked_at = datetime.now(UTC)  # type: ignore[assignment]  # SQLAlchemy ORM attribute
                    await session.commit()
            except Exception:
                logger.debug("Failed to update credential health to healthy", exc_info=True)

            # Audit: log successful credential resolution for automated sessions
            if session_type_str != "interactive":
                from meho_app.modules.audit.service import AuditService

                audit = AuditService(session)
                await audit.log_event(
                    tenant_id=meho.user_context.tenant_id,
                    user_id=meho.user_context.user_id,
                    event_type="automation.credential_resolved",
                    action="resolve",
                    resource_type="connector",
                    resource_id=connector_id,
                    details={
                        "trigger_type": getattr(meho, "trigger_type", None),
                        "trigger_id": getattr(meho, "trigger_id", None),
                        "session_id": meho.session_id,
                        "connector_id": connector_id,
                        "credential_source": resolved.source.value,
                        "delegated_by_user_id": resolved.delegated_by_user_id,
                        "allowed_connector_ids": getattr(meho, "allowed_connector_ids", None),
                    },
                    result="success",
                )
                await session.commit()

            return resolved.credentials, None

    except CredentialScopeError as e:
        # Audit: log scope rejection for automated sessions
        if session_type_str != "interactive":
            try:
                async with session_maker() as session:
                    from meho_app.modules.audit.service import AuditService

                    audit = AuditService(session)
                    await audit.log_event(
                        tenant_id=meho.user_context.tenant_id,
                        user_id=meho.user_context.user_id,
                        event_type="automation.credential_failed",
                        action="resolve",
                        resource_type="connector",
                        resource_id=connector_id,
                        details={
                            "trigger_type": getattr(meho, "trigger_type", None),
                            "trigger_id": getattr(meho, "trigger_id", None),
                            "connector_id": connector_id,
                            "failure_reason": f"scope_rejected: {e}",
                            "allowed_connector_ids": getattr(meho, "allowed_connector_ids", None),
                        },
                        result="failure",
                    )
                    await session.commit()
            except Exception:
                logger.warning("Failed to log credential scope audit event", exc_info=True)
        return {}, json.dumps(
            {"error": f"Connector {connector_id} not in allowed scope: {e.allowed}"}
        )

    except CredentialNotFoundError as e:
        # Phase 75: Mark credentials for this connector as unhealthy
        try:
            from sqlalchemy import select

            from meho_app.modules.connectors.models import UserCredentialModel

            async with session_maker() as health_session:
                health_stmt = select(UserCredentialModel).where(
                    UserCredentialModel.connector_id == connector_id,
                )
                cred_result = await health_session.execute(health_stmt)
                for cred_obj in cred_result.scalars():
                    cred_obj.credential_health = "unhealthy"  # type: ignore[assignment]  # SQLAlchemy ORM attribute
                    cred_obj.credential_health_message = str(e)  # type: ignore[assignment]  # SQLAlchemy ORM attribute
                    cred_obj.credential_health_checked_at = datetime.now(UTC)  # type: ignore[assignment]  # SQLAlchemy ORM attribute
                await health_session.commit()
        except Exception:
            logger.debug("Failed to update credential health status", exc_info=True)

        if session_type_str != "interactive":
            try:
                async with session_maker() as session:
                    from meho_app.modules.audit.service import AuditService

                    audit = AuditService(session)
                    await audit.log_event(
                        tenant_id=meho.user_context.tenant_id,
                        user_id=meho.user_context.user_id,
                        event_type="automation.credential_failed",
                        action="resolve",
                        resource_type="connector",
                        resource_id=connector_id,
                        details={
                            "trigger_type": getattr(meho, "trigger_type", None),
                            "trigger_id": getattr(meho, "trigger_id", None),
                            "connector_id": connector_id,
                            "attempted_sources": e.chain,
                            "failure_reason": str(e),
                            "allowed_connector_ids": getattr(meho, "allowed_connector_ids", None),
                        },
                        result="failure",
                    )
                    await session.commit()
            except Exception:
                logger.warning("Failed to log credential failure audit event", exc_info=True)
        return {}, json.dumps({"error": str(e)})


# =============================================================================
# SEARCH OPERATIONS HANDLER
# =============================================================================


async def search_operations_handler(
    deps: MEHOGraphDeps, args: dict[str, Any]
) -> str:  # NOSONAR (cognitive complexity)
    """
    Search for operations in ANY connector type.

    TASK-97: Generic tool that works for REST, SOAP, and VMware connectors.
    The agent doesn't need to know the connector type - it just searches.

    Routing:
        - REST connectors → search endpoint_descriptor table
        - SOAP connectors → search soap_operation_descriptor table
        - VMware connectors → search connector_operation table

    Args (in args dict):
        connector_id: ID of the connector
        query: Search query for operation names/descriptions
        limit: Max results (default 10)

    Returns:
        JSON list of matching operations
    """
    connector_id = args.get("connector_id")
    query = args.get("query", "")
    limit = args.get("limit", 10)

    logger.info(f"🔍 search_operations: query='{query}', connector={connector_id}")

    async with traced_tool_call(
        "search_operations",
        args,
        connector_id=connector_id,
        user_id=deps.user_id,
        tenant_id=deps.tenant_id,
        session_id=deps.session_id,
    ) as span:
        if not connector_id:
            error_result = json.dumps({"error": MSG_CONNECTOR_ID_REQUIRED})
            span.set_error(MSG_CONNECTOR_ID_REQUIRED)
            return error_result

        if not deps.meho_deps:
            error_result = json.dumps({"error": MSG_MEHO_DEPS_NOT_AVAILABLE})
            span.set_error(MSG_MEHO_DEPS_NOT_AVAILABLE)
            return error_result

        try:
            from meho_app.database import get_session_maker
            from meho_app.modules.connectors.repositories import (
                ConnectorRepository,
            )

            session_maker = get_session_maker()

            async with session_maker() as session:
                # Get connector to check type
                connector_repo = ConnectorRepository(session)
                connector = await connector_repo.get_connector(connector_id)

                if not connector:
                    error_result = json.dumps({"error": MSG_CONNECTOR_NOT_FOUND})
                    span.set_error(MSG_CONNECTOR_NOT_FOUND)
                    return error_result

                # Route based on connector_type
                connector_type = getattr(connector, "connector_type", None) or "rest"
                connector_name = getattr(connector, "name", None)

                # Log routing decision
                span.add_attribute("connector_type", connector_type)
                span.add_attribute("connector_name", connector_name)
                span.add_attribute("routing_to", f"{connector_type}_search")

                logger.info(
                    "search_operations routing",
                    connector_name=connector_name,
                    connector_type=connector_type,
                    query=query,
                )

                if connector_type == "rest":
                    result = await _search_rest_endpoints(deps, connector_id, query, limit)

                elif connector_type == "kubernetes":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "kubernetes"
                    )

                elif connector_type == "vmware":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "vmware"
                    )

                elif connector_type == "proxmox":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "proxmox"
                    )

                elif connector_type == "gcp":
                    result = await _search_typed_operations(deps, connector_id, query, limit, "gcp")

                elif connector_type == "argocd":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "argocd"
                    )

                elif connector_type == "github":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "github"
                    )

                elif connector_type == "prometheus":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "prometheus"
                    )

                elif connector_type == "loki":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "loki"
                    )

                elif connector_type == "tempo":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "tempo"
                    )

                elif connector_type == "alertmanager":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "alertmanager"
                    )

                elif connector_type == "jira":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "jira"
                    )

                elif connector_type == "confluence":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "confluence"
                    )

                elif connector_type == "email":
                    result = await _search_typed_operations(
                        deps, connector_id, query, limit, "email"
                    )

                elif connector_type == "soap":
                    result = await _search_soap_operations(deps, connector, query, limit)

                else:
                    result = json.dumps({"error": f"Unknown connector type: {connector_type}"})
                    span.set_error(f"Unknown connector type: {connector_type}")
                    return result

                # Parse result to log output
                try:
                    result_data = json.loads(result)
                    span.set_output(result_data)

                    # Log result count
                    if isinstance(result_data, list):
                        span.add_attribute("result_count", len(result_data))
                except json.JSONDecodeError:
                    span.set_output({"raw": result[:500]})

                return result

        except Exception as e:
            logger.error(f"❌ search_operations failed: {e}", exc_info=True)
            span.set_error(str(e))
            return json.dumps({"error": str(e)})


async def _search_rest_endpoints(  # NOSONAR (cognitive complexity)
    deps: MEHOGraphDeps, connector_id: str, query: str, limit: int
) -> str:
    """Search REST endpoints using existing BM25 search."""
    if not deps.meho_deps:
        return json.dumps({"error": MSG_MEHO_DEPS_NOT_AVAILABLE})

    results = await deps.meho_deps.search_endpoints(
        connector_id=connector_id, query=query, limit=limit
    )

    logger.info(f"✅ search_operations (rest): found {len(results)} endpoints")
    return json.dumps(results, indent=2, default=str)


async def _search_typed_operations(
    deps: MEHOGraphDeps,
    connector_id: str,
    query: str,
    limit: int,
    connector_type: str,
) -> str:
    """
    Search typed connector operations (VMware, Kubernetes, etc.) using BM25HybridService.

    TASK-126: Uses unified search on knowledge_chunk table with embeddings.
    Operations are ingested with source_type="connector_operation" during sync.

    Features:
    - BM25 with Porter Stemming: "vm" matches "virtual_machines"
    - Semantic search: "show health" matches "status" operations
    - Redis caching: 18x speedup for BM25 component
    """
    if not deps.meho_deps:
        return json.dumps({"error": MSG_MEHO_DEPS_NOT_AVAILABLE})

    from meho_app.modules.knowledge.bm25_hybrid_service import BM25HybridService

    # Use BM25HybridService on knowledge_chunk (same as REST endpoints!)
    hybrid_service = BM25HybridService(
        session=deps.meho_deps.knowledge_store.repository.session,
        embedding_provider=deps.meho_deps.knowledge_store.embedding_provider,
        redis=deps.meho_deps.redis,
    )

    # Search with metadata filters for this connector's operations
    search_results = await hybrid_service.search(
        tenant_id=deps.meho_deps.user_context.tenant_id,
        query=query,
        top_k=limit * 2,  # Get more candidates for better ranking
        metadata_filters={
            "source_type": "connector_operation",
            "connector_id": connector_id,
        },
    )

    # Enrich results with full parameters/examples from the connector_operation table.
    # The BM25Hybrid search only returns knowledge chunk text; the structured operation
    # details (parameter names, types, required flags, examples) live in the DB.
    # For typed connectors these are OUR hardcoded definitions -- a missing DB record
    # means the sync is broken, not an expected condition.
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository

    op_repo = ConnectorOperationRepository(deps.meho_deps.knowledge_store.repository.session)
    db_operations = await op_repo.list_operations(connector_id)
    op_lookup = {op.operation_id: op for op in db_operations}

    formatted_results = []
    for result in search_results[:limit]:
        metadata = result.get("metadata", {})
        score = result.get("rrf_score", result.get("bm25_score", 0))

        op_id = metadata.get("operation_id") or metadata.get("operation_name")
        db_op = op_lookup.get(op_id) if op_id else None

        if not db_op and op_id:
            logger.warning(
                f"Operation '{op_id}' found in knowledge search but missing from "
                f"connector_operation table for connector {connector_id} ({connector_type}). "
                f"This indicates a sync issue -- the agent will not have parameter details."
            )

        # NOTE: Do NOT include "id" (chunk UUID) - LLM confuses it with operation_id
        formatted_results.append(
            {
                "operation_id": op_id,
                "name": db_op.name if db_op else metadata.get("operation_name", ""),
                "description": db_op.description if db_op else result.get("text", "")[:200],
                "category": db_op.category if db_op else metadata.get("category", ""),
                "parameters": db_op.parameters if db_op else [],
                "example": db_op.example if db_op else None,
                "score": score,
            }
        )

    logger.info(
        f"✅ search_operations ({connector_type}): found {len(formatted_results)} operations via BM25Hybrid"
    )
    return json.dumps(formatted_results, indent=2, default=str)


async def _search_soap_operations(
    deps: MEHOGraphDeps, connector: Any, query: str, limit: int
) -> str:
    """
    Search SOAP operations using BM25HybridService on knowledge_chunk.

    TASK-126: Uses unified search on knowledge_chunk table with embeddings.
    SOAP operations are ingested with source_type="connector_operation" during WSDL ingestion.

    Features:
    - BM25 with Porter Stemming: keyword matching
    - Semantic search: natural language queries
    - No re-ingestion of WSDL on every search!
    """
    if not deps.meho_deps:
        return json.dumps({"error": MSG_MEHO_DEPS_NOT_AVAILABLE})

    connector_id = str(connector.id)

    from meho_app.modules.knowledge.bm25_hybrid_service import BM25HybridService

    # Use BM25HybridService on knowledge_chunk (same as REST endpoints!)
    hybrid_service = BM25HybridService(
        session=deps.meho_deps.knowledge_store.repository.session,
        embedding_provider=deps.meho_deps.knowledge_store.embedding_provider,
        redis=deps.meho_deps.redis,
    )

    # Search with metadata filters for this connector's SOAP operations
    search_results = await hybrid_service.search(
        tenant_id=deps.meho_deps.user_context.tenant_id,
        query=query,
        top_k=limit * 2,  # Get more candidates for better ranking
        metadata_filters={
            "source_type": "connector_operation",
            "connector_id": connector_id,
        },
    )

    # Check if we found any results - if not, the WSDL may not have been ingested
    if not search_results:
        protocol_config = getattr(connector, "protocol_config", {}) or {}
        wsdl_url = protocol_config.get("wsdl_url")

        if not wsdl_url:
            return json.dumps(
                {
                    "error": "No WSDL configured for this connector. "
                    "Ingest a WSDL first via POST /connectors/{id}/wsdl"
                }
            )
        else:
            return json.dumps(
                {
                    "message": "No operations found in knowledge base. "
                    "The WSDL may not have been ingested yet. "
                    "Re-ingest via POST /connectors/{id}/wsdl",
                    "results": [],
                }
            )

    # Extract operation details from knowledge chunk metadata
    results = []
    for result in search_results[:limit]:
        metadata = result.get("metadata", {})
        # Use rrf_score from hybrid search
        score = result.get("rrf_score", result.get("bm25_score", 0))

        results.append(
            {
                "name": metadata.get("operation_name", ""),
                "operation_id": metadata.get(
                    "operation_name", ""
                ),  # SOAP uses operation_name as ID
                "service_name": metadata.get("service_name", ""),
                "description": result.get("text", "")[:200],  # Text preview as description
                "soap_action": metadata.get("soap_action", ""),
                "score": score,  # Include score for debugging
            }
        )

    logger.info(f"✅ search_operations (soap): found {len(results)} operations via BM25Hybrid")
    return json.dumps(results, indent=2, default=str)


# =============================================================================
# CALL OPERATION HANDLER
# =============================================================================


async def call_operation_handler(  # NOSONAR (cognitive complexity)
    deps: MEHOGraphDeps, args: dict[str, Any], state: MEHOGraphState | None = None
) -> str:
    """
    Execute an operation on ANY connector type with batch support.

    TASK-97: Generic tool that works for REST, SOAP, and VMware connectors.
    The agent doesn't need to know the connector type - it just calls operations.

    Supports batch execution via parameter_sets - each set is executed sequentially.

    Routing:
        - REST connectors → call REST endpoint
        - SOAP connectors → call SOAP operation via zeep
        - VMware connectors → execute pyvmomi operation

    Args (in args dict):
        connector_id: ID of the connector
        operation_id: ID of the operation to call
        parameter_sets: List of parameter dicts (each set executed sequentially)
            - For REST: each set can have 'path_params', 'query_params', 'body'
            - For SOAP/VMware: each set contains the operation parameters directly

    Returns:
        JSON with operation result(s):
        - Single result if parameter_sets has 1 item
        - Batch result if parameter_sets has multiple items:
          {"batch_results": [...], "total": N, "successful": M}
    """
    connector_id = args.get("connector_id")
    operation_id = args.get("operation_id") or args.get("operation_name") or args.get("endpoint_id")
    parameter_sets = args.get("parameter_sets", [{}])

    # Ensure parameter_sets is a list
    if not isinstance(parameter_sets, list):
        parameter_sets = [parameter_sets] if parameter_sets else [{}]

    logger.info(
        f"🔌 call_operation: {operation_id} on connector={connector_id}, {len(parameter_sets)} sets"
    )

    async with traced_tool_call(
        "call_operation",
        args,
        connector_id=connector_id,
        operation_id=operation_id,
        user_id=deps.user_id,
        tenant_id=deps.tenant_id,
        session_id=deps.session_id,
        extra_attrs={
            "parameter_sets_count": len(parameter_sets),
            "parameter_sets": format_for_logging(parameter_sets),
        },
    ) as span:
        if not connector_id:
            error_result = json.dumps({"error": MSG_CONNECTOR_ID_REQUIRED})
            span.set_error(MSG_CONNECTOR_ID_REQUIRED)
            return error_result

        if not operation_id:
            error_result = json.dumps({"error": "operation_id is required"})
            span.set_error("operation_id is required")
            return error_result

        if not deps.meho_deps:
            error_result = json.dumps({"error": MSG_MEHO_DEPS_NOT_AVAILABLE})
            span.set_error(MSG_MEHO_DEPS_NOT_AVAILABLE)
            return error_result

        try:
            from meho_app.database import get_session_maker
            from meho_app.modules.agents.unified_executor import get_unified_executor
            from meho_app.modules.connectors.repositories import ConnectorRepository

            session_maker = get_session_maker()
            # Pass Redis for persistent cache (L2) - critical for multi-turn!
            redis_client = deps.meho_deps.redis if deps.meho_deps else None
            executor = get_unified_executor(redis_client)

            async with session_maker() as session:
                # Get connector once (not per parameter set)
                connector_repo = ConnectorRepository(session)
                connector = await connector_repo.get_connector(connector_id)

                if not connector:
                    error_result = json.dumps({"error": MSG_CONNECTOR_NOT_FOUND})
                    span.set_error(MSG_CONNECTOR_NOT_FOUND)
                    return error_result

                # Route based on connector_type
                connector_type = getattr(connector, "connector_type", None) or "rest"
                connector_name = getattr(connector, "name", None)

                # Update session state with connector info for topology learning
                if deps.meho_deps and deps.meho_deps.session_state:
                    deps.meho_deps.session_state.get_or_create_connector(
                        connector_id=connector_id,
                        connector_name=connector_name or f"Connector {connector_id[:8]}",
                        connector_type=connector_type,
                    )
                    deps.meho_deps.session_state.primary_connector_id = connector_id

                # Log routing decision
                span.add_attribute("connector_type", connector_type)
                span.add_attribute("connector_name", connector_name)

                logger.info(
                    "call_operation",
                    operation_id=operation_id,
                    connector_name=connector_name,
                    connector_type=connector_type,
                    parameter_sets_count=len(parameter_sets),
                )

                # Execute each parameter set sequentially
                results = []
                for i, param_set in enumerate(parameter_sets):
                    try:
                        if connector_type == "rest":
                            # REST: extract path_params, query_params, body from param_set
                            path_params = param_set.get("path_params", param_set)
                            query_params = param_set.get("query_params", {})
                            body = param_set.get("body")

                            result_json = await _call_rest_endpoint(
                                deps,
                                connector_id,
                                operation_id,
                                path_params,
                                query_params,
                                body,
                                state,
                            )

                        elif connector_type == "kubernetes":
                            # Kubernetes: param_set is the operation params directly
                            result_json = await _call_kubernetes_operation(
                                deps, connector, connector_id, operation_id, param_set, state
                            )

                        elif connector_type == "vmware":
                            # VMware: param_set is the operation params directly
                            result_json = await _call_vmware_operation(
                                deps, connector, connector_id, operation_id, param_set, state
                            )

                        elif connector_type == "proxmox":
                            # Proxmox: param_set is the operation params directly
                            result_json = await _call_proxmox_operation(
                                deps, connector, connector_id, operation_id, param_set, state
                            )

                        elif connector_type == "gcp":
                            # GCP: param_set is the operation params directly
                            result_json = await _call_gcp_operation(
                                deps, connector, connector_id, operation_id, param_set, state
                            )

                        elif connector_type in (
                            "argocd",
                            "github",
                            "prometheus",
                            "loki",
                            "tempo",
                            "alertmanager",
                            "jira",
                            "confluence",
                            "email",
                        ):
                            result_json = await _call_typed_operation(
                                deps,
                                connector,
                                connector_id,
                                connector_type,
                                operation_id,
                                param_set,
                                state,
                            )

                        elif connector_type == "soap":
                            # SOAP: param_set is the operation params directly
                            service_name = (
                                param_set.pop("service_name", None)
                                if isinstance(param_set, dict)
                                else None
                            )
                            port_name = (
                                param_set.pop("port_name", None)
                                if isinstance(param_set, dict)
                                else None
                            )

                            result_json = await _call_soap_operation_zeep(
                                deps,
                                connector,
                                connector_id,
                                operation_id,
                                param_set,
                                service_name,
                                port_name,
                                state,
                            )

                        else:
                            result_json = json.dumps(
                                {"error": f"Unknown connector type: {connector_type}"}
                            )

                        # Parse JSON result to dict
                        result = json.loads(result_json)

                        # TASK-143: Auto-discovery - extract topology entities from successful operations
                        # This is non-blocking: if it fails, the operation still succeeds
                        if result.get("success") and result.get("data"):
                            logger.info(
                                f"🔍 Triggering auto-discovery for {connector_type}/{operation_id}"
                            )
                            await _trigger_auto_discovery(
                                connector_type=connector_type,
                                connector_id=connector_id,
                                connector_name=getattr(connector, "name", None),
                                operation_id=operation_id,
                                result_data=result.get("data"),
                                tenant_id=deps.tenant_id,
                            )
                        else:
                            logger.debug(
                                f"Skipping auto-discovery: success={result.get('success')}, has_data={bool(result.get('data'))}"
                            )

                        # UNIFIED CACHING (TASK-161): Token-aware tiering with schema hints
                        data = result.get("data")
                        if result.get("success") and data:
                            session_id = deps.session_id or "anonymous"
                            # Pass raw data (dict or list) -- QueryEngine.register()
                            # handles shape detection via unwrap='auto'

                            # Query DB for schema hints (same for ALL connector types!)
                            from meho_app.modules.connectors.repositories import (
                                ConnectorOperationRepository,
                            )

                            op_repo = ConnectorOperationRepository(session)
                            operation_meta = await op_repo.get_operation_by_op_id(
                                connector_id, operation_id
                            )

                            # Unified caching with token-aware tiering
                            cached, tier = await executor.cache_data_async(
                                session_id=session_id,
                                source_id=operation_id,
                                source_path=operation_id,
                                connector_id=connector_id,
                                connector_type=connector_type,
                                data=data,
                                entity_type=operation_meta.response_entity_type
                                if operation_meta
                                else None,
                                identifier_field=operation_meta.response_identifier_field
                                if operation_meta
                                else None,
                                display_name_field=operation_meta.response_display_name_field
                                if operation_meta
                                else None,
                            )

                            # Return tier-appropriate summary
                            result = cached.to_llm_summary(tier)
                            logger.info(
                                f"📊 Unified cache: {cached.row_count} items, tier={tier.value}"
                            )

                        results.append(result)

                    except Exception as e:
                        logger.error(f"❌ call_operation failed for set {i}: {e}")
                        results.append({"error": str(e), "success": False})

                # Return single result or batch
                if len(results) == 1:
                    final_result = results[0]
                    span.set_output(final_result)
                    span.add_attribute("success", final_result.get("success", False))
                    return json.dumps(final_result, indent=2, default=str)
                else:
                    successful = sum(1 for r in results if r.get("success", False))
                    final_result = {
                        "batch_results": results,
                        "total": len(results),
                        "successful": successful,
                    }
                    span.set_output(final_result)
                    span.add_attribute("batch_total", len(results))
                    span.add_attribute("batch_successful", successful)
                    return json.dumps(final_result, indent=2, default=str)

        except UpstreamApiError as e:
            connector_name = (
                deps.connector_name if hasattr(deps, "connector_name") else "Unknown connector"
            )
            if e.status_code == 401:
                error_detail = f"{connector_name} authentication failed -- verify credentials"
            elif e.status_code >= 500:
                error_detail = f"{connector_name} server error ({e.status_code})"
            elif e.status_code == 429:
                error_detail = f"{connector_name} rate limited"
            else:
                error_detail = f"{connector_name} error ({e.status_code})"
            logger.warning(f"call_operation connector error: {error_detail}")
            span.set_error(error_detail)
            return json.dumps(
                {
                    "error": error_detail,
                    "error_source": "connector",
                    "error_type": "auth" if e.status_code == 401 else "connection",
                    "status_code": e.status_code,
                }
            )
        except Exception as e:
            logger.error(f"call_operation failed: {e}", exc_info=True)
            span.set_error(str(e))
            return json.dumps({"error": str(e)})


async def _call_rest_endpoint(
    deps: MEHOGraphDeps,
    connector_id: str,
    endpoint_id: str,
    path_params: dict[str, Any],
    query_params: dict[str, Any],
    body: Any | None,
    _state: MEHOGraphState | None,
) -> str:
    """Call REST endpoint using existing MEHODependencies."""
    if not deps.meho_deps:
        return json.dumps({"error": MSG_MEHO_DEPS_NOT_AVAILABLE})

    start_time = time.perf_counter()

    result = await deps.meho_deps.call_endpoint(
        connector_id=connector_id,
        endpoint_id=endpoint_id,
        path_params=path_params if isinstance(path_params, dict) else {},
        query_params=query_params,
        body=body,
    )

    # SECU-07: Emit stale credential warnings via SSE if emitter available
    if result.get("warnings") and deps.emitter:
        for warning in result["warnings"]:
            try:  # noqa: SIM105 -- explicit error handling preferred
                await deps.emitter.warning(
                    warning["message"],
                    details=warning.get("details"),
                )
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Non-blocking: warning emission must never break the flow

    duration_ms = (time.perf_counter() - start_time) * 1000

    # Log detailed HTTP call trace
    logger.info(
        "REST call",
        endpoint_id=endpoint_id,
        status="success" if result.get("success") else "error",
        connector_id=connector_id,
        path_params=format_for_logging(path_params),
        query_params=format_for_logging(query_params),
        request_body=format_for_logging(body) if body else None,
        response_data=format_for_logging(result.get("data")),
        response_error=result.get("error"),
        duration_ms=round(duration_ms, 2),
    )

    logger.info(f"✅ call_operation (rest): success={result.get('success')}")
    return json.dumps(result, indent=2, default=str)


async def _call_vmware_operation(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: dict[str, Any],
    _state: MEHOGraphState | None,
) -> str:
    """Execute a VMware operation using pyvmomi connector."""
    from meho_app.modules.connectors.pool import get_pooled_connector

    protocol_config = getattr(connector, "protocol_config", {}) or {}

    # Resolve credentials via CredentialResolver (Phase 74)
    credentials, err = await _resolve_credentials(deps, connector_id)
    if err:
        return err

    # Get pooled connector
    vmware_connector = await get_pooled_connector(
        connector_type="vmware",
        connector_id=connector_id,
        user_id=deps.user_id or "anonymous",
        config=protocol_config,
        credentials=credentials,
    )

    # Execute operation
    result = await vmware_connector.execute(operation_name, params)

    response = {
        "success": result.success,
        "operation": operation_name,
        "data": result.data,
        "duration_ms": result.duration_ms,
    }

    if not result.success:
        response["error"] = result.error

    # Log detailed VMware operation trace
    logger.info(
        "VMware call",
        operation_name=operation_name,
        status="success" if result.success else "error",
        connector_id=connector_id,
        connector_name=getattr(connector, "name", None),
        params=format_for_logging(params),
        response_data=format_for_logging(result.data),
        response_error=result.error if not result.success else None,
        duration_ms=result.duration_ms,
    )

    logger.info(f"✅ call_vmware_operation: success={result.success}")
    return json.dumps(response, indent=2, default=str)


async def _call_proxmox_operation(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: dict[str, Any],
    _state: MEHOGraphState | None,
) -> str:
    """Execute a Proxmox operation using proxmoxer connector."""
    from meho_app.modules.connectors.pool import get_pooled_connector

    protocol_config = getattr(connector, "protocol_config", {}) or {}

    # Resolve credentials via CredentialResolver (Phase 74)
    credentials, err = await _resolve_credentials(deps, connector_id)
    if err:
        return err

    # Get pooled connector
    proxmox_connector = await get_pooled_connector(
        connector_type="proxmox",
        connector_id=connector_id,
        user_id=deps.user_id or "anonymous",
        config=protocol_config,
        credentials=credentials,
    )

    # Execute operation
    result = await proxmox_connector.execute(operation_name, params)

    response = {
        "success": result.success,
        "operation": operation_name,
        "data": result.data,
        "duration_ms": result.duration_ms,
    }

    if not result.success:
        response["error"] = result.error

    # Log detailed Proxmox operation trace
    logger.info(
        "Proxmox call",
        operation_name=operation_name,
        status="success" if result.success else "error",
        connector_id=connector_id,
        connector_name=getattr(connector, "name", None),
        params=format_for_logging(params),
        response_data=format_for_logging(result.data),
        response_error=result.error if not result.success else None,
        duration_ms=result.duration_ms,
    )

    logger.info(f"✅ call_proxmox_operation: success={result.success}")
    return json.dumps(response, indent=2, default=str)


async def _call_gcp_operation(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: dict[str, Any],
    _state: MEHOGraphState | None,
) -> str:
    """Execute a GCP operation using Google Cloud SDK connector."""
    from meho_app.modules.connectors.pool import get_pooled_connector

    protocol_config = getattr(connector, "protocol_config", {}) or {}

    # Resolve credentials via CredentialResolver (Phase 74)
    credentials, err = await _resolve_credentials(deps, connector_id)
    if err:
        return err

    # Get pooled connector
    gcp_connector = await get_pooled_connector(
        connector_type="gcp",
        connector_id=connector_id,
        user_id=deps.user_id or "anonymous",
        config=protocol_config,
        credentials=credentials,
    )

    # Execute operation
    result = await gcp_connector.execute(operation_name, params)

    response = {
        "success": result.success,
        "operation": operation_name,
        "data": result.data,
        "duration_ms": result.duration_ms,
    }

    if not result.success:
        response["error"] = result.error

    # Log detailed GCP operation trace
    logger.info(
        "GCP call",
        operation_name=operation_name,
        status="success" if result.success else "error",
        connector_id=connector_id,
        connector_name=getattr(connector, "name", None),
        params=format_for_logging(params),
        response_data=format_for_logging(result.data),
        response_error=result.error if not result.success else None,
        duration_ms=result.duration_ms,
    )

    logger.info(f"✅ call_gcp_operation: success={result.success}")
    return json.dumps(response, indent=2, default=str)


async def _call_typed_operation(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    connector_type: str,
    operation_name: str,
    params: dict[str, Any],
    _state: MEHOGraphState | None,
) -> str:
    """Execute an operation on a typed connector (ArgoCD, GitHub, Prometheus, etc.) via pool."""
    from meho_app.modules.connectors.pool import get_pooled_connector

    protocol_config = getattr(connector, "protocol_config", {}) or {}

    # Resolve credentials via CredentialResolver (Phase 74)
    credentials, err = await _resolve_credentials(deps, connector_id)
    if err:
        return err

    typed_connector = await get_pooled_connector(
        connector_type=connector_type,
        connector_id=connector_id,
        user_id=deps.user_id or "anonymous",
        config=protocol_config,
        credentials=credentials,
    )

    result = await typed_connector.execute(operation_name, params)

    response = {
        "success": result.success,
        "operation": operation_name,
        "data": result.data,
        "duration_ms": result.duration_ms,
    }

    if not result.success:
        response["error"] = result.error

    logger.info(
        f"{connector_type} call",
        operation_name=operation_name,
        status="success" if result.success else "error",
        connector_id=connector_id,
        connector_name=getattr(connector, "name", None),
        params=format_for_logging(params),
        response_data=format_for_logging(result.data),
        response_error=result.error if not result.success else None,
        duration_ms=result.duration_ms,
    )

    return json.dumps(response, indent=2, default=str)


async def _call_kubernetes_operation(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: dict[str, Any],
    _state: MEHOGraphState | None,
) -> str:
    """Execute a Kubernetes operation using kubernetes-asyncio connector."""
    from meho_app.modules.connectors.pool import get_pooled_connector

    protocol_config = getattr(connector, "protocol_config", {}) or {}

    # Resolve credentials via CredentialResolver (Phase 74)
    credentials, err = await _resolve_credentials(deps, connector_id)
    if err:
        return err

    # Get pooled connector
    k8s_connector = await get_pooled_connector(
        connector_type="kubernetes",
        connector_id=connector_id,
        user_id=deps.user_id or "anonymous",
        config=protocol_config,
        credentials=credentials,
    )

    # Execute operation
    result = await k8s_connector.execute(operation_name, params)

    response = {
        "success": result.success,
        "operation": operation_name,
        "data": result.data,
        "duration_ms": result.duration_ms,
    }

    if not result.success:
        response["error"] = result.error

    # Log detailed Kubernetes operation trace
    logger.info(
        "Kubernetes call",
        operation_name=operation_name,
        status="success" if result.success else "error",
        connector_id=connector_id,
        connector_name=getattr(connector, "name", None),
        params=format_for_logging(params),
        response_data=format_for_logging(result.data),
        response_error=result.error if not result.success else None,
        duration_ms=result.duration_ms,
    )

    logger.info(f"✅ call_kubernetes_operation: success={result.success}")
    return json.dumps(response, indent=2, default=str)


async def _call_soap_operation_zeep(  # NOSONAR (cognitive complexity)
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: dict[str, Any],
    service_name: str | None,
    port_name: str | None,
    state: MEHOGraphState | None,
) -> str:
    """Execute a SOAP operation using zeep client."""
    from meho_app.modules.connectors.soap import SOAPAuthType, SOAPClient, SOAPConnectorConfig
    from meho_app.modules.connectors.soap.client import VMwareSOAPClient

    protocol_config = getattr(connector, "protocol_config", {}) or {}
    wsdl_url = protocol_config.get("wsdl_url")

    if not wsdl_url:
        return json.dumps({"error": "No WSDL configured for this connector"})

    # Resolve credentials via CredentialResolver (Phase 74)
    credentials, err = await _resolve_credentials(deps, connector_id)
    if err:
        return err
    # Use None if empty dict for SOAP config compatibility
    if not credentials:
        credentials = None  # type: ignore[assignment]

    # Build SOAP config
    auth_type = SOAPAuthType.NONE
    if connector.auth_type == "BASIC":
        auth_type = SOAPAuthType.BASIC
    elif connector.auth_type == "SESSION":
        auth_type = SOAPAuthType.SESSION

    config = SOAPConnectorConfig(
        wsdl_url=wsdl_url,
        auth_type=auth_type,
        username=credentials.get("username") if credentials else None,
        password=credentials.get("password") if credentials else None,
        login_operation=protocol_config.get("login_operation"),
        logout_operation=protocol_config.get("logout_operation"),
        verify_ssl=protocol_config.get("verify_ssl", False),
    )

    # Use VMware SOAP client if applicable
    is_vmware_soap = (
        "vmware" in connector.name.lower()
        or "vim" in wsdl_url.lower()
        or "vsphere" in connector.name.lower()
    )

    client_cls = VMwareSOAPClient if is_vmware_soap else SOAPClient

    # Use cached client if available
    cache_key = (connector_id, deps.user_id or "anonymous")
    client = SOAP_CLIENT_CACHE.get(cache_key)

    if client is None or not client.is_connected:
        logger.info(f"🔌 Creating new SOAP client for {connector.name}")
        client = client_cls(config)
        await asyncio.to_thread(client.connect)
        SOAP_CLIENT_CACHE[cache_key] = client
    else:
        logger.info(f"♻️ Reusing cached SOAP client for {connector.name}")

    response = await asyncio.to_thread(
        client.call,
        operation_name=operation_name,
        params=params,
        service_name=service_name,
        port_name=port_name,
    )

    result = {
        "success": response.success,
        "operation": operation_name,
        "data": response.body,
        "duration_ms": response.duration_ms,
    }

    if not response.success:
        result["fault_code"] = response.fault_code
        result["fault_string"] = response.fault_string

        if "not authenticated" in (response.fault_string or "").lower():
            logger.warning("⚠️ Session expired - clearing cache")
            SOAP_CLIENT_CACHE.pop(cache_key, None)

    # Log detailed SOAP operation trace
    logger.info(
        "SOAP call",
        operation_name=operation_name,
        status="success" if response.success else "error",
        connector_id=connector_id,
        connector_name=getattr(connector, "name", None),
        params=format_for_logging(params),
        service_name=service_name,
        port_name=port_name,
        response_data=format_for_logging(response.body),
        fault_code=response.fault_code if not response.success else None,
        fault_string=response.fault_string if not response.success else None,
        duration_ms=response.duration_ms,
    )

    logger.info(f"✅ call_soap_operation: success={response.success}")
    return json.dumps(result, indent=2, default=str)


# =============================================================================
# AUTO-DISCOVERY HELPER (TASK-143)
# =============================================================================


async def _trigger_auto_discovery(
    connector_type: str,
    connector_id: str,
    connector_name: str | None,
    operation_id: str,
    result_data: Any,
    tenant_id: str,
) -> None:
    """
    Trigger topology auto-discovery for connector operation results.

    TASK-143: Extracts entities from connector operation results and queues
    them for background storage in the topology database.

    This function is non-blocking - if it fails, it logs a warning and returns.
    The original operation result is not affected.

    Args:
        connector_type: Type of connector (vmware, gcp, proxmox, etc.)
        connector_id: Unique connector ID
        connector_name: Human-readable connector name
        operation_id: Operation that was executed
        result_data: Result data from the operation
        tenant_id: Tenant ID for multi-tenancy
    """
    try:
        from meho_app.core.config import get_config

        config = get_config()
        if not config.topology_auto_discovery_enabled:
            logger.info("Auto-discovery disabled in config")
            return

        from meho_app.modules.topology.auto_discovery import get_auto_discovery_service

        logger.info(f"Auto-discovery: processing {connector_type}/{operation_id}")
        service = get_auto_discovery_service()
        count = await service.process_operation_result(
            connector_type=connector_type,
            connector_id=connector_id,
            connector_name=connector_name,
            operation_id=operation_id,
            result_data=result_data,
            tenant_id=tenant_id,
        )

        if count > 0:
            logger.info(f"🔍 Auto-discovery: queued {count} entities from {operation_id}")
        else:
            logger.info(f"Auto-discovery: no entities extracted from {operation_id}")

    except Exception as e:
        # Non-blocking: log warning but don't fail the operation
        logger.warning(f"Auto-discovery failed (non-blocking): {e}", exc_info=True)


# =============================================================================
# SEARCH TYPES HANDLER
# =============================================================================


async def search_types_handler(
    deps: MEHOGraphDeps, args: dict[str, Any]
) -> str:  # NOSONAR (cognitive complexity)
    """
    Search for entity type definitions in ANY connector.

    TASK-97: Generic tool that works for SOAP and VMware connectors.
    REST connectors typically don't have separate type definitions.

    Routing:
        - SOAP connectors → search soap_type_descriptor table
        - VMware connectors → search connector_type table
        - REST connectors → return message that types aren't available

    Args (in args dict):
        connector_id: Required - ID of the connector to search within
        query: Search query (e.g., "cluster", "virtual machine")
        limit: Max results (default 10)

    Returns:
        JSON list of matching types with their properties
    """
    query_text = args.get("query", "")
    connector_id = args.get("connector_id")
    limit = args.get("limit", 10)

    logger.info(f"🔍 search_types: query='{query_text}', connector={connector_id}")

    async with traced_tool_call(
        "search_types",
        args,
        connector_id=connector_id,
        user_id=deps.user_id,
        tenant_id=deps.tenant_id,
        session_id=deps.session_id,
    ) as span:
        if not connector_id:
            error_result = json.dumps(
                {
                    "error": MSG_CONNECTOR_ID_REQUIRED,
                    "hint": "Provide the connector ID to search within",
                }
            )
            span.set_error(MSG_CONNECTOR_ID_REQUIRED)
            return error_result

        try:
            from meho_app.database import get_session_maker
            from meho_app.modules.connectors.repositories import (
                ConnectorRepository,
                ConnectorTypeRepository,
            )
            from meho_app.modules.connectors.soap import SoapTypeRepository

            session_maker = get_session_maker()

            async with session_maker() as session:
                # Get connector to check type
                connector_repo = ConnectorRepository(session)
                connector = await connector_repo.get_connector(connector_id)

                if not connector:
                    error_result = json.dumps({"error": MSG_CONNECTOR_NOT_FOUND})
                    span.set_error(MSG_CONNECTOR_NOT_FOUND)
                    return error_result

                connector_type = getattr(connector, "connector_type", None) or "rest"
                connector_name = getattr(connector, "name", None)

                span.add_attribute("connector_type", connector_type)
                span.add_attribute("connector_name", connector_name)

                if connector_type in (
                    "rest",
                    "kubernetes",
                    "vmware",
                    "proxmox",
                    "gcp",
                    "argocd",
                    "github",
                    "prometheus",
                    "loki",
                    "tempo",
                    "alertmanager",
                    "jira",
                    "confluence",
                    "email",
                ):
                    # TASK-98: REST connectors now have types extracted from OpenAPI schemas
                    # Kubernetes, VMware, Proxmox and GCP connectors also use connector_type table
                    # Search connector_type table
                    type_repo = ConnectorTypeRepository(session)

                    if query_text:
                        vmware_types = await type_repo.search_types(connector_id, query_text, limit)
                    else:
                        vmware_types = await type_repo.list_types(connector_id, limit=limit)

                    results = []
                    for t in vmware_types:
                        props = t.properties or []
                        prop_list = [
                            f"  - {p.get('name', '?')}: {p.get('type', '?')}" for p in props[:10]
                        ]
                        results.append(
                            {
                                "type_name": t.type_name,
                                "description": t.description,
                                "category": t.category,
                                "properties_count": len(props),
                                "properties_preview": prop_list[:5],
                            }
                        )

                    # Return helpful message if no types found
                    if not results:
                        if connector_type in ("rest", "kubernetes"):
                            result = json.dumps(
                                {
                                    "message": f"No schema types found for query '{query_text}'",
                                    "hint": "Upload an OpenAPI spec with components/schemas to enable type search, or use search_endpoints to find endpoint schemas.",
                                }
                            )
                        else:
                            result = json.dumps(
                                {
                                    "message": f"No types found for query '{query_text}'",
                                    "hint": "Try different search terms.",
                                }
                            )
                        span.set_output({"result_count": 0})
                        return result

                    logger.info(f"✅ search_types ({connector_type}): found {len(results)} types")
                    span.set_output(results)
                    span.add_attribute("result_count", len(results))
                    return json.dumps(results, indent=2, default=str)

                elif connector_type == "soap":
                    # SOAP - search soap_type_descriptor table
                    soap_type_repo = SoapTypeRepository(session)

                    if query_text:
                        soap_types = await soap_type_repo.search_types(
                            connector_id, query_text, limit
                        )
                    else:
                        soap_types = await soap_type_repo.list_types(connector_id, limit=limit)

                    if not soap_types:
                        result = json.dumps(
                            {
                                "message": f"No types found for query '{query_text}'",
                                "hint": "Try different search terms, or verify the WSDL has been ingested.",
                            }
                        )
                        span.set_output({"result_count": 0})
                        return result

                    results = []
                    for t in soap_types:  # type: ignore[assignment]
                        props = t.properties or []
                        prop_list = [
                            f"  - {p.get('name', 'unknown') if isinstance(p, dict) else getattr(p, 'name', 'unknown')}: "
                            f"{p.get('type_name', 'unknown') if isinstance(p, dict) else getattr(p, 'type_name', 'unknown')}"
                            for p in props[:10]
                        ]
                        results.append(
                            {
                                "type_name": t.type_name,
                                "namespace": getattr(t, "namespace", None),
                                "base_type": getattr(t, "base_type", None),
                                "properties_count": len(props),
                                "properties_preview": prop_list[:5],
                                "description": t.description,
                            }
                        )

                    logger.info(f"✅ search_types (soap): found {len(results)} types")
                    span.set_output(results)
                    span.add_attribute("result_count", len(results))
                    return json.dumps(results, indent=2, default=str)

                else:
                    error_result = json.dumps(
                        {"error": f"Unknown connector type: {connector_type}"}
                    )
                    span.set_error(f"Unknown connector type: {connector_type}")
                    return error_result

        except Exception as e:
            logger.error(f"❌ search_types failed: {e}", exc_info=True)
            span.set_error(str(e))
            return json.dumps({"error": str(e)})
