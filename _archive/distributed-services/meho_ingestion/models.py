"""
Database models for ingestion service.

Event templates are stored in the database and define how to process webhook events.
"""
from sqlalchemy import Column, String, Text, DateTime, JSON, Index
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid


from meho_knowledge.models import Base


class EventTemplate(Base):
    """
    Event template for processing webhook events.
    
    Templates define how to convert webhook payloads into knowledge chunks:
    - text_template: Jinja2 template for extracting text
    - tag_rules: List of Jinja2 expressions that generate tags
    - issue_detection_rule: Optional Jinja2 boolean expression to detect issues
    
    Example:
        connector_id: "github-prod"
        event_type: "push"
        text_template: "Push to {{ payload.repository.full_name }} ..."
        tag_rules: ["source:github", "repo:{{ payload.repository.full_name }}"]
        issue_detection_rule: "false"
    """
    __tablename__ = "event_templates"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Links to connector (from meho_openapi)
    connector_id = Column(String(255), nullable=False)
    
    # Event type (e.g., "push", "sync_status", "alert")
    event_type = Column(String(255), nullable=False)
    
    # Jinja2 template for generating knowledge chunk text
    text_template = Column(Text, nullable=False)
    
    # List of Jinja2 expressions for generating tags
    # Example: ["source:github", "repo:{{ payload.repository.full_name }}"]
    tag_rules = Column(JSON, nullable=False, default=list)
    
    # Optional Jinja2 boolean expression to detect if event is an issue
    # Example: "{{ payload.health_status == 'Degraded' }}"
    issue_detection_rule = Column(Text, nullable=True)
    
    # Tenant isolation
    tenant_id = Column(String(255), nullable=False)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Indexes for efficient lookups
    __table_args__ = (
        # Unique constraint: one template per connector + event_type combination
        Index('idx_event_templates_connector_event', 'connector_id', 'event_type', unique=True),
        Index('idx_event_templates_tenant', 'tenant_id'),
        Index('idx_event_templates_connector', 'connector_id'),
    )
    
    def __repr__(self) -> str:
        return f"<EventTemplate(id={self.id}, connector={self.connector_id}, event={self.event_type})>"

