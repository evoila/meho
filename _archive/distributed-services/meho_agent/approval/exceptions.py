"""
Approval Exceptions

TASK-76: Exception raised when a tool requires approval before execution.
"""
# mypy: disable-error-code="no-untyped-def"

from typing import Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class ApprovalRequired(Exception):
    """
    Exception raised when a tool call requires user approval.
    
    This exception is raised by risky tools (call_endpoint for POST/PUT/DELETE)
    when they need approval before executing. The streaming agent catches this
    exception, persists the approval request, and yields an approval_required
    event to the frontend.
    
    Usage:
        @agent.tool
        async def call_endpoint(ctx, endpoint_id, ...):
            if endpoint.method in ("POST", "PUT", "DELETE"):
                raise ApprovalRequired(
                    tool_name="call_endpoint",
                    tool_args={"endpoint_id": endpoint_id, ...},
                    danger_level="dangerous",
                    context={
                        "method": "DELETE",
                        "path": "/api/vcenter/vm/{vm}",
                        "description": "Delete virtual machine",
                    }
                )
            # ... execute if safe
    
    Attributes:
        tool_name: Name of the tool that requires approval
        tool_args: Arguments that will be passed to the tool
        danger_level: "safe", "caution", "dangerous", or "critical"
        context: Human-readable context for the approval dialog
    """
    
    tool_name: str
    tool_args: Dict[str, Any]
    danger_level: str = "dangerous"
    context: Dict[str, Any] = field(default_factory=dict)
    
    # Optional: pre-generated approval ID (if created before raising)
    approval_id: Optional[str] = None
    
    def __post_init__(self):
        # Call parent Exception.__init__ with a message
        super().__init__(
            f"Approval required for {self.tool_name}: {self.context.get('description', 'unknown action')}"
        )
    
    @property
    def http_method(self) -> Optional[str]:
        """Get HTTP method from context."""
        return self.context.get("method")
    
    @property
    def endpoint_path(self) -> Optional[str]:
        """Get endpoint path from context."""
        return self.context.get("path")
    
    @property
    def description(self) -> str:
        """Get human-readable description."""
        desc = self.context.get("description")
        if desc is None:
            return f"Execute {self.tool_name}"
        return str(desc)
    
    @property
    def impact_message(self) -> Optional[str]:
        """Get impact warning message."""
        return self.context.get("impact")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for serialization.
        
        Used when persisting to database or sending to frontend.
        """
        return {
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "danger_level": self.danger_level,
            "context": self.context,
            "approval_id": self.approval_id,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRequired":
        """Create from dictionary."""
        return cls(
            tool_name=data["tool_name"],
            tool_args=data["tool_args"],
            danger_level=data.get("danger_level", "dangerous"),
            context=data.get("context", {}),
            approval_id=data.get("approval_id"),
        )


class ApprovalExpired(Exception):
    """
    Exception raised when an approval request has expired.
    
    Approval requests have an optional expiry time. If a user tries
    to approve/reject after expiry, this exception is raised.
    """
    
    def __init__(self, approval_id: str, expired_at: str):
        self.approval_id = approval_id
        self.expired_at = expired_at
        super().__init__(f"Approval request {approval_id} expired at {expired_at}")


class ApprovalNotFound(Exception):
    """
    Exception raised when an approval request is not found.
    """
    
    def __init__(self, approval_id: str):
        self.approval_id = approval_id
        super().__init__(f"Approval request {approval_id} not found")


class ApprovalAlreadyDecided(Exception):
    """
    Exception raised when trying to decide on an already-decided approval.
    """
    
    def __init__(self, approval_id: str, current_status: str):
        self.approval_id = approval_id
        self.current_status = current_status
        super().__init__(f"Approval request {approval_id} already {current_status}")

