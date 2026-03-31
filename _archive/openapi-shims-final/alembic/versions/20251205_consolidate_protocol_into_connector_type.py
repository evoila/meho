"""Consolidate protocol into connector_type

Remove the redundant 'protocol' field and use 'connector_type' as the single
source of truth for connector classification.

Revision ID: consolidate_protocol
Revises: add_vmware_connector_97
Create Date: 2024-12-05

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'consolidate_protocol'
down_revision = 'add_vmware_connector_97'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Copy protocol value to connector_type where connector_type is NULL or 'rest'
    # This ensures existing SOAP/GraphQL/gRPC connectors get their type set correctly
    op.execute("""
        UPDATE connector 
        SET connector_type = protocol 
        WHERE connector_type IS NULL 
           OR (connector_type = 'rest' AND protocol != 'rest')
    """)
    
    # Step 2: Drop the protocol column
    op.drop_column('connector', 'protocol')


def downgrade() -> None:
    # Add protocol column back
    op.add_column('connector', sa.Column('protocol', sa.String(), nullable=False, server_default='rest'))
    
    # Copy connector_type back to protocol
    op.execute("""
        UPDATE connector 
        SET protocol = connector_type
    """)

