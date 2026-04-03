# Scenario 03: DRS Recommendations Analysis and Execution

**Category:** Operations  
**Complexity:** Advanced (includes ACTION requiring approval)  
**Connector:** VMware pyvmomi

## Goal

Review pending DRS (Distributed Resource Scheduler) recommendations and optionally
apply them to balance workloads across the cluster.

## Context

DRS generates recommendations to migrate VMs between hosts to optimize resource
utilization. Operators need to review these recommendations before applying them,
especially in clusters not set to "Fully Automated" mode.

---

## User Queries

### Query 1: List DRS recommendations

```
Show me all pending DRS recommendations
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `get_drs_recommendations`
3. May need to iterate over clusters or ask which cluster
4. Execute and return recommendations

**Operation Used:**
```
get_drs_recommendations(cluster_name="PROD-Cluster-01")
```

**Returns per recommendation:**
- key (recommendation identifier)
- reason (why DRS suggests this)
- target (action details)
- priority (1-5, lower is higher priority)
- VM name, source host, target host

### Query 2: Recommendations for specific cluster

```
What DRS recommendations are pending for cluster PROD-DB-Cluster?
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH (specific cluster)
2. Execute: `get_drs_recommendations(cluster_name="PROD-DB-Cluster")`
3. Return recommendations with details

**Expected Response:**
```
DRS Recommendations for PROD-DB-Cluster:

1. [Priority 2] Migrate VM 'db-replica-02' from esxi-01 to esxi-03
   Reason: Balance CPU load
   
2. [Priority 3] Migrate VM 'web-app-04' from esxi-01 to esxi-02
   Reason: Balance memory load
```

### Query 3: Understand the reason

```
Why does DRS want to move these VMs?
```

**Expected MEHO Behavior:**
1. Analyze recommendation reasons from cached data
2. Explain: "DRS recommends these migrations because..."
3. Common reasons:
   - Balance CPU load across hosts
   - Balance memory load across hosts
   - Host entering maintenance mode
   - Satisfy affinity/anti-affinity rules
   - Reduce resource contention

### Query 4: Apply a recommendation (ACTION - requires approval!)

```
Apply the DRS recommendation for VM web-server-01
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION (state-changing operation)
2. ⚠️ **APPROVAL REQUIRED** - VM migration can cause brief interruption
3. Show approval dialog with:
   - Impact: "Will migrate VM web-server-01 from host-01 to host-02"
   - Risk level: MEDIUM (brief network interruption possible)
4. Only execute after user approval

**Operation Used (after approval):**
```
apply_drs_recommendation(cluster_name="PROD-Cluster-01", recommendation_key="rec-1")
```

### Query 5: Refresh recommendations

```
Refresh the DRS recommendations
```

**Expected MEHO Behavior:**
1. Find operation: `refresh_drs_recommendations`
2. Execute to trigger recalculation
3. Then fetch new recommendations

**Operation Used:**
```
refresh_drs_recommendations(cluster_name="PROD-Cluster-01")
```

### Query 6: Cancel a recommendation

```
Dismiss the recommendation to move web-server-01
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION
2. Find operation: `cancel_drs_recommendation`
3. Request approval (lower risk)
4. Execute after approval

**Operation Used:**
```
cancel_drs_recommendation(cluster_name="PROD-Cluster-01", recommendation_key="rec-1")
```

---

## Approval Flow

For DRS recommendation application, MEHO should:

1. **Identify the action** - Recognize this modifies system state
2. **Assess danger level** - vMotion is generally safe but can have brief impact
3. **Present for approval** - Show what will happen
4. **Execute only after approval** - Call the operation
5. **Report result** - Confirm migration initiated or report error

```
┌──────────────────────────────────────────────────────────────┐
│  ⚠️ ACTION REQUIRES APPROVAL                                 │
│                                                              │
│  Operation: Apply DRS Recommendation                         │
│  Action: Migrate VM 'web-server-01'                         │
│                                                              │
│  Details:                                                    │
│  - Source Host: esxi-01.example.com                         │
│  - Target Host: esxi-02.example.com                         │
│  - Reason: Balance CPU load                                  │
│                                                              │
│  Impact:                                                     │
│  - Brief network interruption possible (<1 second typical)   │
│  - VM remains running during migration                       │
│                                                              │
│  Risk Level: MEDIUM                                          │
│                                                              │
│  [Approve] [Reject]                                          │
└──────────────────────────────────────────────────────────────┘
```

---

## Operations Reference

| Operation | Description | Parameters | Danger |
|-----------|-------------|------------|--------|
| `get_drs_recommendations` | Get pending recommendations | `cluster_name` | Safe |
| `apply_drs_recommendation` | Apply a recommendation | `cluster_name`, `recommendation_key` | MEDIUM |
| `cancel_drs_recommendation` | Dismiss a recommendation | `cluster_name`, `recommendation_key` | LOW |
| `refresh_drs_recommendations` | Force recalculation | `cluster_name` | Safe |

---

## Success Criteria

- [ ] DRS recommendations retrieved via `get_drs_recommendations`
- [ ] Recommendations filtered by cluster
- [ ] Reason/justification explained clearly
- [ ] Apply action triggers approval flow
- [ ] Approval dialog shows impact and risk level
- [ ] Execution happens only after explicit approval
- [ ] Result of action reported back (task ID or completion)
- [ ] Cancel/dismiss recommendation works with approval
