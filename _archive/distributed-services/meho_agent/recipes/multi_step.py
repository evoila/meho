"""
Multi-Step Recipe Support.

Extends the Recipe system to handle:
1. Chained operations (output from A → input to B)
2. Fan-out operations (for each X, call Y)
3. Aggregation across multiple API calls

This addresses scenarios like:
- "Take the cluster ID and get its hosts"
- "For each VM, get its IP address"
- "Get all pods, then get events for failed ones"

Design Principles:
- Steps are declarative (no arbitrary code)
- References use JSONPath-like syntax
- Iteration is explicit and bounded
- Results are automatically aggregated
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class StepAction(str, Enum):
    """Types of actions a recipe step can perform."""
    
    CALL_ENDPOINT = "call_endpoint"      # Make an API call
    EXTRACT = "extract"                   # Extract data from previous step
    REDUCE = "reduce"                     # Apply data reduction query
    AGGREGATE = "aggregate"               # Combine results from multiple steps
    CONDITIONAL = "conditional"           # Branch based on data


class RecipeStep(BaseModel):
    """
    A single step in a multi-step recipe.
    
    Steps can reference outputs from previous steps using
    template syntax: {{step_id.path.to.field}}
    
    For iteration, use {{item}} to reference the current item
    and {{index}} for the iteration index.
    """
    
    id: str = Field(description="Unique step identifier")
    action: StepAction = Field(description="What this step does")
    description: Optional[str] = Field(default=None)
    
    # Dependencies
    depends_on: list[str] = Field(
        default_factory=list,
        description="Steps that must complete before this one"
    )
    
    # For call_endpoint action
    endpoint_id: Optional[UUID] = Field(default=None)
    connector_id: Optional[UUID] = Field(default=None)
    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    body: Optional[dict[str, Any]] = Field(default=None)
    
    # For iteration (fan-out)
    iterate_over: Optional[str] = Field(
        default=None,
        description="JSONPath to array to iterate over, e.g., '{{get_vms.data.vms}}'"
    )
    max_iterations: int = Field(
        default=100,
        description="Safety limit for iterations"
    )
    
    # For extract action
    source_step: Optional[str] = Field(default=None)
    extraction_path: Optional[str] = Field(
        default=None,
        description="JSONPath to extract, e.g., 'data.items[*].id'"
    )
    
    # For reduce action
    reduce_query: Optional[dict[str, Any]] = Field(
        default=None,
        description="DataQuery specification for reduction"
    )
    
    # For aggregate action
    source_steps: list[str] = Field(default_factory=list)
    aggregation_type: Optional[str] = Field(
        default="merge",
        description="How to combine: merge, zip, flatten"
    )


class MultiStepRecipe(BaseModel):
    """
    A recipe with multiple steps that can chain and iterate.
    
    Example: Get VMs and their IP addresses
    
    ```yaml
    name: VMs with IP Addresses
    steps:
      - id: get_vms
        action: call_endpoint
        endpoint_id: "vm-list-endpoint"
        
      - id: get_vm_details
        action: call_endpoint
        endpoint_id: "vm-details-endpoint"
        iterate_over: "{{get_vms.data.vms}}"
        path_params:
          vm_id: "{{item.id}}"
        depends_on: [get_vms]
        
      - id: combine
        action: aggregate
        source_steps: [get_vms, get_vm_details]
        aggregation_type: zip
    ```
    """
    
    # Identity
    id: Optional[UUID] = None
    tenant_id: str
    name: str
    description: Optional[str] = None
    
    # Connector context
    connector_id: UUID
    
    # The steps
    steps: list[RecipeStep] = Field(
        description="Ordered list of steps to execute"
    )
    
    # Parameters that users can customize
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    
    def get_execution_order(self) -> list[str]:
        """
        Get steps in execution order (topological sort).
        
        Returns step IDs in order they should be executed,
        respecting dependencies.
        """
        # Build dependency graph
        in_degree: dict[str, int] = {step.id: len(step.depends_on) for step in self.steps}
        dependents: dict[str, list[str]] = {step.id: [] for step in self.steps}
        
        for step in self.steps:
            for dep in step.depends_on:
                if dep in dependents:
                    dependents[dep].append(step.id)
        
        # Kahn's algorithm
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        order = []
        
        while queue:
            current = queue.pop(0)
            order.append(current)
            
            for dependent in dependents.get(current, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        
        if len(order) != len(self.steps):
            raise ValueError("Circular dependency detected in recipe steps")
        
        return order


# =============================================================================
# Example Recipes
# =============================================================================

EXAMPLE_CHAIN_RECIPE = """
# Example: Get Cluster Details from ID

This recipe demonstrates chaining:
1. User provides cluster name
2. Step 1: Search for cluster by name → get ID
3. Step 2: Use ID to get full cluster details

```yaml
name: Get Cluster Details by Name
parameters:
  - name: cluster_name
    type: string
    
steps:
  - id: find_cluster
    action: call_endpoint
    endpoint_id: "search-clusters"
    query_params:
      name: "{{cluster_name}}"
      
  - id: extract_id
    action: extract
    source_step: find_cluster
    extraction_path: "data.clusters[0].id"
    depends_on: [find_cluster]
    
  - id: get_details
    action: call_endpoint
    endpoint_id: "get-cluster"
    path_params:
      cluster_id: "{{extract_id.result}}"
    depends_on: [extract_id]
```
"""

EXAMPLE_FANOUT_RECIPE = """
# Example: Get IP Addresses for All VMs

This recipe demonstrates fan-out (iteration):
1. Step 1: Get list of all VMs
2. Step 2: For each VM, get its network details
3. Step 3: Aggregate into a single result

```yaml
name: VMs with IP Addresses
steps:
  - id: list_vms
    action: call_endpoint
    endpoint_id: "list-vms"
    
  - id: get_networks
    action: call_endpoint
    endpoint_id: "get-vm-network"
    iterate_over: "{{list_vms.data.vms}}"
    path_params:
      vm_id: "{{item.id}}"
    max_iterations: 50
    depends_on: [list_vms]
    
  - id: combine
    action: aggregate
    source_steps: [list_vms, get_networks]
    aggregation_type: zip
    depends_on: [get_networks]
```

Result structure:
```json
[
  {"vm": {"id": "vm-1", "name": "web-01"}, "network": {"ip": "10.0.0.1"}},
  {"vm": {"id": "vm-2", "name": "web-02"}, "network": {"ip": "10.0.0.2"}},
  ...
]
```
"""

EXAMPLE_CONDITIONAL_RECIPE = """
# Example: Get Events for Failed Pods Only

This recipe demonstrates conditional iteration:
1. Step 1: Get all pods
2. Step 2: Filter to failed pods only
3. Step 3: For each failed pod, get its events

```yaml
name: Failed Pod Events
steps:
  - id: list_pods
    action: call_endpoint
    endpoint_id: "list-pods"
    
  - id: filter_failed
    action: reduce
    source_step: list_pods
    reduce_query:
      source_path: "data.items"
      filter:
        conditions:
          - field: "status.phase"
            operator: "="
            value: "Failed"
    depends_on: [list_pods]
    
  - id: get_events
    action: call_endpoint
    endpoint_id: "get-pod-events"
    iterate_over: "{{filter_failed.records}}"
    query_params:
      pod_name: "{{item.metadata.name}}"
      namespace: "{{item.metadata.namespace}}"
    max_iterations: 20
    depends_on: [filter_failed]
```
"""

