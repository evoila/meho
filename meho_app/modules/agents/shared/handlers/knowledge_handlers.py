# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge and utility handlers for MEHO ReAct Graph (TASK-89)

Handles:
- search_knowledge_handler: Search the knowledge base
- reduce_data_handler: Query cached data with SQL
- list_connectors_handler: List available system connectors

These are THIN WRAPPERS that delegate to MEHODependencies.
All business logic lives in MEHODependencies.

TRACING: Enhanced with comprehensive OTEL tracing for:
- Search queries and results
- SQL queries and result counts
- Connector listings
"""

import json
import time
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.persistence.event_context import get_transcript_collector
from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.handlers.tracing import (
    trace_sql_query,
    traced_tool_call,
)

logger = get_logger(__name__)


async def search_knowledge_handler(deps: MEHOGraphDeps, args: dict[str, Any]) -> str:
    """
    Search the knowledge base for documentation.

    By default, searches DOCUMENTATION only (excludes OpenAPI specs).
    Set include_apis=True to also search OpenAPI endpoint descriptions.

    Connector scoping:
    - Specialist agent passes connector_id -> strict scoping, no fallback
    - Orchestrator omits connector_id -> cross-connector search with attribution

    DELEGATES TO: MEHODependencies.search_docs() or search_knowledge()
    """
    query = args.get("query", "")
    limit = args.get("limit", 10)
    include_apis = args.get("include_apis", False)  # Default: docs only

    # Connector scoping: check deps first (specialist agent context), then args
    connector_id = getattr(deps, "connector_id", None) or args.get("connector_id")

    logger.info(
        f"search_knowledge: query='{query}', include_apis={include_apis}, "
        f"connector_id={connector_id or 'cross-connector'}"
    )

    async with traced_tool_call(
        "search_knowledge",
        args,
        user_id=deps.user_id,
        tenant_id=deps.tenant_id,
        session_id=deps.session_id,
        extra_attrs={"include_apis": include_apis, "connector_id": connector_id},
    ) as span:
        if not deps.meho_deps:
            error_result = json.dumps({"error": "MEHO dependencies not available"})
            span.set_error("MEHO dependencies not available")
            return error_result

        try:
            search_start = time.perf_counter()

            # Use the high-level MEHODependencies methods which have proper filtering
            if include_apis:
                # Search everything (docs + API specs)
                results = await deps.meho_deps.search_knowledge(
                    query=query, top_k=limit, score_threshold=0.6, connector_id=connector_id
                )
            else:
                # Search docs only (excludes OpenAPI specs) - better for general questions
                results = await deps.meho_deps.search_docs(
                    query=query, top_k=limit, score_threshold=0.6, connector_id=connector_id
                )

            search_duration_ms = (time.perf_counter() - search_start) * 1000

            if not results:
                result = json.dumps({"message": f"No knowledge found for '{query}'"})
                span.set_output({"result_count": 0})
                return result

            # Results are formatted as dicts from MEHODependencies
            # {text, source_uri, tags, connector_id, connector_name, connector_type}
            formatted = []
            for item in results:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    formatted.append(
                        {
                            "content": text[:800] + "..." if len(text) > 800 else text,
                            "source": item.get("source_uri"),
                            "tags": item.get("tags"),
                            "connector_name": item.get("connector_name"),
                            "connector_type": item.get("connector_type"),
                        }
                    )
                else:
                    # Fallback for KnowledgeChunk objects
                    text = item.text if hasattr(item, "text") else str(item)
                    formatted.append(
                        {
                            "content": text[:800] + "..." if len(text) > 800 else text,
                            "source": item.source_uri if hasattr(item, "source_uri") else None,
                        }
                    )

            logger.info(
                f"search_knowledge: found {len(formatted)} results "
                f"(include_apis={include_apis}, connector_id={connector_id or 'cross-connector'})"
            )
            span.set_output(formatted)
            span.add_attribute("result_count", len(formatted))

            logger.info(
                "Knowledge search completed",
                query=query[:100],
                count=len(formatted),
                include_apis=include_apis,
            )

            # Emit transcript event for deep observability
            try:
                collector = get_transcript_collector()
                if collector:
                    # Build result snippets for preview (max 3)
                    result_snippets = [
                        {"content": f["content"][:200], "source": f.get("source")}
                        for f in formatted[:3]
                    ]
                    search_type = "hybrid" if include_apis else "docs"
                    event = collector.create_knowledge_search_event(
                        summary=f"Knowledge search: '{query[:50]}' -> {len(formatted)} results",
                        query=query,
                        search_type=search_type,
                        results_count=len(formatted),
                        result_snippets=result_snippets,
                        duration_ms=search_duration_ms,
                    )
                    await collector.add(event)
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Non-blocking: don't let event emission break the handler

            return json.dumps(formatted, indent=2)

        except Exception as e:
            logger.error(f"❌ search_knowledge failed: {e}", exc_info=True)
            span.set_error(str(e))
            return json.dumps({"error": str(e)})


async def reduce_data_handler(deps: MEHOGraphDeps, args: dict[str, Any]) -> str:
    """
    Query cached data using SQL.

    Example:
        {"sql": "SELECT * FROM virtual_machines WHERE num_cpu > 8 ORDER BY memory_mb DESC"}

    Tables are automatically named from the operation (e.g., list_virtual_machines → virtual_machines).
    """
    from meho_app.modules.agents.unified_executor import get_unified_executor

    sql = args.get("sql")

    async with traced_tool_call(
        "reduce_data",
        args,
        user_id=deps.user_id,
        tenant_id=deps.tenant_id,
        session_id=deps.session_id,
    ) as span:
        # Pass Redis for persistent cache (L2) - critical for multi-turn!
        redis_client = deps.meho_deps.redis if deps.meho_deps else None
        executor = get_unified_executor(redis_client)
        session_id = deps.session_id or "anonymous"

        if not sql:
            # No SQL provided - show available tables
            tables_info = await executor.get_session_table_info_async(session_id)
            if tables_info:
                table_names = [t["table"] for t in tables_info]
                result = json.dumps(
                    {
                        "error": "'sql' parameter is required",
                        "available_tables": table_names,
                        "example": f'{{"sql": "SELECT * FROM {table_names[0]} LIMIT 10"}}'  # noqa: S608 -- static SQL query, no user input
                        if table_names
                        else None,
                    },
                    indent=2,
                )
                span.set_error("sql parameter is required")
                span.add_attribute("available_tables", table_names)
                return result
            else:
                result = json.dumps(
                    {
                        "error": "No cached data available. Call an API first.",
                        "hint": "Use call_operation to fetch data, then query with SQL.",
                    },
                    indent=2,
                )
                span.set_error("No cached data available")
                return result

        logger.info(f"🦆 reduce_data SQL: {sql[:100]}...")

        try:
            result = await executor.execute_sql_async(session_id, sql)

            row_count = result.get("count", 0)
            success = result.get("success", False)

            # Log detailed SQL trace
            trace_sql_query(
                operation="SELECT" if sql.strip().upper().startswith("SELECT") else "OTHER",
                sql=sql,
                row_count=row_count,
                error=result.get("error") if not success else None,
            )

            if success:
                logger.info(f"✅ SQL returned {row_count} rows")
                span.set_output(result)
                span.add_attribute("row_count", row_count)
            else:
                logger.warning(f"⚠️ SQL error: {result.get('error')}")
                span.set_error(result.get("error", "SQL execution failed"))

            return json.dumps(result, indent=2, default=str)

        except Exception as e:
            logger.error(f"❌ SQL execution failed: {e}", exc_info=True)
            span.set_error(str(e))
            return json.dumps({"error": str(e)})


async def list_connectors_handler(deps: MEHOGraphDeps, args: dict[str, Any]) -> str:
    """
    List all available system connectors.

    DELEGATES TO: MEHODependencies.connector_repo.list_connectors()
    """
    logger.info("🔍 list_connectors called")

    async with traced_tool_call(
        "list_connectors",
        args,
        user_id=deps.user_id,
        tenant_id=deps.tenant_id,
        session_id=deps.session_id,
    ) as span:
        if not deps.meho_deps or not deps.meho_deps.connector_repo:
            error_result = json.dumps({"error": "Connector repository not available"})
            span.set_error("Connector repository not available")
            return error_result

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
                    "protocol": getattr(c, "protocol", "rest"),  # REST or SOAP
                    "connector_type": getattr(c, "connector_type", "rest"),  # rest, soap, vmware
                    "auth_type": c.auth_type,
                    "is_active": c.is_active,
                }
                for c in raw_connectors
            ]

            logger.info(f"✅ list_connectors: found {len(connectors)} connectors")
            span.set_output(connectors)
            span.add_attribute("connector_count", len(connectors))

            logger.info("Listed connectors", count=len(connectors))

            return json.dumps(connectors, indent=2, default=str)

        except Exception as e:
            logger.error(f"❌ list_connectors failed: {e}", exc_info=True)
            span.set_error(str(e))
            return json.dumps({"error": str(e)})
