# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Enterprise-only session endpoints (team/group sessions).

Phase 80: Extracted from routes_chat_sessions.py. This router is only
registered when a valid enterprise license key is present.
"""

from fastapi import APIRouter, HTTPException

from meho_app.api.dependencies import AgentServiceDep, CurrentUser
from meho_app.api.routes_chat_sessions import TeamSessionResponse
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/chat/sessions/team", response_model=list[TeamSessionResponse])
async def list_team_sessions(
    user: CurrentUser,
    agent_service: AgentServiceDep,
    limit: int = 50,
):
    """
    List group/tenant sessions visible to the user's tenant. Enterprise only.

    Returns non-private sessions with derived status ("awaiting_approval" or "idle")
    and pending approval counts. Used for the "Team" tab in the sidebar.
    """
    try:
        team_sessions = await agent_service.list_team_sessions(
            tenant_id=user.tenant_id,
            limit=limit,
        )

        logger.info(f"Listed {len(team_sessions)} team sessions for tenant {user.tenant_id}")

        return [TeamSessionResponse(**s) for s in team_sessions]

    except Exception as e:
        logger.error(f"Error listing team sessions: {e}")
        raise HTTPException(status_code=500, detail="Failed to list team sessions") from e
