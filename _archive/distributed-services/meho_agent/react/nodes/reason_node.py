from __future__ import annotations
"""
ReasonNode - LLM Reasoning Step (TASK-89, TASK-92)

This node is the "Brain" of the ReAct loop.
It prompts the LLM to generate a Thought and either:
- An Action (tool to call with parameters)
- A Final Answer (response to user)

The output is parsed into a structured ParsedStep which
determines the next node in the graph.

TASK-92: Now returns typed tool nodes for input validation.
"""

import json
import re
import logging
from typing import Union, TYPE_CHECKING
from dataclasses import dataclass

from pydantic import ValidationError
from pydantic_graph import BaseNode, End, GraphRunContext

if TYPE_CHECKING:
    from meho_agent.react.nodes.approval_check_node import ApprovalCheckNode

from meho_agent.react.graph_state import MEHOGraphState, ParsedStep
from meho_agent.react.graph_deps import MEHOGraphDeps
from meho_agent.intent_classifier import RequestType

# Import typed tool nodes (TASK-92, TASK-97)
from meho_agent.react.nodes.tool_nodes import (
    # GENERIC NODES (TASK-97 - work for all connector types)
    SearchOperationsNode,
    CallOperationNode,
    SearchTypesNode,
    # Knowledge & Data
    SearchKnowledgeNode,
    ListConnectorsNode,
    ReduceDataNode,
)
from meho_agent.react.tool_inputs import (
    ReduceDataInput,
    SearchKnowledgeInput,
)

logger = logging.getLogger(__name__)


def _build_system_prompt(state: MEHOGraphState, deps: MEHOGraphDeps) -> str:
    """
    Build the ReAct-format system prompt.
    
    This prompt enforces the Thought/Action/Observation format
    and provides context about available tools and state.
    """
    tool_names = deps.list_tool_names()
    tool_list = "\n".join(f"- {name}" for name in tool_names)
    
    # Build entity summary if we have cached data
    entity_summary = state.get_entity_summary()
    entity_context = ""
    if entity_summary != "none":
        entity_context = f"""
## Cached Data
You have access to cached entities from previous API calls:
{entity_summary}

When the user asks about data you already have, use it directly - DO NOT call APIs again.
"""
    
    # Build SQL tables context (for multi-turn conversations)
    tables_context = ""
    try:
        from meho_agent.unified_executor import get_unified_executor
        # Pass Redis for persistent cache (L2) - critical for multi-turn!
        redis_client = deps.meho_deps.redis if deps.meho_deps else None
        executor = get_unified_executor(redis_client)
        session_id = deps.session_id or "anonymous"
        tables_info = executor.get_session_table_info(session_id)
        
        if tables_info:
            table_lines = []
            for info in tables_info:
                cols = ", ".join(info["columns"][:6])  # Show first 6 columns
                if len(info["columns"]) > 6:
                    cols += ", ..."
                table_lines.append(f"- **{info['table']}** ({info['row_count']} rows): {cols}")
            
            tables_context = f"""
## Available SQL Tables
Query these tables with reduce_data using SQL:
{chr(10).join(table_lines)}

Example: `{{"sql": "SELECT * FROM {tables_info[0]['table']} WHERE ... LIMIT 10"}}`
"""
    except Exception:
        pass  # Tables not available yet
    
    # Build conversation history context (for multi-turn conversations)
    history_context = ""
    if state.conversation_history:
        # Format last few messages for context
        history_lines = []
        for msg in state.conversation_history[-6:]:  # Last 6 messages (3 turns)
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:500]  # Truncate long messages
            if role == "user":
                history_lines.append(f"User: {content}")
            elif role == "assistant":
                history_lines.append(f"You (MEHO): {content}")
        
        if history_lines:
            history_context = f"""
## Conversation History (most recent)
{chr(10).join(history_lines)}

**IMPORTANT**: If the user's current message is answering a question you asked, 
use the context from your previous response. Don't start over - continue from where you left off!
"""
    
    # Add request-type specific guidance (from TASK-87)
    request_guidance = _get_request_type_guidance(state.request_type)
    
    # Build the scratchpad (previous thoughts/observations)
    scratchpad = ""
    if state.scratchpad:
        scratchpad = f"""
## Previous Steps
{state.get_scratchpad_text()}
"""
    
    return f"""You are MEHO, a deterministic ReAct agent for multi-system infrastructure operations.

## Response Format (STRICT - follow exactly)

Always produce your response in EXACTLY this format:

Thought: <your step-by-step reasoning about what to do>
Action: <tool_name>
Action Input: <JSON parameters for the tool>

OR if you can answer the user's question directly:

Thought: <your reasoning about why you can answer now>
Final Answer: <your complete response to the user>

## Available Tools
{tool_list}

## Tool Descriptions

### Connector & Operation Tools (SAME tools for ALL connector types!)
- list_connectors: List all available system connectors. **CALL THIS FIRST** to get connector IDs!
  Returns connector_type field: "rest", "soap", or "vmware"
- search_operations: Search for operations on ANY connector type.
  **REQUIRED PARAMS**: connector_id AND query (NEVER leave query empty!)
  Use descriptive search terms: "disk performance", "network metrics", "list vms", "cpu usage"
  Works for REST endpoints, SOAP operations, AND VMware operations.
  Returns operation_id, name, description, parameters.
- call_operation: Execute an operation on ANY connector type. Takes connector_id, operation_id, and parameter_sets.
  **parameter_sets is ALWAYS a list**: Single call = [{{}}], Batch = [{{item1}}, {{item2}}, ...]
  **For large responses (>20 items)**: Data is cached as a SQL table. Use reduce_data with SQL to filter/query.
- search_types: Search for entity type definitions. Works for SOAP and VMware connectors.
  Use this to discover what properties exist on objects.

### Other Tools
- search_knowledge: Search documentation and knowledge base.
- reduce_data: Query cached data using SQL.
  
  Example:
  ```
  {{"sql": "SELECT name, status FROM resources WHERE status = 'inactive' ORDER BY name"}}
  ```
  
  Common SQL patterns:
  - Filter: `WHERE column = 'value'` or `WHERE column > 5`
  - Sort: `ORDER BY column DESC`
  - Limit: `LIMIT 10`
  - Multiple conditions: `WHERE a = 'x' AND b > 5`
  - Aggregations: `SELECT COUNT(*), AVG(memory) FROM resources`
  
  **LOOKUP MODE**: Find a specific entity by name.
  ```
  {{"sql": "SELECT * FROM resources WHERE name = 'my-resource'"}}
  ```

## SQL Tables Architecture
When call_operation returns a large response (>20 items), it automatically caches data as a SQL table:
- `table`: The table name (e.g., "resources", "pods", "servers") - use this in SQL queries
- `count`: Total rows in the table
- `columns`: Available columns for your SQL queries
- `sample`: Example rows (use to see data structure)

**IMPORTANT**: If you just need to LIST items, the sample in the call_operation response may be enough!
Only use reduce_data with SQL if you need to:
- Filter by specific criteria (e.g., `WHERE status = 'inactive'`)
- Sort the results (e.g., `ORDER BY memory DESC`)
- Aggregate data (e.g., `COUNT(*)`, `AVG(cpu)`)
- Join multiple tables

Tables persist across conversation turns - you can query data from previous API calls!

**IMPORTANT**: Always look at the schema/parameters returned by search_operations to know what fields are available. Do NOT assume field names!

## Workflow (CRITICAL)
**For ALL requests** (regardless of connector type):
1. `list_connectors` → find the right system by name/description
2. `search_operations` → find operations with connector_id and query
3. `call_operation` → execute with connector_id, operation_id, parameter_sets (ALWAYS a list!)
4. If needed: `reduce_data` with SQL → filter/sort/aggregate

**BATCH EXECUTION**: When you need the same operation for N items, pass ALL items in ONE call:
- Single: `parameter_sets=[{{"id": "resource-1"}}]`
- Batch: `parameter_sets=[{{"id": "r1"}}, {{"id": "r2"}}, {{"id": "r3"}}]`
- NEVER make separate calls for each item!

**The agent doesn't need to know REST vs SOAP vs VMware - just use the generic tools!**

**For FOLLOW-UP requests** (user refers to "those resources", "the pods", etc.):
1. Check the table name from a previous call_operation response (e.g., "resources", "pods")
2. Use `reduce_data` with SQL: `{{"sql": "SELECT * FROM <table_name> WHERE ..."}}`
3. Tables persist across turns - NO need to re-fetch data!

**AVOID**:
- Re-calling call_operation when you already have the data cached as a table
- Using the sample when you need filtered/sorted results (use SQL instead!)

## Operation Verification (CRITICAL)
When search_operations returns results, ALWAYS verify:
1. **Name/Description**: Does it match what you're looking for?
2. **Parameters**: What params does it need? Check required vs optional.
3. **Category**: Is it the right type of operation (compute, storage, networking)?

Always verify the operation matches the user's intent by checking parameters, not just the name.
{entity_context}
{tables_context}
{history_context}
{request_guidance}
{scratchpad}
## Important Rules
1. ALWAYS output Thought: first - explain your reasoning
2. After Thought, EITHER output "Action:" + "Action Input:" OR "Final Answer:"
3. NEVER output both Action and Final Answer in the same response
4. Action Input MUST be valid JSON (use double quotes)
5. Use "Final Answer:" when you need to:
   - Return results to the user
   - ASK the user for information (parameters, choices, clarification)
   - Explain something or provide guidance
6. Plan efficiently - you can ask for permission to continue if needed

## Parameter Collection for Complex Operations
When an operation needs multiple parameters (like creating a VM):
1. Search for the operation to discover required parameters
2. Use "Final Answer:" to ask the user for the required information
3. Guide the user step-by-step - don't ask for ALL parameters at once
4. Offer to list available resources (datastores, networks, etc.) if helpful
5. After user provides info, execute the operation

## Current Goal
User: {state.user_goal}
"""


def _get_request_type_guidance(request_type: RequestType) -> str:
    """Get request-type specific guidance for the LLM."""
    guidance = {
        RequestType.DATA_REFORMAT: """
## Request Type: DATA REFORMAT
The user wants to reformat data that you already have.
DO NOT call external APIs again - the data is already cached as SQL tables.

Use `reduce_data` with SQL to get the specific fields needed:
```
{{"sql": "SELECT field1, field2 FROM table_name WHERE ... ORDER BY ..."}}
```

NEVER generate fake/placeholder data. If you can't retrieve the cached data, tell the user.
""",
        RequestType.DATA_RECALL: """
## Request Type: DATA RECALL
The user is asking about specific cached data.
Check the cached entities - if you have the data, use Final Answer.
Only call APIs if the data is not cached.
""",
        RequestType.ACTION: """
## Request Type: ACTION
The user wants to perform an operation (create, update, delete, restart, etc.).

**IMPORTANT: Parameter Collection Flow**
1. Use search_operations to find the relevant operation
2. Check the operation's parameters (required vs optional)
3. If parameters are missing, use "Final Answer:" to ASK the user
   - Ask conversationally, one or two parameters at a time
   - Offer to LIST available resources (datastores, networks, clusters)
4. Once you have all required params, use call_operation to execute
5. Dangerous operations will require user approval (system handles this)

**Example for VM creation:**
- Find create_vm operation → see it needs vm_name, num_cpus, memory_mb, etc.
- Ask: "What should we name the VM? And how many CPUs/memory?"
- User provides values → Execute call_operation
""",
        RequestType.KNOWLEDGE: """
## Request Type: KNOWLEDGE
The user is asking a general question about concepts or best practices.
Use search_knowledge to find relevant documentation.
""",
        RequestType.DATA_QUERY: """
## Request Type: DATA QUERY  
The user wants to retrieve data from a system.
1. Use search_operations to find the right operation
2. Use call_operation to fetch the data
3. Use the response to provide the Final Answer
""",
        RequestType.UNKNOWN: "",
    }
    return guidance.get(request_type, "")


def _parse_react_output(raw_output: str) -> ParsedStep:
    """
    Parse LLM output into structured ParsedStep.
    
    Expected formats:
    
    Format 1 (Action):
        Thought: I need to search for resources
        Action: search_endpoints  
        Action Input: {"query": "list resources"}
    
    Format 2 (Final Answer):
        Thought: I have the data needed
        Final Answer: Here are the results...
    """
    step = ParsedStep(raw_output=raw_output)
    
    # Extract Thought
    thought_match = re.search(r"Thought:\s*(.+?)(?=\n(?:Action:|Final Answer:)|$)", raw_output, re.DOTALL)
    if thought_match:
        step.thought = thought_match.group(1).strip()
    
    # Extract Final Answer
    final_match = re.search(r"Final Answer:\s*(.+?)$", raw_output, re.DOTALL)
    if final_match:
        step.final_answer = final_match.group(1).strip()
        return step  # Final Answer takes precedence
    
    # Extract Action and Action Input
    action_match = re.search(r"Action:\s*(\w+)", raw_output)
    if action_match:
        step.action = action_match.group(1).strip()
    
    action_input_match = re.search(r"Action Input:\s*(.+?)(?=\n\n|$)", raw_output, re.DOTALL)
    if action_input_match:
        step.action_input = action_input_match.group(1).strip()
    
    return step


def _create_typed_tool_node(
    action: str,
    args: dict,
    state: MEHOGraphState,
) -> Union[
    # GENERIC NODES (TASK-97 - work for all connector types)
    SearchOperationsNode,
    CallOperationNode,
    SearchTypesNode,
    # Other tools
    ReduceDataNode,
    SearchKnowledgeNode,
    ListConnectorsNode,
    None,
]:
    """
    Create a typed tool node with validated inputs.
    
    Returns None if validation fails (error added to scratchpad).
    
    This follows pydantic-graph best practice where node fields are typed
    and validated by Pydantic when the node is instantiated.
    """
    try:
        if action == "reduce_data":
            reduce_input = ReduceDataInput(**args)
            return ReduceDataNode(sql=reduce_input.sql)
        
        elif action == "search_knowledge":
            knowledge_input = SearchKnowledgeInput(**args)
            return SearchKnowledgeNode(
                query=knowledge_input.query,
                limit=knowledge_input.limit,
            )
        
        elif action == "list_connectors":
            return ListConnectorsNode()
        
        # =================================================================
        # GENERIC TOOLS (TASK-97: same tools for all connector types)
        # =================================================================
        
        elif action == "search_operations":
            # Generic operation search - routes to REST/SOAP/VMware based on connector_type
            from meho_agent.react.tool_inputs import SearchOperationsInput
            search_ops_input = SearchOperationsInput(**args)
            # Use SearchOperationsNode (creates a dynamic node)
            return SearchOperationsNode(
                connector_id=search_ops_input.connector_id,
                query=search_ops_input.query,
                limit=search_ops_input.limit,
            )
        
        elif action == "call_operation":
            # Generic operation call - routes to REST/SOAP/VMware based on connector_type
            from meho_agent.react.tool_inputs import CallOperationInput
            call_op_input = CallOperationInput(**args)
            # Use CallOperationNode with parameter_sets for batch support
            return CallOperationNode(
                connector_id=call_op_input.connector_id,
                operation_id=call_op_input.operation_id,
                parameter_sets=call_op_input.parameter_sets,
            )
        
        elif action == "search_types":
            # Generic type search - routes to SOAP/VMware based on connector_type
            from meho_agent.react.tool_inputs import SearchTypesInput
            types_input = SearchTypesInput(**args)
            return SearchTypesNode(
                connector_id=types_input.connector_id,
                query=types_input.query,
                limit=types_input.limit,
            )
        
        else:
            logger.warning(f"Unknown action: {action}")
            return None
            
    except ValidationError as e:
        # Extract user-friendly error messages
        error_messages = []
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            error_messages.append(f"- {loc}: {error['msg']}")
        
        error_text = "\n".join(error_messages)
        
        # Add hints for common errors
        hints = []
        if action == "reduce_data":
            hints.append('HINT: reduce_data requires {"sql": "SELECT * FROM table_name WHERE ..."}')
        
        hint_text = "\n".join(hints) if hints else ""
        
        state.add_to_scratchpad(
            f"Observation: Invalid input for {action}:\n{error_text}\n{hint_text}\n"
            "Please correct the format and try again."
        )
        logger.warning(f"Validation error for {action}: {e}")
        return None


# Type alias for all possible node returns
ToolNodeUnion = Union[
    # GENERIC TOOLS (TASK-97 - work for all connector types)
    SearchOperationsNode,
    CallOperationNode,
    SearchTypesNode,
    # Other tools
    ReduceDataNode,
    SearchKnowledgeNode,
    ListConnectorsNode,
]


async def _generate_progress_summary(state: MEHOGraphState, deps: MEHOGraphDeps) -> str:
    """
    Generate a human-readable summary of what the agent has learned so far.
    
    Uses the LLM to synthesize findings from the scratchpad into a clear summary.
    """
    # Extract key information from scratchpad
    scratchpad = state.scratchpad or ""
    
    summary_prompt = f"""You are summarizing the progress of an investigation for a user.

USER'S ORIGINAL REQUEST: {state.user_goal}

WORK COMPLETED SO FAR:
{scratchpad[-8000:] if len(scratchpad) > 8000 else scratchpad}

Write a brief, helpful summary that includes:
1. **What I found:** Key facts discovered (specific values, names, metrics)
2. **Current status:** What the investigation has determined so far
3. **Next steps:** What still needs to be checked

Keep it concise but include specific data values (e.g., "CPU usage: 85%", "Memory: 4GB", "Status: running").
Format with markdown for readability. Do NOT include raw JSON."""

    try:
        result = await deps.llm_agent.run(
            "Summarize progress",
            instructions=summary_prompt,
        )
        if hasattr(result, 'output'):
            return str(result.output)
        elif hasattr(result, 'data'):
            return str(result.data)
        else:
            return str(result)
    except Exception as e:
        logger.warning(f"Failed to generate summary: {e}")
        # Fallback to simple summary
        return f"Working on: {state.user_goal}\nLast observation available but summary generation failed."


@dataclass
class ReasonNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    LLM Reasoning Node.
    
    Prompts the LLM to think about the current state and decide
    what action to take (or provide a final answer).
    
    Transitions (TASK-92 - typed nodes):
    - If Final Answer → End (done)
    - If Action → Typed tool node (validates input via Pydantic)
    - If validation fails → ReasonNode (retry with error feedback)
    - If no valid output → retry or End with error
    """
    
    async def run(
        self, 
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union[ToolNodeUnion, 'ApprovalCheckNode', End[None], 'ReasonNode']:
        """Execute LLM reasoning step."""
        state = ctx.state
        deps = ctx.deps
        
        logger.info(f"ReasonNode: Step {state.step_count + 1}/{deps.max_steps}")
        
        # Check depth limit - generate summary and ask user for permission to continue
        if state.step_count >= deps.max_steps:
            # Generate a meaningful summary using LLM
            summary = await _generate_progress_summary(state, deps)
            state.final_answer = (
                f"I've taken {deps.max_steps} steps and want to make sure I'm on the right track.\n\n"
                f"{summary}\n\n"
                f"Would you like me to continue? Just say **'continue'** or tell me what to do next."
            )
            await deps.emit_progress("final_answer", {"content": state.final_answer})
            return End(None)
        
        # Build system prompt with current context
        system_prompt = _build_system_prompt(state, deps)
        
        # Call LLM for reasoning
        try:
            result = await deps.llm_agent.run(
                state.user_goal,
                instructions=system_prompt,
            )
            # PydanticAI returns .output for typed results, .data for untyped
            if hasattr(result, 'output'):
                raw_output = str(result.output)
            elif hasattr(result, 'data'):
                raw_output = str(result.data)
            else:
                raw_output = str(result)
            logger.debug(f"LLM output: {raw_output[:500]}...")
            
        except Exception as e:
            logger.error(f"LLM reasoning failed: {e}")
            state.error_message = str(e)
            await deps.emit_progress("error", {"message": str(e)})
            return End(None)
        
        # Parse the ReAct output
        parsed = _parse_react_output(raw_output)
        
        # Emit thought to frontend
        if parsed.thought:
            await deps.emit_progress("thought", {"content": parsed.thought})
            state.add_to_scratchpad(f"Thought: {parsed.thought}")
        
        # Check for Final Answer
        if parsed.final_answer:
            state.final_answer = parsed.final_answer
            await deps.emit_progress("final_answer", {"content": parsed.final_answer})
            return End(None)
        
        # Check for valid Action
        if not parsed.action:
            # LLM didn't produce valid action format
            if not state.missing_action_retry:
                # Allow one retry
                logger.warning("LLM output missing Action, retrying")
                state.missing_action_retry = True
                state.add_to_scratchpad(
                    "Observation: Your response was missing Action or Final Answer. "
                    "Please respond with Thought: followed by either Action:/Action Input: or Final Answer:"
                )
                # Recurse to try again
                return await self.run(ctx)
            else:
                # Already retried, give up
                logger.error(f"LLM output invalid after retry: {raw_output[:200]}")
                state.final_answer = "I encountered an issue with my reasoning. Please try rephrasing your request."
                await deps.emit_progress("error", {"message": "Invalid LLM output format"})
                return End(None)
        
        # Reset retry flag on success
        state.missing_action_retry = False
        
        # Verify tool exists
        if not deps.get_tool(parsed.action):
            state.add_to_scratchpad(
                f"Observation: Tool '{parsed.action}' is not available. "
                f"Available tools: {', '.join(deps.list_tool_names())}"
            )
            state.step_count += 1
            # Try reasoning again with error feedback
            return await self.run(ctx)
        
        # Parse action input
        try:
            if parsed.action_input:
                args = json.loads(parsed.action_input)
            else:
                args = {}
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Action Input as JSON: {e}")
            args = {"raw": parsed.action_input}
        
        # Log the action
        state.add_to_scratchpad(f"Action: {parsed.action}")
        state.add_to_scratchpad(f"Action Input: {parsed.action_input or '{}'}")
        
        await deps.emit_progress("action", {
            "tool": parsed.action,
            "args": args,
        })
        
        # TASK-92: Create typed tool node with validation
        typed_node = _create_typed_tool_node(parsed.action, args, state)
        
        if typed_node is None:
            # Validation failed - error already added to scratchpad
            state.step_count += 1
            # Return to reasoning with error feedback
            return ReasonNode()
        
        # Return the typed node (will be executed by pydantic-graph)
        return typed_node


# Import for type checking
from meho_agent.react.nodes.approval_check_node import ApprovalCheckNode  # noqa: E402
