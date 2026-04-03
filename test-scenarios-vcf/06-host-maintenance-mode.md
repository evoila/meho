# Scenario 06: ESXi Host Maintenance Mode

**Category:** Operations  
**Complexity:** Advanced (includes CRITICAL ACTION)  
**Connector:** VMware pyvmomi

## Goal

Put an ESXi host into maintenance mode for patching or hardware maintenance,
ensuring VMs are properly migrated first, then bring the host back online.

## Context

Before performing maintenance on an ESXi host (patching, hardware repair, etc.),
the host must enter maintenance mode. This triggers VM migrations to other hosts.
The process depends on cluster DRS settings and available capacity.

---

## User Queries

### Query 1: Check host status

```
What is the status of host esxi-01.example.com?
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `get_host`
3. Execute with host name
4. Return comprehensive host status

**Operation Used:**
```
get_host(host_name="esxi-01.example.com")
```

**Returns:**
- name
- connection_state (connected, disconnected, notResponding)
- power_state (poweredOn, poweredOff, standBy)
- maintenance_mode (true/false)
- standby_mode
- boot_time
- hardware: vendor, model, uuid, cpu_mhz, cpu_cores, memory_mb
- stats: cpu_usage_mhz, memory_usage_mb, uptime_seconds
- parent_cluster
- vms (list of VMs on this host)
- datastores, networks

### Query 2: List VMs on a host

```
What VMs are running on host esxi-01?
```

**Expected MEHO Behavior:**
1. Find operation: `get_host_vms`
2. Execute with host name
3. Return list of VMs with power state

**Operation Used:**
```
get_host_vms(host_name="esxi-01.example.com")
```

**Returns:**
- List of VMs on the host
- Per VM: name, power_state, cpu, memory

### Query 3: Enter maintenance mode (CRITICAL ACTION!)

```
Put host esxi-01 into maintenance mode
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (CRITICAL!)
2. ⚠️ **APPROVAL REQUIRED** - Maintenance mode will:
   - Migrate all VMs off the host (if DRS enabled)
   - Make host unavailable for workloads
   - May require manual intervention if DRS not fully automated
3. Pre-flight information gathering:
   - How many VMs on host?
   - What cluster is host in?
   - Is DRS enabled? What level?
4. Show CRITICAL risk approval with full context
5. Execute only after explicit approval

**Pre-flight Checks (automatic):**
```
Gathering information for host esxi-01.example.com:
- Cluster: PROD-Cluster-01
- DRS: Enabled (partiallyAutomated)
- VMs on host: 15 (12 powered on, 3 powered off)
- Host CPU: 45% utilized
- Host Memory: 72% utilized
```

**Approval Dialog:**
```
┌──────────────────────────────────────────────────────────────┐
│  🚨 CRITICAL ACTION REQUIRES APPROVAL                        │
│                                                              │
│  Operation: Enter Maintenance Mode                           │
│  Host: esxi-01.example.com                                   │
│  Cluster: PROD-Cluster-01                                    │
│                                                              │
│  Current State:                                              │
│  - 15 VMs on host (12 powered on)                           │
│  - DRS: partiallyAutomated                                   │
│  - Cluster has 3 other hosts with capacity                   │
│                                                              │
│  What will happen:                                           │
│  - DRS will generate migration recommendations               │
│  - You may need to manually approve migrations               │
│  - Powered-off VMs will be evacuated                        │
│  - Host becomes unavailable for new VMs                      │
│                                                              │
│  ⚠️ If DRS is not fullyAutomated, check DRS recommendations │
│     and apply them before host can enter maintenance mode    │
│                                                              │
│  Risk Level: CRITICAL                                        │
│                                                              │
│  [Approve] [Reject]                                          │
└──────────────────────────────────────────────────────────────┘
```

**Operation Used (after approval):**
```
enter_maintenance_mode(
    host_name="esxi-01.example.com",
    evacuate_vms=True,
    timeout=0
)
```

### Query 4: Check migration progress

```
Are there any running vMotions?
```

**Expected MEHO Behavior:**
1. Find operation: `list_tasks`
2. Filter for migration tasks
3. Return active vMotion status

**Operation Used:**
```
list_tasks(limit=50)
```

Then filter for task types containing "migrate" or "vMotion".

### Query 5: Check DRS recommendations during maintenance

```
What DRS recommendations are there for the cluster?
```

**Expected MEHO Behavior:**
1. Get DRS recommendations for the host's cluster
2. These may include mandatory migrations for maintenance

**Operation Used:**
```
get_drs_recommendations(cluster_name="PROD-Cluster-01")
```

### Query 6: Exit maintenance mode (ACTION)

```
Take host esxi-01 out of maintenance mode
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (lower risk than entering)
2. ⚠️ **APPROVAL REQUIRED** 
3. Execute after approval

**Approval Dialog:**
```
┌──────────────────────────────────────────────────────────────┐
│  ⚠️ ACTION REQUIRES APPROVAL                                 │
│                                                              │
│  Operation: Exit Maintenance Mode                            │
│  Host: esxi-01.example.com                                   │
│                                                              │
│  What will happen:                                           │
│  - Host becomes available for VM workloads                   │
│  - DRS may migrate VMs to this host for load balancing       │
│                                                              │
│  Risk Level: LOW                                             │
│                                                              │
│  [Approve] [Reject]                                          │
└──────────────────────────────────────────────────────────────┘
```

**Operation Used (after approval):**
```
exit_maintenance_mode(host_name="esxi-01.example.com", timeout=0)
```

---

## Complete Maintenance Workflow

```
┌────────────────────────────────────────────────────────────────┐
│  User: "Put host esxi-01 into maintenance mode"                │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│  MEHO: Pre-flight checks                                       │
│  ├─ get_host(host_name="esxi-01")                             │
│  ├─ get_cluster(cluster_name=<from host>)                     │
│  └─ get_host_vms(host_name="esxi-01")                         │
│                                                                │
│  Results:                                                      │
│  - 15 VMs on host                                              │
│  - Cluster has capacity for migration                          │
│  - DRS: partiallyAutomated                                     │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│  🚨 APPROVAL REQUIRED (CRITICAL)                               │
│                                                                │
│  Shows: VM count, DRS level, impact warning                    │
│                                                                │
│  [Approve] [Reject]                                            │
└────────────────────────────────────────────────────────────────┘
                          │
                     (User approves)
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│  MEHO: enter_maintenance_mode(host_name="esxi-01")             │
│                                                                │
│  Returns: Task initiated, migrations starting...               │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│  User: "Check migration progress"                              │
│                                                                │
│  MEHO: list_tasks() + filter migrations                        │
│                                                                │
│  Returns: 5 migrations in progress, 7 complete, 3 pending      │
└────────────────────────────────────────────────────────────────┘
```

---

## Operations Reference

| Operation | Description | Parameters | Danger |
|-----------|-------------|------------|--------|
| `get_host` | Host details including MM status | `host_name` | Safe |
| `get_host_vms` | VMs on a host | `host_name` | Safe |
| `list_hosts` | All hosts | None | Safe |
| `enter_maintenance_mode` | Enter MM | `host_name`, `evacuate_vms`, `timeout` | CRITICAL |
| `exit_maintenance_mode` | Exit MM | `host_name`, `timeout` | LOW |
| `list_tasks` | Recent tasks | `limit` | Safe |
| `get_drs_recommendations` | DRS recommendations | `cluster_name` | Safe |

---

## Success Criteria

- [ ] Host status retrieved correctly via `get_host`
- [ ] VMs on host listed via `get_host_vms`
- [ ] Enter maintenance triggers CRITICAL risk approval
- [ ] Pre-flight checks show VM count and DRS level
- [ ] Risk level clearly shown as CRITICAL
- [ ] Action executes only after explicit approval
- [ ] Migration progress checkable via `list_tasks`
- [ ] Exit maintenance works with LOW risk approval
- [ ] Task status reported after operation initiated
