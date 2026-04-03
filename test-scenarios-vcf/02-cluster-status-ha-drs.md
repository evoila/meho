# Scenario 02: Cluster Status and HA/DRS Configuration

**Category:** Health Checks / Inventory  
**Complexity:** Intermediate  
**Connector:** VMware pyvmomi

## Goal

Analyze cluster configurations including High Availability (HA) and Distributed Resource
Scheduler (DRS) settings, failover policies, and resource utilization.

## Context

An operator needs to verify cluster health before maintenance or wants to understand
the current HA/DRS configuration for compliance or troubleshooting purposes.

---

## User Queries

### Query 1: List all clusters

```
Show me all clusters in the vCenter
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `list_clusters`
3. Execute and return cluster list with summary

**Operation Used:**
```
list_clusters()
```

**Returns per cluster:**
- name
- host_count
- drs_enabled, drs_automation_level
- ha_enabled
- total_cpu_mhz, total_memory_mb
- used_cpu_mhz, used_memory_mb

### Query 2: HA Configuration details

```
What is the HA configuration for cluster PROD-Cluster-01?
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH (specific resource)
2. Find operation: `get_cluster`
3. Execute with `cluster_name="PROD-Cluster-01"`
4. Return detailed HA/DRS configuration

**Operation Used:**
```
get_cluster(cluster_name="PROD-Cluster-01")
```

**Returns:**
- name, host_count
- drs_config: enabled, automation_level, vm_behavior
- ha_config: enabled, host_monitoring, admission_control
- resource_usage: cpu/memory totals and usage

### Query 3: DRS automation level

```
Is DRS enabled and what is the automation level?
```

**Expected MEHO Behavior:**
1. Use cached cluster data or fetch if needed
2. Extract DRS fields: enabled, automation_level
3. Return: "DRS is enabled with fullyAutomated/partiallyAutomated/manual level"

**DRS Automation Levels:**
- `manual` - Recommendations only, no automatic actions
- `partiallyAutomated` - Initial placement automatic, no automatic vMotion
- `fullyAutomated` - Both initial placement and vMotion automatic

### Query 4: HA runtime status

```
What's the HA status - are there any issues?
```

**Expected MEHO Behavior:**
1. Find operation: `get_cluster_ha_status`
2. Execute to get real-time HA runtime info
3. Return HA health status and any issues

**Operation Used:**
```
get_cluster_ha_status(cluster_name="PROD-Cluster-01")
```

**Returns:**
- HA runtime info from DAS (Distributed Availability Services)
- Heartbeat datastores
- Host participation status
- Any failure conditions

### Query 5: Cross-reference with hosts

```
How many hosts are in each cluster and what's their status?
```

**Expected MEHO Behavior:**
1. Get cluster list (may use cached data)
2. Optionally fetch host details: `list_hosts`
3. Aggregate: total hosts, connected hosts, maintenance mode hosts
4. Return summary table

**Operations Used:**
```
list_clusters()
list_hosts()
```

### Query 6: Cluster resource usage

```
What is the resource utilization of the Production cluster?
```

**Expected MEHO Behavior:**
1. Find operation: `get_cluster_resource_usage`
2. Execute to get current resource summary
3. Return CPU/memory usage with percentages

**Operation Used:**
```
get_cluster_resource_usage(cluster_name="Production")
```

**Returns:**
- cpuUsedMHz, cpuCapacityMHz
- memUsedMB, memCapacityMB
- storageUsedMB, storageCapacityMB

---

## Operations Reference

| Operation | Description | Parameters |
|-----------|-------------|------------|
| `list_clusters` | All clusters with DRS/HA summary | None |
| `get_cluster` | Detailed cluster config | `cluster_name` (required) |
| `get_cluster_ha_status` | HA runtime info | `cluster_name` (required) |
| `get_cluster_resource_usage` | Current resource usage | `cluster_name` (required) |
| `list_hosts` | All hosts with cluster membership | None |

---

## Expected Response Example

```
📊 Cluster: PROD-Cluster-01

Configuration:
- Hosts: 4 (all connected)
- DRS: Enabled (fullyAutomated)
- HA: Enabled (host monitoring active)
- Admission Control: Enabled (1 host failure tolerated)

Resource Usage:
- CPU: 45,600 MHz / 96,000 MHz (47.5%)
- Memory: 384 GB / 512 GB (75%)
```

---

## Success Criteria

- [ ] Cluster list retrieved via `list_clusters`
- [ ] DRS/HA configuration accessible via `get_cluster`
- [ ] HA runtime status available via `get_cluster_ha_status`
- [ ] Resource usage available via `get_cluster_resource_usage`
- [ ] Filter operations on cluster data work
- [ ] Clear explanation of HA/DRS concepts in responses
- [ ] Host count and status cross-referenced correctly
