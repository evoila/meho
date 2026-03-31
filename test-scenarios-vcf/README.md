# VMware vSphere Test Scenarios for MEHO

This directory contains test scenarios for the MEHO VMware vSphere connector.
These scenarios represent typical operator tasks that MEHO handles via natural language.

## Architecture

MEHO uses a **native pyvmomi connector** for VMware vSphere integration, providing:
- Direct vCenter API access via pyvmomi (not REST API)
- 179 pre-registered operations across 6 categories
- Full PerformanceManager API support for detailed metrics
- Guest operations via VMware Tools

## Connector Capabilities

| Category | Operations | Description |
|----------|------------|-------------|
| **Compute** | 91 | VM lifecycle, snapshots, clusters, hosts, resource pools, guest ops |
| **Storage** | 28 | Datastores, VM disks, storage vMotion, Storage DRS |
| **Networking** | 17 | Networks, DVS, port groups, VM NICs, firewall |
| **Monitoring** | 18 | Alarms, performance metrics (quickStats + PerformanceManager), events |
| **Inventory** | 15 | Datacenters, folders, templates, content libraries, tags |
| **System** | 10 | vCenter info, tasks, host services, licensing |

## Scenario Categories

| Category | Description | Scenarios |
|----------|-------------|-----------|
| **Inventory** | List and analyze infrastructure components | 01, 02, 04, 09 |
| **Health Checks** | Verify system health and configuration | 02, 03 |
| **Operations** | Perform operational tasks | 03, 05, 06 |
| **Monitoring** | Check alerts, performance, and events | 07, 10 |
| **Capacity** | Analyze storage and resource capacity | 04, 09 |
| **Network** | Network configuration analysis | 08 |
| **Diagnostics** | Multi-step troubleshooting | 10 |

## How to Use

Each scenario file contains:
1. **Goal** - What the operator wants to achieve
2. **Context** - Background information and constraints
3. **User Queries** - Natural language queries the operator might ask
4. **Expected Behavior** - What MEHO should do (operations, reasoning)
5. **Operations Used** - Specific connector operations referenced
6. **Success Criteria** - How to verify the scenario passed

## Testing with MEHO

1. Start MEHO with a VMware connector configured:
   ```bash
   ./scripts/dev-env.sh up
   ```

2. Create a VMware connector via the API or UI with:
   - vCenter hostname
   - Username/password with appropriate permissions
   - SSL verification settings

3. Execute user queries in the chat interface

4. Verify MEHO:
   - Discovers the correct operations via knowledge search
   - Executes operations in logical order
   - Handles data reduction for filtering/sorting
   - Requests approval for dangerous operations
   - Synthesizes results into natural language responses

## Key Operations Reference

### VM Operations
- `list_virtual_machines` - All VMs with comprehensive details
- `get_virtual_machine` - Single VM details
- `power_on_vm`, `power_off_vm`, `shutdown_guest` - Power management
- `create_snapshot`, `list_snapshots`, `delete_snapshot` - Snapshot management
- `migrate_vm` - vMotion to different host
- `clone_vm`, `instant_clone` - VM cloning

### Cluster Operations
- `list_clusters`, `get_cluster` - Cluster info with DRS/HA config
- `get_drs_recommendations`, `apply_drs_recommendation` - DRS management
- `get_cluster_resource_usage`, `get_cluster_ha_status` - Monitoring

### Host Operations
- `list_hosts`, `get_host` - Host info with hardware/VMs/status
- `enter_maintenance_mode`, `exit_maintenance_mode` - Maintenance
- `get_host_vms` - VMs on specific host

### Storage Operations
- `list_datastores`, `get_datastore` - Datastore info with capacity
- `get_vm_disks`, `add_disk`, `extend_disk` - Disk management
- `relocate_vm` - Storage vMotion
- `browse_datastore` - File browser

### Network Operations
- `list_networks`, `list_distributed_switches` - Network inventory
- `list_port_groups`, `get_port_group` - Port group details with VLAN
- `get_vm_nics`, `change_network` - VM network management

### Performance Operations
- `get_vm_performance`, `get_detailed_vm_performance` - VM metrics
- `get_host_performance`, `get_detailed_host_performance` - Host metrics
- `get_datastore_performance`, `get_detailed_datastore_performance` - Storage metrics
- `list_alarms`, `acknowledge_alarm` - Alarm management

## Notes

- Operations that modify state require user approval (ACTION intent)
- The agent uses `reduce_data` tool for filtering/sorting large datasets
- Performance metrics support multiple intervals: realtime, 5min, 1hour, etc.
- Guest operations require VMware Tools to be installed and running
