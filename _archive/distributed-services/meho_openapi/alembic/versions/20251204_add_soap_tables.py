"""Add SOAP operation and type descriptor tables

Revision ID: a1b2c3d4e5f6
Revises: 20251203_add_protocol_to_connector
Create Date: 2025-12-04

TASK-96: SOAP Type Support

Creates tables for storing SOAP operations and type definitions,
mirroring the pattern used for REST endpoint_descriptor.
Enables BM25 search on-the-fly (like REST endpoints).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'add_soap_tables_96'
down_revision = 'add_protocol_column'  # Chain from 20251203_add_protocol_to_connector.py
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create soap_operation_descriptor table
    op.create_table(
        'soap_operation_descriptor',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('connector_id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        
        # SOAP operation identification
        sa.Column('service_name', sa.String(), nullable=False),
        sa.Column('port_name', sa.String(), nullable=False),
        sa.Column('operation_name', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),  # Full name
        
        # Documentation
        sa.Column('description', sa.Text(), nullable=True),
        
        # SOAP-specific details
        sa.Column('soap_action', sa.String(), nullable=True),
        sa.Column('namespace', sa.String(), nullable=True),
        sa.Column('style', sa.String(), nullable=False, server_default='document'),
        
        # Schemas
        sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('output_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('protocol_details', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        
        # Search optimization
        sa.Column('search_content', sa.Text(), nullable=True),
        
        # Activation & Safety
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('safety_level', sa.String(), nullable=False, server_default='caution'),
        sa.Column('requires_approval', sa.Boolean(), nullable=False, server_default='false'),
        
        # Timestamps
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        
        sa.ForeignKeyConstraint(['connector_id'], ['connector.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Indexes for soap_operation_descriptor
    op.create_index('ix_soap_op_connector', 'soap_operation_descriptor', ['connector_id'])
    op.create_index('ix_soap_op_connector_service', 'soap_operation_descriptor', ['connector_id', 'service_name'])
    op.create_index('ix_soap_op_connector_operation', 'soap_operation_descriptor', ['connector_id', 'operation_name'])
    op.create_index('ix_soap_op_tenant', 'soap_operation_descriptor', ['tenant_id'])
    
    # Create soap_type_descriptor table
    op.create_table(
        'soap_type_descriptor',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('connector_id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        
        # Type identification
        sa.Column('type_name', sa.String(), nullable=False),
        sa.Column('namespace', sa.String(), nullable=True),
        
        # Type inheritance
        sa.Column('base_type', sa.String(), nullable=True),
        
        # Properties
        sa.Column('properties', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        
        # Documentation
        sa.Column('description', sa.Text(), nullable=True),
        
        # Search optimization
        sa.Column('search_content', sa.Text(), nullable=True),
        
        # Timestamps
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        
        sa.ForeignKeyConstraint(['connector_id'], ['connector.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Indexes for soap_type_descriptor
    op.create_index('ix_soap_type_connector', 'soap_type_descriptor', ['connector_id'])
    op.create_index('ix_soap_type_connector_name', 'soap_type_descriptor', ['connector_id', 'type_name'])
    op.create_index('ix_soap_type_tenant', 'soap_type_descriptor', ['tenant_id'])


def downgrade() -> None:
    # Drop indexes first
    op.drop_index('ix_soap_type_tenant', table_name='soap_type_descriptor')
    op.drop_index('ix_soap_type_connector_name', table_name='soap_type_descriptor')
    op.drop_index('ix_soap_type_connector', table_name='soap_type_descriptor')
    
    op.drop_index('ix_soap_op_tenant', table_name='soap_operation_descriptor')
    op.drop_index('ix_soap_op_connector_operation', table_name='soap_operation_descriptor')
    op.drop_index('ix_soap_op_connector_service', table_name='soap_operation_descriptor')
    op.drop_index('ix_soap_op_connector', table_name='soap_operation_descriptor')
    
    # Drop tables
    op.drop_table('soap_type_descriptor')
    op.drop_table('soap_operation_descriptor')

