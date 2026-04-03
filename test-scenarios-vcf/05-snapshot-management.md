# Scenario 05: VM Snapshot Management

**Category:** Operations  
**Complexity:** Advanced (includes ACTIONs requiring approval)  
**Connector:** VMware pyvmomi

## Goal

Manage VM snapshots: list existing snapshots, create new snapshots before changes,
and clean up old snapshots to reclaim storage.

## Context

Snapshots are critical for VM protection but can consume significant storage if
not managed. Operators need to review snapshot age and size, create snapshots
before risky changes, and delete old snapshots to prevent datastore issues.

---

## User Queries

### Query 1: List snapshots for a VM

```
Show me all snapshots for VM web-server-01
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `list_snapshots`
3. Execute with VM name
4. Return snapshot tree with details

**Operation Used:**
```
list_snapshots(vm_name="web-server-01")
```

**Returns per snapshot:**
- name
- description
- created_time
- state (poweredOn, poweredOff, suspended)
- quiesced (guest filesystem quiesced)
- children (nested snapshots)

### Query 2: Find VMs with old snapshots

```
Which VMs have snapshots older than 7 days?
```

**Expected MEHO Behavior:**
1. This requires checking snapshots across multiple VMs
2. Get VM list, then check snapshots for each
3. Calculate snapshot age from created_time
4. Filter: age > 7 days
5. Return VMs with stale snapshots

**Note:** This is a multi-step operation. The agent may need to:
1. Call `list_virtual_machines`
2. For VMs that show snapshots, call `list_snapshots`
3. Aggregate and filter results

### Query 3: Snapshot storage consumption

```
How much storage is being consumed by snapshots on web-server-01?
```

**Expected MEHO Behavior:**
1. Get VM disk info which includes snapshot consumption
2. Calculate total snapshot disk usage
3. Return: "Snapshots are consuming X GB"

**Operation Used:**
```
get_vm_disks(vm_name="web-server-01")
```

### Query 4: Create a snapshot (ACTION - requires approval!)

```
Create a snapshot of VM database-primary called "pre-patch-backup"
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION
2. ⚠️ **APPROVAL REQUIRED** - Snapshot creation can:
   - Cause brief I/O pause (especially with quiesce)
   - Consume significant storage over time
3. Show approval dialog with options
4. Execute only after approval

**Approval Dialog:**
```
┌──────────────────────────────────────────────────────────────┐
│  ⚠️ ACTION REQUIRES APPROVAL                                 │
│                                                              │
│  Operation: Create VM Snapshot                               │
│  VM: database-primary                                        │
│  Snapshot Name: pre-patch-backup                             │
│                                                              │
│  Options:                                                    │
│  - Include Memory: No (faster, smaller)                      │
│  - Quiesce Guest: Yes (ensures filesystem consistency)       │
│                                                              │
│  Impact:                                                     │
│  - Brief I/O pause during quiesce                           │
│  - Storage consumption increases over time                   │
│  - Snapshot should be removed after patch verification       │
│                                                              │
│  Risk Level: MEDIUM                                          │
│                                                              │
│  [Approve] [Reject]                                          │
└──────────────────────────────────────────────────────────────┘
```

**Operation Used (after approval):**
```
create_snapshot(
    vm_name="database-primary",
    snapshot_name="pre-patch-backup",
    description="Created via MEHO before patching",
    include_memory=False,
    quiesce_guest=True
)
```

### Query 5: Delete a specific snapshot (ACTION - requires approval!)

```
Delete the snapshot "pre-patch-backup" from VM database-primary
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (MEDIUM risk)
2. ⚠️ **APPROVAL REQUIRED** - Snapshot deletion:
   - Can cause I/O during disk consolidation
   - Is irreversible
3. Show approval with snapshot details
4. Execute only after explicit approval

**Operation Used (after approval):**
```
delete_snapshot(
    vm_name="database-primary",
    snapshot_name="pre-patch-backup",
    remove_children=False,
    consolidate=True
)
```

### Query 6: Delete ALL snapshots (ACTION - HIGH risk!)

```
Delete all snapshots for VM web-server-01
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (HIGH risk!)
2. ⚠️ **APPROVAL REQUIRED** with strong warning
3. List all snapshots that will be deleted
4. Show HIGH risk level
5. Execute only after explicit approval

**Approval Dialog:**
```
┌──────────────────────────────────────────────────────────────┐
│  🚨 HIGH RISK ACTION REQUIRES APPROVAL                       │
│                                                              │
│  Operation: Delete ALL Snapshots                             │
│  VM: web-server-01                                           │
│                                                              │
│  Snapshots to be PERMANENTLY DELETED:                        │
│  1. pre-upgrade (created 2024-01-15)                        │
│  2. weekly-backup (created 2024-01-20)                      │
│  3. config-test (created 2024-01-22)                        │
│                                                              │
│  Impact:                                                     │
│  - This operation is IRREVERSIBLE                           │
│  - Disk consolidation may cause temporary I/O increase       │
│  - Cannot restore to any of these states after deletion      │
│                                                              │
│  Risk Level: HIGH                                            │
│                                                              │
│  [Approve] [Reject]                                          │
└──────────────────────────────────────────────────────────────┘
```

**Operation Used (after approval):**
```
delete_all_snapshots(vm_name="web-server-01", consolidate=True)
```

### Query 7: Revert to a snapshot (ACTION - HIGH risk!)

```
Revert VM web-server-01 to snapshot "known-good-state"
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (HIGH risk!)
2. ⚠️ **APPROVAL REQUIRED** - Revert will:
   - Discard all changes since snapshot
   - Potentially power off/on the VM
3. Show HIGH risk approval
4. Execute only after approval

**Operation Used (after approval):**
```
revert_snapshot(
    vm_name="web-server-01",
    snapshot_name="known-good-state",
    suppress_power_on=False
)
```

---

## Operations Reference

| Operation | Description | Parameters | Danger |
|-----------|-------------|------------|--------|
| `list_snapshots` | Get snapshot tree | `vm_name` | Safe |
| `create_snapshot` | Create new snapshot | `vm_name`, `snapshot_name`, `description`, `include_memory`, `quiesce_guest` | MEDIUM |
| `delete_snapshot` | Delete specific snapshot | `vm_name`, `snapshot_name`, `remove_children`, `consolidate` | MEDIUM |
| `delete_all_snapshots` | Delete all snapshots | `vm_name`, `consolidate` | HIGH |
| `revert_snapshot` | Revert to snapshot | `vm_name`, `snapshot_name`, `suppress_power_on` | HIGH |
| `revert_to_current_snapshot` | Revert to most recent | `vm_name`, `suppress_power_on` | HIGH |

---

## Success Criteria

- [ ] Snapshots listed correctly via `list_snapshots`
- [ ] Snapshot age calculation works for filtering
- [ ] Create snapshot triggers MEDIUM risk approval
- [ ] Delete snapshot triggers MEDIUM risk approval
- [ ] Delete ALL snapshots triggers HIGH risk approval
- [ ] Revert triggers HIGH risk approval
- [ ] Actions execute only after explicit approval
- [ ] Results confirmed after action (task ID or completion)
- [ ] Storage consumption information available
