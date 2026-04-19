# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP Server exposing 4 curated READ-only tools for external AI agents and IDEs.

Tools:
    meho_investigate      -- Trigger a full MEHO investigation session
    meho_search_knowledge -- Search MEHO's knowledge base
    meho_query_topology   -- Query the topology graph
    meho_list_connectors  -- List available connectors and health status

All tools are annotated with readOnlyHint=True (no destructive operations).
All tool calls are audit-logged with user identity.

Transport: Streamable HTTP (mounted at /mcp in FastAPI) or stdio (Claude Desktop).
"""

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from meho_app.core.auth_context import UserContext

logger = logging.getLogger(__name__)

# Create the FastMCP server instance
# stateless_http=True: each request = fresh session (no session state needed)
mcp_server = FastMCP(
    name="MEHO Infrastructure Intelligence",
    stateless_http=True,
    streamable_http_path="/",  # Avoids /mcp/mcp when FastAPI mounts at /mcp (Pitfall 1)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_user_context(ctx: Context) -> UserContext:
    """Extract user context injected by MCPAuthMiddleware.

    The auth middleware stores user_context in scope["state"]["user_context"].
    For stdio transport (no middleware), returns a default system context.
    """
    try:
        request_ctx = ctx.request_context
        if request_ctx and hasattr(request_ctx, "scope"):
            scope = request_ctx.scope
            state = scope.get("state", {})
            user_context = state.get("user_context")
            if isinstance(user_context, UserContext):
                return user_context
    except Exception:
        pass

    # Fallback for stdio transport or missing middleware
    return UserContext(
        user_id="mcp-stdio",
        name="MCP stdio client",
        tenant_id="default",
        roles=["user"],
    )


async def _audit_log(
    user_context: UserContext,
    tool_name: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log an audit event for an MCP tool call.

    Best-effort: failures are logged but do not block the tool response.
    """
    try:
        from meho_app.database import get_session_maker
        from meho_app.modules.audit.service import AuditService

        session_maker = get_session_maker()
        async with session_maker() as session:
            audit_svc = AuditService(session)
            await audit_svc.log_event(
                tenant_id=user_context.tenant_id or "unknown",
                user_id=user_context.user_id,
                event_type="mcp.tool_call",
                action="execute",
                resource_type="mcp_tool",
                resource_name=tool_name,
                details=details or {},
                result="success",
            )
            await session.commit()
    except Exception:
        logger.warning(f"Failed to audit MCP tool call: {tool_name}", exc_info=True)


# ---------------------------------------------------------------------------
# Tool 1: meho_investigate
# ---------------------------------------------------------------------------


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def meho_investigate(
    query: str,
    connector_scope: list[str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Trigger a full MEHO investigation session.

    Sends a diagnostic query to MEHO's agent engine. The agent uses
    its ReAct loop to investigate across connected infrastructure
    systems. Returns investigation results including findings and
    connectors used.

    Args:
        query: The diagnostic question to investigate.
        connector_scope: Optional list of connector IDs to limit scope.
    """
    user_context = (
        _get_user_context(ctx)
        if ctx
        else UserContext(user_id="mcp-stdio", tenant_id="default", roles=["user"])
    )

    await _audit_log(user_context, "meho_investigate", {"query": query})

    try:
        import uuid

        from meho_app.database import get_session_maker
        from meho_app.modules.chat.service import ChatService

        session_maker = get_session_maker()
        async with session_maker() as session:
            chat_svc = ChatService(session)

            # Create a new session for this investigation
            session_id = str(uuid.uuid4())
            chat_session = await chat_svc.create_session(
                tenant_id=user_context.tenant_id or "default",
                user_id=user_context.user_id,
                title=f"MCP: {query[:80]}",
            )
            session_id = str(chat_session.id)

            # Send the message and collect the response
            response_text = ""
            async for event in chat_svc.send_message_streaming(
                session_id=session_id,
                user_message=query,
                user_id=user_context.user_id,
                tenant_id=user_context.tenant_id or "default",
            ):
                if hasattr(event, "data") and event.event == "message":
                    response_text += event.data

            result = {
                "session_id": session_id,
                "status": "completed",
                "summary": response_text[:2000] if response_text else "No response generated",
                "findings": [response_text] if response_text else [],
                "connectors_used": [],
            }

            return json.dumps(result, indent=2)

    except Exception as e:
        logger.error(f"MCP investigate failed: {e}", exc_info=True)
        return json.dumps(
            {
                "session_id": "",
                "status": "failed",
                "summary": f"Investigation failed: {e!s}",
                "findings": [],
                "connectors_used": [],
            }
        )


# ---------------------------------------------------------------------------
# Tool 2: meho_search_knowledge
# ---------------------------------------------------------------------------


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def meho_search_knowledge(
    query: str,
    limit: int = 10,
    ctx: Context | None = None,
) -> str:
    """Search MEHO's knowledge base for relevant information.

    Performs hybrid search (BM25 + semantic) across the knowledge base.
    Returns matching documents with text, score, and source metadata.

    Args:
        query: Search query for the knowledge base.
        limit: Maximum number of results (1-50, default 10).
    """
    user_context = (
        _get_user_context(ctx)
        if ctx
        else UserContext(user_id="mcp-stdio", tenant_id="default", roles=["user"])
    )

    await _audit_log(user_context, "meho_search_knowledge", {"query": query, "limit": limit})

    try:
        from meho_app.database import get_session_maker
        from meho_app.modules.knowledge.embeddings import get_embedding_provider
        from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
        from meho_app.modules.knowledge.repository import KnowledgeRepository

        session_maker = get_session_maker()
        async with session_maker() as session:
            repo = KnowledgeRepository(session)
            embedding_provider = get_embedding_provider()
            hybrid_svc = PostgresFTSHybridService(repo, embedding_provider)

            results = await hybrid_svc.search(
                query=query,
                user_context=user_context,
                top_k=min(limit, 50),
            )

            output = {
                "results": [
                    {
                        "text": r.get("text", "")[:500],
                        "score": r.get("rrf_score", 0),
                        "metadata": r.get("metadata", {}),
                    }
                    for r in results
                ],
                "total": len(results),
            }

            return json.dumps(output, indent=2, default=str)

    except Exception as e:
        logger.error(f"MCP search_knowledge failed: {e}", exc_info=True)
        return json.dumps({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool 3: meho_query_topology
# ---------------------------------------------------------------------------


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def meho_query_topology(  # NOSONAR (cognitive complexity)
    entity_name: str,
    entity_type: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Query MEHO's topology graph for entity details and relationships.

    Looks up an entity by name in the topology graph. Returns the entity
    details plus its relationships and cross-connector correlations.

    Args:
        entity_name: Name of the entity to look up.
        entity_type: Optional entity type filter (e.g., 'Pod', 'VM').
    """
    user_context = (
        _get_user_context(ctx)
        if ctx
        else UserContext(user_id="mcp-stdio", tenant_id="default", roles=["user"])
    )

    await _audit_log(
        user_context,
        "meho_query_topology",
        {"entity_name": entity_name, "entity_type": entity_type},
    )

    try:
        from meho_app.database import get_session_maker
        from meho_app.modules.topology.schemas import LookupTopologyInput
        from meho_app.modules.topology.service import TopologyService

        session_maker = get_session_maker()
        async with session_maker() as session:
            topology_svc = TopologyService(session)

            lookup_input = LookupTopologyInput(
                query=entity_name,
                traverse_depth=2,
                cross_connectors=True,
            )

            result = await topology_svc.lookup(
                input=lookup_input,
                tenant_id=user_context.tenant_id or "default",
            )

            if not result.found:
                return json.dumps(
                    {
                        "entity": None,
                        "relationships": [],
                        "suggestions": result.suggestions or [],
                    }
                )

            entity_dict = result.entity.model_dump(mode="json") if result.entity else None

            relationships = []
            if result.topology_chain:
                for item in result.topology_chain:
                    relationships.append(item.model_dump(mode="json"))

            same_as = []
            if result.same_as_entities:
                for corr in result.same_as_entities:
                    same_as.append(
                        {
                            "entity": corr.entity.model_dump(mode="json") if corr.entity else None,
                            "connector_type": corr.connector_type,
                            "verified_via": corr.verified_via,
                        }
                    )

            output = {
                "entity": entity_dict,
                "relationships": relationships,
                "same_as_entities": same_as,
                "connectors_traversed": result.connectors_traversed or [],
            }

            return json.dumps(output, indent=2, default=str)

    except Exception as e:
        logger.error(f"MCP query_topology failed: {e}", exc_info=True)
        return json.dumps({"entity": None, "relationships": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Tool 4: meho_list_connectors
# ---------------------------------------------------------------------------


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def meho_list_connectors(ctx: Context | None = None) -> str:
    """List available connectors and their health status.

    Returns all active connectors for the authenticated user's tenant,
    including connector ID, name, type, and active status.
    """
    user_context = (
        _get_user_context(ctx)
        if ctx
        else UserContext(user_id="mcp-stdio", tenant_id="default", roles=["user"])
    )

    await _audit_log(user_context, "meho_list_connectors")

    try:
        from meho_app.database import get_session_maker
        from meho_app.modules.connectors.service import ConnectorService

        session_maker = get_session_maker()
        async with session_maker() as session:
            connector_svc = ConnectorService(session)
            connectors = await connector_svc.list_connectors(
                tenant_id=user_context.tenant_id or "default",
                active_only=True,
            )

            output = {
                "connectors": [
                    {
                        "id": str(c.id),
                        "name": c.name,
                        "connector_type": c.connector_type,
                        "is_active": c.is_active,
                        "description": c.description or "",
                    }
                    for c in connectors
                ],
            }

            return json.dumps(output, indent=2)

    except Exception as e:
        logger.error(f"MCP list_connectors failed: {e}", exc_info=True)
        return json.dumps({"connectors": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------


def get_mcp_http_app() -> Any:
    """Get ASGI app for mounting at /mcp in FastAPI.

    Returns the FastMCP streamable HTTP app wrapped with auth middleware.
    Uses streamable_http_path="/" to avoid double-path issue (Pitfall 1:
    /mcp/mcp) when FastAPI mounts at /mcp.
    """
    from meho_app.api.mcp_server.auth import MCPAuthMiddleware

    raw_app = mcp_server.streamable_http_app()
    return MCPAuthMiddleware(raw_app)


async def run_stdio() -> None:
    """Run the curated MCP server as stdio for Claude Desktop."""
    await mcp_server.run(transport="stdio")  # type: ignore[func-returns-value,misc]  # FastMCP.run() is async but typed as returning None
