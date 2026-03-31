# Scenario 04: Datastore Capacity and Usage Analysis

**Category:** Capacity / Inventory  
**Complexity:** Basic to Intermediate  
**Connector:** VMware pyvmomi

## Goal

Analyze datastore capacity, identify datastores running low on space,
and understand storage utilization across the environment.

## Context

Storage capacity planning is critical for vSphere environments. Operators need to
monitor datastore utilization, identify candidates for cleanup or expansion, and
plan for future storage needs.

---

## User Queries

### Query 1: List all datastores

```
Show me all datastores with their capacity and free space
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `list_datastores`
3. Execute and return datastore list with capacity info

**Operation Used:**
```
list_datastores()
```

**Returns per datastore:**
- name
- type (VMFS, NFS, vSAN)
- capacity_gb, free_space_gb
- accessible (boolean)
- multiple_host_access (shared vs local)
- maintenance_mode
- uncommitted_gb (thin provisioning)

### Query 2: Low space alert

```
Which datastores have less than 20% free space?
```

**Expected MEHO Behavior:**
1. Recognize intent: DATA_REDUCTION
2. Calculate: free_percent = (free_space_gb / capacity_gb) * 100
3. Filter: free_percent < 20
4. Return critical datastores sorted by free space

**Agent Internal:**
```json
{
  "compute": [
    {
      "name": "free_percent",
      "expression": "(free_space_gb / capacity_gb) * 100"
    }
  ],
  "filter": {
    "conditions": [{"field": "free_percent", "operator": "<", "value": 20}]
  },
  "sort": {"field": "free_percent", "direction": "asc"}
}
```

### Query 3: Datastore details

```
Show me details for datastore SAN-PROD-01
```

**Expected MEHO Behavior:**
1. Find operation: `get_datastore`
2. Execute with datastore name
3. Return detailed information

**Operation Used:**
```
get_datastore(datastore_name="SAN-PROD-01")
```

**Returns:**
- All fields from list plus:
- hosts (list of connected ESXi hosts)
- vm_count (VMs stored on this datastore)

### Query 4: VM count per datastore

```
Which datastore has the most VMs?
```

**Expected MEHO Behavior:**
1. Option A: Use vm_count from `list_datastores` if available
2. Option B: Call `get_datastore_vms` for each datastore
3. Return top datastores by VM count

**Operation Used:**
```
get_datastore_vms(datastore_name="SAN-PROD-01")
```

**Returns:**
- List of VMs stored on the datastore
- VM names and basic info

### Query 5: Storage by type breakdown

```
How much total storage is VMFS vs NFS vs vSAN?
```

**Expected MEHO Behavior:**
1. Use cached datastore data
2. Aggregate by type field
3. Sum capacity_gb and free_space_gb per type
4. Return breakdown

**Expected Response:**
```
📊 Storage by Type:

| Type | Total Capacity | Free Space | Utilization |
|------|---------------|------------|-------------|
| VMFS | 50 TB | 15 TB | 70% |
| NFS | 20 TB | 8 TB | 60% |
| vSAN | 100 TB | 35 TB | 65% |
```

### Query 6: Datastore performance

```
What's the performance of datastore SAN-PROD-01?
```

**Expected MEHO Behavior:**
1. Find operation: `get_datastore_performance` (quickStats)
2. Or for detailed metrics: `get_detailed_datastore_performance`
3. Return performance metrics

**Operations Used:**
```
get_datastore_performance(datastore_name="SAN-PROD-01")
get_detailed_datastore_performance(datastore_name="SAN-PROD-01", interval="realtime")
```

**Returns (detailed):**
- Read/write throughput (KB/s)
- Read/write IOPS
- Read/write latency (ms)
- Capacity utilization

### Query 7: Format as capacity report

```
Create a capacity report sorted by utilization percentage
```

**Expected MEHO Behavior:**
1. Use cached data
2. Calculate utilization: (1 - free_space_gb/capacity_gb) * 100
3. Format as table with columns: Name, Type, Capacity, Free, Utilization
4. Sort by utilization descending

---

## Operations Reference

| Operation | Description | Parameters |
|-----------|-------------|------------|
| `list_datastores` | All datastores with capacity | None |
| `get_datastore` | Single datastore details | `datastore_name` |
| `get_datastore_vms` | VMs on a datastore | `datastore_name` |
| `browse_datastore` | File browser | `datastore_name`, `path` (optional) |
| `get_datastore_performance` | Quick metrics | `datastore_name` |
| `get_detailed_datastore_performance` | Full IOPS/latency | `datastore_name`, `interval` |

---

## Expected Capacity Report

```
📊 Datastore Capacity Report

| Datastore | Type | Capacity | Free | Used | Utilization |
|-----------|------|----------|------|------|-------------|
| SAN-PROD-01 | VMFS | 10.0 TB | 1.5 TB | 8.5 TB | 85% ⚠️ |
| NFS-Archive | NFS | 50.0 TB | 12.0 TB | 38.0 TB | 76% |
| vSAN-Cluster | vSAN | 100.0 TB | 45.0 TB | 55.0 TB | 55% |

⚠️ 1 datastore above 80% utilization threshold
```

---

## Success Criteria

- [ ] Datastore list retrieved with capacity info via `list_datastores`
- [ ] Calculated fields (percentages, utilization) work correctly
- [ ] Filter by capacity/usage works
- [ ] Aggregations by type work
- [ ] Table formatting includes calculated columns
- [ ] Critical datastores (low space) identified and flagged
- [ ] Performance metrics available via `get_detailed_datastore_performance`
- [ ] VMs per datastore queryable via `get_datastore_vms`
