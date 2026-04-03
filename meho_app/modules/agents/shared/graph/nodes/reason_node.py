# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

"""
ReasonNode - LLM Reasoning Step (TASK-89, TASK-92, TASK-193)

This node is the "Brain" of the ReAct loop.
It prompts the LLM to generate a Thought and either:
- An Action (tool to call with parameters)
- A Final Answer (response to user)

The output is parsed into a structured ParsedStep which
determines the next node in the graph.

TASK-92: Now returns typed tool nodes for input validation.
TASK-193: Observability via EventEmitter detailed events (thought_detailed, action_detailed).
"""

import json  # noqa: E402 -- conditional/deferred import
import re  # noqa: E402 -- conditional/deferred import
import time  # noqa: E402 -- conditional/deferred import
from dataclasses import dataclass  # noqa: E402 -- conditional/deferred import
from typing import Any  # noqa: E402 -- conditional/deferred import

from pydantic_graph import (  # noqa: E402 -- conditional/deferred import
    BaseNode,
    End,
    GraphRunContext,
)

from meho_app.core.config import get_config  # noqa: E402 -- conditional/deferred import
from meho_app.core.otel import get_logger  # noqa: E402 -- conditional/deferred import
from meho_app.modules.agents.intent_classifier import (  # noqa: E402 -- conditional/deferred import
    RequestType,
)
from meho_app.modules.agents.shared.graph.graph_deps import (  # noqa: E402 -- conditional/deferred import
    MEHOGraphDeps,
)
from meho_app.modules.agents.shared.graph.graph_state import (  # noqa: E402 -- conditional/deferred import
    MEHOGraphState,
    ParsedStep,
)

logger = get_logger(__name__)


def _build_system_prompt(
    state: MEHOGraphState, deps: MEHOGraphDeps
) -> str:  # NOSONAR (cognitive complexity)
    """
    Build the ReAct-format system prompt using Claude-optimized XML tag structure.

    Uses <role>, <rules>, <response_format>, <tools>, <examples> XML tags
    which Claude follows more reliably than markdown headers.
    """
    tool_names = deps.list_tool_names()
    tool_list = "\n".join(f"- {name}" for name in tool_names)

    # Build SQL tables context (for multi-turn conversations)
    tables_context = ""
    try:
        from meho_app.modules.agents.unified_executor import get_unified_executor

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

            tables_context = f"""  # noqa: S608 -- static SQL query, no user input
<sql_tables>
Query these tables with reduce_data using SQL:
{chr(10).join(table_lines)}

Example: `{{"sql": "SELECT * FROM {tables_info[0]["table"]} WHERE ... LIMIT 10"}}`
</sql_tables>
"""
    except Exception:  # noqa: S110 -- intentional silent exception handling
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
<history>
{chr(10).join(history_lines)}

IMPORTANT: If the user's current message is answering a question you asked,
use the context from your previous response. Don't start over -- continue from where you left off.
</history>
"""

    # Add request-type specific guidance (from TASK-87)
    request_guidance = _get_request_type_guidance(state.request_type)

    # Build the scratchpad (previous thoughts/observations)
    scratchpad = ""
    if state.scratchpad:
        scratchpad = f"""
<scratchpad>
{state.get_scratchpad_text()}
</scratchpad>
"""

    # Build topology context (TASK-127 - system topology learning)
    topology_context = ""
    if state.topology_context:
        topology_context = f"""
<topology>
{state.topology_context}
</topology>
"""

    return f"""<role>  # noqa: S608 -- static SQL query, no user input
You are MEHO, an expert infrastructure diagnostics agent. You operate like a senior SRE -- direct, technically precise, and you show your reasoning at every step. You investigate multi-system infrastructure (Kubernetes, VMware, REST APIs, SOAP services) to answer operator questions with real data, not guesses.
</role>

<response_format>
You MUST respond in exactly one of these two formats:

Format A -- When you need to use a tool:
Thought: [Your step-by-step reasoning about what to investigate and why]
Action: [tool_name]
Action Input: [Valid JSON parameters]

Format B -- When you can answer the user's question:
Thought: [Your reasoning about why you have enough information]
Final Answer: [Your complete response to the operator]

<examples>
<example type="tool_call">
Thought: The operator wants to see pods in the production namespace. I need to find the Kubernetes connector first, then search for a "list pods" operation.
Action: list_connectors
Action Input: {{}}
</example>

<example type="tool_call_with_params">
Thought: I found the K8s connector (ID: k8s-prod). Now I need to search for an operation that lists pods, filtering by namespace.
Action: search_operations
Action Input: {{"connector_id": "k8s-prod", "query": "list pods namespace"}}
</example>

<example type="cached_data_flow">
Thought: call_operation returned data_available=false with table "pods" (44 rows). I do NOT have the actual data yet -- only metadata. I must query the table with reduce_data before I can answer.
Action: reduce_data
Action Input: {{"sql": "SELECT name, status, namespace FROM pods WHERE namespace = 'production'"}}
</example>

<example type="final_answer">
Thought: I now have the pod list from the production namespace. There are 12 pods running, 2 in CrashLoopBackOff. I can answer with the actual data.
Final Answer: Found 12 pods in the `production` namespace:

| Name | Status | Restarts |
|------|--------|----------|
| api-gateway-7f8d9 | CrashLoopBackOff | 47 |
| auth-service-3b2c1 | CrashLoopBackOff | 23 |
| web-frontend-a1b2c | Running | 0 |
| ... | ... | ... |

The two CrashLoopBackOff pods have been restarting for the last 15 minutes. Check logs for `api-gateway-7f8d9` -- exit code 137 suggests OOMKilled (memory limit too low).
</example>
</examples>
</response_format>

<tools>
Available tools: {tool_list}

<tool_group name="connector_operations">
These tools work identically for ALL connector types (REST, SOAP, VMware):

- list_connectors: List all available system connectors. CALL THIS FIRST to get connector IDs.
  Returns connector_type field: "rest", "soap", or "vmware"

- search_operations: Search for operations on ANY connector type.
  REQUIRED PARAMS: connector_id AND query (NEVER leave query empty!)
  Use descriptive search terms: "disk performance", "network metrics", "list vms", "cpu usage"
  Returns operation_id, name, description, parameters.

- call_operation: Execute an operation on ANY connector type.
  Takes connector_id, operation_id, and parameter_sets.
  parameter_sets is ALWAYS a list: Single call = [{{}}], Batch = [{{item1}}, {{item2}}, ...]
  For large responses (>20 items): Data is cached as a SQL table. Use reduce_data to query.

- search_types: Search for entity type definitions.
  Works for SOAP and VMware connectors. Use to discover object properties.
</tool_group>

<tool_group name="data_tools">
- search_knowledge: Search documentation and knowledge base.

- reduce_data: Query cached data using SQL.
  Common patterns:
  - Filter: WHERE column = 'value' or WHERE column > 5
  - Sort: ORDER BY column DESC
  - Limit: LIMIT 10
  - Aggregations: SELECT COUNT(*), AVG(memory) FROM resources
  - Lookup: SELECT * FROM resources WHERE name = 'my-resource'
</tool_group>

<tool_group name="topology_tools">
- lookup_topology: Check known entities from previous investigations.
  Input: {{"query": "shop.example.com", "traverse_depth": 10, "cross_connectors": true}}
  Returns: full topology chain (URL -> Ingress -> Pod -> Node -> VM -> Storage)
  Also returns possibly_related entities across connectors.
  Use BEFORE making API calls to check existing knowledge.

- invalidate_topology: Mark an entity as stale when it no longer exists.
  Input: {{"entity_name": "shop-ingress", "reason": "Not found in K8s API (404)"}}

Topology is learned AUTOMATICALLY after Final Answer -- no manual storage needed.
</tool_group>
</tools>

<rules>
- Show your reasoning at every step -- operators need to see WHY you're checking each system
- Never summarize raw data unless explicitly asked -- show actual values, counts, names
- When data spans multiple systems, explicitly state the cross-system correlation
- Use infrastructure terminology naturally: pods, nodes, ingress, VMs, not "computing units"
- If a tool call fails, explain what happened and try an alternative approach
- For large datasets, use reduce_data with SQL to filter before presenting
- Always call list_connectors FIRST if you don't know which connectors are available
- ALWAYS output Thought: first -- explain your reasoning before acting
- After Thought, output EITHER "Action:" + "Action Input:" OR "Final Answer:" -- never both
- Action Input MUST be valid JSON (use double quotes)
- Use "Final Answer:" to return results, ask for info, explain, or provide guidance

<critical_rules>
HANDLING CACHED DATA (data_available: false):
When call_operation returns "data_available": false:
- You do NOT have the actual data -- only metadata
- You MUST call reduce_data with SQL before answering
- Do NOT hallucinate or guess data values
- Do NOT say "I have retrieved X items" -- you haven't retrieved the actual data

Required flow when data_available is false:
1. call_operation returns: {{"data_available": false, "table": "namespaces", "row_count": 29}}
2. You MUST call: reduce_data({{"sql": "SELECT name FROM namespaces"}})
3. ONLY THEN can you present actual data to the user

BATCH EXECUTION:
When you need the same operation for N items, pass ALL items in ONE call:
- Single: parameter_sets=[{{"id": "resource-1"}}]
- Batch: parameter_sets=[{{"id": "r1"}}, {{"id": "r2"}}, {{"id": "r3"}}]
- NEVER make separate calls for each item

FOLLOW-UP REQUESTS:
Tables persist across conversation turns. When the user refers to "those resources" or "the pods":
1. Use reduce_data with SQL on the existing table
2. Do NOT re-call call_operation for data you already have
</critical_rules>
</rules>

<workflow>
For ALL requests (regardless of connector type):
1. list_connectors -> find the right system by name/description
2. search_operations -> find operations with connector_id and query
3. call_operation -> execute with connector_id, operation_id, parameter_sets (ALWAYS a list)
4. If needed: reduce_data with SQL -> filter/sort/aggregate

When search_operations returns results, ALWAYS verify:
- Name/Description: Does it match what you're looking for?
- Parameters: What params does it need? Check required vs optional.
- Category: Is it the right type of operation?

For complex operations needing multiple parameters (like creating a VM):
1. Search for the operation to discover required parameters
2. Use Final Answer to ask the user for required information
3. Guide step-by-step -- don't ask for ALL parameters at once
4. Offer to list available resources (datastores, networks, etc.)
</workflow>

<output_formatting>
ALWAYS format Final Answer data as markdown tables.

| Data Type | Format |
|-----------|--------|
| Lists (namespaces, names, IDs) | Single column table |
| Entities with attributes | Multi-column table |
| Key-value pairs | Two column table |

For large results (50+ items): Show first 20 rows, then add a summary row with total count.
</output_formatting>
{tables_context}
{topology_context}
{history_context}
{request_guidance}
{scratchpad}
<current_goal>
User: {state.user_goal}
</current_goal>
"""


def _get_request_type_guidance(request_type: RequestType) -> str:
    """Get request-type specific guidance for the LLM, wrapped in XML tags."""
    guidance = {
        RequestType.DATA_REFORMAT: """
<request_guidance type="DATA_REFORMAT">
The user wants to reformat data that you already have.
DO NOT call external APIs again -- the data is already cached as SQL tables.

Use reduce_data with SQL to get the specific fields needed:
{{"sql": "SELECT field1, field2 FROM table_name WHERE ... ORDER BY ..."}}

NEVER generate fake/placeholder data. If you can't retrieve the cached data, tell the user.
</request_guidance>
""",
        RequestType.DATA_RECALL: """
<request_guidance type="DATA_RECALL">
The user is asking about specific cached data.
Check the cached entities -- if you have the data, use Final Answer.
Only call APIs if the data is not cached.
</request_guidance>
""",
        RequestType.ACTION: """
<request_guidance type="ACTION">
The user wants to perform an operation (create, update, delete, restart, etc.).

Parameter Collection Flow:
1. Use search_operations to find the relevant operation
2. Check the operation's parameters (required vs optional)
3. If parameters are missing, use Final Answer to ASK the user
   - Ask conversationally, one or two parameters at a time
   - Offer to list available resources (datastores, networks, clusters)
4. Once you have all required params, use call_operation to execute
5. Dangerous operations will require user approval (system handles this)
</request_guidance>
""",
        RequestType.KNOWLEDGE: """
<request_guidance type="KNOWLEDGE">
The user is asking a general question about concepts or best practices.
Use search_knowledge to find relevant documentation.
</request_guidance>
""",
        RequestType.DATA_QUERY: """
<request_guidance type="DATA_QUERY">
The user wants to retrieve data from a system.
1. Use search_operations to find the right operation
2. Use call_operation to fetch the data
3. Use the response to provide the Final Answer
</request_guidance>
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
    thought_match = re.search(
        r"Thought:\s*(.+?)(?=\n(?:Action:|Final Answer:)|$)", raw_output, re.DOTALL
    )
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
    _args: dict,
    state: MEHOGraphState,
) -> Any:
    """
    DEPRECATED: Legacy ReAct tool dispatch, replaced by meho_app.modules.agents ToolDispatchNode.

    Kept as a stub for backward compatibility. Returns None, which triggers the
    existing "validation failed" path (line ~812-816) that gracefully returns to
    reasoning with error feedback.
    """
    logger.warning(
        f"_create_typed_tool_node called for '{action}' -- this is the deprecated ReAct path. "
        "Use new agent architecture."
    )
    state.add_to_scratchpad(
        "Observation: Tool dispatch unavailable in legacy ReAct path. "
        "The system should be using the new agent architecture."
    )
    return None


# Type alias for all possible node returns (legacy -- tool nodes moved to meho_app.modules.agents)
ToolNodeUnion = Any


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
        return str(result.output)
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

    async def run(  # NOSONAR (cognitive complexity)
        self, ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> ToolNodeUnion | End[None] | ReasonNode:
        """Execute LLM reasoning step."""
        state = ctx.state
        deps = ctx.deps
        current_step = state.step_count

        logger.info(f"ReasonNode: Step {current_step + 1}/{deps.max_steps}")

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

        # Track timing for LLM call
        llm_start_time = time.perf_counter()

        # Call LLM for reasoning
        try:
            result = await deps.llm_agent.run(
                state.user_goal,
                instructions=system_prompt,
            )
            # Extract output from PydanticAI v1.63+ result
            raw_output = str(result.output)

            llm_duration_ms = (time.perf_counter() - llm_start_time) * 1000
            logger.debug(f"LLM output: {raw_output[:500]}...")

            # Claude ThinkingPart -> SSE "thought" event
            # When Claude uses adaptive thinking, ThinkingPart objects appear in the
            # response messages. Extract and emit them so the operator sees the agent's
            # internal reasoning in real time. Non-Anthropic providers or disabled
            # thinking produce no ThinkingPart objects -- this path is a no-op.
            # See: RESEARCH.md Pitfall 4, CONTEXT.md "stream everything" decision.
            try:
                from pydantic_ai.messages import ThinkingPart

                for msg in result.all_messages():
                    if hasattr(msg, "parts"):
                        for part in msg.parts:
                            if isinstance(part, ThinkingPart) and part.content:
                                emitter = deps.emitter
                                if emitter and hasattr(emitter, "thinking_part"):
                                    await emitter.thinking_part(part.content)
                                else:
                                    await deps.emit_progress("thought", {"content": part.content})
            except (ImportError, AttributeError):
                pass  # ThinkingPart not available or messages don't have parts -- skip

        except Exception as e:
            logger.error(f"LLM reasoning failed: {e}")
            state.error_message = str(e)
            await deps.emit_progress("error", {"message": str(e)})
            return End(None)

        # Parse the ReAct output
        parsed = _parse_react_output(raw_output)

        # Extract token usage if available from PydanticAI result
        token_usage = None
        if hasattr(result, "usage") and result.usage():
            usage_data = result.usage()
            token_usage = {
                "prompt": getattr(usage_data, "prompt_tokens", 0),
                "completion": getattr(usage_data, "completion_tokens", 0),
                "total": getattr(usage_data, "total_tokens", 0),
            }
        elif hasattr(result, "_usage"):
            usage = result._usage
            token_usage = {
                "prompt": getattr(usage, "request_tokens", 0),
                "completion": getattr(usage, "response_tokens", 0),
                "total": getattr(usage, "total_tokens", 0),
            }

        # Store trace data in state for OTEL span enrichment
        state.current_parsed = parsed
        state.last_system_prompt = system_prompt
        state.last_llm_response = raw_output
        state.last_llm_duration_ms = llm_duration_ms
        state.last_token_usage = token_usage

        # Emit thought to frontend (TASK-193: use detailed events for transcript)
        if parsed.thought:
            emitter = deps.emitter
            if (
                emitter
                and hasattr(emitter, "has_transcript_collector")
                and emitter.has_transcript_collector
            ):
                # Emit detailed thought with full LLM context for transcript persistence
                await emitter.thought_detailed(
                    summary=parsed.thought[:200] + "..."
                    if len(parsed.thought) > 200
                    else parsed.thought,
                    prompt=system_prompt,
                    response=raw_output,
                    parsed={
                        "thought": parsed.thought,
                        "action": parsed.action,
                        "action_input": parsed.action_input,
                        "final_answer": parsed.final_answer,
                    },
                    prompt_tokens=token_usage.get("prompt", 0) if token_usage else 0,
                    completion_tokens=token_usage.get("completion", 0) if token_usage else 0,
                    model=getattr(deps.llm_agent, "model", None) or get_config().llm_model,
                    duration_ms=llm_duration_ms,
                )
            else:
                # Fallback to simple event for backward compatibility
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
                state.final_answer = (
                    "I encountered an issue with my reasoning. Please try rephrasing your request."
                )
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
            args = json.loads(parsed.action_input) if parsed.action_input else {}
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Action Input as JSON: {e}")
            args = {"raw": parsed.action_input}

        # Log the action
        state.add_to_scratchpad(f"Action: {parsed.action}")
        state.add_to_scratchpad(f"Action Input: {parsed.action_input or '{}'}")

        # TASK-193: Use detailed action event for transcript persistence
        emitter = deps.emitter
        if (
            emitter
            and hasattr(emitter, "has_transcript_collector")
            and emitter.has_transcript_collector
        ):
            await emitter.action_detailed(
                tool=parsed.action,
                args=args,
                summary=f"Calling {parsed.action}",
            )
        else:
            await deps.emit_progress(
                "action",
                {
                    "tool": parsed.action,
                    "args": args,
                },
            )

        # TASK-92: Create typed tool node with validation
        typed_node = _create_typed_tool_node(parsed.action, args, state)

        if typed_node is None:
            # Validation failed - error already added to scratchpad
            state.step_count += 1
            # Return to reasoning with error feedback
            return ReasonNode()

        # Return the typed node (will be executed by pydantic-graph)
        return typed_node
