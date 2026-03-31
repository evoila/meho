"""add_chat_sessions

Revision ID: a1b2c3d4e5f6
Revises: 07da984a2169
Create Date: 2025-11-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '07da984a2169'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if tables already exist
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    
    # Create chat_session table if it doesn't exist
    if 'chat_session' not in existing_tables:
        op.create_table('chat_session',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('tenant_id', sa.String(), nullable=False),
            sa.Column('user_id', sa.String(), nullable=False),
            sa.Column('title', sa.String(), nullable=True),
            sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
            sa.Column('updated_at', sa.TIMESTAMP(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_chat_session_tenant_id'), 'chat_session', ['tenant_id'], unique=False)
        op.create_index(op.f('ix_chat_session_user_id'), 'chat_session', ['user_id'], unique=False)
    
    # Create chat_message table if it doesn't exist
    if 'chat_message' not in existing_tables:
        op.create_table('chat_message',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('session_id', sa.UUID(), nullable=False),
            sa.Column('role', sa.String(), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('workflow_id', sa.UUID(), nullable=True),
            sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['chat_session.id'], ),
            sa.ForeignKeyConstraint(['workflow_id'], ['workflow.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_chat_message_session_id'), 'chat_message', ['session_id'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_index(op.f('ix_chat_message_session_id'), table_name='chat_message')
    op.drop_table('chat_message')
    op.drop_index(op.f('ix_chat_session_user_id'), table_name='chat_session')
    op.drop_index(op.f('ix_chat_session_tenant_id'), table_name='chat_session')
    op.drop_table('chat_session')

