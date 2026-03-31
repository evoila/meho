# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Raw data access endpoint for frontend lazy-loading.

Serves paginated cached data from agent sessions. Frontend displays
this in an interactive table modal (DataTableModal component).

Security: Validates session belongs to user's tenant before returning data.
Returns 404 (not 403) on mismatch to avoid information disclosure.
"""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from meho_app.api.config import get_api_config
from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.core.redis import get_redis_client
from meho_app.modules.agents.models import ChatSessionModel
from meho_app.modules.agents.unified_executor import get_unified_executor

router = APIRouter(prefix="/data", tags=["data"])


@router.get("/{session_id}/{table}")
async def get_cached_data(
    session_id: str,
    table: str,
    user: CurrentUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=1000),
):
    """Fetch paginated cached data for a session table.

    Returns:
        {
            "rows": [...],      # Paginated row data
            "total": N,         # Total row count
            "page": 1,          # Current page
            "size": 100,        # Page size
            "columns": [...],   # Column names
            "table": "name",    # Table name
        }
    """
    # SECURITY: Verify session belongs to user's tenant AND user (IDOR fix)
    result = await db.execute(
        select(ChatSessionModel.tenant_id, ChatSessionModel.user_id).where(
            ChatSessionModel.id == session_id
        )
    )
    row = result.one_or_none()
    if row is None or row.tenant_id != user.tenant_id or row.user_id != user.user_id:
        # Return 404 (not 403) to avoid information disclosure
        raise HTTPException(status_code=404, detail="Session not found")

    # Load cached tables from Redis
    config = get_api_config()
    redis_client = await get_redis_client(config.redis_url)
    executor = get_unified_executor(redis_client=redis_client)
    tables = await executor.get_session_tables_async(session_id)

    if table not in tables:
        raise HTTPException(status_code=404, detail=f"Table '{table}' not found in session")

    cached = tables[table]

    # Convert Arrow table to list of dicts (no pandas)
    rows = cached.arrow_table.to_pylist() if cached.arrow_table is not None else []
    total = len(rows)

    # Paginate
    start = (page - 1) * size
    end = start + size
    page_rows = rows[start:end]

    return {
        "rows": page_rows,
        "total": total,
        "page": page,
        "size": size,
        "columns": cached.columns,
        "table": table,
    }
