"""
HTTP client for Agent Service.

Provides methods to interact with the agent service for workflow
creation and management via HTTP REST APIs.
"""
# mypy: disable-error-code="no-any-return,no-untyped-def,arg-type"
import httpx
from typing import Dict, Any, Optional, AsyncGenerator
from meho_api.config import get_api_config
from meho_core.auth_context import UserContext
import logging
import json

logger = logging.getLogger(__name__)


class AgentServiceClient:
    """HTTP client for Agent service"""
    
    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize agent service client.
        
        Args:
            base_url: Override default service URL (useful for testing)
        """
        self.config = get_api_config()
        self.base_url = base_url or self.config.agent_service_url
        
    def _get_client(self) -> httpx.AsyncClient:
        """Create async HTTP client with appropriate timeout"""
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0, connect=5.0)  # Longer timeout for plan generation
        )
    
    async def create_workflow(
        self,
        goal: str,
        user_context: UserContext
    ) -> Dict[str, Any]:
        """
        Create a new workflow via HTTP.
        
        Note: This only creates the workflow record, not the plan.
        Plan generation happens in the BFF with full MEHODependencies.
        
        Args:
            goal: User's goal or question
            user_context: User context
            
        Returns:
            Created workflow data
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/agent/workflows",
                    json={
                        "goal": goal,
                        "tenant_id": user_context.tenant_id,
                        "user_id": user_context.user_id
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Create workflow failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Create workflow request failed: {e}")
                raise
    
    async def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """
        Get workflow by ID via HTTP.
        
        Args:
            workflow_id: Workflow ID
            
        Returns:
            Workflow data or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/agent/workflows/{workflow_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get workflow failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get workflow request failed: {e}")
                raise
    
    async def update_workflow(
        self,
        workflow_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update workflow via HTTP.
        
        Args:
            workflow_id: Workflow ID
            updates: Dictionary of fields to update
            
        Returns:
            Updated workflow data
        """
        async with self._get_client() as client:
            try:
                response = await client.patch(
                    f"/agent/workflows/{workflow_id}",
                    json=updates
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update workflow failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update workflow request failed: {e}")
                raise
    
    async def list_workflows(
        self,
        tenant_id: str,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50
    ) -> list[Dict[str, Any]]:
        """
        List workflows via HTTP.
        
        Args:
            tenant_id: Tenant ID
            user_id: Optional user ID filter
            status: Optional status filter
            limit: Max number to return
            
        Returns:
            List of workflows
        """
        async with self._get_client() as client:
            try:
                params = {"tenant_id": tenant_id, "limit": limit}
                if user_id:
                    params["user_id"] = user_id
                if status:
                    params["status"] = status
                    
                response = await client.get("/agent/workflows", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List workflows failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List workflows request failed: {e}")
                raise
    
    async def update_workflow_status(
        self,
        workflow_id: str,
        status: str
    ) -> Dict[str, Any]:
        """
        Update workflow status via HTTP.
        
        Args:
            workflow_id: Workflow ID
            status: New status (PENDING, WAITING_APPROVAL, RUNNING, COMPLETED, FAILED, CANCELLED)
            
        Returns:
            Update confirmation
        """
        async with self._get_client() as client:
            try:
                response = await client.patch(
                    f"/agent/workflows/{workflow_id}/status",
                    params={"status": status}
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update status failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update status request failed: {e}")
                raise
    
    async def cancel_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """
        Cancel a workflow via HTTP.
        
        Args:
            workflow_id: Workflow ID
            
        Returns:
            Cancellation confirmation
        """
        async with self._get_client() as client:
            try:
                response = await client.post(f"/agent/workflows/{workflow_id}/cancel")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Cancel workflow failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Cancel workflow request failed: {e}")
                raise
    
    async def update_workflow_plan(
        self,
        workflow_id: str,
        plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update workflow plan via HTTP.
        
        Args:
            workflow_id: Workflow ID
            plan: New plan data
            
        Returns:
            Updated workflow
        """
        async with self._get_client() as client:
            try:
                response = await client.put(
                    f"/agent/workflows/{workflow_id}/plan",
                    json=plan
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update plan failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update plan request failed: {e}")
                raise
    
    async def clone_workflow(
        self,
        workflow_id: str,
        modified_plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Clone a workflow via HTTP.
        
        Args:
            workflow_id: Workflow ID to clone
            modified_plan: Optional modified plan for the clone
            
        Returns:
            Cloned workflow
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    f"/agent/workflows/{workflow_id}/clone",
                    json={"modified_plan": modified_plan} if modified_plan else {}
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Clone workflow failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Clone workflow request failed: {e}")
                raise
    
    # ========================================================================
    # Workflow Template Methods
    # ========================================================================
    
    async def create_template(
        self,
        tenant_id: str,
        created_by: str,
        name: str,
        plan_template: Dict[str, Any],
        description: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[list[str]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        is_public: bool = False
    ) -> Dict[str, Any]:
        """Create a new workflow template via HTTP."""
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/agent/workflow-templates",
                    json={
                        "tenant_id": tenant_id,
                        "created_by": created_by,
                        "name": name,
                        "description": description,
                        "category": category,
                        "tags": tags or [],
                        "plan_template": plan_template,
                        "parameters": parameters or {},
                        "is_public": is_public
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Create template failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Create template request failed: {e}")
                raise
    
    async def list_templates(
        self,
        tenant_id: str,
        category: Optional[str] = None,
        is_public: Optional[bool] = None,
        limit: int = 50
    ) -> list[Dict[str, Any]]:
        """List workflow templates via HTTP."""
        async with self._get_client() as client:
            try:
                params = {"tenant_id": tenant_id, "limit": limit}
                if category:
                    params["category"] = category
                if is_public is not None:
                    params["is_public"] = is_public
                    
                response = await client.get("/agent/workflow-templates", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List templates failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List templates request failed: {e}")
                raise
    
    async def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """Get a workflow template by ID via HTTP."""
        async with self._get_client() as client:
            try:
                response = await client.get(f"/agent/workflow-templates/{template_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get template failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get template request failed: {e}")
                raise
    
    async def update_template(
        self,
        template_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a workflow template via HTTP."""
        async with self._get_client() as client:
            try:
                response = await client.patch(
                    f"/agent/workflow-templates/{template_id}",
                    json=updates
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update template failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update template request failed: {e}")
                raise
    
    async def delete_template(self, template_id: str) -> None:
        """Delete a workflow template via HTTP."""
        async with self._get_client() as client:
            try:
                response = await client.delete(f"/agent/workflow-templates/{template_id}")
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Delete template failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Delete template request failed: {e}")
                raise
    
    async def list_executions(
        self,
        tenant_id: str,
        template_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50
    ) -> list[Dict[str, Any]]:
        """List workflow executions via HTTP."""
        async with self._get_client() as client:
            try:
                params = {"tenant_id": tenant_id, "limit": limit}
                if template_id:
                    params["template_id"] = template_id
                if status:
                    params["status"] = status
                    
                response = await client.get("/agent/executions", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List executions failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List executions request failed: {e}")
                raise
    
    async def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """Get a workflow execution by ID via HTTP."""
        async with self._get_client() as client:
            try:
                response = await client.get(f"/agent/executions/{execution_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get execution failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get execution request failed: {e}")
                raise
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Check if agent service is healthy.
        
        Returns:
            Health status
        """
        async with self._get_client() as client:
            try:
                response = await client.get("/agent/health")
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                raise
    
    # ========================================================================
    # Chat Session Methods
    # ========================================================================
    
    async def create_chat_session(
        self,
        tenant_id: str,
        user_id: str,
        title: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new chat session via HTTP.
        
        Args:
            tenant_id: Tenant ID
            user_id: User ID
            title: Optional session title
            session_id: Optional client-provided session ID for continuity
            
        Returns:
            Created session data
        """
        async with self._get_client() as client:
            try:
                payload = {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "title": title
                }
                if session_id:
                    payload["id"] = session_id
                
                response = await client.post(
                    "/agent/chat/sessions",
                    json=payload
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Create chat session failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Create chat session request failed: {e}")
                raise
    
    async def list_chat_sessions(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50
    ) -> list[Dict[str, Any]]:
        """
        List chat sessions for a user via HTTP.
        
        Args:
            tenant_id: Tenant ID
            user_id: User ID
            limit: Max number of sessions to return
            
        Returns:
            List of sessions with message counts
        """
        async with self._get_client() as client:
            try:
                response = await client.get(
                    "/agent/chat/sessions",
                    params={
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "limit": limit
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List chat sessions failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List chat sessions request failed: {e}")
                raise
    
    async def get_chat_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a chat session with all messages via HTTP.
        
        Args:
            session_id: Session ID
            
        Returns:
            Session data with messages or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/agent/chat/sessions/{session_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get chat session failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get chat session request failed: {e}")
                raise
    
    async def update_chat_session(
        self,
        session_id: str,
        title: str
    ) -> Dict[str, Any]:
        """
        Update chat session metadata via HTTP.
        
        Args:
            session_id: Session ID
            title: New title
            
        Returns:
            Updated session data
        """
        async with self._get_client() as client:
            try:
                response = await client.patch(
                    f"/agent/chat/sessions/{session_id}",
                    json={"title": title}
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update chat session failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update chat session request failed: {e}")
                raise
    
    async def delete_chat_session(self, session_id: str) -> None:
        """
        Delete a chat session via HTTP.
        
        Args:
            session_id: Session ID
        """
        async with self._get_client() as client:
            try:
                response = await client.delete(f"/agent/chat/sessions/{session_id}")
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Delete chat session failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Delete chat session request failed: {e}")
                raise
    
    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        workflow_id: Optional[str] = None,
        message_data: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Add a message to a chat session via HTTP.
        
        Args:
            session_id: Session ID
            role: Message role ('user' or 'assistant')
            content: Message content
            workflow_id: Optional workflow ID
            message_data: Full PydanticAI message structure (Session 69)
            
        Returns:
            Created message data
        """
        async with self._get_client() as client:
            try:
                payload = {
                    "role": role,
                    "content": content,
                    "workflow_id": workflow_id
                }
                
                # Include full message data if available (Session 69)
                if message_data is not None:
                    payload["message_data"] = message_data
                
                response = await client.post(
                    f"/agent/chat/sessions/{session_id}/messages",
                    json=payload
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Add message failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Add message request failed: {e}")
                raise


# Singleton instance
_agent_client = None


def get_agent_client() -> AgentServiceClient:
    """Get agent service client singleton"""
    global _agent_client
    if _agent_client is None:
        _agent_client = AgentServiceClient()
    return _agent_client


def reset_agent_client():
    """Reset client singleton (for testing)"""
    global _agent_client
    _agent_client = None

