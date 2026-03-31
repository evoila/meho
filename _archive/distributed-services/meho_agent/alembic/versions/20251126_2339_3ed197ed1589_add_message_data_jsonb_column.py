"""add_message_data_jsonb_column

Revision ID: 3ed197ed1589
Revises: 20251122_2200
Create Date: 2025-11-26 23:39:50.142330

Adds message_data JSONB column to chat_message table to store full PydanticAI 
message structure including tool calls and tool results.

This enables proper conversation continuity where the LLM can see its previous
tool calls and results, not just text responses.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '3ed197ed1589'
down_revision = '20251122_2200'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add message_data column to store full PydanticAI message format
    # This includes tool calls, tool results, and message parts
    op.add_column(
        'chat_message',
        sa.Column('message_data', JSONB, nullable=True)
    )
    
    # Note: Column is nullable for backward compatibility with existing messages
    # New messages will populate this field, old messages will have NULL


def downgrade() -> None:
    # Remove message_data column
    op.drop_column('chat_message', 'message_data')

