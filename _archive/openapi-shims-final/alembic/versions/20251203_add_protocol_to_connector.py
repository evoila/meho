"""Add protocol column to connector table for multi-protocol support

Revision ID: add_protocol_column
Revises: 20251130_add_llm_instructions_to_endpoint
Create Date: 2025-12-03

TASK-75: Multi-Protocol Support (GraphQL, gRPC, SOAP)

This migration adds:
- protocol: The API protocol type (rest, graphql, grpc, soap)
- protocol_config: Protocol-specific configuration (JSONB)

Default value is 'rest' for backward compatibility with existing connectors.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = 'add_protocol_column'
down_revision = '7a1b2c3d4e5f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add protocol column with default 'rest' for backward compatibility
    op.add_column(
        'connector',
        sa.Column('protocol', sa.String(), nullable=False, server_default='rest')
    )
    
    # Add protocol_config column for protocol-specific settings
    op.add_column(
        'connector',
        sa.Column('protocol_config', JSONB, nullable=True)
    )
    
    # Add index on protocol for filtering
    op.create_index(
        'ix_connector_protocol',
        'connector',
        ['protocol']
    )
    
    # Add composite index for tenant + protocol queries
    op.create_index(
        'ix_connector_tenant_protocol',
        'connector',
        ['tenant_id', 'protocol']
    )


def downgrade() -> None:
    # Remove indexes
    op.drop_index('ix_connector_tenant_protocol', table_name='connector')
    op.drop_index('ix_connector_protocol', table_name='connector')
    
    # Remove columns
    op.drop_column('connector', 'protocol_config')
    op.drop_column('connector', 'protocol')

