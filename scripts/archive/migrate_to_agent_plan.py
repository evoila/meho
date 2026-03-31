#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Migrate workflow tables to agent_plan schema.

This script performs the database migration for the Plan vs Workflow refactoring.
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def migrate():
    """Run the migration"""
    engine = create_async_engine(
        "postgresql+asyncpg://meho:password@localhost:5432/meho_test",
        echo=True
    )
    
    async with engine.begin() as conn:
        # Check what tables exist
        result = await conn.execute(text("""
            SELECT tablename FROM pg_tables 
            WHERE schemaname = 'public' 
            AND tablename IN ('workflow', 'workflow_step', 'agent_plan', 'agent_plan_step')
        """))
        existing_tables = [row[0] for row in result]
        print(f"Existing tables: {existing_tables}")
        
        # Step 1: Rename workflow_step → agent_plan_step (child first)
        if 'workflow_step' in existing_tables and 'agent_plan_step' not in existing_tables:
            print("Renaming workflow_step → agent_plan_step...")
            await conn.execute(text("ALTER TABLE workflow_step RENAME TO agent_plan_step"))
            await conn.execute(text("ALTER INDEX IF EXISTS ix_workflow_step_workflow_id RENAME TO ix_agent_plan_step_agent_plan_id"))
            print("✅ Renamed workflow_step")
        
        # Step 2: Rename workflow → agent_plan (parent)
        if 'workflow' in existing_tables and 'agent_plan' not in existing_tables:
            print("Renaming workflow → agent_plan...")
            await conn.execute(text("ALTER TABLE workflow RENAME TO agent_plan"))
            await conn.execute(text("ALTER INDEX IF EXISTS ix_workflow_tenant_id RENAME TO ix_agent_plan_tenant_id"))
            await conn.execute(text("ALTER INDEX IF EXISTS ix_workflow_user_id RENAME TO ix_agent_plan_user_id"))
            print("✅ Renamed workflow")
        
        # Step 3: Rename foreign key column
        result = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'agent_plan_step' AND column_name = 'workflow_id'
        """))
        if result.rowcount > 0:
            print("Renaming workflow_id → agent_plan_id in agent_plan_step...")
            await conn.execute(text("ALTER TABLE agent_plan_step RENAME COLUMN workflow_id TO agent_plan_id"))
            print("✅ Renamed foreign key column")
        
        # Step 4: Add new columns to agent_plan
        print("Adding new columns to agent_plan...")
        await conn.execute(text("""
            ALTER TABLE agent_plan 
            ADD COLUMN IF NOT EXISTS session_id UUID,
            ADD COLUMN IF NOT EXISTS requires_approval BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP
        """))
        
        # Add foreign key constraint for session_id
        await conn.execute(text("""
            ALTER TABLE agent_plan 
            ADD CONSTRAINT fk_agent_plan_session 
            FOREIGN KEY (session_id) REFERENCES chat_session(id)
            ON DELETE SET NULL
        """))
        print("✅ Added new columns")
        
        # Step 5: Add agent_plan_id to chat_message
        result = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'chat_message' AND column_name = 'agent_plan_id'
        """))
        if result.rowcount == 0:
            print("Adding agent_plan_id to chat_message...")
            await conn.execute(text("ALTER TABLE chat_message ADD COLUMN agent_plan_id UUID"))
            
            # Copy data from workflow_id
            await conn.execute(text("""
                UPDATE chat_message SET agent_plan_id = workflow_id 
                WHERE workflow_id IS NOT NULL
            """))
            
            # Add foreign key
            await conn.execute(text("""
                ALTER TABLE chat_message 
                ADD CONSTRAINT fk_chat_message_agent_plan 
                FOREIGN KEY (agent_plan_id) REFERENCES agent_plan(id)
                ON DELETE SET NULL
            """))
            print("✅ Added agent_plan_id to chat_message")
    
    print("\n✅ Migration to agent_plan schema complete!")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(migrate())

