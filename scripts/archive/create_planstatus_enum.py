#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Create planstatus enum type in database.

This fixes the issue where the database doesn't have the planstatus enum
after renaming WorkflowStatus to PlanStatus.
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def create_enum():
    """Create planstatus enum type"""
    engine = create_async_engine(
        "postgresql+asyncpg://meho:password@localhost:5432/meho_test",
        echo=True
    )
    
    async with engine.begin() as conn:
        # Check if enum exists
        result = await conn.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = 'planstatus'
            )
        """))
        exists = result.scalar()
        
        if not exists:
            print("Creating planstatus enum type...")
            await conn.execute(text("""
                CREATE TYPE planstatus AS ENUM (
                    'PLANNING',
                    'WAITING_APPROVAL',
                    'RUNNING',
                    'COMPLETED',
                    'FAILED',
                    'CANCELLED'
                )
            """))
            print("✅ Created planstatus enum")
        else:
            print("✅ planstatus enum already exists")
        
        # Also check for executionstatus (for workflow_execution)
        result = await conn.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = 'executionstatus'
            )
        """))
        exists = result.scalar()
        
        if not exists:
            print("Creating executionstatus enum type...")
            await conn.execute(text("""
                CREATE TYPE executionstatus AS ENUM (
                    'PENDING',
                    'RUNNING',
                    'COMPLETED',
                    'FAILED',
                    'CANCELLED'
                )
            """))
            print("✅ Created executionstatus enum")
        else:
            print("✅ executionstatus enum already exists")
    
    await engine.dispose()
    print("\n✅ Database enum types ready!")

if __name__ == "__main__":
    asyncio.run(create_enum())

