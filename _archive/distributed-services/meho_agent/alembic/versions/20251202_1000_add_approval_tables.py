"""Add approval_request and approval_audit tables

TASK-76: Approval Flow Architecture

Revision ID: 20251202_1000_approval
Revises: tenant_agent_config
Create Date: 2025-12-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20251202_1000_approval'
down_revision = 'tenant_agent_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enums
    approval_status_enum = postgresql.ENUM(
        'pending', 'approved', 'rejected', 'expired',
        name='approvalstatus',
        create_type=False
    )
    danger_level_enum = postgresql.ENUM(
        'safe', 'caution', 'dangerous', 'critical',
        name='dangerlevel',
        create_type=False
    )
    
    # Create enums in database
    approval_status_enum.create(op.get_bind(), checkfirst=True)
    danger_level_enum.create(op.get_bind(), checkfirst=True)
    
    # Create approval_request table
    op.create_table(
        'approval_request',
        # Identity
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('session_id', postgresql.UUID(as_uuid=True), 
                  sa.ForeignKey('chat_session.id'), nullable=False, index=True),
        sa.Column('tenant_id', sa.String(), nullable=False, index=True),
        sa.Column('user_id', sa.String(), nullable=False, index=True),
        
        # What needs approval
        sa.Column('tool_name', sa.String(100), nullable=False),
        sa.Column('tool_args', postgresql.JSONB(), nullable=False),
        sa.Column('tool_args_hash', sa.String(64), nullable=False),
        
        # Human-readable context
        sa.Column('danger_level', danger_level_enum, nullable=False),
        sa.Column('http_method', sa.String(10), nullable=True),
        sa.Column('endpoint_path', sa.String(500), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('impact_message', sa.Text(), nullable=True),
        
        # State for resume
        sa.Column('user_message', sa.Text(), nullable=False),
        sa.Column('conversation_history', postgresql.JSONB(), nullable=True),
        
        # Status
        sa.Column('status', approval_status_enum, nullable=False, 
                  server_default='pending'),
        
        # Decision
        sa.Column('decided_by', sa.String(), nullable=True),
        sa.Column('decided_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('decision_reason', sa.Text(), nullable=True),
        
        # Timestamps
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False, 
                  server_default=sa.func.now()),
        sa.Column('expires_at', sa.TIMESTAMP(), nullable=True),
    )
    
    # Create approval_audit table
    op.create_table(
        'approval_audit',
        # Identity
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('approval_request_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('approval_request.id'), nullable=True),
        sa.Column('session_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('tenant_id', sa.String(), nullable=False, index=True),
        
        # What happened
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('actor_id', sa.String(), nullable=True),
        
        # Details
        sa.Column('tool_name', sa.String(100), nullable=False),
        sa.Column('tool_args', postgresql.JSONB(), nullable=True),
        sa.Column('danger_level', sa.String(20), nullable=True),
        
        # Request context
        sa.Column('http_method', sa.String(10), nullable=True),
        sa.Column('endpoint_path', sa.String(500), nullable=True),
        
        # Metadata
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        
        # Timestamp
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False,
                  server_default=sa.func.now(), index=True),
    )
    
    # Create indexes for common queries
    op.create_index(
        'ix_approval_request_session_status',
        'approval_request',
        ['session_id', 'status']
    )
    op.create_index(
        'ix_approval_audit_session_created',
        'approval_audit',
        ['session_id', 'created_at']
    )


def downgrade() -> None:
    # Drop tables
    op.drop_table('approval_audit')
    op.drop_table('approval_request')
    
    # Drop enums
    op.execute('DROP TYPE IF EXISTS approvalstatus')
    op.execute('DROP TYPE IF EXISTS dangerlevel')

