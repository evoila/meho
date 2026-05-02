# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Endpoint handlers for MEHO ReAct Graph (TASK-89)

Handles REST endpoint operations:
- search_endpoints_handler: Search for API endpoints
- call_endpoint_handler: Execute API calls

These are THIN WRAPPERS that delegate to MEHODependencies.
All business logic lives in MEHODependencies.
"""

import json
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.graph.graph_state import MEHOGraphState

logger = get_logger(__name__)


async def search_endpoints_handler(deps: MEHOGraphDeps, args: dict[str, Any]) -> str:
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
            limit=limit,
        )

        logger.info(f"✅ search_endpoints: found {len(results)} endpoints")
        return json.dumps(results, indent=2, default=str)

    except Exception as e:
        logger.error(f"❌ search_endpoints failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


async def call_endpoint_handler(  # NOSONAR (cognitive complexity)
    deps: MEHOGraphDeps, args: dict[str, Any], state: MEHOGraphState | None = None
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
    from meho_app.modules.agents.unified_executor import get_unified_executor

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

            # Delegate to MEHODependencies - it handles credentials, sessions, etc.
            result = await deps.meho_deps.call_endpoint(
                connector_id=connector_id or "",
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

            # UNIFIED CACHING (TASK-161): Token-aware tiering with schema hints
            if result.get("success") and result.get("data"):
                data = result.get("data")
                endpoint_path = result.get("endpoint_path", "")
                session_id = deps.session_id or "anonymous"
                data_list = data if isinstance(data, list) else [data]

                # Get response schema from endpoint (for schema-driven lookup)
                entity_type = None
                identifier_field = None
                display_name_field = None

                if deps.meho_deps and deps.meho_deps.endpoint_repo:
                    try:
                        endpoint = await deps.meho_deps.endpoint_repo.get_endpoint(endpoint_id)
                        if endpoint and endpoint.response_schema:
                            # Extract x-meho-* extensions from OpenAPI schema
                            schema = endpoint.response_schema
                            items_schema = (
                                schema.get("items", schema)
                                if schema.get("type") == "array"
                                else schema
                            )
                            entity_type = items_schema.get("x-meho-entity-type")

                            # Find identifier and display_name from properties
                            for prop_name, prop_def in items_schema.get("properties", {}).items():
                                if isinstance(prop_def, dict):
                                    if prop_def.get("x-meho-identifier"):
                                        identifier_field = prop_name
                                    if prop_def.get("x-meho-display-name"):
                                        display_name_field = prop_name
                    except Exception as e:
                        logger.warning(f"Could not fetch endpoint schema: {e}")

                # Unified caching with token-aware tiering
                cached, tier = await executor.cache_data_async(
                    session_id=session_id,
                    source_id=endpoint_id,
                    source_path=endpoint_path,
                    connector_id=connector_id or "",
                    connector_type="rest",
                    data=data_list,
                    entity_type=entity_type,
                    identifier_field=identifier_field,
                    display_name_field=display_name_field,
                )

                # Return tier-appropriate summary
                result = cached.to_llm_summary(tier)
                logger.info(f"📊 Unified cache: {cached.row_count} items, tier={tier.value}")

            results.append(result)

        # Return single result or batch summary
        if len(results) == 1:
            return json.dumps(results[0], indent=2, default=str)
        else:
            return json.dumps(
                {
                    "batch_results": results,
                    "total": len(results),
                    "successful": sum(1 for r in results if r.get("success")),
                },
                indent=2,
                default=str,
            )

    except Exception as e:
        logger.error(f"❌ call_endpoint failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})
