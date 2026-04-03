# Scenario 01: VM Inventory and Resource Analysis

**Category:** Inventory  
**Complexity:** Basic  
**Connector:** VMware pyvmomi

## Goal

Get a comprehensive inventory of all VMs in the environment with their resource usage,
network configuration, and status information.

## Context

An operator needs to review the VM inventory for capacity planning or audit purposes.
They want to see CPU, memory, storage usage, network assignments, and operational status.

---

## User Queries

### Query 1: List all VMs

```
List all virtual machines from the vCenter
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH (data retrieval)
2. Search operations for "virtual machines" or "VMs"
3. Find operation: `list_virtual_machines`
4. Execute via connector
5. Return summary with key details

**Operation Used:**
```
list_virtual_machines()
```

**Returns per VM:**
- name, power_state, connection_state
- host, cluster, resource_pool
- cpu_count, memory_mb
- guest_os, vmware_tools_status
- ip_address, hostname (if tools running)
- datastores, networks
- cpu_usage_mhz, memory_usage_mb, uptime_seconds

### Query 2: Filter by power state

```
Which VMs are powered off?
```

**Expected MEHO Behavior:**
1. Recognize intent: DATA_REDUCTION (filter cached/previous data)
2. Use agent's `reduce_data` capability on VM list
3. Apply filter: `power_state = "poweredOff"`
4. Return filtered results

**Agent Internal:**
```json
{
  "filter": {
    "conditions": [{"field": "power_state", "operator": "=", "value": "poweredOff"}]
  }
}
```

### Query 3: Resource usage summary

```
Show me VMs with more than 8 vCPUs sorted by memory
```

**Expected MEHO Behavior:**
1. Recognize intent: DATA_REDUCTION
2. Apply filter: `cpu_count > 8`
3. Apply sort: `memory_mb DESC`
4. Return sorted, filtered results

### Query 4: Specific VM details

```
Get detailed information about VM web-server-01
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH (specific resource)
2. Find operation: `get_virtual_machine`
3. Execute with parameter: `vm_name="web-server-01"`
4. Return comprehensive VM details

**Operation Used:**
```
get_virtual_machine(vm_name="web-server-01")
```

**Returns:**
- All fields from list plus:
- instance_uuid, uuid
- boot_time, guest_state
- guest tools version
- detailed guest info (if tools running)

### Query 5: Format as table

```
Format that as a table with name, CPU, memory, and power state
```

**Expected MEHO Behavior:**
1. Recognize intent: DATA_REFORMAT
2. Use cached data from previous query (no new API call)
3. Select fields: name, cpu_count, memory_mb, power_state
4. Return markdown table

---

## Operations Reference

| Operation | Description | Parameters |
|-----------|-------------|------------|
| `list_virtual_machines` | All VMs with comprehensive details | None |
| `get_virtual_machine` | Single VM details | `vm_name` (required) |

---

## Success Criteria

- [ ] VM list retrieved via `list_virtual_machines` operation
- [ ] Response includes: name, power_state, cpu, memory, host, networks
- [ ] Filter operations work without re-fetching data
- [ ] Sort operations work correctly
- [ ] Format requests use cached data
- [ ] No unnecessary API calls for follow-up questions
- [ ] Natural language summary provided
