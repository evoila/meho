"""
MEHO ReAct Graph State (TASK-89)

State for the ReAct reasoning loop (EPHEMERAL - one request only).

Architecture:
- MEHOGraphState: ReAct-specific (scratchpad, steps) - EPHEMERAL
- AgentSessionState: Persistent data (entities, connectors) - REDIS

GraphState has a REFERENCE to SessionState - no duplication!
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, TYPE_CHECKING
from datetime import datetime

from meho_agent.intent_classifier import RequestType

if TYPE_CHECKING:
    from meho_agent.session_state import AgentSessionState, ExtractedEntity


@dataclass
class ParsedStep:
    """
    Parsed output from LLM reasoning step.
    
    The LLM outputs in ReAct format:
        Thought: <reasoning>
        Action: <tool_name>
        Action Input: <json_params>
    
    OR:
        Thought: <reasoning>
        Final Answer: <response>
    """
    thought: Optional[str] = None
    action: Optional[str] = None
    action_input: Optional[str] = None
    final_answer: Optional[str] = None
    raw_output: str = ""


@dataclass 
class ActionSignature:
    """
    A compact representation of an action for loop detection.
    Used to identify when the agent is repeating similar actions.
    """
    tool_name: str
    key_args: str  # Simplified string of key arguments (e.g., "connector_id:abc,query:list")
    
    def __hash__(self) -> int:
        return hash((self.tool_name, self.key_args))
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ActionSignature):
            return False
        return self.tool_name == other.tool_name and self.key_args == other.key_args


@dataclass
class MEHOGraphState:
    """
    State for the ReAct reasoning loop (EPHEMERAL - one request).
    
    Architecture:
    - This state is for ReAct-specific fields (scratchpad, steps, approval)
    - Persistent data (entities, connectors) lives in session_state (Redis)
    - This state has a REFERENCE to session_state - no duplication!
    
    Lifetime: Created at start of request, discarded at end.
    Exception: Approval flow saves/restores this state temporarily.
    """
    
    # =========================================================================
    # USER INPUT
    # =========================================================================
    
    user_goal: str
    """The original user message/question"""
    
    request_type: RequestType = RequestType.UNKNOWN
    """Detected request type from TASK-87 intent classifier"""
    
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    """
    Previous messages in this conversation (last N messages).
    Used to understand context when user is following up or answering
    a clarifying question the agent asked.
    Format: [{"role": "user/assistant", "content": "..."}]
    """
    
    # =========================================================================
    # REACT LOOP STATE (ephemeral - one request)
    # =========================================================================
    
    scratchpad: List[str] = field(default_factory=list)
    """
    Accumulated thoughts and observations for THIS request.
    Each entry is either:
    - LLM reasoning output (Thought/Action/Action Input)
    - Tool observation result
    Reset each request - conversation history provides cross-request context.
    """
    
    pending_tool: Optional[str] = None
    """Tool name that needs to be executed (set by ReasonNode)"""
    
    pending_args: Optional[Dict[str, Any]] = None
    """Tool arguments (set by ReasonNode, may be JSON string or dict)"""
    
    step_count: int = 0
    """Number of Action→Observation cycles completed (for depth limiting)"""
    
    last_observation: Optional[str] = None
    """Result of the most recent tool execution"""
    
    final_answer: Optional[str] = None
    """The final response to return to user (set when done)"""
    
    # =========================================================================
    # LOOP DETECTION STATE (TASK-XX: prevent circular reasoning)
    # =========================================================================
    
    action_history: List[ActionSignature] = field(default_factory=list)
    """
    History of actions taken for loop detection.
    Each entry is an ActionSignature with tool name and key arguments.
    """
    
    loop_warning_count: int = 0
    """Number of times the agent has been warned about looping."""
    
    forced_conclusion_mode: bool = False
    """
    When True, the agent MUST provide a Final Answer.
    Set when loop detection triggers twice.
    """
    
    explored_approaches: List[str] = field(default_factory=list)
    """
    Track what approaches have been tried.
    Helps the agent understand what NOT to repeat.
    """
    
    # =========================================================================
    # ERROR HANDLING
    # =========================================================================
    
    missing_action_retry: bool = False
    """Flag to allow one retry if LLM doesn't output proper Action format"""
    
    error_message: Optional[str] = None
    """Error message if something went wrong"""
    
    # =========================================================================
    # APPROVAL STATE (TASK-76)
    # =========================================================================
    
    pending_approval_id: Optional[str] = None
    """ID of pending approval request in database"""
    
    approval_granted: bool = False
    """Set to True when user approves, triggers CallEndpointNode to proceed"""
    
    approval_rejected: bool = False
    """Set to True when user rejects, triggers ReasonNode with rejection info"""
    
    # =========================================================================
    # SESSION STATE REFERENCE (persistent data lives here!)
    # =========================================================================
    
    session_state: Optional[Any] = None  # AgentSessionState
    """
    Reference to the persistent session state (stored in Redis).
    Contains: entities, connectors, primary_connector_id, reduction stats.
    Tools write directly to this - no duplication!
    """
    
    # =========================================================================
    # METADATA
    # =========================================================================
    
    session_id: Optional[str] = None
    """Chat session ID for persistence"""
    
    created_at: datetime = field(default_factory=datetime.utcnow)
    """When this state was created"""
    
    # =========================================================================
    # SERIALIZATION (for approval flow only - temporary save/restore)
    # =========================================================================
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize ReAct-specific state for approval flow.
        
        NOTE: Only used for approval flow (temporary save/restore).
        Persistent data (entities, connectors) lives in session_state (Redis).
        """
        return {
            # User input
            "user_goal": self.user_goal,
            "request_type": self.request_type.value,
            
            # ReAct loop
            "scratchpad": self.scratchpad,
            "pending_tool": self.pending_tool,
            "pending_args": self.pending_args,
            "step_count": self.step_count,
            "last_observation": self.last_observation,
            "final_answer": self.final_answer,
            
            # Error handling
            "missing_action_retry": self.missing_action_retry,
            "error_message": self.error_message,
            
            # Approval
            "pending_approval_id": self.pending_approval_id,
            "approval_granted": self.approval_granted,
            "approval_rejected": self.approval_rejected,
            
            # Loop detection
            "action_history": [
                {"tool_name": a.tool_name, "key_args": a.key_args}
                for a in self.action_history
            ],
            "loop_warning_count": self.loop_warning_count,
            "forced_conclusion_mode": self.forced_conclusion_mode,
            "explored_approaches": self.explored_approaches,
            
            # Metadata
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            
            # NOTE: session_state is NOT serialized here - it's separate in Redis
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MEHOGraphState":
        """Deserialize ReAct state (for approval flow continuation)."""
        # Restore action history
        action_history_data = data.get("action_history", [])
        action_history = [
            ActionSignature(tool_name=a["tool_name"], key_args=a["key_args"])
            for a in action_history_data
        ]
        
        state = cls(
            user_goal=data.get("user_goal", ""),
            request_type=RequestType(data.get("request_type", "unknown")),
            scratchpad=data.get("scratchpad", []),
            pending_tool=data.get("pending_tool"),
            pending_args=data.get("pending_args"),
            step_count=data.get("step_count", 0),
            last_observation=data.get("last_observation"),
            final_answer=data.get("final_answer"),
            missing_action_retry=data.get("missing_action_retry", False),
            error_message=data.get("error_message"),
            pending_approval_id=data.get("pending_approval_id"),
            approval_granted=data.get("approval_granted", False),
            approval_rejected=data.get("approval_rejected", False),
            session_id=data.get("session_id"),
            # Loop detection state
            action_history=action_history,
            loop_warning_count=data.get("loop_warning_count", 0),
            forced_conclusion_mode=data.get("forced_conclusion_mode", False),
            explored_approaches=data.get("explored_approaches", []),
            # session_state will be set separately after loading from Redis
        )
        
        # Parse created_at
        if "created_at" in data and data["created_at"]:
            try:
                state.created_at = datetime.fromisoformat(data["created_at"])
            except (ValueError, TypeError):
                pass
        
        return state
    
    # =========================================================================
    # HELPER METHODS (delegate to session_state for persistent data)
    # =========================================================================
    
    def get_entity_count(self) -> Dict[str, int]:
        """Get count of entities by type (from session_state)."""
        if not self.session_state:
            return {}
        result: Dict[str, int] = self.session_state.get_entity_count()
        return result
    
    def get_entity_summary(self) -> str:
        """Get a summary of cached entities for context injection."""
        if not self.session_state:
            return "none"
        counts = self.session_state.get_entity_count()
        if not counts:
            return "none"
        return ", ".join(f"{count} {etype}(s)" for etype, count in counts.items())
    
    def find_entity(self, name_or_id: str) -> Optional[Any]:  # Optional[ExtractedEntity]
        """Find an entity by name or ID (from session_state)."""
        if not self.session_state:
            return None
        return self.session_state.find_entity(name_or_id)
    
    def add_to_scratchpad(self, content: str) -> None:
        """Add content to the scratchpad."""
        self.scratchpad.append(content)
    
    def get_scratchpad_text(self) -> str:
        """Get the full scratchpad as a single string."""
        if not self.scratchpad:
            return "(empty)"
        return "\n".join(self.scratchpad)
    
    # =========================================================================
    # LOOP DETECTION METHODS
    # =========================================================================
    
    def record_action(self, tool_name: str, args: Dict[str, Any]) -> None:
        """
        Record an action for loop detection.
        Creates a signature from tool name and key arguments.
        """
        # Extract key arguments based on tool type
        key_parts = []
        
        if tool_name == "list_connectors":
            key_parts = ["list_connectors"]
        elif tool_name in ("search_operations", "search_endpoints", "search_types"):
            connector_id = args.get("connector_id", "")[:8] if args.get("connector_id") else "none"
            query = args.get("query", args.get("search", ""))[:50].lower()
            key_parts = [connector_id, query]
        elif tool_name in ("call_operation", "call_endpoint"):
            operation_id = args.get("operation_id", args.get("endpoint_id", ""))[:8]
            # For call operations, also track the first parameter set's key values
            param_sets = args.get("parameter_sets", [{}])
            if param_sets and len(param_sets) > 0:
                first_params = param_sets[0]
                # Get first 2 param values as identifiers
                param_vals = list(first_params.values())[:2]
                param_str = ",".join(str(v)[:20] for v in param_vals)
                key_parts = [operation_id, param_str]
            else:
                key_parts = [operation_id]
        elif tool_name == "reduce_data":
            sql = args.get("sql", "")[:100].lower()
            key_parts = [sql]
        elif tool_name == "search_knowledge":
            query = args.get("query", "")[:50].lower()
            key_parts = [query]
        else:
            # Generic: use first 2 arg values
            values = list(args.values())[:2]
            key_parts = [str(v)[:30] for v in values]
        
        key_args = "|".join(key_parts)
        signature = ActionSignature(tool_name=tool_name, key_args=key_args)
        self.action_history.append(signature)
    
    def detect_loop(self, window_size: int = 10, repeat_threshold: int = 3) -> Optional[str]:
        """
        Detect if the agent is in a loop.
        
        Checks the last `window_size` actions for repeated patterns.
        Returns a description of the loop if detected, None otherwise.
        
        Loop patterns detected:
        1. Same exact action repeated >= repeat_threshold times
        2. Same tool called >= repeat_threshold+1 times with similar args
        3. Oscillating pattern (A -> B -> A -> B)
        """
        if len(self.action_history) < window_size:
            return None
        
        recent = self.action_history[-window_size:]
        
        # Pattern 1: Exact same action repeated
        from collections import Counter
        action_counts = Counter(recent)
        for action, count in action_counts.most_common(3):
            if count >= repeat_threshold:
                return f"Repeated '{action.tool_name}' {count} times with similar parameters"
        
        # Pattern 2: Same tool called too many times
        tool_counts = Counter(a.tool_name for a in recent)
        for tool, count in tool_counts.most_common(3):
            if count >= repeat_threshold + 2:  # More lenient for tool-level
                return f"Called '{tool}' {count} times in last {window_size} actions"
        
        # Pattern 3: Oscillating between two tools
        if len(recent) >= 6:
            last_6_tools = [a.tool_name for a in recent[-6:]]
            # Check for A-B-A-B-A-B pattern
            if (last_6_tools[0] == last_6_tools[2] == last_6_tools[4] and
                last_6_tools[1] == last_6_tools[3] == last_6_tools[5] and
                last_6_tools[0] != last_6_tools[1]):
                return f"Oscillating between '{last_6_tools[0]}' and '{last_6_tools[1]}'"
        
        return None
    
    def get_action_summary(self) -> str:
        """Get a summary of actions taken for debugging."""
        if not self.action_history:
            return "No actions taken yet"
        
        from collections import Counter
        tool_counts = Counter(a.tool_name for a in self.action_history)
        summary_parts = [f"{tool}: {count}" for tool, count in tool_counts.most_common()]
        return f"Actions taken: {', '.join(summary_parts)}"
    
    def add_explored_approach(self, approach: str) -> None:
        """Record an approach that was tried (for context in prompts)."""
        if approach not in self.explored_approaches:
            self.explored_approaches.append(approach)

