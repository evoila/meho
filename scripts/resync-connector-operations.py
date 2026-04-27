#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
One-time script to re-sync all typed connector operations.

This updates existing connector_operation records with the new
response schema fields (response_entity_type, response_identifier_field,
response_display_name_field) added in TASK-161.

Usage:
    cd /path/to/MEHO.X
    source .venv/bin/activate
    export DATABASE_URL="postgresql+asyncpg://meho:password@localhost:5432/meho"
    python scripts/resync-connector-operations.py
"""
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("❌ DATABASE_URL environment variable not set")
        print("   export DATABASE_URL='postgresql+asyncpg://meho:password@localhost:5432/meho'")
        sys.exit(1)

    print("🔄 Re-syncing all typed connector operations with response schema fields...")
    print(f"   Database: {database_url.split('@')[1] if '@' in database_url else database_url}")
    print()

    engine = create_async_engine(database_url)

    # Import operation definitions
    from meho_app.modules.connectors.kubernetes.operations import KUBERNETES_OPERATIONS
    from meho_app.modules.connectors.vmware.operations import VMWARE_OPERATIONS
    from meho_app.modules.connectors.proxmox.operations import PROXMOX_OPERATIONS
    from meho_app.modules.connectors.gcp.operations import GCP_OPERATIONS

    all_operations = {
        "kubernetes": KUBERNETES_OPERATIONS,
        "vmware": VMWARE_OPERATIONS,
        "proxmox": PROXMOX_OPERATIONS,
        "gcp": GCP_OPERATIONS,
    }

    total_updated = 0

    async with engine.begin() as conn:
        for connector_type, operations in all_operations.items():
            print(f"➡️  Updating {connector_type} operations ({len(operations)} ops)...")
            updated = 0

            for op in operations:
                # Update the operation with response schema fields
                result = await conn.execute(
                    text("""
                        UPDATE connector_operation
                        SET response_entity_type = :entity_type,
                            response_identifier_field = :identifier_field,
                            response_display_name_field = :display_name_field
                        WHERE operation_id = :operation_id
                    """),
                    {
                        "operation_id": op.operation_id,
                        "entity_type": op.response_entity_type,
                        "identifier_field": op.response_identifier_field,
                        "display_name_field": op.response_display_name_field,
                    }
                )
                if result.rowcount > 0:
                    updated += result.rowcount

            print(f"   ✅ Updated {updated} operations")
            total_updated += updated

    await engine.dispose()

    print()
    print("=" * 50)
    print("✅ Re-sync complete!")
    print("=" * 50)
    print(f"   Total operations updated: {total_updated}")


if __name__ == "__main__":
    asyncio.run(main())
