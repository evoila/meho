# Scenario 09: Resource Pool Analysis

**Category:** Capacity / Inventory  
**Complexity:** Basic to Intermediate  
**Connector:** VMware pyvmomi

## Goal

Analyze resource pool configurations including CPU and memory reservations,
limits, and shares to understand resource allocation policies.

## Context

Resource pools in vSphere control how CPU and memory resources are distributed
among VMs. Operators need to verify resource pool settings for capacity planning
and to troubleshoot performance issues caused by resource contention.

---

## User Queries

### Query 1: List all resource pools

```
Show me all resource pools
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `list_resource_pools`
3. Execute and return pool list

**Operation Used:**
```
list_resource_pools()
```

**Returns per pool:**
- name
- parent (parent pool or cluster name)
- cpu_reservation_mhz
- cpu_limit_mhz (-1 = unlimited)
- cpu_shares (low/normal/high or custom value)
- memory_reservation_mb
- memory_limit_mb (-1 = unlimited)
- memory_shares
- vm_count (VMs in this pool)

### Query 2: Resource pool hierarchy

```
What resource pools are in cluster PROD-Cluster-01?
```

**Expected MEHO Behavior:**
1. Filter resource pools by parent cluster
2. Show hierarchy: cluster → resource pools → child pools
3. May need to use `get_cluster` to identify root pool

### Query 3: Reservation analysis

```
Which resource pools have CPU reservations set?
```

**Expected MEHO Behavior:**
1. Filter pools where cpu_reservation_mhz > 0
2. Return: pool name, reservation amount, parent

**Agent Internal:**
```json
{
  "filter": {
    "conditions": [{"field": "cpu_reservation_mhz", "operator": ">", "value": 0}]
  }
}
```

### Query 4: VMs in a resource pool

```
What VMs are in resource pool "Production-Tier1"?
```

**Expected MEHO Behavior:**
1. Get VM list and filter by resource_pool field
2. Or use cluster/pool structure to enumerate VMs
3. Return VM list with their resource consumption

**Using VM list:**
```
list_virtual_machines()
```
Then filter by resource_pool = "Production-Tier1"

### Query 5: Over-committed resources

```
Are any resource pools over-committed?
```

**Expected MEHO Behavior:**
1. For each resource pool:
   - Sum VM reservations
   - Compare to pool's limit (if set)
2. Identify pools where sum(VM reservations) > pool limit
3. Flag potential issues

**Analysis:**
- Over-commitment is normal with shares-based allocation
- True over-commitment occurs when guaranteed reservations exceed capacity

### Query 6: Create a resource pool (ACTION)

```
Create a resource pool called "Dev-Pool" under the default Resources pool with 4 GHz CPU reservation
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION
2. Find operation: `create_resource_pool`
3. Request approval
4. Execute after approval

**Operation Used (after approval):**
```
create_resource_pool(
    parent_pool="Resources",
    pool_name="Dev-Pool",
    cpu_reservation=4000,
    memory_reservation_mb=0
)
```

### Query 7: Update resource pool (ACTION)

```
Increase the CPU limit on Dev-Pool to 10 GHz
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION
2. Find operation: `update_resource_pool`
3. Request approval
4. Execute after approval

**Operation Used (after approval):**
```
update_resource_pool(
    pool_name="Dev-Pool",
    cpu_limit=10000
)
```

---

## Resource Allocation Concepts

| Term | Description |
|------|-------------|
| **Reservation** | Guaranteed minimum resources. Pool/VM will always get at least this much. |
| **Limit** | Maximum resources allowed. -1 means unlimited. |
| **Shares** | Priority during contention. Higher shares = more resources when competing. |
| **Expandable** | If true, pool can borrow resources from parent when needed. |

### Shares Values
- **Low:** 2000 shares per vCPU / 5 shares per MB
- **Normal:** 4000 shares per vCPU / 10 shares per MB
- **High:** 8000 shares per vCPU / 20 shares per MB
- **Custom:** Specific share value

---

## Operations Reference

| Operation | Description | Parameters | Danger |
|-----------|-------------|------------|--------|
| `list_resource_pools` | All resource pools | None | Safe |
| `create_resource_pool` | Create new pool | `parent_pool`, `pool_name`, `cpu_reservation`, `memory_reservation_mb` | MEDIUM |
| `update_resource_pool` | Update pool config | `pool_name`, `cpu_reservation`, `cpu_limit`, `memory_reservation_mb`, `memory_limit_mb` | MEDIUM |
| `destroy_resource_pool` | Delete pool | `pool_name` | HIGH |
| `list_virtual_machines` | VMs with pool info | None | Safe |

---

## Sample Response

```
📊 Resource Pool Summary

| Pool | Parent | CPU Res | CPU Limit | Mem Res | Mem Limit | VMs |
|------|--------|---------|-----------|---------|-----------|-----|
| Prod-DB | PROD-01 | 20 GHz | Unlimited | 128 GB | 256 GB | 12 |
| Prod-Web | PROD-01 | 10 GHz | 50 GHz | 64 GB | 128 GB | 25 |
| Dev | DEV-01 | None | 20 GHz | None | 64 GB | 40 |
| Test | DEV-01 | None | 10 GHz | None | 32 GB | 15 |

Notes:
- Prod-DB has guaranteed 20 GHz CPU and 128 GB memory
- Dev and Test pools share DEV-01 cluster with limits
- Prod-Web has both reservation and limit set
```

---

## Hierarchy Example

```
PROD-Cluster-01 (Cluster)
└── Resources (Root Resource Pool)
    ├── Production-Tier1
    │   ├── web-01, web-02, web-03
    │   └── app-01, app-02
    ├── Production-Tier2
    │   └── batch-01, batch-02
    └── Dev-Pool
        └── dev-vm-01
```

---

## Success Criteria

- [ ] Resource pools listed correctly via `list_resource_pools`
- [ ] Hierarchy understood (cluster → pool → child pools)
- [ ] Reservations/limits queryable and displayed
- [ ] VMs per pool retrievable via VM filtering
- [ ] Over-commitment analysis possible
- [ ] Create resource pool triggers approval
- [ ] Update resource pool triggers approval
- [ ] Clear explanation of resource allocation concepts
- [ ] Parent-child relationships shown correctly
