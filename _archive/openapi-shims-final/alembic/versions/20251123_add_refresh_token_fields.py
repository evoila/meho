"""add_refresh_token_fields

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2025-11-23 18:00:00.000000

Adds refresh token support for SESSION authentication:
- session_refresh_token: Encrypted refresh token
- session_refresh_expires_at: Refresh token expiry time
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6g7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add refresh token fields to user_connector_credential table"""
    
    # Add refresh token fields
    op.add_column('user_connector_credential', 
                  sa.Column('session_refresh_token', sa.Text(), nullable=True,
                           comment='Encrypted refresh token for SESSION auth'))
    
    op.add_column('user_connector_credential', 
                  sa.Column('session_refresh_expires_at', sa.TIMESTAMP(), nullable=True,
                           comment='Refresh token expiry (null = never expires)'))


def downgrade() -> None:
    """Remove refresh token fields"""
    
    op.drop_column('user_connector_credential', 'session_refresh_expires_at')
    op.drop_column('user_connector_credential', 'session_refresh_token')

