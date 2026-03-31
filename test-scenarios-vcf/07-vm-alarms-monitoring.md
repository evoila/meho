# Scenario 07: VM and Host Alarms Monitoring

**Category:** Monitoring  
**Complexity:** Basic to Intermediate  
**Connector:** VMware pyvmomi

## Goal

Monitor and analyze triggered alarms on VMs and hosts to identify
issues requiring attention.

## Context

vCenter generates alarms when conditions exceed thresholds (CPU, memory, storage)
or when events occur (VM powered off unexpectedly, host disconnected). Operators
need to review these alarms to prioritize remediation.

---

## User Queries

### Query 1: List all triggered alarms

```
Show me all current alarms in the vCenter
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `list_alarms`
3. Execute and return triggered alarms

**Operation Used:**
```
list_alarms()
```

**Returns per alarm:**
- entity_name (VM, host, datastore, etc.)
- entity_type
- alarm_name
- status (red, yellow, green)
- triggered_time
- acknowledged (true/false)
- acknowledged_by (user who acknowledged)

### Query 2: Critical alarms only

```
What critical alarms are there?
```

**Expected MEHO Behavior:**
1. Use cached alarm data or re-fetch
2. Filter by status: "red" (critical)
3. Return prioritized list

**Agent Internal:**
```json
{
  "filter": {
    "conditions": [{"field": "status", "operator": "=", "value": "red"}]
  },
  "sort": {"field": "triggered_time", "direction": "desc"}
}
```

### Query 3: Alarms for specific VM

```
Are there any alarms on VM database-primary?
```

**Expected MEHO Behavior:**
1. Filter alarms by entity_name = "database-primary"
2. Return alarms specific to that VM

### Query 4: Acknowledge an alarm (ACTION)

```
Acknowledge the CPU alarm on web-server-01
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (state change)
2. Find operation: `acknowledge_alarm`
3. Request approval (low risk)
4. Execute after approval

**Operation Used (after approval):**
```
acknowledge_alarm(entity_name="web-server-01", entity_type="vm")
```

### Query 5: Alarm summary by type

```
Give me a breakdown of alarms by type
```

**Expected MEHO Behavior:**
1. Use cached alarm data
2. Aggregate by alarm_name
3. Return count per type

**Expected Response:**
```
📊 Alarm Breakdown:

| Alarm Type | Count | Critical | Warning |
|------------|-------|----------|---------|
| VM CPU usage | 5 | 2 | 3 |
| VM memory usage | 3 | 1 | 2 |
| Datastore usage | 2 | 2 | 0 |
| Host connection | 1 | 1 | 0 |

Total: 11 alarms (6 critical, 5 warning)
```

### Query 6: Recent events

```
What events happened in the last hour?
```

**Expected MEHO Behavior:**
1. Find operation: `get_events`
2. Execute with limit
3. Filter by time if needed

**Operation Used:**
```
get_events(limit=100, entity_type="vm")
```

**Returns per event:**
- event_type
- entity_name
- timestamp
- message
- user (who triggered, if applicable)

---

## Typical Alarm Types

| Alarm | Trigger | Entity Type |
|-------|---------|-------------|
| VM CPU usage | CPU above threshold | VM |
| VM memory usage | Memory above threshold | VM |
| Datastore usage | Datastore low on space | Datastore |
| Host CPU usage | Host CPU above threshold | Host |
| Host memory usage | Host memory above threshold | Host |
| Host connection state | Host disconnected | Host |
| VM power state | VM powered off unexpectedly | VM |
| VMware Tools status | Tools not running | VM |

---

## Operations Reference

| Operation | Description | Parameters | Danger |
|-----------|-------------|------------|--------|
| `list_alarms` | All triggered alarms | None | Safe |
| `acknowledge_alarm` | Acknowledge alarm | `entity_name`, `entity_type` | LOW |
| `get_events` | Recent vCenter events | `limit`, `entity_type` | Safe |

---

## Expected Response Format

```
🚨 Triggered Alarms (5 total)

Critical (2):
1. database-primary - VM CPU usage (95%)
   Triggered: 2 hours ago | Not Acknowledged

2. DS-PROD-01 - Datastore space usage (92%)
   Triggered: 1 day ago | Not Acknowledged

Warning (3):
3. web-server-01 - VM memory usage (85%)
   Triggered: 30 min ago | Acknowledged by admin

4. web-server-02 - VM CPU usage (78%)
   Triggered: 45 min ago | Not Acknowledged

5. web-server-03 - VMware Tools status
   Triggered: 3 hours ago | Not Acknowledged
```

---

## Success Criteria

- [ ] Alarms retrieved correctly via `list_alarms`
- [ ] Severity/status filtering works (red, yellow)
- [ ] Entity-specific alarm lookup works
- [ ] Acknowledge action triggers low-risk approval
- [ ] Aggregations by alarm type work
- [ ] Events retrievable via `get_events`
- [ ] Clear presentation of alarm priority and status
- [ ] Timestamp and acknowledgment info displayed
