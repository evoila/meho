"""
VMware Operation Definitions - Split by Category

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.openapi.connectors.base import OperationDefinition


# MONITORING OPERATIONS

MONITORING_OPERATIONS = [
    OperationDefinition(
        operation_id="list_alarms",
        name="List Triggered Alarms",
        description="Get currently triggered alarms in vCenter.",
        category="monitoring",
        parameters=[],
        example="list_alarms()",
    ),
    OperationDefinition(
        operation_id="get_vm_performance",
        name="Get VM Performance Metrics",
        description="Get VM performance metrics from quickStats: CPU usage (MHz), memory usage (MB), active memory, ballooned memory, consumed overhead, swapped memory, host memory usage, and uptime. NOTE: Disk I/O and network throughput require PerformanceManager API (not yet implemented).",
        category="monitoring",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_vm_performance(vm_name='web-01', interval='past_day')",
    ),
    OperationDefinition(
        operation_id="get_host_performance",
        name="Get Host Performance Metrics",
        description="Get host performance metrics from quickStats: CPU usage (MHz), memory usage (MB), distributed CPU/memory fairness, total capacity (CPU MHz, memory MB), and uptime. NOTE: Disk latency and network throughput require PerformanceManager API (not yet implemented).",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_host_performance(host_name='esxi-01.example.com', interval='realtime')",
    ),
    OperationDefinition(
        operation_id="get_events",
        name="Get Recent Events",
        description="Get recent events from vCenter event log.",
        category="monitoring",
        parameters=[
            {"name": "limit", "type": "integer", "required": False, "description": "Max events to return (default: 50)"},
            {"name": "entity_type", "type": "string", "required": False, "description": "Filter by entity type: vm, host, cluster"},
        ],
        example="get_events(limit=100, entity_type='vm')",
    ),
    OperationDefinition(
        operation_id="acknowledge_alarm",
        name="Acknowledge Alarm",
        description="Acknowledge a triggered alarm on an entity.",
        category="monitoring",
        parameters=[
            {"name": "entity_name", "type": "string", "required": True, "description": "Name of entity (VM, host, etc.)"},
            {"name": "entity_type", "type": "string", "required": True, "description": "Type: vm, host, cluster, datastore"},
        ],
        example="acknowledge_alarm(entity_name='web-01', entity_type='vm')",
    ),
    OperationDefinition(
        operation_id="retrieve_hardware_uptime",
        name="Get Host Uptime",
        description="Get hardware uptime for an ESXi host in seconds. pyvmomi: RetrieveHardwareUptime()",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="retrieve_hardware_uptime(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="acquire_cim_ticket",
        name="Acquire CIM Services Ticket",
        description="Get a ticket for CIM (hardware monitoring) access. pyvmomi: AcquireCimServicesTicket()",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="acquire_cim_ticket(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_cluster_resource_usage",
        name="Get Cluster Resource Usage",
        description="Get current resource usage summary for a cluster. pyvmomi: GetResourceUsage()",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="get_cluster_resource_usage(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="get_cluster_ha_status",
        name="Get HA Runtime Info",
        description="Get High Availability runtime information for a cluster. pyvmomi: RetrieveDasAdvancedRuntimeInfo()",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
        ],
        example="get_cluster_ha_status(cluster_name='Production')",
    ),
    OperationDefinition(
        operation_id="query_memory_overhead",
        name="Query Memory Overhead",
        description="Query memory overhead for a VM on a host. pyvmomi: QueryMemoryOverheadEx(vmConfigInfo)",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of VM to check overhead for"},
        ],
        example="query_memory_overhead(host_name='esxi-01.example.com', vm_name='web-01')",
    ),
    OperationDefinition(
        operation_id="query_tpm_attestation",
        name="Query TPM Attestation",
        description="Query TPM attestation report for host security. pyvmomi: QueryTpmAttestationReport()",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="query_tpm_attestation(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_cluster_performance",
        name="Get Cluster Performance",
        description="Get aggregated performance metrics for a cluster.",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_cluster_performance(cluster_name='Production', interval='past_day')",
    ),
    OperationDefinition(
        operation_id="get_datastore_performance",
        name="Get Datastore Performance",
        description="Get datastore capacity metrics: total capacity (GB), free space (GB), uncommitted space (GB), accessible status, and maintenance mode. NOTE: IOPS, latency, and throughput require PerformanceManager API (not yet implemented).",
        category="monitoring",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
            {"name": "interval", "type": "string", "required": False, "description": "Interval: realtime, past_day, past_week"},
        ],
        example="get_datastore_performance(datastore_name='datastore1', interval='realtime')",
    ),
    
    # =========================================================================
    # DETAILED PERFORMANCE (PerformanceManager API)
    # These provide disk I/O, network throughput, and historical metrics
    # =========================================================================
    
    OperationDefinition(
        operation_id="get_detailed_vm_performance",
        name="Get Detailed VM Performance (Disk/Network/Historical)",
        description="""Get comprehensive VM performance metrics, statistics, and counters using vSphere PerformanceManager API.

KEYWORDS: disk performance, network performance, disk metrics, network metrics, disk statistics, 
network statistics, disk io, network io, disk latency, network throughput, performance counters,
vm statistics, vm metrics, storage performance, bandwidth, iops

INCLUDES METRICS NOT AVAILABLE IN QUICKSTATS:
- Disk performance: Read/write rates (KB/s), IOPS, disk latency (ms), storage throughput
- Network performance: Receive/transmit rates (KB/s), packet counts, dropped packets, bandwidth
- CPU: Ready time (contention indicator), wait time, co-stop time
- Memory: Balloon driver activity, swap activity, granted vs consumed

TIME INTERVALS:
- realtime: 20-second samples, last ~1 hour (best for live troubleshooting)
- 5min: 5-minute rollups, last 24 hours
- 1hour: Hourly rollups, last 7 days
- 6hour/12hour/24hour: Aggregated views
- 7day: Weekly summary

DIAGNOSTIC HIGHLIGHTS:
Returns pre-analyzed indicators like "High disk latency (45ms)" or "CPU ready time elevated (8%)" 
so you don't need to interpret raw numbers.

USE THIS OPERATION WHEN:
- Diagnosing slow VM performance
- Investigating disk I/O issues or disk performance problems
- Troubleshooting network problems or network performance issues
- Analyzing historical performance trends
- Identifying resource contention
- Getting disk statistics or network statistics for a VM""",
        category="monitoring",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": "Name of the VM"},
            {"name": "interval", "type": "string", "required": False, "description": "Time interval: realtime, 5min, 1hour, 6hour, 12hour, 24hour, 7day (default: realtime)"},
            {"name": "metrics", "type": "array", "required": False, "description": "Metric groups to include: cpu, memory, disk, network (default: all)"},
        ],
        example="get_detailed_vm_performance(vm_name='web-01', interval='1hour', metrics=['disk', 'network'])",
    ),
    OperationDefinition(
        operation_id="get_detailed_host_performance",
        name="Get Detailed Host Performance (Disk/Network/Historical)",
        description="""Get comprehensive ESXi host performance metrics using vSphere PerformanceManager API.

INCLUDES METRICS NOT AVAILABLE IN QUICKSTATS:
- Disk: Read/write rates (KB/s), IOPS, latency (ms) across all storage
- Network: Throughput per physical NIC, packet drops
- CPU: Per-core utilization, ready time aggregates
- Memory: Detailed memory state breakdown

TIME INTERVALS:
- realtime: 20-second samples, last ~1 hour
- 5min: 5-minute rollups, last 24 hours
- 1hour: Hourly rollups, last 7 days
- 24hour: Daily rollups, last 30 days

USE THIS OPERATION WHEN:
- Diagnosing host-level performance issues
- Identifying storage bottlenecks affecting multiple VMs
- Investigating network infrastructure problems
- Capacity planning and trending analysis""",
        category="monitoring",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "interval", "type": "string", "required": False, "description": "Time interval: realtime, 5min, 1hour, 6hour, 12hour, 24hour, 7day (default: realtime)"},
            {"name": "metrics", "type": "array", "required": False, "description": "Metric groups to include: cpu, memory, disk, network (default: all)"},
        ],
        example="get_detailed_host_performance(host_name='esxi-01.example.com', interval='5min')",
    ),
    OperationDefinition(
        operation_id="get_detailed_datastore_performance",
        name="Get Detailed Datastore Performance (IOPS/Latency)",
        description="""Get comprehensive datastore performance metrics using vSphere PerformanceManager API.

INCLUDES METRICS NOT AVAILABLE IN BASIC DATASTORE INFO:
- Read/write throughput (KB/s)
- Read/write IOPS
- Read/write latency (ms)
- Capacity and utilization

TIME INTERVALS:
- realtime: 20-second samples, last ~1 hour
- 5min: 5-minute rollups, last 24 hours  
- 1hour: Hourly rollups, last 7 days

USE THIS OPERATION WHEN:
- Diagnosing storage performance issues
- Identifying datastores with high latency
- Comparing datastore performance
- Storage capacity planning""",
        category="monitoring",
        parameters=[
            {"name": "datastore_name", "type": "string", "required": True, "description": "Name of datastore"},
            {"name": "interval", "type": "string", "required": False, "description": "Time interval: realtime, 5min, 1hour, 6hour, 12hour, 24hour, 7day (default: realtime)"},
        ],
        example="get_detailed_datastore_performance(datastore_name='SAN-LUN01', interval='realtime')",
    ),
    OperationDefinition(
        operation_id="get_cluster_detailed_performance",
        name="Get Cluster Detailed Performance (Aggregated)",
        description="""Get aggregated performance metrics for all hosts in a cluster.

Queries each host in the cluster and provides:
- Per-host detailed metrics (disk I/O, network, CPU, memory)
- Aggregated totals across the cluster
- Issue/warning counts per host
- Cluster-wide resource summary

USE THIS OPERATION WHEN:
- Diagnosing cluster-wide performance issues
- Identifying which hosts have problems
- Capacity planning for the cluster
- Comparing host performance within a cluster""",
        category="monitoring",
        parameters=[
            {"name": "cluster_name", "type": "string", "required": True, "description": "Name of cluster"},
            {"name": "interval", "type": "string", "required": False, "description": "Time interval: realtime, 5min, 1hour (default: realtime)"},
        ],
        example="get_cluster_detailed_performance(cluster_name='Production', interval='5min')",
    ),
    OperationDefinition(
        operation_id="list_available_metrics",
        name="List Available Performance Metrics",
        description="""Discover what performance metrics are available for a specific entity.

Returns all available metrics organized by category (cpu, memory, disk, network).
Useful for understanding what data can be collected from a specific vCenter/entity.

Different vCenter versions and configurations may have different metrics available.""",
        category="monitoring",
        parameters=[
            {"name": "entity_type", "type": "string", "required": True, "description": "Type of entity: vm, host, datastore"},
            {"name": "entity_name", "type": "string", "required": True, "description": "Name of the entity"},
        ],
        example="list_available_metrics(entity_type='vm', entity_name='web-01')",
    ),
]
