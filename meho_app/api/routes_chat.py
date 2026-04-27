# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Chat routes with streaming support (Server-Sent Events).

Provides Cursor-like streaming UX for MEHO chat interface.
"""

# mypy: disable-error-code="no-untyped-def,arg-type"
import contextlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.config import get_api_config
from meho_app.api.dependencies import (
    CurrentUser,
    DbSession,
    create_state_store,
    get_agent_service_dep,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# =============================================================================
# Chat Commands (TASK-186 Phase 5)
# =============================================================================


async def process_chat_command(
    message: str,
    session_id: str | None,
    user: "UserContext",
    db_session: AsyncSession,
) -> str | None:
    """
    Process slash commands like /explain-last.

    Args:
        message: The user's message.
        session_id: Current session ID.
        user: Current authenticated user.
        db_session: Database session.

    Returns:
        Command response string if message is a command, None otherwise.
    """
    msg = message.strip()

    if msg.startswith("/explain-last"):
        return await _handle_explain_last(session_id, user, db_session)

    if msg.startswith("/help"):
        return _handle_help_command()

    return None


async def _handle_explain_last(  # NOSONAR (cognitive complexity)
    session_id: str | None,
    user: "UserContext",
    db_session: AsyncSession,
) -> str:
    """
    Handle /explain-last command - explain what happened in the previous turn.

    Returns a human-readable explanation of the last session's execution.
    """
    from sqlalchemy import desc, select

    from meho_app.modules.agents.models import ChatSessionModel
    from meho_app.modules.agents.persistence.transcript_service import TranscriptService

    try:
        # Find the most recent session with a transcript for this tenant
        stmt = (
            select(ChatSessionModel)
            .where(ChatSessionModel.tenant_id == user.tenant_id)
            .order_by(desc(ChatSessionModel.created_at))
            .limit(1)
        )
        result = await db_session.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if chat_session is None:
            return "No previous sessions found. Run a query first, then use /explain-last to understand what happened."

        # Get transcript for this session
        service = TranscriptService(db_session)
        transcript = await service.get_transcript(chat_session.id)

        if transcript is None:
            return "No execution transcript found for the last session. The session may not have generated detailed events yet."

        # Get events for analysis
        events = await service.get_events(transcript.id, limit=50)

        # Build explanation
        lines = []
        lines.append("## What happened in your last query\n")
        lines.append(f"**Query:** {transcript.user_query or 'N/A'}")
        lines.append(f"**Status:** {transcript.status}")
        lines.append(f"**Duration:** {transcript.total_duration_ms:.0f}ms\n")

        # Summary stats
        lines.append("### Execution Summary\n")
        if transcript.total_llm_calls > 0:
            lines.append(f"- **{transcript.total_llm_calls}** LLM call(s)")
        if transcript.total_operation_calls > 0:
            lines.append(f"- **{transcript.total_operation_calls}** operation call(s)")
        if transcript.total_sql_queries > 0:
            lines.append(f"- **{transcript.total_sql_queries}** SQL query(ies)")
        if transcript.total_tool_calls > 0:
            lines.append(f"- **{transcript.total_tool_calls}** tool call(s)")
        lines.append(f"- **{transcript.total_tokens:,}** tokens used")
        if transcript.total_cost_usd:
            lines.append(f"- Estimated cost: **${transcript.total_cost_usd:.4f}**")
        lines.append("")

        # Key events timeline
        major_events = [
            e
            for e in events
            if e.type in ("thought", "action", "observation", "error", "final_answer")
        ][:8]

        if major_events:
            lines.append("### Step-by-Step\n")
            for i, e in enumerate(major_events, 1):
                icon = {  # type: ignore[call-overload]  # SQLAlchemy ORM attribute access
                    "thought": "thinking",
                    "action": "tool",
                    "observation": "observe",
                    "error": "error",
                    "final_answer": "done",
                }.get(e.type, "-")
                lines.append(
                    f"{i}. [{icon}] **{e.type.replace('_', ' ').title()}**: {e.summary[:120]}"
                )
            lines.append("")

        # Check for errors
        error_events = [
            e for e in events if e.type == "error" or (e.details and e.details.get("tool_error"))
        ]
        if error_events:
            lines.append("### Issues Found\n")
            for e in error_events[:3]:
                lines.append(f"- Error: {e.summary}")
                if e.details and e.details.get("tool_error"):
                    lines.append(f"  Details: {e.details.get('tool_error')[:200]}")
            lines.append("")

        lines.append(f"\n*View full transcript at: /sessions/{chat_session.id}*")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error handling /explain-last: {e}", exc_info=True)
        return f"Error generating explanation: {e!s}"


def _handle_help_command() -> str:
    """Handle /help command - show available commands."""
    return """## Available Commands

- **/explain-last** - Explain what happened in your last query
  Shows the execution flow, LLM calls, HTTP requests, and any errors.

- **/help** - Show this help message

*Tip: Use /explain-last after a query to understand how MEHO processed your request.*
"""


async def get_conversation_history(  # NOSONAR (cognitive complexity)
    session_id: str,
    agent_service: "AgentService",  # type: ignore[name-defined]  # noqa: F821
    limit: int = 10,
) -> list[dict[str, str]]:
    """
    Fetch conversation history for context-aware chat.

    Enables LLM to understand references like "that endpoint" or "the system we discussed".

    Args:
        session_id: Chat session ID
        agent_service: AgentService instance
        limit: Maximum number of messages to fetch (default: 10, optimized for performance)

    Returns:
        List of messages in format: [{"role": "user", "content": "..."}, ...]
        Ordered oldest to newest (for LLM context)
    """
    if not session_id:
        return []

    try:
        # Use AgentService to fetch session data directly
        session_data = await agent_service.get_chat_session(session_id, include_messages=True)

        if not session_data or not session_data.messages:
            logger.warning(f"No session data found for {session_id}")
            return []

        # Convert ChatMessage objects to dicts
        messages = [
            {
                "role": msg.role,
                "content": msg.content,
                "message_data": msg.message_data if hasattr(msg, "message_data") else None,
                "sender_name": getattr(msg, "sender_name", None),  # Phase 39: war room attribution
            }
            for msg in session_data.messages
        ]

        # Take last N messages and format for LLM
        recent_messages = messages[-limit:] if len(messages) > limit else messages

        # Return full message data if available (Session 69 - includes tool calls)
        # Otherwise fallback to simple role/content format
        history = []
        for msg in recent_messages:
            if msg.get("message_data"):
                # Full PydanticAI message available - use it!
                message_data = msg["message_data"]

                # message_data could be:
                # - A single message dict: {"role": "...", "parts": [...]}
                # - A list of message dicts: [{...}, {...}, ...]
                if isinstance(message_data, list):
                    # Multiple messages (e.g., all new_messages from a turn)
                    # Extend history with all of them
                    history.extend(message_data)
                elif isinstance(message_data, dict):
                    # Single message - append it
                    history.append(message_data)
                else:
                    # Unexpected format - log and skip
                    logger.warning(f"Unexpected message_data type: {type(message_data)}")
            else:
                # Fallback to simple format (backward compatibility)
                # Phase 39: Include sender_name for war room attribution
                entry = {"role": msg["role"], "content": msg["content"]}
                if msg.get("sender_name"):
                    entry["sender_name"] = msg["sender_name"]
                history.append(entry)

        logger.info(f"Fetched {len(history)} messages from session {session_id} for context")
        tool_call_count = sum(1 for msg in history if isinstance(msg, dict) and "parts" in msg)
        if tool_call_count > 0:
            logger.info(f"   Including {tool_call_count} messages with tool calls/results")
        return history

    except Exception as e:
        logger.error(f"Error fetching conversation history: {e}", exc_info=True)
        return []


class ChatRequest(BaseModel):
    """Chat request from user"""

    message: str
    system_context: str | None = None  # Optional: specific system to query
    session_id: str | None = None  # Optional: link to chat session
    connector_id: str | None = None  # Phase 63: @mention direct routing bypass
    session_mode: str | None = "agent"  # Phase 65: "ask" or "agent" mode


class ChatResponse(BaseModel):
    """Chat response (non-streaming)"""

    response: str
    workflow_id: str | None = None


# =============================================================================
# TASK-76: Approval Flow Endpoints
# =============================================================================


class ApprovalDecision(BaseModel):
    """Request body for approval decision."""

    approved: bool
    reason: str | None = None


class ApprovalResponse(BaseModel):
    """Response from approval decision."""

    status: str  # "approved", "rejected", "error"
    message: str
    approval_id: str | None = None


class PendingApproval(BaseModel):
    """Pending approval request info."""

    approval_id: str
    tool_name: str
    danger_level: str
    method: str | None = None
    path: str | None = None
    description: str | None = None
    tool_args: dict[str, Any] | None = None
    created_at: str


@router.post("/{session_id}/approve/{approval_id}", response_model=ApprovalResponse)
async def approve_action(  # NOSONAR (cognitive complexity)
    session_id: str,
    approval_id: str,
    decision: ApprovalDecision,
    user: CurrentUser,
    db_session: DbSession,
):
    """
    Approve or reject a pending action.

    TASK-76: Approval Flow Architecture

    When agent wants to execute a dangerous operation (POST, PUT, DELETE),
    it yields an approval_required event. The frontend displays an approval
    dialog, and the user calls this endpoint to approve or reject.

    Args:
        session_id: Chat session ID
        approval_id: Approval request UUID
        decision: ApprovalDecision with approved=True/False

    Returns:
        ApprovalResponse with status and message
    """
    from uuid import UUID

    from meho_app.modules.agents.approval import ApprovalStore
    from meho_app.modules.agents.approval.exceptions import (
        ApprovalAlreadyDecided,
        ApprovalExpired,
        ApprovalNotFound,
    )
    from meho_app.modules.agents.approval.pending_approvals import resolve_pending

    logger.info(
        f"Approval decision: session={session_id[:8]}..., "
        f"approval={approval_id[:8]}..., approved={decision.approved}"
    )

    # Phase 38: Group-aware approval authorization
    from meho_app.modules.agents.service import AgentService

    agent_svc = AgentService(db_session)
    session_obj = await agent_svc.get_chat_session(session_id, include_messages=False)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found")
    if session_obj.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if session_obj.visibility == "private" and session_obj.user_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the session owner can approve in private sessions",
        )

    try:
        store = ApprovalStore(db_session)

        if decision.approved:
            await store.approve(
                approval_id=UUID(approval_id),
                decided_by=user.user_id,
                reason=decision.reason,
            )
            await db_session.commit()

            # Phase 5: Signal the waiting agent coroutine via asyncio.Event
            signaled = resolve_pending(str(session_id), approved=True)
            if signaled:
                logger.info(f"Signaled pending approval for session {session_id[:8]}...")

            logger.info(f"Approval {approval_id[:8]}... approved by {user.user_id}")

            # Audit: log workflow approval
            try:
                from meho_app.modules.audit.service import AuditService

                audit = AuditService(db_session)
                await audit.log_event(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    user_email=getattr(user, "email", None),
                    event_type="workflow.approve",
                    action="approve",
                    resource_type="workflow",
                    resource_id=approval_id,
                    details={"session_id": session_id},
                    result="success",
                )
                await db_session.commit()
            except Exception as audit_err:
                logger.warning(f"Audit logging failed for approval: {audit_err}")

            return ApprovalResponse(
                status="approved",
                message="Action approved and executing.",
                approval_id=approval_id,
            )
        else:
            await store.reject(
                approval_id=UUID(approval_id),
                decided_by=user.user_id,
                reason=decision.reason,
            )
            await db_session.commit()

            # Phase 5: Signal the waiting agent coroutine via asyncio.Event
            signaled = resolve_pending(str(session_id), approved=False)
            if signaled:
                logger.info(f"Signaled denial for session {session_id[:8]}...")

            logger.info(f"Approval {approval_id[:8]}... rejected by {user.user_id}")

            # Audit: log workflow denial
            try:
                from meho_app.modules.audit.service import AuditService

                audit = AuditService(db_session)
                await audit.log_event(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    user_email=getattr(user, "email", None),
                    event_type="workflow.deny",
                    action="deny",
                    resource_type="workflow",
                    resource_id=approval_id,
                    details={"session_id": session_id},
                    result="success",
                )
                await db_session.commit()
            except Exception as audit_err:
                logger.warning(f"Audit logging failed for denial: {audit_err}")

            return ApprovalResponse(
                status="rejected",
                message="Action denied. Agent will adapt.",
                approval_id=approval_id,
            )

    except ApprovalNotFound:
        logger.warning(f"Approval not found in DB: {approval_id}. Trying in-memory resolve.")
        signaled = resolve_pending(str(session_id), approved=decision.approved)
        if signaled:
            logger.info(f"In-memory approval resolved for session {session_id[:8]}...")
            status = "approved" if decision.approved else "rejected"
            return ApprovalResponse(
                status=status,
                message="Action approved and executing."
                if decision.approved
                else "Action denied. Agent will adapt.",
                approval_id=approval_id,
            )
        raise HTTPException(status_code=404, detail="Approval request not found") from None

    except ApprovalExpired as e:
        logger.warning(f"⚠️ Approval expired: {approval_id}")
        raise HTTPException(
            status_code=400, detail=f"Approval request expired at {e.expired_at}"
        ) from e

    except ApprovalAlreadyDecided as e:
        logger.warning(f"⚠️ Approval already decided: {approval_id} ({e.current_status})")
        raise HTTPException(status_code=400, detail=f"Approval already {e.current_status}") from e

    except Exception as e:
        logger.error(f"❌ Error processing approval: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{session_id}/pending-approvals", response_model=list[PendingApproval])
async def get_pending_approvals(
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
):
    """
    Get all pending approval requests for a session.

    TASK-76: Approval Flow Architecture

    Used by frontend to show pending approval dialogs if user refreshes
    or reconnects while an approval is pending.

    Args:
        session_id: Chat session ID

    Returns:
        List of PendingApproval objects
    """
    from uuid import UUID

    from meho_app.modules.agents.approval import ApprovalStore

    logger.info(f"Getting pending approvals for session {session_id[:8]}...")

    # Phase 38: Group-aware access check for pending approvals
    from meho_app.modules.agents.service import AgentService

    agent_svc = AgentService(db_session)
    session_obj = await agent_svc.get_chat_session(session_id, include_messages=False)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found")
    if session_obj.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if session_obj.visibility == "private" and session_obj.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        store = ApprovalStore(db_session)

        pending = await store.get_pending_for_session(
            session_id=UUID(session_id),
            tenant_id=user.tenant_id,
        )

        return [
            PendingApproval(
                approval_id=str(p.id),
                tool_name=p.tool_name,
                danger_level=p.danger_level,
                method=p.http_method,
                path=p.endpoint_path,
                description=p.description,
                tool_args=p.tool_args,
                created_at=p.created_at.isoformat(),
            )
            for p in pending
        ]

    except Exception as e:
        logger.error(f"❌ Error getting pending approvals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


class ResumeRequest(BaseModel):
    """Request body for resume after approval."""

    approval_id: str


class ResumeResponse(BaseModel):
    """Response from resume after approval."""

    status: str  # "success", "error"
    message: str


@router.post("/{session_id}/resume", response_model=ResumeResponse)
async def resume_after_approval(
    session_id: str,
    request: ResumeRequest,
    user: CurrentUser,
    db_session: DbSession,
):
    """
    Resume execution after approval.

    TASK-76: After user approves a dangerous action, this endpoint
    triggers re-execution of the original user message. Since the
    approval now exists in the database, the tool will find it and
    proceed without asking again.

    This returns immediately with a status - the actual execution
    happens when the frontend re-sends the chat message via /stream.

    Note: The recommended flow is:
    1. User sees approval_required event
    2. User clicks "Allow" → POST /approve/{approval_id}
    3. Frontend re-submits original message to /stream
    4. Agent finds existing approval, executes tool

    This endpoint is provided for cases where frontend needs to
    explicitly trigger resume rather than re-sending the message.

    Args:
        session_id: Chat session ID
        request: ResumeRequest with approval_id

    Returns:
        ResumeResponse with status and message
    """
    from uuid import UUID

    from meho_app.modules.agents.approval import ApprovalStore
    from meho_app.modules.agents.models import ApprovalStatus

    logger.info(
        f"🔄 Resume request: session={session_id[:8]}..., approval={request.approval_id[:8]}..."
    )

    try:
        store = ApprovalStore(db_session)

        # Get the approval request
        approval = await store.get_by_id(UUID(request.approval_id))

        if not approval:
            raise HTTPException(status_code=404, detail="Approval not found")

        # SECURITY: Verify tenant ownership (IDOR fix)
        if approval.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Approval not found")

        # Check if it belongs to this session
        if str(approval.session_id) != session_id:
            raise HTTPException(status_code=400, detail="Approval does not belong to this session")

        # Check if it's approved
        if approval.status != ApprovalStatus.APPROVED:
            return ResumeResponse(
                status="error", message=f"Approval is {approval.status.value}, not approved"
            )

        # The frontend should re-send the original message to /stream
        # The agent will find the approval and execute the tool
        return ResumeResponse(
            status="success",
            message="Approval is valid. Re-send the original message to /chat/stream to execute.",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error resuming: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Phase 39: Session access check helper (shared across endpoints)
# =============================================================================


async def check_session_access(
    session_obj: "ChatSessionModel",  # type: ignore[name-defined]  # noqa: F821
    user: UserContext,
) -> None:
    """Verify user has access to a session based on visibility.

    Tenant isolation: user must belong to the same tenant.
    Privacy: private sessions are restricted to the owner.
    Group/tenant sessions are accessible to any user in the tenant.

    Args:
        session_obj: The ChatSessionModel to check access for.
        user: The authenticated user.

    Raises:
        HTTPException: 403 if access is denied.
    """
    if session_obj.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if session_obj.visibility == "private" and str(session_obj.user_id) != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")


# =============================================================================
# Phase 38: Viewer SSE endpoint for group session fan-out
# =============================================================================


@router.get("/{session_id}/events")
async def session_events_stream(
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
):
    """
    Stream real-time events for a group session via Redis pub/sub.

    Phase 38: Viewers watching a group/tenant session subscribe to this
    endpoint. Events published by the agent's chat_stream are relayed
    in real-time through Redis pub/sub fan-out.

    Args:
        session_id: Chat session UUID
        user: Authenticated user
        db_session: Database session

    Returns:
        StreamingResponse with SSE events
    """
    from meho_app.core.redis import get_redis_client
    from meho_app.modules.agents.service import AgentService
    from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster

    # Load session and verify access
    agent_svc = AgentService(db_session)
    session_obj = await agent_svc.get_chat_session(session_id, include_messages=False)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found")
    await check_session_access(session_obj, user)

    api_config = get_api_config()
    redis_client = await get_redis_client(api_config.redis_url)
    broadcaster = RedisSSEBroadcaster(redis_client)

    async def generate():
        async for event in broadcaster.subscribe(session_id):
            if event.get("type") == "keepalive":
                # Send SSE comment keepalive instead of data event
                yield ": keepalive\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# TASK-89: ReAct Graph Architecture (v2 Streaming)
# =============================================================================


@router.post("/stream")
async def chat_stream(  # NOSONAR (cognitive complexity)
    request: ChatRequest,
    user: CurrentUser,
):
    """
    Stream chat responses using the ReAct graph architecture (TASK-89).

    ReAct-based implementation that provides:
    - Explicit Thought → Action → Observation loops
    - Visible reasoning (thoughts shown to user)
    - Built-in depth limiting (no infinite loops)
    - Cleaner approval flow integration
    - Generic design - works with REST, SOAP, VMware, any system

    SSE Message Types:
    - 'thought': Agent reasoning step
    - 'action': Tool being called
    - 'observation': Tool result
    - 'approval_required': Needs user approval
    - 'final_answer': Complete response
    - 'done': Stream complete
    - 'error': Something failed
    """
    from meho_app.api.dependencies import create_agent_dependencies
    from meho_app.core.redis import get_redis_client
    from meho_app.modules.agents.unified_executor import get_unified_executor

    async def generate():
        """Generate SSE stream with ReAct graph"""
        # Get a fresh database session for this request
        from meho_app.database import get_session_maker

        session_maker = get_session_maker()
        async with session_maker() as session:
            state_store = create_state_store()

            # TASK-93: Initialize UnifiedExecutor with Redis for persistent response cache
            config = get_api_config()
            redis_client = get_redis_client(config.redis_url)
            get_unified_executor(redis_client=redis_client)  # Upgrade singleton with Redis
            # Get AgentService for this session
            agent_service = get_agent_service_dep(session)

            try:
                # Initialize early so finally block never hits UnboundLocalError
                final_answer_content = None
                synthesis_chunks_acc: list[str] = []

                # Load agent state from Redis (same as v1!)
                from meho_app.modules.agents.session_state import AgentSessionState

                session_state = None
                if request.session_id:
                    session_state = await state_store.load_state(request.session_id)
                    if session_state:
                        logger.info(f"📬 v2: Loaded state for session {request.session_id[:8]}...")
                    else:
                        logger.info(
                            f"📭 v2: No state for {request.session_id[:8]}... (creating new)"
                        )
                        session_state = AgentSessionState()
                else:
                    session_state = AgentSessionState()

                # Phase 65: Resolve session_mode
                # Priority: request body > stored session > default "agent"
                effective_session_mode = request.session_mode or "agent"
                if effective_session_mode not in ("ask", "agent"):
                    effective_session_mode = "agent"

                # Create dependencies with loaded state
                dependencies = create_agent_dependencies(user, session, request.message)
                dependencies.session_state = session_state  # Inject loaded state!

                # Setup approval store
                if request.session_id:
                    from meho_app.modules.agents.approval import ApprovalStore

                    ApprovalStore(session)
                else:
                    pass

                # Load conversation history for multi-turn context
                conversation_history = (
                    await get_conversation_history(request.session_id, agent_service)
                    if request.session_id
                    else []
                )
                logger.info(
                    f"📜 Loaded {len(conversation_history)} messages from conversation history"
                )

                # TASK-186 Phase 5: Check for slash commands first
                command_response = await process_chat_command(
                    request.message, request.session_id, user, session
                )
                if command_response:
                    # It's a command - return the response directly
                    yield f"data: {json.dumps({'type': 'final_answer', 'content': command_response})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Phase 39/59: Multi-user authorization and session setup
                broadcaster = None
                is_group_session = False
                session_obj = None
                if request.session_id:
                    session_obj = await agent_service.get_chat_session(
                        request.session_id, include_messages=False
                    )
                    if session_obj:
                        # Phase 39: Check access (403 for wrong tenant or non-owner on private)
                        await check_session_access(session_obj, user)

                        # Phase 65: Read stored session_mode if request didn't specify one
                        stored_mode = getattr(session_obj, "session_mode", "agent")
                        if request.session_mode and request.session_mode != stored_mode:
                            # User is switching mode mid-session -- update DB
                            try:
                                session_obj.session_mode = effective_session_mode
                                await session.commit()
                                logger.info(
                                    f"Phase 65: Session mode changed to '{effective_session_mode}' "
                                    f"for {request.session_id[:8]}..."
                                )
                            except Exception as mode_err:
                                logger.warning(f"Failed to persist session_mode: {mode_err}")
                        elif not request.session_mode or request.session_mode == "agent":
                            # Use stored mode if request didn't explicitly set one
                            effective_session_mode = stored_mode or "agent"

                        # Phase 59: Always create broadcaster so events SSE works
                        # for ALL sessions (private included), enabling reconnect on return
                        from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster

                        broadcaster = RedisSSEBroadcaster(redis_client)

                        if getattr(session_obj, "visibility", "private") != "private":
                            is_group_session = True

                            # Phase 39: Atomic processing guard via Redis SETNX
                            # SETNX doubles as the active flag for group sessions
                            # (do NOT call set_active() first — it uses the same key
                            #  and would cause SETNX to always fail)
                            acquired = await redis_client.set(
                                f"meho:active:{request.session_id}",
                                user.user_id,
                                nx=True,
                                ex=300,
                            )
                            if not acquired:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Agent is currently processing",
                                )

                            # Phase 39: Broadcast processing_started event
                            await broadcaster.publish(
                                request.session_id,
                                {"type": "processing_started", "sender_id": user.user_id},
                            )
                            logger.info(
                                f"Group session {request.session_id[:8]}... - "
                                f"broadcasting enabled, SETNX acquired by {user.user_id}"
                            )
                        else:
                            # Phase 59: Set active flag for private sessions
                            # (enables is_active detection for reconnect)
                            try:
                                await broadcaster.set_active(request.session_id)
                            except Exception:
                                logger.warning(
                                    f"Failed to set active flag for {request.session_id[:8]}..."
                                )

                # Save user message to session BEFORE streaming (for next turn context)
                # This is critical for multi-turn conversations!
                if request.session_id:
                    try:
                        # Auto-create session if it doesn't exist
                        if len(conversation_history) == 0 and not session_obj:
                            logger.info(f"📝 v2: Auto-creating session {request.session_id[:8]}...")
                            await agent_service.create_chat_session(
                                tenant_id=user.tenant_id or "default",
                                user_id=user.user_id or "anonymous",
                                title=f"Chat {request.session_id[:8]}",
                                session_id=request.session_id,
                            )

                        # Phase 39: Save user message with sender attribution
                        sender_display = user.name or user.user_id
                        await agent_service.add_chat_message(
                            session_id=request.session_id,
                            role="user",
                            content=request.message,
                            message_data=None,
                            sender_id=user.user_id,
                            sender_name=sender_display,
                        )
                        logger.info(
                            f"💾 v2: Saved user message to session (sender: {sender_display})"
                        )

                        # Phase 39: Broadcast user_message event for group session viewers
                        if broadcaster and is_group_session:
                            await broadcaster.publish(
                                request.session_id,
                                {
                                    "type": "user_message",
                                    "content": request.message,
                                    "sender_id": user.user_id,
                                    "sender_name": sender_display,
                                },
                            )
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.error(f"v2: Failed to save user message: {e}")

                # Reset tracking for this streaming iteration
                final_answer_content = None
                synthesis_chunks_acc.clear()

                # Phase 63: @mention direct routing bypass
                # When connector_id is provided, skip orchestrator and route
                # directly to the specified connector's specialist agent.
                if request.connector_id:
                    from meho_app.modules.agents.adapter import _convert_event_to_sse_format
                    from meho_app.modules.agents.factory import create_agent

                    # Look up the connector
                    connector = await dependencies.connector_repo.get_connector(
                        request.connector_id, tenant_id=user.tenant_id
                    )
                    if not connector:
                        yield f"data: {json.dumps({'type': 'error', 'message': f'Connector {request.connector_id} not found'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                    logger.info(
                        f"Phase 63: @mention direct routing to connector "
                        f"{connector.name} ({request.connector_id[:8]}...)"
                    )

                    specialist = create_agent(
                        dependencies=dependencies,
                        connector_id=str(connector.id),
                        connector_name=connector.name,
                        connector_type=connector.connector_type,
                        routing_description=connector.description or "",
                        skill_name=getattr(connector, "skill_name", None),
                        iteration=1,
                        prior_findings=[],
                        generated_skill=getattr(connector, "generated_skill", None),
                        custom_skill=getattr(connector, "custom_skill", None),
                    )

                    async for event in specialist.run_streaming(
                        user_message=request.message,
                        session_id=request.session_id,
                        context={"iteration": 1, "prior_findings": []},
                    ):
                        sse_data = _convert_event_to_sse_format(event)
                        # Wrap specialist events as agent_event for consistent frontend handling
                        wrapped = {
                            "type": "agent_event",
                            "agent_source": {
                                "connector_id": str(connector.id),
                                "connector_name": connector.name,
                                "iteration": 1,
                            },
                            "inner_event": {
                                "type": sse_data.get("type", ""),
                                "data": {k: v for k, v in sse_data.items() if k != "type"},
                            },
                        }
                        yield f"data: {json.dumps(wrapped)}\n\n"

                        if broadcaster:
                            try:
                                await broadcaster.publish(request.session_id, wrapped)
                            except Exception as pub_err:
                                logger.warning(f"Failed to publish event to Redis: {pub_err}")

                        # Track final answer from specialist
                        if sse_data.get("type") == "final_answer":
                            final_answer_content = sse_data.get("content", "")
                            # Also emit as top-level final_answer for frontend
                            yield f"data: {json.dumps({'type': 'final_answer', 'content': final_answer_content})}\n\n"

                else:
                    # Normal orchestrator flow (existing code, unchanged)
                    from meho_app.api.dependencies import create_agent_state_store
                    from meho_app.modules.agents.adapter import run_orchestrator_streaming
                    from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

                    logger.info("Using OrchestratorAgent")

                    agent = OrchestratorAgent(dependencies=dependencies)

                    # TASK-185: Create state store for multi-turn persistence
                    state_store = create_agent_state_store()  # type: ignore[assignment]  # AgentStateStore subclasses RedisStateStore at runtime

                    event_stream = run_orchestrator_streaming(
                        agent=agent,
                        user_message=request.message,
                        session_id=request.session_id,
                        conversation_history=conversation_history,
                        state_store=state_store,
                        session_mode=effective_session_mode,
                    )

                    # Stream events (unified for both old and new agents)
                    async for sse_data in event_stream:
                        yield f"data: {json.dumps(sse_data)}\n\n"

                        # Phase 38: Publish to Redis for group session viewers
                        if broadcaster:
                            try:
                                await broadcaster.publish(request.session_id, sse_data)
                            except Exception as pub_err:
                                logger.warning(f"Failed to publish event to Redis: {pub_err}")

                        # Log progress
                        event_type = sse_data.get("type", "")
                        if event_type == "thought":
                            logger.info(f"💭 Thought: {sse_data.get('content', '')[:50]}...")
                        elif event_type == "action":
                            logger.info(f"🔧 Action: {sse_data.get('tool', '')}")
                        elif event_type == "observation":
                            logger.info(f"👁️ Observation: {str(sse_data.get('result', ''))[:50]}...")
                        elif event_type == "synthesis_chunk":
                            chunk_content = sse_data.get("content", "")
                            if chunk_content:
                                synthesis_chunks_acc.append(chunk_content)
                        elif event_type == "final_answer":
                            logger.info("✅ Final Answer ready")
                            # Capture final answer for saving
                            final_answer_content = sse_data.get("content", "")
                        elif event_type == "approval_required":
                            logger.info(f"🚨 Approval required: {sse_data.get('description', '')}")

                # Phase 63-02: Emit context_usage SSE event before done
                try:
                    all_messages = (
                        await get_conversation_history(request.session_id, agent_service, limit=500)
                        if request.session_id
                        else []
                    )
                    # Estimate tokens: ~4 chars per token heuristic
                    conversation_tokens = sum(
                        len(str(m.get("content", ""))) // 4 for m in all_messages
                    )
                    context_limit = 200000
                    usage_pct = min(100, int(conversation_tokens / context_limit * 100))
                    yield f"data: {json.dumps({'type': 'context_usage', 'percentage': usage_pct, 'tokens_used': conversation_tokens, 'tokens_limit': context_limit})}\n\n"
                except Exception as ctx_err:
                    logger.warning(f"Failed to compute context_usage: {ctx_err}")

                # Send completion
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

                # Note: Orchestrator saves its own state in adapter.py

            except HTTPException as http_exc:
                # Phase 39: HTTPException (e.g. 409 SETNX conflict) -- surface to client
                yield f"data: {json.dumps({'type': 'error', 'message': http_exc.detail, 'status_code': http_exc.status_code})}\n\n"
            except Exception as e:
                logger.error(f"ReAct graph error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            finally:
                # Phase 59: Save assistant message even on client disconnect
                save_content = final_answer_content or (
                    "".join(synthesis_chunks_acc) if synthesis_chunks_acc else None
                )
                if request.session_id and save_content:
                    try:
                        await agent_service.add_chat_message(
                            session_id=request.session_id,
                            role="assistant",
                            content=save_content,
                            message_data=None,
                        )
                        logger.info("v2: Saved assistant message to session")
                    except Exception as e:
                        logger.error(f"v2: Failed to save assistant message: {e}")

                # Phase 39: Broadcast processing_complete and clear active status
                if broadcaster and request.session_id:
                    try:
                        await broadcaster.publish(
                            request.session_id,
                            {"type": "processing_complete"},
                        )
                    except Exception:
                        logger.warning(
                            f"Failed to publish processing_complete for {request.session_id[:8]}..."
                        )
                    try:
                        await broadcaster.clear_active(request.session_id)
                    except Exception:
                        logger.warning(
                            f"Failed to clear active status for {request.session_id[:8]}..."
                        )
                # Also clear via raw redis delete for SETNX key (covers non-broadcaster path)
                if is_group_session and request.session_id:
                    with contextlib.suppress(Exception):
                        await redis_client.delete(f"meho:active:{request.session_id}")
                await session.close()

    return StreamingResponse(generate(), media_type="text/event-stream")


# =============================================================================
# Phase 63-02: Session Summarize Endpoint
# =============================================================================


class SummarizeResponse(BaseModel):
    """Response from session summarize endpoint."""

    new_session_id: str
    summary: str


@router.post("/sessions/{session_id}/summarize", response_model=SummarizeResponse)
async def summarize_session(
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
):
    """
    Summarize the current session and create a new one with the summary.

    Phase 63-02: Context monitoring handoff. When context usage is high,
    users can start a new chat with an LLM-generated investigation summary
    as the first message, preserving continuity.

    Args:
        session_id: Chat session UUID to summarize.
        user: Authenticated user.
        db_session: Database session.

    Returns:
        SummarizeResponse with new_session_id and the generated summary.
    """
    from meho_app.modules.agents.service import AgentService

    agent_service = AgentService(db_session)

    # Verify session exists and user has access
    session_obj = await agent_service.get_chat_session(session_id, include_messages=True)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found")
    await check_session_access(session_obj, user)

    # Collect message content for summarization
    messages_text = []
    if session_obj.messages:
        for msg in session_obj.messages:
            role_label = "User" if msg.role == "user" else "Assistant"
            messages_text.append(f"{role_label}: {msg.content}")

    # Generate summary via LLM (or fallback for empty sessions)
    if not messages_text or len(messages_text) < 2:
        summary = "No prior context to summarize. Starting fresh."
    else:
        try:
            from meho_app.modules.agents.base.inference import infer

            conversation_text = "\n".join(messages_text[-50:])  # Last 50 messages max
            summary = await infer(
                system_prompt="Summarize this investigation conversation. Include: key findings, entities discussed, hypotheses (validated/invalidated), and any unresolved questions. Keep it concise (3-5 paragraphs).",
                message=conversation_text,
            )
        except Exception as e:
            logger.error(f"LLM summarization failed: {e}", exc_info=True)
            # Fallback: create a basic summary from message count
            summary = (
                f"Previous investigation had {len(messages_text)} messages. "
                "LLM summarization was unavailable. Please refer to the previous session for details."
            )

    # Create new session
    new_session = await agent_service.create_chat_session(
        tenant_id=user.tenant_id or "default",
        user_id=user.user_id or "anonymous",
        title="Continued Investigation",
    )

    # Insert summary as the first assistant message in the new session
    await agent_service.add_chat_message(
        session_id=str(new_session.id),
        role="assistant",
        content=f"## Investigation Summary\n\n{summary}\n\n---\n*Summarized from previous session. Continue your investigation below.*",
    )

    logger.info(
        f"Summarized session {session_id[:8]}... -> new session {str(new_session.id)[:8]}..."
    )

    return SummarizeResponse(
        new_session_id=str(new_session.id),
        summary=summary,
    )
