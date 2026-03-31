"""
Approval Store Repository

TASK-76: Persistence layer for approval requests and audit logging.
"""
# mypy: disable-error-code="assignment,arg-type"

from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime, timedelta
import hashlib
import json
import logging

from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession

from meho_agent.models import (
    ApprovalRequestModel,
    ApprovalAuditModel,
    ApprovalStatus,
    DangerLevel as DangerLevelEnum,
)
from meho_agent.approval.exceptions import (
    ApprovalNotFound,
    ApprovalExpired,
    ApprovalAlreadyDecided,
)

logger = logging.getLogger(__name__)


class ApprovalStore:
    """
    Repository for approval requests and audit logging.
    
    Provides CRUD operations for the approval flow:
    - Create pending approvals when risky tools are intercepted
    - Approve/reject with audit trail
    - List pending approvals for a session
    - Check if approval exists for tool call
    
    TASK-76: Approval Flow Architecture
    """
    
    def __init__(self, session: AsyncSession):
        """
        Initialize with database session.
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
    
    # =========================================================================
    # APPROVAL REQUEST CRUD
    # =========================================================================
    
    async def create_pending(
        self,
        session_id: UUID,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        danger_level: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        http_method: Optional[str] = None,
        endpoint_path: Optional[str] = None,
        description: Optional[str] = None,
        impact_message: Optional[str] = None,
        expiry_minutes: Optional[int] = 60,
    ) -> ApprovalRequestModel:
        """
        Create a pending approval request.
        
        Called when a risky tool (call_endpoint for POST/PUT/DELETE) is intercepted.
        The agent execution is paused until this approval is decided.
        
        Args:
            session_id: Chat session ID
            tenant_id: Tenant identifier
            user_id: User who triggered the action
            tool_name: Name of the tool (e.g., "call_endpoint")
            tool_args: Arguments for the tool
            danger_level: "safe", "caution", "dangerous", "critical"
            user_message: Original user request (for resume)
            conversation_history: Chat history (for resume)
            http_method: HTTP method (GET, POST, DELETE, etc.)
            endpoint_path: API endpoint path
            description: Human-readable action description
            impact_message: Warning about consequences
            expiry_minutes: Auto-expiry time (None = no expiry)
            
        Returns:
            Created ApprovalRequestModel
        """
        # Generate hash for deduplication
        args_hash = self._hash_tool_args(tool_args)
        
        # Map danger level string to enum
        danger_enum = DangerLevelEnum(danger_level)
        
        # Calculate expiry time
        expires_at = None
        if expiry_minutes:
            expires_at = datetime.utcnow() + timedelta(minutes=expiry_minutes)
        
        # Create approval request
        approval = ApprovalRequestModel(
            id=uuid4(),
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_args_hash=args_hash,
            danger_level=danger_enum,
            http_method=http_method,
            endpoint_path=endpoint_path,
            description=description,
            impact_message=impact_message,
            user_message=user_message,
            conversation_history=conversation_history,
            status=ApprovalStatus.PENDING,
            expires_at=expires_at,
        )
        
        self.session.add(approval)
        await self.session.flush()
        
        # Log to audit
        await self._log_audit(
            approval_request_id=approval.id,
            session_id=session_id,
            tenant_id=tenant_id,
            action="created",
            actor_id=user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            danger_level=danger_level,
            http_method=http_method,
            endpoint_path=endpoint_path,
        )
        
        logger.info(
            f"📋 Created approval request {approval.id} for {tool_name} "
            f"({danger_level}) in session {session_id}"
        )
        
        return approval
    
    async def get_by_id(self, approval_id: UUID) -> Optional[ApprovalRequestModel]:
        """
        Get approval request by ID.
        
        Args:
            approval_id: Approval request UUID
            
        Returns:
            ApprovalRequestModel or None if not found
        """
        result = await self.session.execute(
            select(ApprovalRequestModel).where(ApprovalRequestModel.id == approval_id)
        )
        return result.scalar_one_or_none()
    
    async def get_pending_for_session(
        self,
        session_id: UUID,
        tenant_id: str
    ) -> List[ApprovalRequestModel]:
        """
        Get all pending approval requests for a session.
        
        Args:
            session_id: Chat session ID
            tenant_id: Tenant identifier
            
        Returns:
            List of pending ApprovalRequestModels
        """
        result = await self.session.execute(
            select(ApprovalRequestModel)
            .where(
                and_(
                    ApprovalRequestModel.session_id == session_id,
                    ApprovalRequestModel.tenant_id == tenant_id,
                    ApprovalRequestModel.status == ApprovalStatus.PENDING,
                )
            )
            .order_by(ApprovalRequestModel.created_at.desc())
        )
        return list(result.scalars().all())
    
    async def check_approval(
        self,
        session_id: UUID,
        tool_name: str,
        tool_args: Dict[str, Any]
    ) -> Optional[ApprovalRequestModel]:
        """
        Check if there's an approval for a specific tool call.
        
        Used by tools to check if they've been approved before executing.
        
        Args:
            session_id: Chat session ID
            tool_name: Tool name (e.g., "call_endpoint")
            tool_args: Tool arguments
            
        Returns:
            ApprovalRequestModel if approved, None if not found or not approved
        """
        args_hash = self._hash_tool_args(tool_args)
        
        result = await self.session.execute(
            select(ApprovalRequestModel)
            .where(
                and_(
                    ApprovalRequestModel.session_id == session_id,
                    ApprovalRequestModel.tool_name == tool_name,
                    ApprovalRequestModel.tool_args_hash == args_hash,
                    ApprovalRequestModel.status == ApprovalStatus.APPROVED,
                )
            )
        )
        return result.scalar_one_or_none()
    
    # =========================================================================
    # APPROVAL DECISIONS
    # =========================================================================
    
    async def approve(
        self,
        approval_id: UUID,
        decided_by: str,
        reason: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ApprovalRequestModel:
        """
        Approve a pending request.
        
        Args:
            approval_id: Approval request UUID
            decided_by: User ID who approved
            reason: Optional reason for approval
            ip_address: Client IP (for audit)
            user_agent: Client user agent (for audit)
            
        Returns:
            Updated ApprovalRequestModel
            
        Raises:
            ApprovalNotFound: If approval doesn't exist
            ApprovalExpired: If approval has expired
            ApprovalAlreadyDecided: If already approved/rejected
        """
        approval = await self._get_and_validate(approval_id)
        
        # Update status
        approval.status = ApprovalStatus.APPROVED
        approval.decided_by = decided_by
        approval.decided_at = datetime.utcnow()
        approval.decision_reason = reason
        
        await self.session.flush()
        
        # Log to audit
        await self._log_audit(
            approval_request_id=approval.id,
            session_id=approval.session_id,
            tenant_id=approval.tenant_id,
            action="approved",
            actor_id=decided_by,
            tool_name=approval.tool_name,
            tool_args=approval.tool_args,
            danger_level=approval.danger_level.value,
            http_method=approval.http_method,
            endpoint_path=approval.endpoint_path,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        
        logger.info(f"✅ Approval {approval_id} approved by {decided_by}")
        
        return approval
    
    async def reject(
        self,
        approval_id: UUID,
        decided_by: str,
        reason: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ApprovalRequestModel:
        """
        Reject a pending request.
        
        Args:
            approval_id: Approval request UUID
            decided_by: User ID who rejected
            reason: Optional reason for rejection
            ip_address: Client IP (for audit)
            user_agent: Client user agent (for audit)
            
        Returns:
            Updated ApprovalRequestModel
            
        Raises:
            ApprovalNotFound: If approval doesn't exist
            ApprovalExpired: If approval has expired
            ApprovalAlreadyDecided: If already approved/rejected
        """
        approval = await self._get_and_validate(approval_id)
        
        # Update status
        approval.status = ApprovalStatus.REJECTED
        approval.decided_by = decided_by
        approval.decided_at = datetime.utcnow()
        approval.decision_reason = reason
        
        await self.session.flush()
        
        # Log to audit
        await self._log_audit(
            approval_request_id=approval.id,
            session_id=approval.session_id,
            tenant_id=approval.tenant_id,
            action="rejected",
            actor_id=decided_by,
            tool_name=approval.tool_name,
            tool_args=approval.tool_args,
            danger_level=approval.danger_level.value,
            http_method=approval.http_method,
            endpoint_path=approval.endpoint_path,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        
        logger.info(f"❌ Approval {approval_id} rejected by {decided_by}")
        
        return approval
    
    async def expire_old_requests(self, tenant_id: Optional[str] = None) -> int:
        """
        Expire old pending requests.
        
        Called periodically to clean up stale approvals.
        
        Args:
            tenant_id: Optional tenant filter
            
        Returns:
            Number of expired requests
        """
        now = datetime.utcnow()
        
        conditions = [
            ApprovalRequestModel.status == ApprovalStatus.PENDING,
            ApprovalRequestModel.expires_at.isnot(None),
            ApprovalRequestModel.expires_at < now,
        ]
        
        if tenant_id:
            conditions.append(ApprovalRequestModel.tenant_id == tenant_id)
        
        # Find expired requests
        result = await self.session.execute(
            select(ApprovalRequestModel).where(and_(*conditions))
        )
        expired = list(result.scalars().all())
        
        # Update each and log
        for approval in expired:
            approval.status = ApprovalStatus.EXPIRED
            approval.decided_at = now
            
            await self._log_audit(
                approval_request_id=approval.id,
                session_id=approval.session_id,
                tenant_id=approval.tenant_id,
                action="expired",
                actor_id="system",
                tool_name=approval.tool_name,
                danger_level=approval.danger_level.value,
            )
        
        await self.session.flush()
        
        if expired:
            logger.info(f"⏰ Expired {len(expired)} stale approval requests")
        
        return len(expired)
    
    # =========================================================================
    # AUDIT QUERIES
    # =========================================================================
    
    async def get_audit_log(
        self,
        session_id: Optional[UUID] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ApprovalAuditModel]:
        """
        Get audit log entries.
        
        Args:
            session_id: Optional filter by session
            tenant_id: Optional filter by tenant
            limit: Max entries to return
            
        Returns:
            List of ApprovalAuditModel entries
        """
        query = select(ApprovalAuditModel)
        
        conditions = []
        if session_id:
            conditions.append(ApprovalAuditModel.session_id == session_id)
        if tenant_id:
            conditions.append(ApprovalAuditModel.tenant_id == tenant_id)
        
        if conditions:
            query = query.where(and_(*conditions))
        
        query = query.order_by(ApprovalAuditModel.created_at.desc()).limit(limit)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================
    
    async def _get_and_validate(self, approval_id: UUID) -> ApprovalRequestModel:
        """
        Get approval and validate it can be decided.
        
        Raises:
            ApprovalNotFound: If not found
            ApprovalExpired: If expired
            ApprovalAlreadyDecided: If not pending
        """
        approval = await self.get_by_id(approval_id)
        
        if not approval:
            raise ApprovalNotFound(str(approval_id))
        
        # Check expiry
        if approval.expires_at and approval.expires_at < datetime.utcnow():
            approval.status = ApprovalStatus.EXPIRED
            await self.session.flush()
            raise ApprovalExpired(str(approval_id), str(approval.expires_at))
        
        # Check status
        if approval.status != ApprovalStatus.PENDING:
            raise ApprovalAlreadyDecided(str(approval_id), approval.status.value)
        
        return approval
    
    async def _log_audit(
        self,
        session_id: UUID,
        tenant_id: str,
        action: str,
        tool_name: str,
        approval_request_id: Optional[UUID] = None,
        actor_id: Optional[str] = None,
        tool_args: Optional[Dict] = None,
        danger_level: Optional[str] = None,
        http_method: Optional[str] = None,
        endpoint_path: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ApprovalAuditModel:
        """
        Log an audit entry.
        """
        audit = ApprovalAuditModel(
            id=uuid4(),
            approval_request_id=approval_request_id,
            session_id=session_id,
            tenant_id=tenant_id,
            action=action,
            actor_id=actor_id,
            tool_name=tool_name,
            tool_args=tool_args,
            danger_level=danger_level,
            http_method=http_method,
            endpoint_path=endpoint_path,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        
        self.session.add(audit)
        return audit
    
    @staticmethod
    def _hash_tool_args(tool_args: Dict[str, Any]) -> str:
        """
        Generate deterministic hash of tool arguments.
        
        Used for deduplication - same args = same hash.
        """
        # Sort keys for deterministic serialization
        serialized = json.dumps(tool_args, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()

