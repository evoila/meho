#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Create workflow_template and workflow_execution tables"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from meho_agent.models import WorkflowTemplateModel, WorkflowExecutionModel

async def create_tables():
    """Create workflow template tables"""
    engine = create_async_engine(
        "postgresql+asyncpg://meho:password@localhost:5432/meho_test",
        echo=True
    )
    
    async with engine.begin() as conn:
        # Create workflow template and execution tables
        await conn.run_sync(lambda sync_conn: WorkflowTemplateModel.__table__.create(sync_conn, checkfirst=True))
        await conn.run_sync(lambda sync_conn: WorkflowExecutionModel.__table__.create(sync_conn, checkfirst=True))
    
    print("✅ Workflow template tables created successfully!")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(create_tables())

