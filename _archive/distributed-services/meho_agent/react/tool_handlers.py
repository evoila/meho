"""
Tool Handlers for MEHO ReAct Graph (TASK-89)

IMPORTANT: These are THIN WRAPPERS that delegate to MEHODependencies.
All business logic lives in MEHODependencies - we just adapt the interface.

This approach:
- Reuses existing, tested code (credential handling, BM25 search, etc.)
- Avoids duplication and the bugs that come with it
- Ensures consistent behavior between old and new agent
"""

import json
import logging
from typing import Dict, Any, Optional

from meho_agent.react.graph_deps import MEHOGraphDeps
from meho_agent.react.graph_state import MEHOGraphState

logger = logging.getLogger(__name__)

# =============================================================================
# SOAP CLIENT CACHE - Keep sessions alive across multiple calls
# =============================================================================
# Key: (connector_id, user_id) -> connected SOAP client
# This allows multiple SOAP operations to share a session
_soap_client_cache: Dict[tuple, Any] = {}


# =============================================================================
# TOOL HANDLERS - Thin wrappers around MEHODependencies methods
# =============================================================================


async def search_endpoints_handler(deps: MEHOGraphDeps, args: Dict[str, Any]) -> str:
    """
    Search for API endpoints matching a query.
    
    DELEGATES TO: MEHODependencies.search_endpoints()
    which uses BM25 with Porter Stemming and Redis caching.
    """
    query = args.get("query", "")
    connector_id = args.get("connector_id")
    limit = args.get("limit", 10)
    
    logger.info(f"🔍 search_endpoints: query='{query}', connector={connector_id}")
    
    if not deps.meho_deps:
        return json.dumps({"error": "MEHODependencies not available"})
    
    try:
        # Delegate to MEHODependencies - it has the full BM25 implementation
        # Pass connector_id as-is (can be None to search ALL connectors)
        results = await deps.meho_deps.search_endpoints(
            connector_id=connector_id or "",  # MEHODependencies handles empty string
            query=query,
            limit=limit
        )
        
        logger.info(f"✅ search_endpoints: found {len(results)} endpoints")
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"❌ search_endpoints failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def call_endpoint_handler(
    deps: MEHOGraphDeps, 
    args: Dict[str, Any],
    state: Optional[MEHOGraphState] = None
) -> str:
    """
    Execute an API call to a specific endpoint.
    
    DELEGATES TO: MEHODependencies.call_endpoint()
    which handles credential fetching, session management, etc.
    
    BRAIN-MUSCLE ARCHITECTURE (TASK-91):
    - For large responses (>CACHE_THRESHOLD items), returns SUMMARY to Brain
    - Full data stored server-side in UnifiedExecutor cache
    - Brain can request filter/reduce operations via reduce_data tool
    """
    from meho_agent.unified_executor import get_unified_executor
    
    endpoint_id = args.get("endpoint_id")
    connector_id = args.get("connector_id")
    parameter_sets = args.get("parameter_sets", [{}])
    
    if not endpoint_id:
        return json.dumps({"error": "endpoint_id is required"})
    
    if not deps.meho_deps:
        return json.dumps({"error": "MEHODependencies not available"})
    
    # Handle single params or parameter_sets for batch
    if not isinstance(parameter_sets, list):
        parameter_sets = [parameter_sets] if parameter_sets else [{}]
    
    logger.info(f"🔍 call_endpoint: endpoint={endpoint_id}, params={len(parameter_sets)} sets")
    
    # Threshold for caching large responses (Brain-Muscle architecture)
    CACHE_THRESHOLD = 20  # Cache responses with >20 items
    
    try:
        results = []
        # Pass Redis for persistent cache (L2)
        redis_client = deps.meho_deps.redis if deps.meho_deps else None
        executor = get_unified_executor(redis_client)
        
        for params in parameter_sets:
            # Extract params in the format MEHODependencies expects
            path_params = params.get("path_params", params)
            query_params = params.get("query_params", {})
            body = params.get("body")
            
            # Resolve entity references if state is available
            if state:
                path_params = _resolve_entity_references(path_params, state)
            
            # Delegate to MEHODependencies - it handles credentials, sessions, etc.
            result = await deps.meho_deps.call_endpoint(
                connector_id=connector_id or "",
                endpoint_id=endpoint_id,
                path_params=path_params if isinstance(path_params, dict) else {},
                query_params=query_params,
                body=body
            )
            
            # ENTITY EXTRACTION: Always extract entities for context persistence
            # This is CRITICAL for multi-turn conversations!
            if result.get("success"):
                data = result.get("data")
                endpoint_path = result.get("endpoint_path", "")
                
                # Extract entities from ANY successful response to session_state
                if deps.meho_deps and deps.meho_deps.session_state:
                    session_state = deps.meho_deps.session_state
                    
                    # Derive entity type from endpoint path (generic, no hardcoding!)
                    entity_type = _detect_entity_type_from_path(endpoint_path)
                    
                    # Handle list responses
                    if isinstance(data, list) and len(data) > 0:
                        session_state.add_entities_from_response(
                            entity_type=entity_type,
                            items=data,
                            connector_id=connector_id or "",
                        )
                        logger.info(f"📦 Extracted {len(data)} {entity_type}(s) to session_state")
                    
                    # Handle single object responses (e.g., GET /cluster/{id})
                    elif isinstance(data, dict) and data:
                        session_state.add_entities_from_response(
                            entity_type=entity_type,
                            items=[data],
                            connector_id=connector_id or "",
                        )
                        logger.info(f"📦 Extracted 1 {entity_type} to session_state")
                
                # BRAIN-MUSCLE: For large responses, also cache and return summary
                if isinstance(data, list) and len(data) > CACHE_THRESHOLD:
                    # Get session_id for cache key
                    session_id = deps.session_id or "anonymous"
                    
                    # Get response schema from endpoint (for schema-driven lookup)
                    response_schema: Dict[str, Any] = {}
                    if deps.meho_deps and deps.meho_deps.endpoint_repo:
                        try:
                            endpoint = await deps.meho_deps.endpoint_repo.get_endpoint(endpoint_id)
                            if endpoint:
                                response_schema = endpoint.response_schema or {}
                        except Exception as e:
                            logger.warning(f"Could not fetch endpoint schema: {e}")
                    
                    # Cache full response (L1 + L2 Redis), get summary for Brain
                    cached = await executor.cache_response_async(
                        session_id=session_id,
                        endpoint_id=endpoint_id,
                        endpoint_path=endpoint_path,
                        connector_id=connector_id or "",
                        response_schema=response_schema,
                        data=data
                    )
                    
                    # Return SUMMARY to Brain (not full data!)
                    summary = cached.summarize_for_brain(sample_size=5)
                    result = {
                        "success": True,
                        "status_code": result.get("status_code"),
                        "cached": True,
                        "message": f"Retrieved {len(data)} items. Data cached for filtering/lookup.",
                        **summary
                    }
                    logger.info(f"🧠💪 Large response ({len(data)} items) cached, summary sent to Brain")
            
            results.append(result)
        
        # Return single result or batch summary
        if len(results) == 1:
            return json.dumps(results[0], indent=2, default=str)
        else:
            return json.dumps({
                "batch_results": results,
                "total": len(results),
                "successful": sum(1 for r in results if r.get("success"))
            }, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"❌ call_endpoint failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def search_knowledge_handler(deps: MEHOGraphDeps, args: Dict[str, Any]) -> str:
    """
    Search the knowledge base for documentation.
    
    DELEGATES TO: MEHODependencies.knowledge_store.hybrid_search()
    """
    query = args.get("query", "")
    limit = args.get("limit", 5)
    
    logger.info(f"🔍 search_knowledge: query='{query}'")
    
    if not deps.meho_deps or not deps.meho_deps.knowledge_store:
        return json.dumps({"error": "Knowledge store not available"})
    
    try:
        results = await deps.meho_deps.knowledge_store.hybrid_search(
            query=query,
            limit=limit
        )
        
        if not results:
            return json.dumps({"message": f"No knowledge found for '{query}'"})
        
        # Format results
        formatted = []
        for chunk in results:
            formatted.append({
                "content": chunk.content[:500] + "..." if len(chunk.content) > 500 else chunk.content,
                "source": chunk.source_uri,
                "score": getattr(chunk, 'score', None),
            })
        
        logger.info(f"✅ search_knowledge: found {len(formatted)} results")
        return json.dumps(formatted, indent=2)
        
    except Exception as e:
        logger.error(f"❌ search_knowledge failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def reduce_data_handler(deps: MEHOGraphDeps, args: Dict[str, Any]) -> str:
    """
    Query cached data using SQL.
    
    Example:
        {"sql": "SELECT * FROM virtual_machines WHERE num_cpu > 8 ORDER BY memory_mb DESC"}
    
    Tables are automatically named from the operation (e.g., list_virtual_machines → virtual_machines).
    """
    from meho_agent.unified_executor import get_unified_executor
    
    # Pass Redis for persistent cache (L2) - critical for multi-turn!
    redis_client = deps.meho_deps.redis if deps.meho_deps else None
    executor = get_unified_executor(redis_client)
    session_id = deps.session_id or "anonymous"
    
    sql = args.get("sql")
    if not sql:
        # No SQL provided - show available tables
        tables_info = await executor.get_session_table_info_async(session_id)
        if tables_info:
            table_names = [t["table"] for t in tables_info]
            return json.dumps({
                "error": "'sql' parameter is required",
                "available_tables": table_names,
                "example": f'{{"sql": "SELECT * FROM {table_names[0]} LIMIT 10"}}' if table_names else None,
            }, indent=2)
        else:
            return json.dumps({
                "error": "No cached data available. Call an API first.",
                "hint": "Use call_operation to fetch data, then query with SQL."
            }, indent=2)
    
    logger.info(f"🦆 reduce_data SQL: {sql[:100]}...")
    
    try:
        result = await executor.execute_sql_async(session_id, sql)
        
        if result.get("success"):
            logger.info(f"✅ SQL returned {result.get('count', 0)} rows")
        else:
            logger.warning(f"⚠️ SQL error: {result.get('error')}")
        
        return json.dumps(result, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"❌ SQL execution failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def list_connectors_handler(deps: MEHOGraphDeps, args: Dict[str, Any]) -> str:
    """
    List all available system connectors.
    
    DELEGATES TO: MEHODependencies.connector_repo.list_connectors()
    """
    logger.info("🔍 list_connectors called")
    
    if not deps.meho_deps or not deps.meho_deps.connector_repo:
        return json.dumps({"error": "Connector repository not available"})
    
    try:
        tenant_id = deps.tenant_id
        raw_connectors = await deps.meho_deps.connector_repo.list_connectors(
            tenant_id=tenant_id
        )
        
        # Convert to simplified format for LLM
        # CRITICAL: Include protocol and connector_type so agent knows REST vs SOAP vs VMware
        connectors = [
            {
                "id": str(c.id),
                "name": c.name,
                "description": c.description,
                "base_url": c.base_url,
                "protocol": getattr(c, 'protocol', 'rest'),  # REST or SOAP
                "connector_type": getattr(c, 'connector_type', 'rest'),  # rest, soap, vmware
                "auth_type": c.auth_type,
                "is_active": c.is_active,
            }
            for c in raw_connectors
        ]
        
        logger.info(f"✅ list_connectors: found {len(connectors)} connectors")
        return json.dumps(connectors, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"❌ list_connectors failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


# =============================================================================
# GENERIC TOOLS (TASK-97: Same tools for all connector types)
# =============================================================================


async def search_operations_handler(deps: MEHOGraphDeps, args: Dict[str, Any]) -> str:
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
    
    if not connector_id:
        return json.dumps({"error": "connector_id is required"})
    
    if not deps.meho_deps:
        return json.dumps({"error": "MEHODependencies not available"})
    
    try:
        from meho_openapi.repository import ConnectorRepository, ConnectorOperationRepository
        from meho_openapi.database import create_session_maker
        
        session_maker = create_session_maker()
        
        async with session_maker() as session:
            # Get connector to check type
            connector_repo = ConnectorRepository(session)
            connector = await connector_repo.get_connector(connector_id)
            
            if not connector:
                return json.dumps({"error": "Connector not found"})
            
            # Route based on connector_type
            connector_type = getattr(connector, 'connector_type', None) or 'rest'
            
            if connector_type == "rest":
                # REST connector - delegate to existing search_endpoints logic
                return await _search_rest_endpoints(deps, connector_id, query, limit)
            
            elif connector_type == "vmware":
                # VMware connector - BM25 search with Porter Stemming + Redis
                redis_client = deps.meho_deps.redis if deps.meho_deps else None
                return await _search_typed_operations(session, connector_id, query, limit, "vmware", redis=redis_client)
            
            elif connector_type == "soap":
                # SOAP connector - search SOAP operations
                return await _search_soap_operations(deps, connector, query, limit)
            
            else:
                return json.dumps({"error": f"Unknown connector type: {connector_type}"})
            
    except Exception as e:
        logger.error(f"❌ search_operations failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def _search_rest_endpoints(
    deps: MEHOGraphDeps,
    connector_id: str,
    query: str,
    limit: int
) -> str:
    """Search REST endpoints using existing BM25 search."""
    if not deps.meho_deps:
        return json.dumps({"error": "MEHODependencies not available"})
    
    results = await deps.meho_deps.search_endpoints(
        connector_id=connector_id,
        query=query,
        limit=limit
    )
    
    logger.info(f"✅ search_operations (rest): found {len(results)} endpoints")
    return json.dumps(results, indent=2, default=str)


async def _search_typed_operations(
    session: Any,
    connector_id: str,
    query: str,
    limit: int,
    connector_type: str,
    redis: Any = None  # Redis client for BM25 caching
) -> str:
    """
    Search typed connector operations (VMware, Kubernetes, etc.) using BM25.
    
    Uses Porter Stemming + Redis caching for:
    - "vm" matches "virtual_machines"
    - "VMs" matches "virtual machines"
    - 18x speedup after first search (Redis cache)
    """
    from meho_openapi.bm25_operation_search import OperationBM25Service
    
    # Use BM25 search with Porter Stemming (same quality as REST endpoints!)
    bm25_service = OperationBM25Service(session, redis=redis)
    results = await bm25_service.search(connector_id, query, limit)
    
    # BM25 results already include all fields from get_all_for_bm25()
    # Remove internal fields before returning to agent
    formatted_results = []
    for op in results:
        formatted_results.append({
            "id": op.get("id"),
            "operation_id": op.get("operation_id"),
            "name": op.get("name"),
            "description": op.get("description"),
            "category": op.get("category"),
            "parameters": op.get("parameters"),
            "example": op.get("example"),
            "bm25_score": op.get("bm25_score"),  # Include score for debugging
        })
    
    logger.info(f"✅ search_operations ({connector_type}): found {len(formatted_results)} operations via BM25")
    return json.dumps(formatted_results, indent=2, default=str)


async def _search_soap_operations(
    deps: MEHOGraphDeps,
    connector: Any,
    query: str,
    limit: int
) -> str:
    """Search SOAP operations from WSDL."""
    from uuid import UUID
    from meho_openapi.soap import SOAPSchemaIngester, SOAPConnectorConfig
    
    protocol_config = getattr(connector, 'protocol_config', {}) or {}
    wsdl_url = protocol_config.get('wsdl_url')
    verify_ssl = protocol_config.get('verify_ssl', True)
    
    if not wsdl_url:
        return json.dumps({
            "error": "No WSDL configured for this connector. "
                     "Ingest a WSDL first via POST /connectors/{id}/wsdl"
        })
    
    soap_config = SOAPConnectorConfig(wsdl_url=wsdl_url, verify_ssl=verify_ssl)
    ingester = SOAPSchemaIngester(config=soap_config)
    soap_operations, _, _ = await ingester.ingest_wsdl(
        wsdl_url=wsdl_url,
        connector_id=UUID(str(connector.id)) if not isinstance(connector.id, UUID) else connector.id,
        tenant_id=deps.tenant_id or "unknown",
    )
    
    # Filter by query
    if query:
        query_lower = query.lower()
        soap_operations = [
            op for op in soap_operations
            if query_lower in op.name.lower() or
               query_lower in (op.description or "").lower() or
               query_lower in (op.search_content or "").lower()
        ]
    
    # Apply limit
    soap_operations = soap_operations[:limit]
    
    # Format results
    results = [
        {
            "name": op.name,
            "operation_id": op.operation_name,  # Use operation_name as ID
            "service_name": op.service_name,
            "description": op.description,
            "soap_action": op.soap_action,
            "input_schema_summary": _summarize_schema(op.input_schema),
            "output_schema_summary": _summarize_schema(op.output_schema),
        }
        for op in soap_operations
    ]
    
    logger.info(f"✅ search_operations (soap): found {len(results)} operations")
    return json.dumps(results, indent=2, default=str)


async def call_operation_handler(
    deps: MEHOGraphDeps,
    args: Dict[str, Any],
    state: Optional[MEHOGraphState] = None
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
    
    logger.info(f"🔌 call_operation: {operation_id} on connector={connector_id}, {len(parameter_sets)} sets")
    
    if not connector_id:
        return json.dumps({"error": "connector_id is required"})
    
    if not operation_id:
        return json.dumps({"error": "operation_id is required"})
    
    if not deps.meho_deps:
        return json.dumps({"error": "MEHODependencies not available"})
    
    # Threshold for caching large responses (Brain-Muscle architecture)
    CACHE_THRESHOLD = 20  # Cache responses with >20 items
    
    try:
        from meho_openapi.repository import ConnectorRepository
        from meho_openapi.database import create_session_maker
        from meho_agent.unified_executor import get_unified_executor
        
        session_maker = create_session_maker()
        # Pass Redis for persistent cache (L2) - critical for multi-turn!
        redis_client = deps.meho_deps.redis if deps.meho_deps else None
        executor = get_unified_executor(redis_client)
        
        async with session_maker() as session:
            # Get connector once (not per parameter set)
            connector_repo = ConnectorRepository(session)
            connector = await connector_repo.get_connector(connector_id)
            
            if not connector:
                return json.dumps({"error": "Connector not found"})
            
            # Route based on connector_type
            connector_type = getattr(connector, 'connector_type', None) or 'rest'
            
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
                            deps, connector_id, operation_id, 
                            path_params, query_params, body, state
                        )
                    
                    elif connector_type == "vmware":
                        # VMware: param_set is the operation params directly
                        result_json = await _call_vmware_operation(
                            deps, connector, connector_id, operation_id, 
                            param_set, state
                        )
                    
                    elif connector_type == "soap":
                        # SOAP: param_set is the operation params directly
                        service_name = param_set.pop("service_name", None) if isinstance(param_set, dict) else None
                        port_name = param_set.pop("port_name", None) if isinstance(param_set, dict) else None
                        
                        result_json = await _call_soap_operation_zeep(
                            deps, connector, connector_id, operation_id,
                            param_set, service_name, port_name, state
                        )
                    
                    else:
                        result_json = json.dumps({"error": f"Unknown connector type: {connector_type}"})
                    
                    # Parse JSON result to dict
                    result = json.loads(result_json)
                    
                    # SQL TABLE CACHING: For large responses, cache as SQL table
                    data = result.get("data")
                    if result.get("success") and isinstance(data, list) and len(data) > CACHE_THRESHOLD:
                        session_id = deps.session_id or "anonymous"
                        
                        # Cache as SQL table (Redis + in-memory)
                        cached_table = await executor.cache_as_table_async(
                            session_id=session_id,
                            operation_id=operation_id,
                            connector_id=connector_id,
                            data=data,
                        )
                        
                        # Return table info to agent (not full data!)
                        result = {
                            "success": True,
                            "cached": True,
                            "table": cached_table.table_name,
                            "count": cached_table.row_count,
                            "columns": cached_table.columns,
                            "sample": data[:5],  # Show 5 sample rows
                            "message": f"Retrieved {len(data)} items. Cached as table '{cached_table.table_name}'. Query with SQL.",
                        }
                        logger.info(f"🦆 SQL table '{cached_table.table_name}' cached ({len(data)} rows)")
                    
                    results.append(result)
                    
                except Exception as e:
                    logger.error(f"❌ call_operation failed for set {i}: {e}")
                    results.append({"error": str(e), "success": False})
            
            # Return single result or batch
            if len(results) == 1:
                return json.dumps(results[0], indent=2, default=str)
            else:
                successful = sum(1 for r in results if r.get("success", False))
                return json.dumps({
                    "batch_results": results,
                    "total": len(results),
                    "successful": successful
                }, indent=2, default=str)
                
    except Exception as e:
        logger.error(f"❌ call_operation failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def _call_rest_endpoint(
    deps: MEHOGraphDeps,
    connector_id: str,
    endpoint_id: str,
    path_params: Dict[str, Any],
    query_params: Dict[str, Any],
    body: Optional[Any],
    state: Optional[MEHOGraphState]
) -> str:
    """Call REST endpoint using existing MEHODependencies."""
    if not deps.meho_deps:
        return json.dumps({"error": "MEHODependencies not available"})
    
    result = await deps.meho_deps.call_endpoint(
        connector_id=connector_id,
        endpoint_id=endpoint_id,
        path_params=path_params if isinstance(path_params, dict) else {},
        query_params=query_params,
        body=body
    )
    
    # Extract entities for session state
    if result.get("success") and state and deps.meho_deps and deps.meho_deps.session_state:
        session_state = deps.meho_deps.session_state
        data = result.get("data")
        endpoint_path = result.get("endpoint_path", "")
        entity_type = _detect_entity_type_from_path(endpoint_path)
        
        if isinstance(data, list) and len(data) > 0:
            session_state.add_entities_from_response(
                entity_type=entity_type,
                items=data,
                connector_id=connector_id,
            )
            logger.info(f"📦 Extracted {len(data)} {entity_type}(s) from REST response")
        elif isinstance(data, dict) and data:
            session_state.add_entities_from_response(
                entity_type=entity_type,
                items=[data],
                connector_id=connector_id,
            )
            logger.info(f"📦 Extracted 1 {entity_type} from REST response")
    
    logger.info(f"✅ call_operation (rest): success={result.get('success')}")
    return json.dumps(result, indent=2, default=str)


async def _call_vmware_operation(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: Dict[str, Any],
    state: Optional[MEHOGraphState]
) -> str:
    """Execute a VMware operation using pyvmomi connector."""
    from meho_openapi.connectors import get_pooled_connector
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.database import create_session_maker
    
    protocol_config = getattr(connector, 'protocol_config', {}) or {}
    
    # Get credentials
    credentials = {}
    if connector.credential_strategy == "USER_PROVIDED":
        session_maker = create_session_maker()
        async with session_maker() as session:
            cred_repo = UserCredentialRepository(session)
            creds = await cred_repo.get_credentials(
                deps.user_id or "anonymous",
                connector_id
            )
            if creds:
                credentials = creds
            else:
                return json.dumps({
                    "error": "Credentials required for VMware connector. Configure credentials first."
                })
    
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
    
    # Extract entities for session state
    if result.success and state and deps.meho_deps:
        session_state = deps.meho_deps.session_state
        if session_state:
            entity_type = _detect_entity_type_from_soap_operation(operation_name)
            data = result.data
            
            if isinstance(data, list) and len(data) > 0:
                session_state.add_entities_from_response(
                    entity_type=entity_type,
                    items=data,
                    connector_id=connector_id,
                )
                logger.info(f"📦 Extracted {len(data)} {entity_type}(s) from VMware response")
            elif isinstance(data, dict) and data:
                session_state.add_entities_from_response(
                    entity_type=entity_type,
                    items=[data],
                    connector_id=connector_id,
                )
                logger.info(f"📦 Extracted 1 {entity_type} from VMware response")
    
    logger.info(f"✅ call_vmware_operation: success={result.success}")
    return json.dumps(response, indent=2, default=str)


async def _call_soap_operation_zeep(
    deps: MEHOGraphDeps,
    connector: Any,
    connector_id: str,
    operation_name: str,
    params: Dict[str, Any],
    service_name: Optional[str],
    port_name: Optional[str],
    state: Optional[MEHOGraphState]
) -> str:
    """Execute a SOAP operation using zeep client."""
    from meho_openapi.soap import SOAPClient, SOAPConnectorConfig, SOAPAuthType
    from meho_openapi.soap.client import VMwareSOAPClient
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.database import create_session_maker
    
    protocol_config = getattr(connector, 'protocol_config', {}) or {}
    wsdl_url = protocol_config.get('wsdl_url')
    
    if not wsdl_url:
        return json.dumps({"error": "No WSDL configured for this connector"})
    
    # Get user credentials if needed
    credentials = None
    if connector.credential_strategy == "USER_PROVIDED":
        session_maker = create_session_maker()
        async with session_maker() as session:
            cred_repo = UserCredentialRepository(session)
            credentials = await cred_repo.get_credentials(
                deps.user_id or "anonymous",
                connector_id
            )
            
            if not credentials:
                return json.dumps({
                    "error": "Credentials required. Configure credentials first."
                })
    
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
        "vmware" in connector.name.lower() or
        "vim" in wsdl_url.lower() or
        "vsphere" in connector.name.lower()
    )
    
    ClientClass = VMwareSOAPClient if is_vmware_soap else SOAPClient
    
    # Use cached client if available
    cache_key = (connector_id, deps.user_id or "anonymous")
    client = _soap_client_cache.get(cache_key)
    
    if client is None or not client.is_connected:
        logger.info(f"🔌 Creating new SOAP client for {connector.name}")
        client = ClientClass(config)
        await client.connect()
        _soap_client_cache[cache_key] = client
    else:
        logger.info(f"♻️ Reusing cached SOAP client for {connector.name}")
    
    response = await client.call(
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
            _soap_client_cache.pop(cache_key, None)
    
    # Extract entities for session state
    if response.success and state and deps.meho_deps:
        session_state = deps.meho_deps.session_state
        if session_state:
            entity_type = _detect_entity_type_from_soap_operation(operation_name)
            data = response.body
            
            if isinstance(data, list) and len(data) > 0:
                session_state.add_entities_from_response(
                    entity_type=entity_type,
                    items=data,
                    connector_id=connector_id,
                )
                logger.info(f"📦 Extracted {len(data)} {entity_type}(s) from SOAP response")
            elif isinstance(data, dict) and data:
                session_state.add_entities_from_response(
                    entity_type=entity_type,
                    items=[data],
                    connector_id=connector_id,
                )
                logger.info(f"📦 Extracted 1 {entity_type} from SOAP response")
    
    logger.info(f"✅ call_soap_operation: success={response.success}")
    return json.dumps(result, indent=2, default=str)


async def search_types_handler(deps: MEHOGraphDeps, args: Dict[str, Any]) -> str:
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
    
    if not connector_id:
        return json.dumps({
            "error": "connector_id is required",
            "hint": "Provide the connector ID to search within"
        })
    
    try:
        from meho_openapi.database import create_session_maker
        from meho_openapi.repository import ConnectorRepository, SoapTypeRepository, ConnectorTypeRepository
        
        session_maker = create_session_maker()
        
        async with session_maker() as session:
            # Get connector to check type
            connector_repo = ConnectorRepository(session)
            connector = await connector_repo.get_connector(connector_id)
            
            if not connector:
                return json.dumps({"error": "Connector not found"})
            
            connector_type = getattr(connector, 'connector_type', None) or 'rest'
            
            if connector_type in ("rest", "vmware"):
                # TASK-98: REST connectors now have types extracted from OpenAPI schemas
                # VMware connectors also use connector_type table
                # VMware - search connector_type table
                type_repo = ConnectorTypeRepository(session)
                
                if query_text:
                    vmware_types = await type_repo.search_types(connector_id, query_text, limit)
                else:
                    vmware_types = await type_repo.list_types(connector_id, limit=limit)
                
                results = []
                for t in vmware_types:
                    props = t.properties or []
                    prop_list = [
                        f"  - {p.get('name', '?')}: {p.get('type', '?')}"
                        for p in props[:10]
                    ]
                    results.append({
                        "type_name": t.type_name,
                        "description": t.description,
                        "category": t.category,
                        "properties_count": len(props),
                        "properties_preview": prop_list[:5],
                    })
                
                # Return helpful message if no types found
                if not results:
                    if connector_type == "rest":
                        return json.dumps({
                            "message": f"No schema types found for query '{query_text}'",
                            "hint": "Upload an OpenAPI spec with components/schemas to enable type search, or use search_endpoints to find endpoint schemas."
                        })
                    else:
                        return json.dumps({
                            "message": f"No types found for query '{query_text}'",
                            "hint": "Try different search terms."
                        })
                
                logger.info(f"✅ search_types ({connector_type}): found {len(results)} types")
                return json.dumps(results, indent=2, default=str)
            
            elif connector_type == "soap":
                # SOAP - search soap_type_descriptor table
                soap_type_repo = SoapTypeRepository(session)
                
                if query_text:
                    soap_types = await soap_type_repo.search_types(connector_id, query_text, limit)
                else:
                    soap_types = await soap_type_repo.list_types(connector_id, limit=limit)
                
                if not soap_types:
                    return json.dumps({
                        "message": f"No types found for query '{query_text}'",
                        "hint": "Try different search terms, or verify the WSDL has been ingested."
                    })
                
                results = []
                for t in soap_types:  # type: ignore[assignment]
                    props = t.properties or []
                    prop_list = [
                        f"  - {p.get('name', 'unknown') if isinstance(p, dict) else getattr(p, 'name', 'unknown')}: "
                        f"{p.get('type_name', 'unknown') if isinstance(p, dict) else getattr(p, 'type_name', 'unknown')}"
                        for p in props[:10]
                    ]
                    results.append({
                        "type_name": t.type_name,
                        "namespace": getattr(t, 'namespace', None),
                        "base_type": getattr(t, 'base_type', None),
                        "properties_count": len(props),
                        "properties_preview": prop_list[:5],
                        "description": t.description,
                    })
                
                logger.info(f"✅ search_types (soap): found {len(results)} types")
                return json.dumps(results, indent=2, default=str)
            
            else:
                return json.dumps({"error": f"Unknown connector type: {connector_type}"})
        
    except Exception as e:
        logger.error(f"❌ search_types failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


def _summarize_schema(schema: Dict[str, Any]) -> str:
    """Create a brief summary of a JSON schema for LLM context"""
    if not schema:
        return "(no schema)"
    
    props = schema.get("properties", {})
    if props:
        param_names = list(props.keys())[:5]  # First 5 params
        if len(props) > 5:
            return f"params: {', '.join(param_names)}, +{len(props) - 5} more"
        return f"params: {', '.join(param_names)}"
    
    return "(complex schema)"


def _detect_entity_type_from_soap_operation(operation_name: str) -> str:
    """
    Derive entity type from SOAP operation name.
    
    Examples:
        RetrieveProperties → "property"
        GetVMs → "vm"
        ListClusters → "cluster"
        PowerOnVM_Task → "vm"
    
    Generic pattern matching - not hardcoded for specific systems!
    """
    if not operation_name:
        return "resource"
    
    name = operation_name.lower()
    
    # Remove common prefixes
    for prefix in ["get", "list", "retrieve", "find", "search", "query"]:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    
    # Remove common suffixes
    for suffix in ["_task", "task", "_async", "async", "info", "data"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    
    # Handle underscores
    name = name.replace("_", "")
    
    # Singularize
    if name.endswith("ies"):
        name = name[:-3] + "y"
    elif name.endswith("s") and len(name) > 2:
        name = name[:-1]
    
    return name or "resource"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _resolve_entity_references(
    params: Dict[str, Any], 
    state: MEHOGraphState
) -> Dict[str, Any]:
    """Resolve entity references in parameters (e.g., 'vm-57' → actual ID)."""
    if not params or not isinstance(params, dict):
        return params or {}
    
    resolved = {}
    for key, value in params.items():
        if isinstance(value, str):
            entity = state.find_entity(value)
            if entity:
                resolved[key] = entity.entity_id
            else:
                resolved[key] = value
        else:
            resolved[key] = value
    
    return resolved


def _detect_entity_type_from_path(endpoint_path: Optional[str]) -> str:
    """
    Derive entity type GENERICALLY from endpoint path.
    
    Examples:
        /api/vcenter/vm → "vm"
        /api/kubernetes/pods → "pod"
        /api/github/repos → "repo"
        /api/v1/clusters/{id} → "cluster"
    
    NO hardcoded system-specific logic - works for ANY API!
    """
    if not endpoint_path:
        return "resource"
    
    # Extract last meaningful path segment (skip path params like {id})
    segments = [s for s in endpoint_path.split("/") if s and not s.startswith("{")]
    
    if not segments:
        return "resource"
    
    last_segment = segments[-1].lower()
    
    # Singularize common patterns (generic, not system-specific)
    if last_segment.endswith("ies"):
        return last_segment[:-3] + "y"  # "entries" → "entry"
    elif last_segment.endswith("ses"):
        return last_segment[:-2]  # "addresses" → "address"
    elif last_segment.endswith("s") and len(last_segment) > 2:
        return last_segment[:-1]  # "vms" → "vm", "pods" → "pod"
    
    return last_segment


# =============================================================================
# REGISTRATION
# =============================================================================


def register_default_tools(deps: MEHOGraphDeps) -> None:
    """
    Register all default tool handlers.
    
    TASK-97: Uses generic tool names that work for ALL connector types.
    The agent doesn't need to know REST vs SOAP vs VMware.
    """
    # GENERIC TOOLS (work for all connector types)
    deps.register_tool("search_operations", search_operations_handler)
    deps.register_tool("call_operation",
        lambda d, a: call_operation_handler(d, a, state=None))
    deps.register_tool("search_types", search_types_handler)
    
    # Connector management
    deps.register_tool("list_connectors", list_connectors_handler)
    
    # Knowledge search
    deps.register_tool("search_knowledge", search_knowledge_handler)
    
    # Data reduction (Brain-Muscle architecture)
    deps.register_tool("reduce_data", reduce_data_handler)
