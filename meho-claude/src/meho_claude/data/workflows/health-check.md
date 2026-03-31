---
name: health-check
description: Multi-system health overview
budget: 10
version: "1.1"
tags:
  - health
  - monitoring
---

# Multi-System Health Check Workflow

## Objective
Perform a quick health overview across all connected infrastructure systems. Identify systems or entities in degraded or error states.

## Prerequisites
- At least one connector configured: `meho connector list`

## Visual Input

If the user provides a screenshot of a monitoring dashboard, alert page, or system status display, analyze it to extract system names, health indicators, error states, and metric values. Use these as focus areas for the health check -- prioritize checking systems that appear degraded in the visual input.

If the user provides architecture diagrams, runbooks, or other documents, read them directly for context on expected system topology and health baselines. Do NOT ingest them into the knowledge base -- they are ad-hoc context for this health check only.

## Budget
Maximum **10 connector calls** per health check. Track your count.

## Steps

### Step 1: Enumerate Connected Systems
List all configured connectors and their status:

```bash
meho connector list
```

Note which connectors are active and reachable.

### Step 2: Per-System Health Probe
For each active connector, run a representative READ operation to verify connectivity and gather current state:

```bash
# Find a suitable list operation
meho connector search-ops "list" --connector <connector>

# Execute the list operation
meho connector call <connector> <list-operation>
```

Note: Choose a lightweight list operation (e.g., list namespaces, list VMs) that confirms the system is responsive.

### Step 3: Check Topology for Issues
Query topology for entities that may indicate problems:

```bash
meho topology lookup "" --depth 1
```

Look for entities with error indicators in their attributes (restart counts, error states, resource pressure).

### Step 4: Summarize Health
For each system, produce a health status:
- **Healthy** (green): System responsive, no entity issues detected
- **Degraded** (yellow): System responsive but some entities have warnings
- **Unhealthy** (red): System unresponsive or entities in error state
- **Unknown**: Unable to determine (no data, connector not tested)

## Output Format

```json
{
  "status": "health_check_complete",
  "query_budget": {"used": 0, "max": 10},
  "systems": [
    {
      "connector": "connector-name",
      "type": "kubernetes|vmware|rest",
      "health": "healthy|degraded|unhealthy|unknown",
      "entity_count": 0,
      "issues": ["description of any problems"],
      "last_checked": "timestamp"
    }
  ],
  "summary": "Overall health assessment in one sentence"
}
```

### For Human (narrative mode)
Present a dashboard-style overview:
- System name: status indicator
- Key metrics per system
- Any alerts or warnings highlighted
