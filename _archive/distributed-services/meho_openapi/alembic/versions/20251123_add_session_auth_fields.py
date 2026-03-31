"""add_session_auth_fields

Revision ID: a1b2c3d4e5f6
Revises: 5bed5b682e72
Create Date: 2025-11-23 12:00:00.000000

Adds support for session-based authentication:
- login_url, login_method, login_config to connector table
- session_token, session_token_expires_at, session_state to user_connector_credential table
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '5bed5b682e72'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add session auth fields to connector and user_connector_credential tables"""
    
    # Add session auth configuration fields to connector table
    op.add_column('connector', sa.Column('login_url', sa.String(), nullable=True))
    op.add_column('connector', sa.Column('login_method', sa.String(), nullable=True))
    op.add_column('connector', sa.Column('login_config', postgresql.JSONB(), nullable=True))
    
    # Add session state tracking fields to user_connector_credential table
    op.add_column('user_connector_credential', sa.Column('session_token', sa.Text(), nullable=True))
    op.add_column('user_connector_credential', sa.Column('session_token_expires_at', sa.TIMESTAMP(), nullable=True))
    op.add_column('user_connector_credential', sa.Column('session_state', sa.String(), nullable=True))


def downgrade() -> None:
    """Remove session auth fields"""
    
    # Remove session state tracking fields from user_connector_credential
    op.drop_column('user_connector_credential', 'session_state')
    op.drop_column('user_connector_credential', 'session_token_expires_at')
    op.drop_column('user_connector_credential', 'session_token')
    
    # Remove session auth configuration fields from connector
    op.drop_column('connector', 'login_config')
    op.drop_column('connector', 'login_method')
    op.drop_column('connector', 'login_url')

