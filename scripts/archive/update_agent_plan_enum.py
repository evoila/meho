#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Update agent_plan table to use planstatus enum.

The table might still be using the old enum type name.
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def update_enum():
    """Update agent_plan table enum type"""
    engine = create_async_engine(
        "postgresql+asyncpg://meho:password@localhost:5432/meho_test",
        echo=True
    )
    
    async with engine.begin() as conn:
        # Check current column type
        result = await conn.execute(text("""
            SELECT data_type, udt_name 
            FROM information_schema.columns 
            WHERE table_name = 'agent_plan' AND column_name = 'status'
        """))
        row = result.fetchone()
        
        if row:
            current_type = row[1]  # udt_name
            print(f"Current status column type: {current_type}")
            
            if current_type != 'planstatus':
                print(f"Updating status column from {current_type} to planstatus...")
                
                # First, check if we need to convert existing data
                # If old enum exists, we might need to map values
                
                # Update the column type
                await conn.execute(text("""
                    ALTER TABLE agent_plan 
                    ALTER COLUMN status TYPE planstatus 
                    USING status::text::planstatus
                """))
                print("✅ Updated agent_plan.status to use planstatus enum")
            else:
                print("✅ agent_plan.status already uses planstatus enum")
        else:
            print("⚠️ agent_plan table or status column not found")
    
    await engine.dispose()
    print("\n✅ Database update complete!")

if __name__ == "__main__":
    asyncio.run(update_enum())

