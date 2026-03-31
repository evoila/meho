from __future__ import annotations
"""
MEHO ReAct Graph - Main Graph Implementation (TASK-89, TASK-92)

This is the primary entry point for the ReAct agent.
It builds the pydantic-graph and provides methods to run it.

TASK-92: Now uses typed tool nodes for input validation.
"""

import asyncio
import logging
from typing import AsyncIterator, Dict, Any, Optional, List, TYPE_CHECKING, Union
from dataclasses import dataclass, field

from pydantic_ai import Agent

from meho_agent.react.graph_state import MEHOGraphState
from meho_agent.react.graph_deps import MEHOGraphDeps
from meho_agent.intent_classifier import detect_request_type
from meho_agent.session_state import ExtractedEntity, ConnectorContext

# Import typed tool nodes (TASK-92/97 - generic)
from meho_agent.react.nodes.tool_nodes import (
    SearchOperationsNode,
    CallOperationNode,
    SearchTypesNode,
    ReduceDataNode,
    SearchKnowledgeNode,
    ListConnectorsNode,
)

logger = logging.getLogger(__name__)


@dataclass
class GraphEvent:
    """Event emitted during graph execution for SSE streaming."""
    type: str  # "thought", "action", "observation", "final_answer", "approval_required", "error"
    data: Dict[str, Any]


class MEHOReActGraph:
    """
    The MEHO ReAct Graph Agent.
    
    This is a pydantic-graph based implementation of the ReAct pattern:
    Thought → Action → Observation → ... → Final Answer
    
    Features:
    - Depth limiting (max_steps)
    - Approval flow integration (TASK-76)
    - Intent detection (TASK-87)
    - Entity side-channel (Session 80)
    - Data reduction (TASK-83)
    - SSE streaming support
    
    Usage:
        graph = MEHOReActGraph(
            llm_model="gpt-4.1-mini",
            max_steps=100,
        )
        
        async for event in graph.run_streaming("Show me all resources"):
            print(event.type, event.data)
    """
    
    def __init__(
        self,
        meho_dependencies: Any,  # MEHODependencies - the source of truth!
        approval_store: Any = None,
        llm_model: str = "gpt-4.1-mini",
        max_steps: int = 100,
    ) -> None:
        """
        Initialize the MEHO ReAct Graph.
        
        Args:
            meho_dependencies: MEHODependencies instance with all business logic
            approval_store: Optional approval flow repository (TASK-76)
            llm_model: LLM model name
            max_steps: Maximum reasoning steps before forced completion
        
        Note: All services (endpoint_repo, connector_repo, http_client, etc.)
        come from MEHODependencies. We don't duplicate them here.
        """
        self.meho_dependencies = meho_dependencies
        self.approval_store = approval_store
        self.llm_model = llm_model
        self.max_steps = max_steps
        
        # Create the LLM agent for reasoning
        # instrument=True enables Logfire tracing for LLM calls
        self.llm_agent: Agent[None, str] = Agent(llm_model, instrument=True)
        
        logger.info(f"MEHOReActGraph initialized with model={llm_model}, max_steps={max_steps}")
    
    async def run_streaming(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        user_id: str = "anonymous",
        existing_state: Optional[MEHOGraphState] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[GraphEvent]:
        """
        Run the ReAct graph with streaming output.
        
        This is the main entry point for chat interactions.
        Events are yielded as the graph executes.
        
        Args:
            user_message: The user's question/request
            session_id: Optional session ID for state persistence
            user_id: User ID for audit trail
            existing_state: Optional existing state (for resume after approval)
            conversation_history: Previous messages in this conversation (for context)
        
        Yields:
            GraphEvent objects for each step:
            - thought: LLM reasoning output
            - action: Tool being called
            - observation: Tool result
            - final_answer: Response to user
            - approval_required: Need user approval
            - error: Something went wrong
        """
        # Create event queue for streaming
        event_queue: asyncio.Queue[GraphEvent] = asyncio.Queue()
        
        # Progress callback that puts events into queue
        async def progress_callback(event_type: str, data: Dict[str, Any]) -> None:
            await event_queue.put(GraphEvent(type=event_type, data=data))
        
        # Create or restore state
        if existing_state:
            state = existing_state
            logger.info(f"Resuming from existing state (step {state.step_count})")
        else:
            # Get session_state from MEHODependencies FIRST (needed for intent detection)
            session_state = None
            if self.meho_dependencies and hasattr(self.meho_dependencies, 'session_state'):
                session_state = self.meho_dependencies.session_state
            
            # Detect request type (TASK-87) - pass cached entity info!
            has_cached_entities = bool(session_state and session_state.entities)
            cached_entity_names = []
            if session_state and session_state.entities:
                cached_entity_names = [e.entity_name for e in session_state.entities.values()]
            
            detection = detect_request_type(
                user_message,
                has_cached_entities=has_cached_entities,
                cached_entity_names=cached_entity_names,
            )
            request_type = detection.request_type
            
            logger.info(
                f"Intent detection: {request_type.value}, "
                f"cached_entities={len(cached_entity_names)}, "
                f"pattern='{detection.matched_pattern or 'none'}'"
            )
            
            state = MEHOGraphState(
                user_goal=user_message,
                request_type=request_type,
                session_id=session_id,
                session_state=session_state,  # Reference to persistent state!
                conversation_history=conversation_history or [],  # Previous messages for context
            )
            logger.info(f"New graph state: request_type={request_type.value}, has_session_state={session_state is not None}, history_len={len(conversation_history or [])}")
        
        # Create dependencies - pass MEHODependencies through!
        deps = MEHOGraphDeps(
            meho_deps=self.meho_dependencies,  # The source of truth
            llm_agent=self.llm_agent,
            approval_store=self.approval_store,
            tools={},  # Will be registered below
            max_steps=self.max_steps,
            session_id=session_id,
            progress_callback=progress_callback,
        )
        
        # Register tool handlers with state access
        self._register_tools_with_state(deps, state)
        
        # Run the graph in a background task
        async def run_graph() -> None:
            try:
                await self._execute_graph(state, deps)
            except Exception as e:
                logger.error(f"Graph execution failed: {e}", exc_info=True)
                await event_queue.put(GraphEvent(
                    type="error",
                    data={"message": str(e)}
                ))
            finally:
                # Signal completion
                await event_queue.put(GraphEvent(type="_done", data={}))
        
        # Start graph execution
        graph_task = asyncio.create_task(run_graph())
        
        # Yield events as they come
        try:
            while True:
                event = await event_queue.get()
                if event.type == "_done":
                    break
                yield event
        finally:
            # Ensure graph task completes
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except asyncio.CancelledError:
                    pass
    
    def _register_tools_with_state(
        self, 
        deps: MEHOGraphDeps, 
        state: MEHOGraphState
    ) -> None:
        """Register tool handlers with access to current state."""
        # Import handlers here to avoid circular imports
        from meho_agent.react.tool_handlers import (
            # GENERIC TOOLS (TASK-97 - work for all connector types)
            search_operations_handler,
            call_operation_handler,
            search_types_handler,
            # Other tools
            search_knowledge_handler,
            list_connectors_handler,
            reduce_data_handler,
        )
        
        # =================================================================
        # GENERIC TOOLS (TASK-97 - work for all connector types)
        # =================================================================
        deps.register_tool("search_operations", search_operations_handler)
        
        async def call_operation_with_state(d: MEHOGraphDeps, args: Dict[str, Any]) -> str:
            return await call_operation_handler(d, args, state=state)
        deps.register_tool("call_operation", call_operation_with_state)
        
        deps.register_tool("search_types", search_types_handler)
        
        # =================================================================
        # OTHER TOOLS
        # =================================================================
        deps.register_tool("search_knowledge", search_knowledge_handler)
        deps.register_tool("list_connectors", list_connectors_handler)
        deps.register_tool("reduce_data", reduce_data_handler)
    
    async def _execute_graph(
        self, 
        state: MEHOGraphState, 
        deps: MEHOGraphDeps
    ) -> None:
        """
        Execute the ReAct graph loop.
        
        This is a manual graph execution since pydantic-graph requires
        Python 3.12+ for native graph syntax. We implement the same
        flow manually.
        
        TASK-92 Flow (typed nodes):
        
        ReasonNode → (validates input) → TypedToolNode → ReasonNode
                  ↘ (validation fails) → ReasonNode (retry with error)
                  ↘ (Final Answer) → End
        
        Where TypedToolNode is one of:
        - SearchEndpointsNode
        - CallEndpointNode  
        - ReduceDataNode
        - SearchKnowledgeNode
        - ListConnectorsNode
        """
        # Import nodes here to avoid circular imports
        from meho_agent.react.nodes.reason_node import ReasonNode
        from meho_agent.react.nodes.approval_check_node import ApprovalCheckNode
        
        # Start with ReasonNode
        current_node: Any = ReasonNode()
        
        while True:
            # Create a mock run context
            @dataclass
            class MockRunContext:
                state: MEHOGraphState
                deps: MEHOGraphDeps
            
            ctx = MockRunContext(state=state, deps=deps)
            
            # Execute current node
            node_name = type(current_node).__name__
            logger.info(f"Executing node: {node_name}")
            
            try:
                next_node = await current_node.run(ctx)
            except Exception as e:
                logger.error(f"Node execution failed: {e}", exc_info=True)
                state.error_message = str(e)
                await deps.emit_progress("error", {"message": str(e)})
                break
            
            # Check if we're done
            if next_node is None or (hasattr(next_node, '__class__') and 
                                      next_node.__class__.__name__ == 'End'):
                logger.info("Graph completed")
                break
            
            # Continue to next node
            current_node = next_node
    
    async def run(self, user_message: str, **kwargs: Any) -> str:
        """
        Run the graph synchronously (non-streaming).
        
        Returns the final answer as a string.
        """
        final_answer = None
        
        async for event in self.run_streaming(user_message, **kwargs):
            if event.type == "final_answer":
                final_answer = event.data.get("content", "")
            elif event.type == "error":
                return f"Error: {event.data.get('message', 'Unknown error')}"
            elif event.type == "approval_required":
                return f"Approval required: {event.data.get('description', 'Action needs approval')}"
        
        return final_answer or "No response generated"
    
    def get_state_for_persistence(self, state: MEHOGraphState) -> Dict[str, Any]:
        """Get state dict for Redis persistence."""
        return state.to_dict()
    
    def restore_state_from_persistence(self, data: Dict[str, Any]) -> MEHOGraphState:
        """Restore state from Redis persistence."""
        return MEHOGraphState.from_dict(data)

