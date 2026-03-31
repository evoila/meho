# Adding a New Connector

> Last verified: v2.3

This walkthrough guides you through creating a new MEHO connector from scratch, using the GCP connector as a reference implementation. By the end, your connector will be discoverable by the agent and integrated into the topology graph.

For an overview of how connectors fit into the system architecture, see the [System Architecture Overview](overview.md).

## Prerequisites

- Understanding of the [BaseConnector interface](overview.md#baseconnector-interface) (6 abstract methods)
- Familiarity with your target system's API or SDK
- Python 3.13+
- A running MEHO development environment (`docker compose -f docker-compose.dev.yml up`)

## Step 1: Create the Directory Structure

Create a new directory under `meho_app/modules/connectors/` matching this layout:

```
meho_app/modules/connectors/your_connector/
  __init__.py
  connector.py          # Main connector class
  handlers/             # Handler mixins (one per service area)
    __init__.py
    example_handlers.py
  operations/           # OperationDefinition lists
    __init__.py          # Aggregates all operation lists
    example.py           # One file per category
  types.py              # TypeDefinition list for topology
  serializers.py        # Raw API response -> clean dict
  sync.py               # Topology sync: discover entities, create edges
  helpers.py            # Shared utilities
```

**Reference:** See the GCP connector at [`meho_app/modules/connectors/gcp/`](../../meho_app/modules/connectors/gcp/) for a complete example.

## Step 2: Define Operations

Operations tell the agent what your connector can do. Each operation is an `OperationDefinition` with a unique ID, human-readable name, description, category, parameters, and trust level.

Create a file like `operations/compute.py` with your operation definitions. Here is a real example from the GCP connector (`meho_app/modules/connectors/gcp/operations/compute.py`):

```python
from meho_app.modules.connectors.base import OperationDefinition

COMPUTE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_instances",
        name="List Compute Engine Instances",
        description="List all VM instances in the project. Returns instance details "
                    "including name, zone, machine type, status, IP addresses, and labels.",
        category="compute",
        parameters=[
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone to list instances from (default: all zones)",
            },
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression (e.g., 'status=RUNNING')",
            },
        ],
        example="list_instances(zone='us-central1-a')",
        response_entity_type="Instance",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
]
```

**Key fields:**

| Field | Purpose |
|-------|---------|
| `operation_id` | Unique ID. Must match the handler method name: `_handle_{operation_id}` |
| `name` | Human-readable name shown in the agent's tool descriptions |
| `description` | Detailed description -- the agent uses this to decide when to call the operation |
| `category` | Logical grouping (e.g., `compute`, `network`, `monitoring`) |
| `parameters` | List of parameter dicts with `name`, `type`, `required`, `description` |
| `response_entity_type` | The topology entity type returned (e.g., `"Instance"`) |

Then aggregate all operation lists in `operations/__init__.py`. From the GCP connector (`meho_app/modules/connectors/gcp/operations/__init__.py`):

```python
from meho_app.modules.connectors.gcp.operations.compute import COMPUTE_OPERATIONS
from meho_app.modules.connectors.gcp.operations.gke import GKE_OPERATIONS
from meho_app.modules.connectors.gcp.operations.network import NETWORK_OPERATIONS
from meho_app.modules.connectors.gcp.operations.monitoring import MONITORING_OPERATIONS
from meho_app.modules.connectors.gcp.operations.cloud_build import CLOUD_BUILD_OPERATIONS
from meho_app.modules.connectors.gcp.operations.artifact_registry import ARTIFACT_REGISTRY_OPERATIONS

GCP_OPERATIONS = (
    COMPUTE_OPERATIONS
    + GKE_OPERATIONS
    + NETWORK_OPERATIONS
    + MONITORING_OPERATIONS
    + CLOUD_BUILD_OPERATIONS
    + ARTIFACT_REGISTRY_OPERATIONS
)
```

## Step 3: Implement the Connector Class

Your connector class inherits from `BaseConnector` and mixes in handler classes. From the GCP connector (`meho_app/modules/connectors/gcp/connector.py`):

```python
from meho_app.modules.connectors.base import (
    BaseConnector, OperationDefinition, OperationResult, TypeDefinition,
)
from meho_app.modules.connectors.gcp.handlers import (
    ComputeHandlerMixin, GKEHandlerMixin, NetworkHandlerMixin,
    MonitoringHandlerMixin, CloudBuildHandlerMixin, ArtifactRegistryHandlerMixin,
)
from meho_app.modules.connectors.gcp.operations import GCP_OPERATIONS
from meho_app.modules.connectors.gcp.types import GCP_TYPES


class GCPConnector(
    BaseConnector,
    ComputeHandlerMixin,
    GKEHandlerMixin,
    NetworkHandlerMixin,
    MonitoringHandlerMixin,
    CloudBuildHandlerMixin,
    ArtifactRegistryHandlerMixin,
):
    def __init__(self, connector_id, config, credentials):
        super().__init__(connector_id, config, credentials)
        # Initialize SDK clients to None (created in connect())
        self._compute_client = None
        self._project_id = config.get("project_id")

    async def connect(self) -> bool:
        """Authenticate and initialize API clients."""
        self._credentials = self._get_credentials()
        self._initialize_clients()
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        """Clean up client references."""
        self._compute_client = None
        self._is_connected = False

    async def test_connection(self) -> bool:
        """Verify the connection is alive with a lightweight API call."""
        # Make a minimal API call to verify credentials work
        ...

    async def execute(self, operation_id, parameters) -> OperationResult:
        """Dispatch to the handler method matching the operation_id."""
        handler = getattr(self, f"_handle_{operation_id}", None)
        if handler is None:
            return OperationResult(success=False, error=f"Unknown operation: {operation_id}")
        result = await handler(parameters)
        return OperationResult(success=True, data=result, operation_id=operation_id)

    def get_operations(self) -> list[OperationDefinition]:
        return GCP_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        return GCP_TYPES
```

**Important:** The `execute()` method uses a naming convention to dispatch: `operation_id="list_instances"` calls `self._handle_list_instances()`. This convention connects your operation definitions to your handler implementations with zero configuration.

## Step 4: Write Handler Mixins

Handler mixins contain the actual implementation logic for each operation. One mixin per service area keeps the code organized. From the GCP connector (`meho_app/modules/connectors/gcp/handlers/compute_handlers.py`):

```python
import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.modules.connectors.gcp.serializers import serialize_instance

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector


class ComputeHandlerMixin:
    """Mixin providing Compute Engine operation handlers."""

    async def _handle_list_instances(
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List Compute Engine instances."""
        from google.cloud import compute_v1

        zone = params.get("zone")
        request = compute_v1.ListInstancesRequest(
            project=self.project_id, zone=zone,
        )
        response = await asyncio.to_thread(
            lambda: list(self._instances_client.list(request=request))
        )
        return [serialize_instance(inst) for inst in response]
```

**Pattern highlights:**

- Each handler method is named `_handle_{operation_id}` matching the operation definition.
- The `self` parameter is typed as the concrete connector class for IDE support.
- Synchronous SDK calls are wrapped with `asyncio.to_thread()` to avoid blocking the event loop.
- Raw API responses are passed through serializers before returning.

## Step 5: Add Serializers

Serializers transform raw API responses into clean, consistent dictionaries. This matters because the agent reasons about the output -- consistent field names and structures produce better results.

From the GCP connector (`meho_app/modules/connectors/gcp/serializers.py`):

```python
from typing import Any

def serialize_instance(instance: Any) -> dict[str, Any]:
    """Serialize a Compute Engine Instance to a dictionary."""
    return {
        "id": str(instance.id),
        "name": instance.name,
        "zone": extract_zone_from_url(instance.zone or ""),
        "machine_type": parse_machine_type(instance.machine_type or ""),
        "status": instance.status,
        "internal_ip": ...,
        "external_ip": ...,
        "labels": parse_labels(instance.labels),
    }
```

**Guidelines:**

- Extract only the fields the agent needs -- don't dump entire SDK objects.
- Use consistent naming (`id`, `name`, `status`) across all entity types.
- Parse URLs and nested objects into simple values (e.g., extract zone name from a full URL).

## Step 6: Define Topology Types

Topology types describe the entities your connector discovers. These are used by the topology graph to visualize relationships across systems.

From the GCP connector (`meho_app/modules/connectors/gcp/types.py`):

```python
from meho_app.modules.connectors.base import TypeDefinition

GCP_TYPES = [
    TypeDefinition(
        type_name="Instance",
        description="A GCP Compute Engine virtual machine instance. Contains compute "
                    "resources with CPU, memory, and storage. Runs in a specific zone.",
        category="compute",
        properties=[
            {"name": "id", "type": "string", "description": "Unique instance ID"},
            {"name": "name", "type": "string", "description": "Instance name"},
            {"name": "zone", "type": "string", "description": "Zone where instance runs"},
            {"name": "machine_type", "type": "string", "description": "Machine type"},
            {"name": "status", "type": "string", "description": "Instance status"},
            {"name": "internal_ip", "type": "string", "description": "Internal IP address"},
        ],
    ),
    TypeDefinition(
        type_name="GKECluster",
        description="A Google Kubernetes Engine cluster.",
        category="containers",
        properties=[
            {"name": "name", "type": "string", "description": "Cluster name"},
            {"name": "location", "type": "string", "description": "Zone or region"},
            {"name": "status", "type": "string", "description": "Cluster status"},
            {"name": "current_node_count", "type": "integer", "description": "Total nodes"},
        ],
    ),
]
```

Each `TypeDefinition` has a `type_name` (the entity label in the topology graph), a human-readable `description`, a `category` for grouping, and a `properties` list describing the key attributes.

## Step 7: Implement Topology Sync

The `sync.py` module discovers entities from your connector and registers them in the topology graph. It also creates edges to represent relationships (e.g., a VM `RUNS_ON` a host, a pod `CONTAINS` containers).

From the GCP connector (`meho_app/modules/connectors/gcp/sync.py`), the sync function:

1. Queries the connector for current entities (e.g., all instances, clusters, networks).
2. Creates or updates topology nodes with key properties.
3. Creates edges between related entities (e.g., Instance -> VPCNetwork).
4. Creates knowledge chunks for hybrid search so the agent can find operations by keyword.

The sync runs automatically when operations are registered and whenever the operations version changes.

## Step 8: Register the Connector

Add your connector to the connector pool in [`meho_app/modules/connectors/pool.py`](../../meho_app/modules/connectors/pool.py). This is the **only** place in the codebase that switches on `connector_type`:

```python
# In meho_app/modules/connectors/pool.py

async def get_connector_instance(connector_type, connector_id, config, credentials):
    ...
    if connector_type == "your_connector":
        from meho_app.modules.connectors.your_connector import YourConnector
        return YourConnector(connector_id, config, credentials)
    ...
```

Use lazy imports to avoid loading your connector's SDK when it is not needed.

## Step 9: Write Documentation

Add a connector guide page at `docs/connectors/your-connector.md` following the established template used by all existing guides. The template structure:

1. **Title and description** -- what the connector does and its scope
2. **Authentication** -- table of credential fields with setup steps
3. **Operations** -- table per category with operation ID, trust level, and description
4. **Example Queries** -- natural language questions the agent can answer
5. **Topology** -- entity types discovered and cross-system relationships
6. **Troubleshooting** -- common issues with symptoms, causes, and fixes

See any existing guide in `docs/connectors/` (e.g., `gcp.md`, `kubernetes.md`) as a reference.

Add your new page to the `nav` section in `mkdocs.yml`.

## Checklist

Before submitting your connector, verify:

- [ ] Directory structure created under `meho_app/modules/connectors/your_connector/`
- [ ] Operations defined with correct trust levels (READ for queries, WRITE for mutations, DESTRUCTIVE for irreversible actions)
- [ ] Connector class implements all 6 `BaseConnector` methods (`connect`, `disconnect`, `test_connection`, `execute`, `get_operations`, `get_types`)
- [ ] Handler mixins cover all defined operations (each `operation_id` has a matching `_handle_{id}` method)
- [ ] Serializers produce clean dictionaries with consistent field names
- [ ] Topology types and sync defined for entity discovery
- [ ] Connector registered in `pool.py` with lazy import
- [ ] Documentation page written in `docs/connectors/` following the existing template
- [ ] Page added to `mkdocs.yml` navigation
