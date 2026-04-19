# RFC: Typed Tool Actions for Specialist Agent

**Status:** Draft -- seeking second opinion  
**Date:** 2026-03-25  
**Author:** MEHO development team  
**Affects:** `meho_app/modules/agents/specialist_agent/`

---

## 1. Problem Statement

The specialist agent's ReAct loop uses a `dict[str, Any]` for tool parameters, giving
the LLM zero schema guidance. This causes intermittent failures where the LLM omits
required fields.

### Current architecture

The specialist agent uses PydanticAI with `output_type=ReActStep` to get structured
LLM responses. There are **no PydanticAI tools registered** -- tool execution is
handled entirely by application code.

```python
# meho_app/modules/agents/specialist_agent/agent.py (line 565)
persistent_agent: Agent[None, ReActStep] = Agent(
    model_name,
    output_type=ReActStep,       # ONLY schema the LLM sees
    # tools= is NOT set         # No native tool calling
)
```

The `ReActStep` model that the LLM must produce:

```python
# meho_app/modules/agents/specialist_agent/models.py
class ReActStep(BaseModel):
    thought: str
    response_type: Literal["action", "final_answer"]
    action: str | None = None             # tool name as free string
    action_input: dict[str, Any] | None   # THE PROBLEM: untyped dict
    final_answer: str | None = None
    extend_budget: bool = False
```

### What goes wrong

The JSON schema sent to the LLM API for `action_input` is:

```json
{"action_input": {"type": "object"}}
```

No properties, no required fields, no type constraints. The LLM must read the
system prompt's natural-language `<tools>` section to figure out what keys to include:

```markdown
- **search_operations**: Find available API operations. Args: {"query": "what to search for"}
- **call_operation**: Execute an operation. Args: {"operation_id": "...", "parameter_sets": [{}]}
```

This is fragile. Observed failures:

| Tool | LLM sent | Missing field | Error |
|------|----------|---------------|-------|
| `search_operations` | `{}` (connector_id injected server-side, so `{"connector_id": "..."}`) | `query` | Pydantic ValidationError |
| `lookup_topology` | `{}` | `query` | Pydantic ValidationError |

After the validation error, the error message fed back to the LLM was a raw Pydantic
traceback. The LLM retried with the same empty input, hit loop detection, and was
forced to produce a final answer with zero actual data.

### The parameter blindness problem (call_operation)

There's a second, deeper issue beyond missing `query` on `search_operations`.

When `search_operations` runs, the handler at
`meho_app/modules/agents/shared/handlers/operation_handlers.py` (line 498-508)
returns **rich operation metadata** including `parameters`, `description`, and
`example`:

```python
formatted_results.append({
    "operation_id": op_id,
    "name": db_op.name,
    "description": db_op.description,
    "category": db_op.category,
    "parameters": db_op.parameters,     # FULL parameter definitions
    "example": db_op.example,           # Example usage
    "score": score,
})
```

However, the observation compressor at
`meho_app/modules/agents/specialist_agent/compressor.py` (line 121-147)
**strips all of this** and only shows the LLM a compact list:

```
Found 5 operations:
- list_gce_instances: List all GCE instances in the project (compute)
- get_gce_instance: Get details of a specific instance (compute)
```

The `parameters` and `example` fields are discarded. So when the LLM then needs
to call `call_operation` with the right `parameter_sets`, it is **guessing blindly**.
It has never seen the parameter schema for the operation it selected.

This means the untyped `dict[str, Any]` problem compounds:
1. `search_operations` needs `{"query": "..."}` -- LLM sometimes omits `query`
2. `call_operation` needs `{"operation_id": "...", "parameter_sets": [...]}` --
   LLM doesn't know what parameters the operation accepts because the compressor
   stripped them

The compressor was designed for token optimization (Phase 33), but it went too far
by removing the parameter metadata the LLM needs to construct valid `call_operation`
inputs.

### Why this matters

- Every specialist agent call that fails this way produces a **hallucinated or empty
  answer** -- the agent says "I found X" without having called any operations.
- It's non-deterministic -- works most of the time, fails unpredictably.
- The existing tool InputSchema Pydantic models (with proper types, required fields,
  min_length, etc.) are completely unused by the LLM. They only validate AFTER the
  LLM has already produced output, by which point the step is wasted.
- The observation compressor strips operation parameter metadata, leaving the LLM
  blind to how to construct `call_operation` inputs.

### Constraint: why we can't register operations as PydanticAI tools

MEHO's architecture is built around **meta-tools** that operate on dynamically
discovered API operations. A single MEHO deployment may have hundreds of connectors
with thousands of operations. These operations are stored in a database and discovered
at runtime via `search_operations`. They cannot be registered as individual PydanticAI
tools because:

1. The operation set is dynamic (changes when connectors are synced)
2. There could be thousands of operations (exceeds LLM context/tool limits)
3. Operations are scoped to connectors, and each specialist agent only sees one connector
4. The meta-tool pattern (`search_operations` -> `call_operation`) is fundamental to
   the architecture

The **7 fixed meta-tools** are the right abstraction:

| Meta-tool | Purpose |
|-----------|---------|
| `search_operations` | Find available API operations for a connector |
| `call_operation` | Execute a discovered operation by ID |
| `reduce_data` | Query cached operation results with SQL |
| `lookup_topology` | Look up entity in the topology graph |
| `search_knowledge` | Search the knowledge base |
| `store_memory` | Store operator memory |
| `forget_memory` | Search/delete memories |

---

## 2. Solution Options

### Option A: PydanticAI Native Tools (Full Refactor)

Register the 7 meta-tools as PydanticAI `@agent.tool` functions with typed parameters.
Use `output_type` for the final answer only.

```python
agent = Agent(model_name, output_type=FinalAnswer)

@agent.tool_plain
async def search_operations(query: str, limit: int = 10) -> str:
    """Search for API operations on the connector.

    Args:
        query: Search terms for operation names.
        limit: Maximum number of results (1-50).
    """
    # PydanticAI auto-generates JSON schema:
    # {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}
    ...

@agent.tool_plain
async def call_operation(operation_id: str, parameter_sets: list[dict] = [{}]) -> str:
    ...
```

**How it works:** PydanticAI sends each tool's typed schema to the LLM via the
model's native tool-calling API (Anthropic `tool_use` blocks). The LLM decides
whether to call a tool or produce the final answer. PydanticAI manages the
think-act-observe loop internally.

**Pros:**
- Provider-level schema enforcement (Anthropic constrained decoding)
- Industry-standard pattern -- this is what PydanticAI is designed for
- PydanticAI handles retries, validation, and the tool loop
- Cleanest JSON schema presentation to the LLM

**Cons:**
- Requires rewriting ~600 lines of battle-tested ReAct loop code
- All custom logic must move INTO tool functions or hook points:
  - Approval flow (trust classification, pending approval creation, SSE pause/resume)
  - SSE event streaming (thought events, action events, observation events)
  - Transcript collection (DetailedEvent recording per step)
  - Loop detection (duplicate action tracking)
  - Step budget management (8 steps, extension to 12)
  - Observation compression
  - Topology pre-population
  - Memory context injection
- PydanticAI's internal loop is less observable -- harder to emit per-step SSE events
- Risk of regressions in approval flow and event streaming
- May require PydanticAI hooks/callbacks that may not exist for all our needs

**Effort:** Large (estimated 2-3 days)  
**Risk:** High -- approval flow and SSE streaming are critical user-facing features

---

### Option B: Discriminated Union on `action_input` (Targeted Refactor)

Replace `action_input: dict[str, Any]` with a Pydantic discriminated union of typed
action models. Keep the existing custom ReAct loop unchanged.

```python
class SearchOperationsAction(BaseModel):
    tool: Literal["search_operations"] = "search_operations"
    query: str = Field(min_length=1, description="Search terms for operation names")
    limit: int = Field(default=10, ge=1, le=50, description="Max results")

class CallOperationAction(BaseModel):
    tool: Literal["call_operation"] = "call_operation"
    operation_id: str = Field(description="Operation ID from search_operations results")
    parameter_sets: list[dict[str, Any]] = Field(default_factory=lambda: [{}])

class ReduceDataAction(BaseModel):
    tool: Literal["reduce_data"] = "reduce_data"
    sql: str = Field(min_length=1, description="SQL query on cached table data")

class LookupTopologyAction(BaseModel):
    tool: Literal["lookup_topology"] = "lookup_topology"
    query: str = Field(description="Entity name to look up")
    traverse_depth: int = Field(default=10, ge=1, le=20)
    cross_connectors: bool = Field(default=True)

class SearchKnowledgeAction(BaseModel):
    tool: Literal["search_knowledge"] = "search_knowledge"
    query: str = Field(min_length=1, description="Search terms")
    limit: int = Field(default=5, ge=1, le=20)

class StoreMemoryAction(BaseModel):
    tool: Literal["store_memory"] = "store_memory"
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    memory_type: str = Field(default="config")
    tags: list[str] = Field(default_factory=list)

class ForgetMemoryAction(BaseModel):
    tool: Literal["forget_memory"] = "forget_memory"
    action: Literal["search", "delete"]
    query: str | None = None
    memory_id: str | None = None

ToolAction = Annotated[
    SearchOperationsAction | CallOperationAction | ReduceDataAction
    | LookupTopologyAction | SearchKnowledgeAction
    | StoreMemoryAction | ForgetMemoryAction,
    Field(discriminator="tool"),
]

class ReActStep(BaseModel):
    thought: str = Field(description="Step-by-step reasoning")
    response_type: Literal["action", "final_answer"]
    action_input: ToolAction | None = None    # TYPED discriminated union
    final_answer: str | None = None
    extend_budget: bool = False
```

**How it works:** PydanticAI still uses `output_type=ReActStep` (structured output,
not native tool calling). But the JSON schema for `action_input` now contains a
`oneOf` with each tool variant's full typed schema:

```json
{
  "action_input": {
    "oneOf": [
      {
        "properties": {
          "tool": {"const": "search_operations"},
          "query": {"type": "string", "minLength": 1},
          "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50}
        },
        "required": ["tool", "query"]
      },
      {
        "properties": {
          "tool": {"const": "call_operation"},
          "operation_id": {"type": "string"},
          "parameter_sets": {"type": "array", "items": {"type": "object"}}
        },
        "required": ["tool", "operation_id"]
      },
      ...
    ]
  }
}
```

The LLM can no longer produce `{}` for `search_operations` -- the schema requires
`query`. The `tool` field acts as the discriminator so Pydantic knows which variant
to validate against.

In the agent code, the dispatch changes minimally:

```python
# Before:
action_input = react_step.action_input or {}
tool_name = react_step.action

# After:
typed_action = react_step.action_input
tool_name = typed_action.tool
action_input = typed_action.model_dump(exclude={"tool"})
```

**Pros:**
- Pydantic-level schema enforcement via structured output JSON schema
- Minimal code change -- only models.py and ~20 lines in agent.py
- All custom logic preserved: approval, SSE, transcripts, loop detection, budgets
- The `action` field on ReActStep becomes redundant (derived from `action_input.tool`)
- Existing tool InputSchema models served as the design reference
- `connector_id` excluded from action models (server-injected, not LLM-provided)
- Easy to add new meta-tools: just add a new action model to the union

**Cons:**
- Schema enforcement is at the PydanticAI/Pydantic level, not at the LLM provider's
  constrained decoding level (though Anthropic's API does honor JSON schema in
  structured output for `tool_use` responses)
- `oneOf` in JSON schema can be verbose -- adds tokens to the system message
- Still a custom ReAct loop (not the PydanticAI-native pattern)
- `call_operation.parameter_sets` remains `list[dict[str, Any]]` since operation
  parameters are dynamic -- but this is unavoidable regardless of approach

**Effort:** Small (estimated 2-4 hours)  
**Risk:** Low -- existing loop logic is untouched

---

### Option C: Keep `dict[str, Any]`, Improve Error Recovery (Band-Aid)

Keep the untyped dict but add better pre-flight validation and error messages so the
LLM can self-correct.

```python
# Pre-flight: catch missing 'query' before tool execution
if react_step.action in ("search_operations", "lookup_topology") and not action_input.get("query"):
    observation = f"{react_step.action} requires a 'query' parameter..."
    # Feed back to LLM for retry
```

**Pros:**
- Trivial change
- No schema changes

**Cons:**
- Does not fix the root cause -- the LLM still has no schema guidance
- Wastes a step on every failure (retry burns budget)
- Must anticipate every possible missing field manually
- Different models may fail differently -- whack-a-mole

**Effort:** Minimal (already partially implemented)  
**Risk:** Medium -- doesn't solve the fundamental problem, just masks it

---

## 3. Comparison Matrix

| Criterion | Option A: Native Tools | Option B: Discriminated Union | Option C: Band-Aid |
|-----------|----------------------|------------------------------|-------------------|
| Schema enforcement level | Provider (best) | Pydantic/JSON schema (good) | Prompt only (poor) |
| LLM sees typed params | Yes (native tool_use) | Yes (oneOf in JSON schema) | No (reads prompt) |
| Code change size | ~600 lines rewrite | ~20 lines + new models | ~15 lines |
| Preserves existing loop | No | Yes | Yes |
| Preserves approval flow | Must re-implement | Yes | Yes |
| Preserves SSE events | Must re-implement | Yes | Yes |
| Preserves transcripts | Must re-implement | Yes | Yes |
| Preserves loop detection | Must re-implement | Yes | Yes |
| Preserves step budgets | Must re-implement | Yes | Yes |
| Risk of regression | High | Low | Medium |
| Estimated effort | 2-3 days | 2-4 hours | 30 minutes |
| Future-proof | Most | Good | Poor |
| Solves root cause | Yes | Yes | No |

---

## 4. Recommendation

**Option B (Discriminated Union)** is the recommended approach.

**Rationale:**
- It solves the root cause (untyped dict -> typed schema) without touching the
  proven ReAct loop logic.
- The 7 meta-tools are a fixed, small set -- the union is manageable.
- All existing Pydantic InputSchema models provide the field definitions.
- `connector_id` is excluded from action models since it's server-injected.
- Adding a new meta-tool in the future means adding one model to the union.
- Option A can be pursued later as a separate improvement if the native PydanticAI
  pattern proves beneficial, without being blocked by this bug.

**Deferred:** Option A (native PydanticAI tools) is architecturally cleaner but is a
large refactor with high regression risk. It should be considered as a future milestone
when the approval flow and SSE streaming can be designed for PydanticAI's internal
tool loop from the start, rather than retrofitted.

---

## 5. Implementation Sketch (Option B)

### Files to change

1. **`meho_app/modules/agents/specialist_agent/models.py`**
   - Add 7 action models (one per meta-tool)
   - Define `ToolAction` discriminated union
   - Update `ReActStep.action_input` type from `dict[str, Any]` to `ToolAction | None`
   - Remove `action: str | None` field (redundant -- tool name is in `action_input.tool`)
     OR keep it for backward compatibility but derive it from `action_input.tool`

2. **`meho_app/modules/agents/specialist_agent/agent.py`**
   - Change `action_input = react_step.action_input or {}` to extract from typed model
   - Derive tool name from `react_step.action_input.tool`
   - Convert to dict via `model_dump(exclude={"tool"})` for handler compatibility
   - Remove pre-flight checks (schema now prevents missing fields)
   - Remove `ValidationError` catch (validation happens at output parsing)
   - `connector_id` injection stays unchanged (mutate the dict after model_dump)

3. **`meho_app/modules/agents/specialist_agent/prompts/system.md`**
   - Simplify `<tools>` section -- schema handles parameter definitions
   - Keep semantic descriptions of WHEN to use each tool

4. **`meho_app/modules/agents/specialist_agent/compressor.py`**
   - Update `_compress_search_operations()` to **preserve parameter metadata**
   - The compressor currently strips `parameters` and `example` from search results
   - The LLM needs this information to construct valid `call_operation` inputs
   - Add a compact parameter summary per operation, e.g.:
     ```
     Found 3 operations:
     - list_pods: List pods in a namespace (core)
       Params: namespace (str, required), label_selector (str, optional)
     - get_pod: Get pod details (core)
       Params: namespace (str, required), name (str, required)
     ```
   - This gives the LLM enough information to construct `parameter_sets`
     without including the full verbose schema (balancing tokens vs usability)

5. **Tests** -- Update any tests constructing `ReActStep` with raw dicts

### What stays unchanged

- All tool classes in `meho_app/modules/agents/react_agent/tools/`
- `TOOL_REGISTRY` and `BaseTool`
- Approval flow, SSE events, transcript collection
- Loop detection, step budgets, observation compression
- Orchestrator agent (only specialist is affected)
- System prompt structure (only the Args hints change)
